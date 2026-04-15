import asyncio
import json
import os
import re
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
from app.services.conversation_service import (
    build_closed_message,
    get_agent_greeting,
    is_agent_active,
    lookup_customer,
)

router = APIRouter(prefix="/voice", tags=["voice"])

# Directory temporanea per i file MP3 generati da ElevenLabs
AUDIO_DIR = Path("/tmp/takeit_audio")
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

_GATHER_ATTRS = 'input="speech" language="it-IT" speechTimeout="auto"'
_POLLY_FALLBACK = "Polly.Giorgio"
_NO_INPUT_MSG = "Non ho sentito nulla. Riprovi a chiamare, grazie."

# Cache in memoria: testo → filename MP3. Evita chiamate ripetute a ElevenLabs.
_AUDIO_CACHE: dict[str, str] = {}

# Frasi pre-generate all'avvio del server
_CACHED_PHRASES = [
    "Certo, dimmi pure!",
    "Ok!",
    "Che nome metto?",
    "Per che ora?",
    "Perfetto, confermo?",
    _NO_INPUT_MSG,
]

# Stati in cui la risposta può essere semplice/cached → vale la pena speculare
_SPECULATIVE_STATES = {"collecting_items", "confirming_usual"}


def _public_base_url() -> str:
    return os.getenv("PUBLIC_BASE_URL", "https://takeit-local-production.up.railway.app")


def format_time_for_speech(text: str) -> str:
    """Converte orari HH:MM in forma parlata per ElevenLabs.
    'le 19:00' → 'alle 19', 'alle 20:30' → 'alle 20 e 30'
    Applicata a TUTTI i messaggi prima della chiamata ElevenLabs.
    """
    def _spoken(h: int, mm: int) -> str:
        return f"alle {h}" if mm == 0 else f"alle {h} e {mm}"

    # "le HH:MM" → "alle HH" (o "alle HH e MM")
    text = re.sub(
        r'\ble\s+(\d{1,2}):(\d{2})\b',
        lambda m: _spoken(int(m.group(1)), int(m.group(2))),
        text,
    )
    # "alle HH:MM" → "alle HH" (o "alle HH e MM")
    text = re.sub(
        r'\balle\s+(\d{1,2}):(\d{2})\b',
        lambda m: _spoken(int(m.group(1)), int(m.group(2))),
        text,
    )
    # Orari rimasti bare "HH:MM" → "HH" (o "HH e MM")
    text = re.sub(
        r'\b(\d{1,2}):(\d{2})\b',
        lambda m: str(int(m.group(1))) if int(m.group(2)) == 0
                  else f"{int(m.group(1))} e {int(m.group(2))}",
        text,
    )
    return text


def _italian_title(full_name: str) -> str:
    """'il signor' o 'la signora' in base al nome (euristica sul finale)."""
    first = full_name.strip().split()[0] if full_name.strip() else full_name
    if first.lower().endswith("a"):
        return f"la signora {full_name}"
    return f"il signor {full_name}"


def _synthesize(text: str) -> str | None:
    """Chiama ElevenLabs TTS (sync). Controlla la cache prima.
    Usata solo per il prewarm all'avvio; i route handler usano _synthesize_async."""
    if text in _AUDIO_CACHE:
        cached = _AUDIO_CACHE[text]
        if (AUDIO_DIR / cached).exists():
            print(f"[ElevenLabs] Cache hit: {text!r}")
            return cached

    api_key = os.getenv("ELEVENLABS_API_KEY")
    voice_id = os.getenv("ELEVENLABS_VOICE_ID")
    if not api_key or not voice_id:
        print("[ElevenLabs] Credenziali mancanti, fallback a Polly")
        return None
    model_id = os.getenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5")
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
        print(f"[ElevenLabs] HTTP {resp.status_code} size={len(resp.content)}")
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


async def _synthesize_async(text: str) -> str | None:
    """Chiama ElevenLabs TTS (async). Controlla la cache prima."""
    if text in _AUDIO_CACHE:
        cached = _AUDIO_CACHE[text]
        if (AUDIO_DIR / cached).exists():
            print(f"[ElevenLabs] Cache hit: {text!r}")
            return cached

    api_key = os.getenv("ELEVENLABS_API_KEY")
    voice_id = os.getenv("ELEVENLABS_VOICE_ID")
    if not api_key or not voice_id:
        print("[ElevenLabs] Credenziali mancanti, fallback a Polly")
        return None
    model_id = os.getenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    payload = {"text": text, "model_id": model_id}
    print(f"[ElevenLabs] POST {url} model={model_id} text={text!r}")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                url,
                headers={"xi-api-key": api_key, "Content-Type": "application/json"},
                json=payload,
            )
        print(f"[ElevenLabs] HTTP {resp.status_code} size={len(resp.content)}")
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


async def _audio_element_async(text: str) -> str:
    """Restituisce <Play>url</Play> se ElevenLabs funziona, altrimenti <Say voice=Polly>."""
    text = format_time_for_speech(text)
    print(f"[TTS] Testo finale: {text!r}")
    filename = await _synthesize_async(text)
    if filename:
        url = f"{_public_base_url()}/voice/audio/{filename}"
        return f"<Play>{escape(url)}</Play>"
    return f'<Say voice="{_POLLY_FALLBACK}">{escape(text)}</Say>'


def prewarm_audio_cache() -> None:
    """Pre-genera gli audio più frequenti all'avvio e li mette in cache.
    Chiamata da main.py on_startup (sync)."""
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        print("[ElevenLabs] Prewarm skip: ELEVENLABS_API_KEY non configurata")
        return
    print(f"[ElevenLabs] Prewarm di {len(_CACHED_PHRASES)} frasi frequenti...")
    cached_count = 0
    for phrase in _CACHED_PHRASES:
        filename = _synthesize(phrase)
        if filename:
            _AUDIO_CACHE[phrase] = filename
            cached_count += 1
            print(f"[ElevenLabs] Cached: {phrase!r} → {filename}")
        else:
            print(f"[ElevenLabs] Prewarm fallito per: {phrase!r}")
    print(f"[ElevenLabs] Prewarm completato: {cached_count}/{len(_CACHED_PHRASES)} frasi in cache")


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
async def voice_incoming(
    From: str = Form(default=""),
    session: Session = Depends(get_session),
):
    """Webhook Twilio Voice: crea sessione, lookup cliente, risponde con saluto + Gather speech."""
    caller_phone = From.strip() or None
    print(f"[Voice] Chiamata in arrivo da: {caller_phone!r}")

    # Controlla agent_active prima di qualsiasi altra operazione
    if not is_agent_active():
        print("[Voice] agent_active=False → chiusura chiamata")
        closed_audio = await _audio_element_async(build_closed_message())
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<Response>\n"
            f"  {closed_audio}\n"
            "  <Hangup/>\n"
            "</Response>"
        )
        return Response(content=twiml, media_type="application/xml")

    # Avvia il lookup cliente subito in background: il phone è già disponibile
    # prima ancora della sessione DB, così i ~500ms di Base44 si sovrappongono.
    lookup_task = None
    if caller_phone:
        print(f"[Voice] Customer lookup per {caller_phone}")
        lookup_task = asyncio.to_thread(lookup_customer, caller_phone)

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

    # Attendi il lookup (si sovrappone alle operazioni DB sopra)
    if lookup_task is not None:
        customer = await lookup_task
        if customer:
            found_name = (customer.get("full_name") or "").strip()
            if found_name:
                print(f"[Voice] Cliente trovato: {found_name}")
                # Saluta direttamente per nome — il numero è conferma sufficiente
                first_name = found_name.split()[0]
                greeting = f"Ciao {first_name}! Come posso aiutarti?"
                conversation.customer_name = found_name
                # Salva le pizze preferite per il flusso "solite"
                raw_fav = customer.get("favorite_pizzas") or []
                if isinstance(raw_fav, str):
                    raw_fav = [p.strip() for p in raw_fav.split(",") if p.strip()]
                conversation.favorite_pizzas_json = json.dumps(raw_fav[:5], ensure_ascii=False)
                session.add(conversation)
                session.commit()
            else:
                print("[Voice] Cliente non trovato")
        else:
            print("[Voice] Cliente non trovato")

    print(f"[Voice] Saluto: {greeting!r}")

    # Genera in parallelo l'audio del saluto e dell'eventuale no-input timeout
    audio, no_input = await asyncio.gather(
        _audio_element_async(greeting),
        _audio_element_async(_NO_INPUT_MSG),
    )
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f"  <Gather {_GATHER_ATTRS} "
        f'action="/voice/gather?session_id={session_id}" method="POST">\n'
        f"    {audio}\n"
        "  </Gather>\n"
        f"  {no_input}\n"
        "</Response>"
    )
    return Response(content=twiml, media_type="application/xml")


@router.post("/gather")
async def voice_gather(
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

    from sqlmodel import select as _select
    _conv = session.exec(_select(ConversationSession).where(ConversationSession.session_id == session_id)).first()
    _phone = _conv.customer_phone if _conv else "NOT FOUND"
    _state = _conv.state if _conv else "N/A"
    print(f"[Voice] Sessione {session_id}: customer_phone={_phone!r} stato={_state!r}")

    if not speech:
        count = (_conv.no_input_count or 0) + 1 if _conv else 1
        print(f"[Voice] No-input #{count} per session={session_id!r}")

        if count >= 2:
            # Seconda volta consecutiva senza input: saluta e chiudi
            _FAREWELL = "Mi dispiace, non riesco a sentirla. La richiamo appena possibile. Arrivederci!"
            farewell_audio = await _audio_element_async(_FAREWELL)
            twiml = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                "<Response>\n"
                f"  {farewell_audio}\n"
                "  <Hangup/>\n"
                "</Response>"
            )
            if _conv:
                _conv.no_input_count = 0
                session.add(_conv)
                session.commit()
            return Response(content=twiml, media_type="application/xml")

        # Prima volta: chiedi di ripetere e aggiorna il contatore
        if _conv:
            _conv.no_input_count = count
            session.add(_conv)
            session.commit()
        audio, no_input = await asyncio.gather(
            _audio_element_async("Non ho sentito nulla, può ripetere?"),
            _audio_element_async(_NO_INPUT_MSG),
        )
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<Response>\n"
            f"  <Gather {_GATHER_ATTRS} "
            f'action="/voice/gather?session_id={session_id}" method="POST">\n'
            f"    {audio}\n"
            "  </Gather>\n"
            f"  {no_input}\n"
            "</Response>"
        )
        return Response(content=twiml, media_type="application/xml")

    # Input valido: resetta il contatore no-input
    if _conv and _conv.no_input_count:
        _conv.no_input_count = 0
        session.add(_conv)
        session.commit()

    chat_request = ChatRequest(session_id=session_id, message=speech)

    if _state in _SPECULATIVE_STATES:
        # Parallelismo speculativo: OpenAI + pre-generazione "Ok!" in parallelo.
        # Se la risposta reale è già in cache, _audio_element_async è istantaneo
        # e si risparmia tutta la latenza ElevenLabs (≈ 500ms).
        print(f"[Voice] Parallel speculative: OpenAI + ElevenLabs('Ok!') per stato={_state!r}")
        result, _ = await asyncio.gather(
            asyncio.to_thread(chat, chat_request, session),
            _synthesize_async("Ok!"),
        )
    else:
        result = await asyncio.to_thread(chat, chat_request, session)

    reply = result.response_message
    cache_hit = reply in _AUDIO_CACHE and (AUDIO_DIR / _AUDIO_CACHE[reply]).exists()
    print(f"[Voice] Risposta agente: {reply!r} stato={result.state!r} cache_hit={cache_hit}")

    if result.state == "completed":
        # La conferma WhatsApp/SMS è già inviata dentro chat() con gli item arricchiti;
        # qui generiamo solo l'audio di chiusura.
        audio = await _audio_element_async(reply)
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<Response>\n"
            f"  {audio}\n"
            "  <Hangup/>\n"
            "</Response>"
        )
    else:
        # Genera in parallelo: audio risposta + audio no-input (quasi sempre cached)
        audio, no_input = await asyncio.gather(
            _audio_element_async(reply),
            _audio_element_async(_NO_INPUT_MSG),
        )
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<Response>\n"
            f"  <Gather {_GATHER_ATTRS} "
            f'action="/voice/gather?session_id={session_id}" method="POST">\n'
            f"    {audio}\n"
            "  </Gather>\n"
            f"  {no_input}\n"
            "</Response>"
        )

    return Response(content=twiml, media_type="application/xml")
