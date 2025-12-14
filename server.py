import os, json, tempfile
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

app = FastAPI(title="PathLight Dispatch v1")

# (Optional) loosen CORS for testing; tighten later if needed
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    """
    v1 contract to match your Vision Pro uploader:
    - multipart/form-data
    - fields: mode, audio (m4a)
    - response: { transcript, reply }
    """

    # Minimal placeholder (so deployment works even before OpenAI wiring)
    # Replace this with: STT -> chat -> return
    transcript = f"(received {audio.filename}, mode={mode})"
    reply = "Dispatch online. Ask me anything."

    return DispatchOut(transcript=transcript, reply=reply)
