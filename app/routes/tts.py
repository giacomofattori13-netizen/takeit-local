import os

import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.security import require_admin_api_key

load_dotenv()

router = APIRouter(prefix="/tts", tags=["tts"])

ELEVENLABS_MODEL_ID = os.getenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5")
TTS_TIMEOUT = httpx.Timeout(connect=3.0, read=12.0, write=5.0, pool=3.0)


def _get_max_tts_text_length() -> int:
    try:
        return max(1, int(os.getenv("TTS_MAX_TEXT_LENGTH", "800")))
    except ValueError:
        return 800


MAX_TTS_TEXT_LENGTH = _get_max_tts_text_length()


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_TTS_TEXT_LENGTH)


@router.post("/", dependencies=[Depends(require_admin_api_key)])
async def text_to_speech(payload: TTSRequest):
    elevenlabs_api_key = os.getenv("ELEVENLABS_API_KEY")
    elevenlabs_voice_id = os.getenv("ELEVENLABS_VOICE_ID")
    text = payload.text.strip()

    if not elevenlabs_api_key:
        raise HTTPException(status_code=500, detail="ELEVENLABS_API_KEY mancante")
    if not elevenlabs_voice_id:
        raise HTTPException(status_code=500, detail="ELEVENLABS_VOICE_ID mancante")
    if not text:
        raise HTTPException(status_code=400, detail="Testo vuoto")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{elevenlabs_voice_id}"

    headers = {
        "xi-api-key": elevenlabs_api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }

    body = {
        "text": text,
        "model_id": ELEVENLABS_MODEL_ID,
    }

    try:
        async with httpx.AsyncClient(timeout=TTS_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=body)

        if resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail="Errore provider TTS",
            )

        return Response(content=resp.content, media_type="audio/mpeg")

    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Timeout provider TTS") from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail="Errore di rete TTS") from exc
