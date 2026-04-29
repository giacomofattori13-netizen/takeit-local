import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from pathlib import Path
from xml.sax.saxutils import escape

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlmodel import Session

from app.db import get_session, engine as _db_engine
from app.models import ConversationSession
from app.schemas import ChatRequest
from app.services.conversation_service import (
    build_closed_message,
    get_agent_greeting,
    is_agent_active,
    lookup_customer,
)
from app.routes.chat import _extract_local_customer_name, _extract_local_pickup_time

router = APIRouter(prefix="/voice", tags=["voice"])

# Directory temporanea per i file MP3 generati da ElevenLabs
AUDIO_DIR = Path("/tmp/takeit_audio")
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

_GATHER_ATTRS = 'input="speech" language="it-IT" speechTimeout="auto"'
_POLLY_FALLBACK = "Polly.Giorgio"
_NO_INPUT_MSG = "Non ho sentito nulla. Riprovi a chiamare, grazie."

# Cache in memoria: testo → filename MP3. Evita chiamate ripetute a ElevenLabs.
_AUDIO_CACHE: dict[str, str] = {}

# Streaming: testo registrato per essere servito via /voice/stream/{id}
# Twilio richiede l'audio DOPO aver ricevuto il TwiML, quindi non dobbiamo
# attendere ElevenLabs prima di rispondere — registriamo subito e streaminamo dopo.
_pending_streams: dict[str, tuple[str, float]] = {}  # stream_id → (text, created_at)

# Filler audio: riprodotto durante l'elaborazione OpenAI per collecting_items.
# chat() parte in background; Twilio riproduce il filler poi chiama /voice/process.
_FILLER_PHRASE = "Un momento..."
_pending_responses: dict[str, asyncio.Task] = {}  # session_id → Task[ChatResponse]
_filler_last_played: dict[str, float] = {}        # session_id → epoch dell'ultimo filler
_FILLER_COOLDOWN = 20.0                            # secondi minimi tra un filler e il successivo

# Frasi pre-generate all'avvio del server
_CACHED_PHRASES = [
    "Certo, dimmi pure!",
    "Ok!",
    "Che nome metto?",
    "Per che ora?",
    "Perfetto, confermo?",
    _NO_INPUT_MSG,
    _FILLER_PHRASE,
]

# Stati in cui la risposta può essere semplice/cached → vale la pena speculare.
# collecting_items usa il filler+redirect invece, quindi non è più qui.
_SPECULATIVE_STATES = {"confirming_usual"}

# Risposte banali in collecting_items che NON meritano il filler:
# affermazioni/negazioni, intenzioni generiche senza pizza specifica.
# Tutto ciò che matcha → niente filler (il turno si risolve velocemente).
_TRIVIAL_COLLECTING_RE = re.compile(
    r"^(?:"
    # Affermazioni / negazioni semplici
    r"s[iì]|no|ok|okay|va bene|certo|esatto|giusto|perfetto|confermo|no grazie|"
    # voglio/vorrei [ordinare] + intenzione generica senza nome pizza specifico
    r"(?:no[,\s]+)?(?:voglio|vorrei)(?:\s+ordinare)?\s+(?:qualcosa\s+di\s+diverso|(?:qualcosa\s+)?altro(?:\s+\w+)*)|"
    # voglio/vorrei [ordinare] + "una pizza" / "pizza" generico
    r"(?:no[,\s]+)?(?:voglio|vorrei)(?:\s+ordinare)?\s+(?:una?\s+)?pizza|"
    # voglio/vorrei [ordinare] + articolo/partitivo + "pizze" generico
    # copre: "delle pizze", "le pizze", "un po' di pizze", solo "pizze"
    # Nota: \s+ è DENTRO il gruppo opzionale per non consumare lo spazio prima dell'articolo
    r"(?:no[,\s]+)?(?:voglio|vorrei)(?:\s+ordinare)?\s+(?:(?:delle?|del|dei|le|un\s+po['\s]+di)\s+)?pizze|"
    # voglio/vorrei [ordinare] + numero (cifre o parole) + "pizza/pizze"
    # copre: "7 pizze", "due pizze", "ordinare 3 pizze", ecc.
    r"(?:no[,\s]+)?(?:voglio|vorrei)(?:\s+ordinare)?\s+(?:\d+|due|tre|quattro|cinque|sei|sette|otto|nove|dieci)\s+pizze?|"
    # "voglio ordinare" / "vorrei ordinare" da soli (senza nulla dopo)
    r"(?:voglio|vorrei)\s+ordinare|"
    # Altri segnali di cambio/annullamento/intenzione nuda
    r"diversi?a?|altri?e?|cambio|"
    r"ordine|ordinare"
    r")\s*[!.,?]*$",
    re.IGNORECASE,
)


def _needs_filler(speech: str, state: str) -> bool:
    """True se riprodurre 'Un momento...' è appropriato per questo messaggio.

    collecting_name / collecting_pickup_time → filler solo quando il testo non
    è risolvibile dal fast path locale.

    collecting_items → True solo se il testo sembra contenere dati di ordine
    reali (nome pizza, ingrediente, frase complessa). False per risposte
    semplici tipo 'sì/no' o intenzioni generiche tipo 'voglio ordinare' dove
    il filler suonerebbe strano prima di 'Certo, dimmi pure!'.

    Tutti gli altri stati → False (fast path Python, niente LLM pesante).
    """
    if state == "collecting_name":
        return _extract_local_customer_name(speech) is None
    if state == "collecting_pickup_time":
        return _extract_local_pickup_time(speech) is None
    if state != "collecting_items":
        return False
    normalized = speech.strip().rstrip(".,!?")
    return not _TRIVIAL_COLLECTING_RE.match(normalized)


def _public_base_url() -> str:
    return os.getenv("PUBLIC_BASE_URL", "https://takeit-local-production.up.railway.app")


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


async def _verify_twilio_request(request: Request) -> None:
    """Verifica X-Twilio-Signature sui webhook voce.

    In locale si può impostare SKIP_TWILIO_SIGNATURE_VALIDATION=true.
    """
    if _truthy_env("SKIP_TWILIO_SIGNATURE_VALIDATION"):
        return

    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not auth_token:
        raise HTTPException(status_code=503, detail="TWILIO_AUTH_TOKEN non configurato")

    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        raise HTTPException(status_code=403, detail="Firma Twilio mancante")

    public_url = _public_base_url().rstrip("/") + request.url.path
    if request.url.query:
        public_url += f"?{request.url.query}"

    form = await request.form()
    params = [(key, str(value)) for key, value in form.multi_items()]
    payload = public_url + "".join(f"{key}{value}" for key, value in sorted(params))
    expected = base64.b64encode(
        hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    ).decode()

    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=403, detail="Firma Twilio non valida")


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
    payload = {"text": text, "model_id": model_id, "language_code": "it"}
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
    payload = {"text": text, "model_id": model_id, "language_code": "it"}
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
    """Restituisce <Play>url</Play> se ElevenLabs funziona, altrimenti <Say voice=Polly>.

    Per le frasi in cache (pre-riscaldate all'avvio) serve il file MP3 già pronto.
    Per tutto il resto registra uno stream_id e restituisce subito l'URL di streaming:
    Twilio riceverà il TwiML senza attendere ElevenLabs, poi richiederà l'audio e
    lo riceverà in streaming non appena ElevenLabs inizia a generarlo (~200-400ms).
    """
    text = format_time_for_speech(text)
    print(f"[TTS] Testo finale: {text!r}")

    # Cache hit: file già pronto su disco → nessuna latenza ElevenLabs
    if text in _AUDIO_CACHE:
        cached = _AUDIO_CACHE[text]
        if (AUDIO_DIR / cached).exists():
            print(f"[ElevenLabs] Cache hit: {text!r}")
            url = f"{_public_base_url()}/voice/audio/{cached}"
            return f"<Play>{escape(url)}</Play>"

    # ElevenLabs non configurato → fallback Polly (nessuna latenza di rete)
    if not os.getenv("ELEVENLABS_API_KEY") or not os.getenv("ELEVENLABS_VOICE_ID"):
        return f'<Say voice="{_POLLY_FALLBACK}">{escape(text)}</Say>'

    # Registra lo stream e ritorna subito: Twilio recupererà l'audio in streaming
    stream_id = str(uuid.uuid4())
    _pending_streams[stream_id] = (text, time.time())
    url = f"{_public_base_url()}/voice/stream/{stream_id}"
    print(f"[ElevenLabs] Stream registrato {stream_id}: {text!r}")
    return f"<Play>{escape(url)}</Play>"


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


@router.get("/stream/{stream_id}")
async def stream_audio(stream_id: str):
    """Proxy ElevenLabs streaming TTS → Twilio.

    Twilio richiede questo URL dopo aver ricevuto il TwiML con <Play>.
    Avviamo la richiesta a ElevenLabs solo ora e facciamo il proxy dei byte
    non appena arrivano: Twilio inizia a riprodurre l'audio senza attendere
    che sia completamente generato (~200-400ms al primo byte con eleven_flash_v2_5).
    """
    # Pulizia lazy: rimuovi stream_id scaduti (> 60s, mai recuperati da Twilio)
    now = time.time()
    stale = [k for k, (_, ts) in list(_pending_streams.items()) if now - ts > 60]
    for k in stale:
        _pending_streams.pop(k, None)
        print(f"[ElevenLabs] Stream scaduto rimosso: {k}")

    entry = _pending_streams.pop(stream_id, None)
    if entry is None:
        raise HTTPException(status_code=404, detail="Stream non trovato o scaduto")

    text, _ = entry
    api_key = os.getenv("ELEVENLABS_API_KEY")
    voice_id = os.getenv("ELEVENLABS_VOICE_ID")
    model_id = os.getenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5")
    el_url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
    print(f"[ElevenLabs] Avvio stream {stream_id}: model={model_id} text={text!r}")

    async def generate():
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0)
            ) as http:
                async with http.stream(
                    "POST",
                    el_url,
                    headers={"xi-api-key": api_key, "Content-Type": "application/json"},
                    json={"text": text, "model_id": model_id, "language_code": "it"},
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        print(f"[ElevenLabs] Stream error {resp.status_code}: {body[:200]}")
                        return
                    print(f"[ElevenLabs] Stream {stream_id}: primo chunk in arrivo")
                    async for chunk in resp.aiter_bytes(chunk_size=4096):
                        yield chunk
        except Exception as e:
            print(f"[ElevenLabs] Stream {stream_id} eccezione: {type(e).__name__}: {e}")

    return StreamingResponse(generate(), media_type="audio/mpeg")


async def _prefetch_openai_connection() -> None:
    """Dummy call OpenAI per mantenere calda la connessione TCP keepalive.
    Fire-and-forget — avviato dopo che la risposta è già stata inviata all'utente."""
    from app.services.conversation_service import MODEL_NAME as _model, get_openai_client

    def _dummy() -> None:
        t0 = time.time()
        try:
            get_openai_client().chat.completions.create(
                model=_model,
                messages=[{"role": "user", "content": "ok"}],
                max_tokens=1,
            )
            elapsed = int((time.time() - t0) * 1000)
            print(f"[Prefetch] connessione calda in {elapsed}ms")
        except Exception as exc:
            elapsed = int((time.time() - t0) * 1000)
            print(f"[Prefetch] errore in {elapsed}ms: {type(exc).__name__}: {exc}")

    await asyncio.to_thread(_dummy)


def _filler_audio_element() -> str:
    """<Play> del filler dalla cache, o <Say> come fallback. Sempre sync (cache-only)."""
    if _FILLER_PHRASE in _AUDIO_CACHE and (AUDIO_DIR / _AUDIO_CACHE[_FILLER_PHRASE]).exists():
        url = f"{_public_base_url()}/voice/audio/{_AUDIO_CACHE[_FILLER_PHRASE]}"
        return f"<Play>{escape(url)}</Play>"
    return f'<Say voice="{_POLLY_FALLBACK}">{escape(_FILLER_PHRASE)}</Say>'


async def _build_response_twiml(result, session_id: str) -> str:
    """Costruisce il TwiML finale dal risultato di chat()."""
    reply = result.response_message
    cache_hit = reply in _AUDIO_CACHE and (AUDIO_DIR / _AUDIO_CACHE[reply]).exists()
    print(f"[Voice] Risposta agente: {reply!r} stato={result.state!r} cache_hit={cache_hit}")
    if result.state == "completed":
        audio = await _audio_element_async(reply)
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<Response>\n"
            f"  {audio}\n"
            "  <Hangup/>\n"
            "</Response>"
        )
    audio, no_input = await asyncio.gather(
        _audio_element_async(reply),
        _audio_element_async(_NO_INPUT_MSG),
    )
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


@router.post("/process")
async def voice_process(request: Request, session_id: str = Query(...)):
    """Chiamato da Twilio dopo il filler audio per collecting_items.
    Attende il risultato del chat() in background e ritorna il TwiML finale."""
    await _verify_twilio_request(request)
    task = _pending_responses.pop(session_id, None)
    if task is None:
        print(f"[Voice] /process: nessun task per session={session_id!r} (già consumato o scaduto)")
        audio, no_input = await asyncio.gather(
            _audio_element_async("Scusi, può ripetere?"),
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

    try:
        result = await asyncio.wait_for(task, timeout=10.0)
    except Exception as e:
        print(f"[Voice] /process: errore task session={session_id!r}: {type(e).__name__}: {e}")
        audio, no_input = await asyncio.gather(
            _audio_element_async("Scusi, non ho capito. Può ripetere?"),
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

    twiml = await _build_response_twiml(result, session_id)
    if result.state != "completed":
        asyncio.create_task(_prefetch_openai_connection())
    return Response(content=twiml, media_type="application/xml")


@router.post("/incoming")
async def voice_incoming(
    request: Request,
    From: str = Form(default=""),
    session: Session = Depends(get_session),
):
    """Webhook Twilio Voice: crea sessione, lookup cliente, risponde con saluto + Gather speech."""
    await _verify_twilio_request(request)
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
    request: Request,
    session_id: str = Query(...),
    SpeechResult: str = Form(default=""),
    session: Session = Depends(get_session),
):
    """Riceve il testo trascritto da Twilio, lo passa al motore di chat
    e risponde con TwiML per far sentire la risposta al cliente."""
    await _verify_twilio_request(request)
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

    if _needs_filler(speech, _state):
        # Filler path: lancia chat() in background con sessione DB propria,
        # rispondi subito con il filler audio + Redirect a /voice/process.
        def _chat_bg():
            try:
                with Session(_db_engine) as _db:
                    result = chat(chat_request, _db)
                print(
                    f"[Voice] _chat_bg completato: session={session_id!r} stato={result.state!r} "
                    f"order_id={result.order_id!r} phone={_phone!r}"
                )
                if result.state == "completed":
                    print(f"[SMS] Tentativo invio dopo filler: session={session_id!r} phone={_phone!r}")
                return result
            except Exception as exc:
                print(f"[Voice] _chat_bg ERRORE session={session_id!r}: {type(exc).__name__}: {exc}")
                raise

        task = asyncio.create_task(asyncio.to_thread(_chat_bg))
        _pending_responses[session_id] = task

        # Cooldown: riproduci il filler solo se sono passati almeno _FILLER_COOLDOWN
        # secondi dall'ultimo filler nella stessa sessione, per evitare che il cliente
        # senta "Un momento..." ad ogni pizza aggiunta consecutiva.
        _now = time.time()
        _last = _filler_last_played.get(session_id, 0.0)
        _cooldown_ok = (_now - _last) > _FILLER_COOLDOWN
        if _cooldown_ok:
            _filler_last_played[session_id] = _now
            _filler_el = _filler_audio_element()
        else:
            _filler_el = ""
        print(
            f"[Voice] Filler path: task avviato per session={session_id!r} phone={_phone!r} "
            f"filler={'ON' if _cooldown_ok else f'SKIP (cooldown {int(_FILLER_COOLDOWN - (_now - _last))}s)'}"
        )
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<Response>\n"
            f"  {_filler_el}\n"
            f'  <Redirect method="POST">/voice/process?session_id={session_id}</Redirect>\n'
            "</Response>"
        )
        return Response(content=twiml, media_type="application/xml")

    if _state in _SPECULATIVE_STATES:
        # Parallelismo speculativo: OpenAI + pre-generazione "Ok!" in parallelo.
        print(f"[Voice] Parallel speculative: OpenAI + ElevenLabs('Ok!') per stato={_state!r}")
        result, _ = await asyncio.gather(
            asyncio.to_thread(chat, chat_request, session),
            _synthesize_async("Ok!"),
        )
    else:
        result = await asyncio.to_thread(chat, chat_request, session)

    twiml = await _build_response_twiml(result, session_id)
    if result.state != "completed":
        asyncio.create_task(_prefetch_openai_connection())
    return Response(content=twiml, media_type="application/xml")
