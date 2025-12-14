import os
import tempfile
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
    # Basic guardrails
    if not audio.filename:
        raise HTTPException(status_code=400, detail="Missing audio file")
    if audio.content_type and "audio" not in audio.content_type:
        raise HTTPException(status_code=415, detail=f"Unsupported content_type: {audio.content_type}")

    # Save upload to a temp file so OpenAI SDK can read it as a file handle
    suffix = os.path.splitext(audio.filename)[1] or ".m4a"
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
            contents = await audio.read()
            # Optional size limit (e.g., 25MB)
            if len(contents) > 25 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="Audio too large")
            tmp.write(contents)

        # 1) Speech -> Text (transcriptions)
        # Models include whisper-1 and newer transcribe snapshots. :contentReference[oaicite:3]{index=3}
        with open(tmp_path, "rb") as f:
            tx = client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=f,
            )
        transcript = (tx.text or "").strip()
        if not transcript:
            transcript = "(no speech detected)"

        # 2) Text -> Reply (Responses API) :contentReference[oaicite:4]{index=4}
        system_prompt = (
            "You are Dispatch inside PathLight AR. "
            "Be concise, friendly, and accessible for a blind user using VoiceOver. "
            "Prefer short sentences and direct answers. "
            "If the user asks for notes, preferences, or memory, say you can do that (we'll add it next)."
        )

        user_prompt = transcript
        if mode and mode != "talk":
            # keep your existing mode hook alive
            user_prompt = f"[mode={mode}] {transcript}"

        resp = client.responses.create(
            model="gpt-4o-mini",
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        reply = (resp.output_text or "").strip()
        if not reply:
            reply = "I’m here. What would you like to ask?"

        return DispatchOut(transcript=transcript, reply=reply)

    except HTTPException:
        raise
    except Exception as e:
        # Avoid leaking internals to client; keep server logs for debugging
        raise HTTPException(status_code=500, detail=f"Dispatch error: {type(e).__name__}")
    finally:
        try:
            if "tmp_path" in locals() and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
