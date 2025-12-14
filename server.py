import os
import tempfile
import time
import uuid

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI

app = FastAPI(title="PathLight Dispatch v1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten later if you want
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = OpenAI()  # uses OPENAI_API_KEY from env

class DispatchOut(BaseModel):
    transcript: str
    reply: str

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/dispatch", response_model=DispatchOut)
async def dispatch(
    mode: str = Form("talk"),
    audio: UploadFile = File(...),
):
    req_id = str(uuid.uuid4())
    t0 = time.time()

    if not audio.filename:
        raise HTTPException(status_code=400, detail="Missing audio file")

    # Accept common audio extensions; don't over-trust content_type.
    ext = os.path.splitext(audio.filename)[1].lower()
    if ext not in {".m4a", ".mp3", ".wav", ".webm", ".aac"}:
        # Still allow octet-stream with no extension if you want; for now be explicit.
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

        # 2) Text -> Reply
        system_prompt = (
            "You are Dispatch inside PathLight AR. "
            "Be concise, calm, and accessible for a blind user using VoiceOver. "
            "Use short sentences. One idea per sentence. Avoid emojis. "
            "If the user asks to save notes or preferences, say you can do that (coming next)."
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

        dt_ms = int((time.time() - t0) * 1000)
        print(f"✅ /dispatch req_id={req_id} mode={mode} ms={dt_ms} transcript_len={len(transcript)} reply_len={len(reply)}")

        return DispatchOut(transcript=transcript, reply=reply)

    except HTTPException:
        print(f"⚠️ /dispatch req_id={req_id} HTTPException")
        raise
    except Exception as e:
        print(f"❌ /dispatch req_id={req_id} error={type(e).__name__}")
        raise HTTPException(status_code=500, detail=f"Dispatch error: {type(e).__name__}")
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
