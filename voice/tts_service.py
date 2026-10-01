"""Local offline text-to-speech (eSpeak NG). Internal-only service.

User text is always passed after the ``--`` option terminator, so text that
begins with '-' is spoken literally and can never be parsed as an espeak-ng
option. No shell is involved.
"""
import os
import re
import subprocess
import tempfile

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

MAX_TEXT = 4000
# Allowlisted voice profiles -> espeak-ng voice names. Unknown profiles are rejected.
VOICES = {"default": "en-us", "en-us": "en-us", "en-gb": "en-gb", "coach": "en-us+m3", "analyst": "en-gb+f3"}
_SPEED_RE = re.compile(r"[0-9]{2,3}")

app = FastAPI(title="Local TTS")


class Speak(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    voice: str = Field(default="default", max_length=32)


def speed() -> str:
    raw = os.getenv("TTS_SPEED", "165")
    if not _SPEED_RE.fullmatch(raw) or not 80 <= int(raw) <= 450:
        raise RuntimeError("TTS_SPEED must be an integer between 80 and 450")
    return raw


def build_command(text: str, voice: str, out_path: str) -> list[str]:
    if voice not in VOICES:
        raise ValueError("unsupported voice profile")
    if not text or len(text) > MAX_TEXT or "\x00" in text:
        raise ValueError("invalid text")
    return ["espeak-ng", "-v", VOICES[voice], "-s", speed(), "-w", out_path, "--", text]


@app.get("/health")
def health():
    return {"status": "ok", "engine": "espeak-ng"}


@app.post("/synthesize")
def synthesize(body: Speak):
    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        try:
            cmd = build_command(body.text, body.voice, f.name)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=30, shell=False, check=False)
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(504, "speech synthesis timed out") from exc
        if result.returncode:
            raise HTTPException(500, "speech synthesis failed")
        f.seek(0)
        return Response(f.read(), media_type="audio/wav")
