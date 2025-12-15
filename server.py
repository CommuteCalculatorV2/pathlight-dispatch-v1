import os
import tempfile
import time
import uuid
import base64
import re
import json
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI, RateLimitError, BadRequestError

app = FastAPI(title="PathLight Dispatch v1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten later if you want
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = OpenAI()  # uses OPENAI_API_KEY from env

# -----------------------------
# Config: Feedback visibility
# -----------------------------
# If set, /feedback endpoints require ?token=...
FEEDBACK_TOKEN = os.getenv("FEEDBACK_TOKEN", "").strip()

# Optional persistence file (JSON Lines)
# Example: set on Render as /tmp/pathlight_feedback.jsonl
FEEDBACK_STORE_PATH = os.getenv("FEEDBACK_STORE_PATH", "").strip()

# Cap to prevent runaway memory
MAX_FEEDBACK_ITEMS = int(os.getenv("MAX_FEEDBACK_ITEMS", "200"))

# In-memory feedback store
# Each item: {id, ts, note, transcript, req_id}
FEEDBACK: List[Dict[str, Any]] = []


# -----------------------------
# Models
# -----------------------------
class DispatchAction(BaseModel):
    name: str
    args: Dict[str, Any] = {}


class DispatchOut(BaseModel):
    transcript: str
    reply: str
    audio_b64: str | None = None
    audio_mime: str | None = None
    action: DispatchAction | None = None


class FeedbackItem(BaseModel):
    id: str
    ts: float
    note: str
    transcript: str
    req_id: str


# -----------------------------
# Helpers
# -----------------------------
def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def extract_volume_target(text: str) -> Optional[float]:
    """
    Accepts:
      - "volume 60"
      - "volume to 0.6"
      - "set volume to 70%"
    Returns 0.0..1.0 or None.
    """
    t = text.lower()

    m = re.search(r"(?:volume)\s*(?:to)?\s*([0-9]{1,3})\s*%?", t)
    if m:
        n = float(m.group(1))
        if n > 1.0:
            return clamp(n / 100.0, 0.0, 1.0)
        return clamp(n, 0.0, 1.0)

    m = re.search(r"(?:volume)\s*(?:to)?\s*(0?\.\d+)", t)
    if m:
        return clamp(float(m.group(1)), 0.0, 1.0)

    return None


def parse_action(transcript: str) -> Optional[DispatchAction]:
    """
    Very lightweight command detection.
    The model still answers normally, but we optionally attach an action
    for the headset to execute (volume, voice, repeat, etc.)
    """
    t = (transcript or "").strip().lower()
    if not t:
        return None

    # Repeat
    if any(p in t for p in ["repeat that", "say that again", "repeat", "again please"]):
        return DispatchAction(name="repeat_last", args={})

    # Help
    if any(p in t for p in ["help", "what can i say", "commands", "pilot controls"]):
        return DispatchAction(name="help", args={})

    # Speech enable/disable
    if any(p in t for p in ["turn off speech", "disable speech", "no speech", "mute dispatch voice"]):
        return DispatchAction(name="set_tts", args={"enabled": False})
    if any(p in t for p in ["turn on speech", "enable speech", "speech on", "unmute dispatch voice"]):
        return DispatchAction(name="set_tts", args={"enabled": True})

    # Volume up/down
    if any(p in t for p in ["volume up", "turn it up", "louder"]):
        return DispatchAction(name="adjust_volume", args={"delta": +0.1})
    if any(p in t for p in ["volume down", "turn it down", "quieter"]):
        return DispatchAction(name="adjust_volume", args={"delta": -0.1})

    # Absolute volume set
    target = extract_volume_target(t)
    if target is not None:
        return DispatchAction(name="set_volume", args={"value": target})

    # Voice selection (server voice)
    # e.g. "use voice alloy" / "switch voice to nova"
    m = re.search(r"(?:voice)\s*(?:to|=)?\s*([a-zA-Z0-9_-]+)", t)
    if m:
        voice = m.group(1).strip().lower()
        return DispatchAction(name="set_voice", args={"voice": voice})

    # Feedback capture
    # e.g. "feedback: the button is hard to tap"
    if "feedback" in t:
        note = transcript
        m2 = re.search(r"feedback\s*[:\-]\s*(.*)$", transcript, re.IGNORECASE)
        if m2:
            note = m2.group(1).strip()
        return DispatchAction(name="save_feedback", args={"note": note})

    return None


def _require_feedback_token(token: Optional[str]):
    if FEEDBACK_TOKEN:
        if not token or token.strip() != FEEDBACK_TOKEN:
            raise HTTPException(status_code=401, detail="Invalid token")


def _append_feedback(item: Dict[str, Any]):
    FEEDBACK.append(item)
    # Cap memory
    if len(FEEDBACK) > MAX_FEEDBACK_ITEMS:
        del FEEDBACK[0 : (len(FEEDBACK) - MAX_FEEDBACK_ITEMS)]

    # Optional persistence (best effort)
    if FEEDBACK_STORE_PATH:
        try:
            with open(FEEDBACK_STORE_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"⚠️ feedback persist failed path={FEEDBACK_STORE_PATH} err={type(e).__name__} {str(e)[:200]}")


# -----------------------------
# Routes
# -----------------------------
@app.get("/health")
def health():
    return {"ok": True}


@app.get("/feedback", response_model=List[FeedbackItem])
def list_feedback(
    token: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    """
    View recent feedback items.
    Optional token gate via FEEDBACK_TOKEN env var.
    """
    _require_feedback_token(token)
    items = FEEDBACK[-limit:]
    return items[::-1]  # newest first


@app.get("/feedback/latest", response_model=Optional[FeedbackItem])
def latest_feedback(token: Optional[str] = Query(default=None)):
    """
    Quick check for the most recent feedback item.
    """
    _require_feedback_token(token)
    if not FEEDBACK:
        return None
    return FEEDBACK[-1]


@app.post("/dispatch", response_model=DispatchOut)
async def dispatch(
    mode: str = Form("talk"),
    audio: UploadFile = File(...),
    voice: str = Form("nova"),          # server TTS voice
    tts: str = Form("1"),               # "1" = return audio, "0" = text-only
):
    req_id = str(uuid.uuid4())
    t0 = time.time()

    if not audio.filename:
        raise HTTPException(status_code=400, detail="Missing audio file")

    ext = os.path.splitext(audio.filename)[1].lower()
    if ext not in {".m4a", ".mp3", ".wav", ".webm", ".aac"}:
        raise HTTPException(status_code=415, detail=f"Unsupported file extension: {ext or '(none)'}")

    tmp_path = None
    try:
        contents = await audio.read()
        if len(contents) > 25 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Audio too large")

        with tempfile.NamedTemporaryFile(delete=False, suffix=ext or ".m4a") as tmp:
            tmp_path = tmp.name
            tmp.write(contents)

        # 1) Speech -> Text
        with open(tmp_path, "rb") as f:
            tx = client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=f,
            )

        transcript = (getattr(tx, "text", "") or "").strip() or "(no speech detected)"

        # 2) Decide if this is a pilot-control command
        action = parse_action(transcript)

        # 2b) If save_feedback, store it immediately (so we never “lose” it)
        if action and action.name == "save_feedback":
            note = str(action.args.get("note", "")).strip() or transcript
            item = {
                "id": str(uuid.uuid4()),
                "ts": time.time(),
                "note": note,
                "transcript": transcript,
                "req_id": req_id,
            }
            _append_feedback(item)
            print(f"📝 feedback saved id={item['id']} req_id={req_id} note={note[:140]}")

        # 3) Text -> Reply
        system_prompt = (
            "You are Dispatch inside PathLight AR. "
            "Chelsey is blind and uses VoiceOver. "
            "Be calm, concise, and practical. "
            "Use short sentences. One idea per sentence. "
            "Avoid emojis. "
            "If the user asks for commands, briefly list: "
            "repeat, volume up/down, set volume 60, switch voice to alloy, speech on/off, feedback colon message."
        )

        user_prompt = transcript if (not mode or mode == "talk") else f"[mode={mode}] {transcript}"

        resp = client.responses.create(
            model="gpt-4o-mini",
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )

        reply = (getattr(resp, "output_text", "") or "").strip() or "I’m here. What would you like to ask?"

        # 4) Reply -> Speech (optional)
        want_tts = (tts.strip() != "0")
        audio_b64 = None
        audio_mime = None
        audio_size = 0

        if want_tts:
            tts_resp = client.audio.speech.create(
                model="gpt-4o-mini-tts",
                voice=voice,
                input=reply,
                response_format="mp3",  # correct param name
            )
            audio_bytes = tts_resp.read()
            audio_size = len(audio_bytes)
            audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
            audio_mime = "audio/mpeg"

        dt_ms = int((time.time() - t0) * 1000)
        print(
            f"✅ /dispatch req_id={req_id} mode={mode} ms={dt_ms} "
            f"transcript_len={len(transcript)} reply_len={len(reply)} "
            f"tts={want_tts} voice={voice} audio_bytes={audio_size} "
            f"action={(action.name if action else 'none')}"
        )

        return DispatchOut(
            transcript=transcript,
            reply=reply,
            audio_b64=audio_b64,
            audio_mime=audio_mime,
            action=action,
        )

    except RateLimitError:
        print(f"⚠️ /dispatch req_id={req_id} rate_limited")
        raise HTTPException(status_code=429, detail="Dispatch is busy. Please try again in a moment.")

    except BadRequestError as e:
        # Voice/param issues — prints exact error to Render logs.
        print(f"❌ /dispatch req_id={req_id} bad_request: {e}")
        raise HTTPException(status_code=400, detail="Bad request to speech service")

    except HTTPException:
        print(f"⚠️ /dispatch req_id={req_id} HTTPException")
        raise

    except Exception as e:
        print(f"❌ /dispatch req_id={req_id} error={type(e).__name__} msg={str(e)[:200]}")
        raise HTTPException(status_code=500, detail=f"Dispatch error: {type(e).__name__}")

    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
