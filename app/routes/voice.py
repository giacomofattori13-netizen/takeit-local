import os
import uuid
from pathlib import Path
from xml.sax.saxutils import escape

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Query
from fastapi.responses import FileResponse, Response
from sqlmodel import Session

from app.db import get_session
from app.models import ConversationSession
from app.schemas import ChatRequest
from app.services.conversation_service import get_agent_greeting

router = APIRouter(prefix="/voice", tags=["voice"])

# Directory temporanea per i file MP3 generati da ElevenLabs
AUDIO_DIR = Path("/tmp/takeit_audio")
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

_GATHER_ATTRS = 'input="speech" language="it-IT" speechTimeout="auto"'
_POLLY_FALLBACK = "Polly.Giorgio"
_NO_INPUT_MSG = "Non ho sentito nulla. Riprovi a chiamare, grazie."


def _public_base_url() -> str:
    return os.getenv("PUBLIC_BASE_URL", "https://takeit-local-production.up.railway.app")


def _synthesize(text: str) -> str | None:
    """Chiama ElevenLabs TTS, salva l'MP3 in AUDIO_DIR e restituisce il filename.
    Restituisce None se le credenziali mancano o la chiamata fallisce."""
    api_key = os.getenv("ELEVENLABS_API_KEY")
    voice_id = os.getenv("ELEVENLABS_VOICE_ID")
    if not api_key or not voice_id:
        print("[ElevenLabs] Credenziali mancanti, fallback a Polly")
        return None
    model_id = os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    payload = {"text": text, "model_id": model_id}
    print(f"[ElevenLabs] POST {url} model={model_id} text={text!r}")
    try:
        resp = httpx.post(
            url,
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
        print(f"[ElevenLabs] HTTP {resp.status_code} content-type={resp.headers.get('content-type')} size={len(resp.content)}")
        if resp.status_code != 200:
            print(f"[ElevenLabs] Errore risposta: {resp.text}")
            return None
        resp.raise_for_status()
        filename = f"{uuid.uuid4()}.mp3"
        (AUDIO_DIR / filename).write_bytes(resp.content)
        print(f"[ElevenLabs] Audio salvato: {filename} ({len(resp.content)} bytes)")
        return filename
    except httpx.TimeoutException as e:
        print(f"[ElevenLabs] Timeout dopo 15s: {e} — fallback a Polly")
        return None
    except httpx.HTTPStatusError as e:
        print(f"[ElevenLabs] HTTPStatusError {e.response.status_code}: {e.response.text} — fallback a Polly")
        return None
    except Exception as e:
        print(f"[ElevenLabs] Errore inatteso {type(e).__name__}: {e} — fallback a Polly")
        return None


def _audio_element(text: str) -> str:
    """Restituisce <Play>url</Play> se ElevenLabs funziona, altrimenti <Say voice=Polly>."""
    filename = _synthesize(text)
    if filename:
        url = f"{_public_base_url()}/voice/audio/{filename}"
        return f"<Play>{escape(url)}</Play>"
    return f'<Say voice="{_POLLY_FALLBACK}">{escape(text)}</Say>'


def _twiml_gather(session_id: str, text: str) -> str:
    """TwiML che riproduce l'audio e rimane in ascolto per il turno successivo."""
    audio = _audio_element(text)
    no_input = _audio_element(_NO_INPUT_MSG)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f"  <Gather {_GATHER_ATTRS} "
        f'action="/voice/gather?session_id={session_id}" method="POST">\n'
        f"    {audio}\n"
        "  </Gather>\n"
        f"  {no_input}\n"
        "</Response>"
    )


def _twiml_end(text: str) -> str:
    """TwiML che riproduce l'audio e chiude la chiamata."""
    audio = _audio_element(text)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f"  {audio}\n"
        "  <Hangup/>\n"
        "</Response>"
    )


@router.get("/audio/{filename}")
def serve_audio(filename: str):
    """Serve i file MP3 generati da ElevenLabs a Twilio."""
    # Blocca path traversal: accetta solo UUID.mp3
    if not filename.endswith(".mp3") or "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = AUDIO_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Audio not found")
    return FileResponse(path, media_type="audio/mpeg")


@router.post("/incoming")
def voice_incoming(
    From: str = Form(default=""),
    session: Session = Depends(get_session),
):
    """Webhook Twilio Voice: crea sessione, risponde con saluto + Gather speech."""
    caller_phone = From.strip() or None
    print(f"[Voice] Chiamata in arrivo da: {caller_phone!r}")

    session_id = str(uuid.uuid4())
    conversation = ConversationSession(
        session_id=session_id,
        customer_phone=caller_phone,
        items_json="[]",
        state="collecting_items",
        completed=False,
    )
    session.add(conversation)
    session.commit()
    print(f"[Voice] Sessione creata: {session_id}")

    greeting = get_agent_greeting()
    print(f"[Voice] Saluto: {greeting!r}")

    return Response(content=_twiml_gather(session_id, greeting), media_type="application/xml")


@router.post("/gather")
def voice_gather(
    session_id: str = Query(...),
    SpeechResult: str = Form(default=""),
    session: Session = Depends(get_session),
):
    """Riceve il testo trascritto da Twilio, lo passa al motore di chat
    e risponde con TwiML per far sentire la risposta al cliente."""
    # Import locale per evitare import circolare (chat importa da conversation_service)
    from app.routes.chat import chat  # noqa: PLC0415

    speech = SpeechResult.strip()
    print(f"[Voice] Gather session={session_id!r} speech={speech!r}")

    if not speech:
        twiml = _twiml_gather(session_id, "Non ho capito. Può ripetere?")
        return Response(content=twiml, media_type="application/xml")

    # Log del customer_phone nella sessione prima di chiamare il motore di chat
    from sqlmodel import select as _select
    _conv = session.exec(_select(ConversationSession).where(ConversationSession.session_id == session_id)).first()
    print(f"[Voice] Sessione {session_id}: customer_phone={_conv.customer_phone!r if _conv else 'NOT FOUND'} stato={_conv.state!r if _conv else 'N/A'}")

    chat_request = ChatRequest(session_id=session_id, message=speech)
    result = chat(chat_request, session)

    reply = result.response_message
    print(f"[Voice] Risposta agente: {reply!r} stato={result.state!r}")

    if result.state == "completed":
        twiml = _twiml_end(reply)
    else:
        twiml = _twiml_gather(session_id, reply)

    return Response(content=twiml, media_type="application/xml")
