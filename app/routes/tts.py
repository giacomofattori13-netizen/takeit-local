import os

import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

load_dotenv()

router = APIRouter(prefix="/tts", tags=["tts"])

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")
ELEVENLABS_MODEL_ID = os.getenv("ELEVENLABS_MODEL_ID", "eleven_turbo_v2_5")


class TTSRequest(BaseModel):
    text: str


@router.post("/")
async def text_to_speech(payload: TTSRequest):
    if not ELEVENLABS_API_KEY:
        raise HTTPException(status_code=500, detail="ELEVENLABS_API_KEY mancante")
    if not ELEVENLABS_VOICE_ID:
        raise HTTPException(status_code=500, detail="ELEVENLABS_VOICE_ID mancante")
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="Testo vuoto")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"

    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }

    body = {
        "text": payload.text,
        "model_id": ELEVENLABS_MODEL_ID,
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, headers=headers, json=body)

        if resp.status_code != 200:
            raise HTTPException(
                status_code=resp.status_code,
                detail=f"Errore ElevenLabs: {resp.text}",
            )

        return Response(content=resp.content, media_type="audio/mpeg")

    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Errore di rete TTS: {exc}") from exc