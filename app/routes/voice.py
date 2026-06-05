import asyncio
import base64
import datetime
import hashlib
import hmac
import json
import os
import re
import threading
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
from app.privacy import describe_text_for_log, mask_name, mask_phone
from app.schemas import ChatRequest
from app.telemetry import record_latency
from app.services.conversation_service import (
    build_closed_message,
    get_agent_greeting,
    is_agent_active,
    lookup_customer,
    resolve_restaurant_from_phone,
)
from app.routes.chat import (
    _extract_local_customer_name,
    _extract_local_pickup_time,
    _extract_party_size,
    _reservation_confirmation_intent,
)

router = APIRouter(prefix="/voice", tags=["voice"])

# ── CallLog tracking ─────────────────────────────────────────────────────────
# session_id → (call_log_id, started_at_epoch)  — in-memory, intentionally ephemeral
_call_logs: dict[str, tuple[str, float]] = {}


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


async def _call_log_create_instant(
    *,
    restaurant_id: str,
    caller_phone: str | None,
    outcome: str,
    summary: str = "",
) -> None:
    """Fire-and-forget: create a completed CallLog in a single step (no update needed)."""
    now = _now_iso()
    data: dict = {
        "restaurant_id": restaurant_id or None,
        "started_at": now,
        "ended_at": now,
        "duration_seconds": 0,
        "caller_phone": caller_phone,
        "outcome": outcome,
    }
    if summary:
        data["summary"] = summary
    try:
        from app.services.base44_client import create_call_log
        await asyncio.to_thread(create_call_log, data)
    except Exception as exc:
        print(f"[CallLog] Errore create_instant outcome={outcome!r}: {type(exc).__name__}: {exc}")


async def _call_log_create(
    session_id: str,
    restaurant_id: str,
    caller_phone: str | None,
) -> None:
    """Fire-and-forget: create a CallLog with outcome=abbandonata as safety default."""
    started_epoch = time.time()
    data = {
        "restaurant_id": restaurant_id or None,
        "started_at": _now_iso(),
        "caller_phone": caller_phone,
        "outcome": "abbandonata",
    }
    try:
        from app.services.base44_client import create_call_log
        result = await asyncio.to_thread(create_call_log, data)
        if result and result.get("id"):
            _call_logs[session_id] = (str(result["id"]), started_epoch)
            print(f"[CallLog] Creato id={result['id']!r} session={session_id!r}")
        else:
            print(f"[CallLog] Creazione fallita (nessun id) session={session_id!r}")
    except Exception as exc:
        print(f"[CallLog] Errore creazione session={session_id!r}: {type(exc).__name__}: {exc}")


async def _call_log_update(
    session_id: str,
    outcome: str,
    *,
    order_id: int | None = None,
    summary: str = "",
) -> None:
    """Fire-and-forget: finalise a CallLog with the real outcome."""
    entry = _call_logs.pop(session_id, None)
    if not entry:
        return
    log_id, started_epoch = entry
    ended_epoch = time.time()
    patch: dict = {
        "ended_at": _now_iso(),
        "duration_seconds": max(0, int(ended_epoch - started_epoch)),
        "outcome": outcome,
    }
    if order_id is not None:
        patch["order_id"] = str(order_id)
    if summary:
        patch["summary"] = summary
    try:
        from app.services.base44_client import update_call_log
        await asyncio.to_thread(update_call_log, log_id, patch)
    except Exception as exc:
        print(f"[CallLog] Errore aggiornamento id={log_id!r}: {type(exc).__name__}: {exc}")


# Directory temporanea per i file MP3 generati da ElevenLabs
AUDIO_DIR = Path("/tmp/takeit_audio")
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

_GATHER_ATTRS = 'input="speech" language="it-IT" speechTimeout="auto"'
_POLLY_FALLBACK = "Polly.Giorgio"
_NO_INPUT_MSG = "Non ho sentito nulla. Riprovi a chiamare, grazie."

# Cache in memoria: testo → filename MP3. Evita chiamate ripetute a ElevenLabs.
_AUDIO_CACHE: dict[str, str] = {}
_AUDIO_CACHE_META: dict[str, dict[str, float | bool]] = {}
_AUDIO_CACHE_LOCK = threading.Lock()

# Streaming: testo registrato per essere servito via /voice/stream/{id}
# Twilio richiede l'audio DOPO aver ricevuto il TwiML, quindi non dobbiamo
# attendere ElevenLabs prima di rispondere — registriamo subito e streaminamo dopo.
_pending_streams: dict[str, tuple[str, float]] = {}  # stream_id → (text, created_at)

# Filler audio: riprodotto durante l'elaborazione OpenAI per collecting_items.
# chat() parte in background; Twilio riproduce il filler poi chiama /voice/process.
_FILLER_PHRASE = "Un momento..."
_pending_responses: dict[str, asyncio.Task] = {}  # session_id → Task[ChatResponse]
_pending_response_created_at: dict[str, float] = {}
_filler_last_played: dict[str, float] = {}        # session_id → epoch dell'ultimo filler
_FILLER_COOLDOWN = 20.0                            # secondi minimi tra un filler e il successivo
_CUSTOMER_LOOKUP_TIMEOUT_DEFAULT_SECONDS = 1.0
_VOICE_GREETING_LOOKUP_TIMEOUT_DEFAULT_SECONDS = 0.25
_PENDING_RESPONSE_TTL_DEFAULT_SECONDS = 90.0
_PENDING_RESPONSE_DONE_GRACE_DEFAULT_SECONDS = 30.0
_pending_response_last_cleanup = 0.0
_PREWARM_THREAD_STARTED = False
_PREWARM_THREAD_LOCK = threading.Lock()
_tts_stream_client: httpx.AsyncClient | None = None

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

    Stati prenotazione con rete/Base44 → filler solo quando il testo può far
    partire davvero disponibilità o conferma.

    Tutti gli altri stati → False (fast path Python, niente LLM pesante).
    """
    if state == "collecting_name":
        return _extract_local_customer_name(speech) is None
    if state == "collecting_pickup_time":
        return _extract_local_pickup_time(speech) is None
    if state == "collecting_reservation_party":
        return _extract_party_size(speech) is not None
    if state == "awaiting_reservation_confirmation":
        return _reservation_confirmation_intent(speech) == "confirm"
    if state != "collecting_items":
        return False
    normalized = speech.strip().rstrip(".,!?")
    return not _TRIVIAL_COLLECTING_RE.match(normalized)


def _public_base_url() -> str:
    return os.getenv("PUBLIC_BASE_URL", "https://takeit-local-production.up.railway.app")


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, "").strip())
    except ValueError:
        return default
    return value if value > 0 else default


def _positive_float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, "").strip())
    except ValueError:
        return default
    return value if value > 0 else default


def _audio_cache_ttl_seconds() -> float:
    return _positive_float_env("VOICE_AUDIO_CACHE_TTL_SECONDS", 24 * 60 * 60)


def _audio_cache_max_items() -> int:
    return _positive_int_env("VOICE_AUDIO_CACHE_MAX_ITEMS", 128)


def _customer_lookup_timeout_seconds() -> float:
    return _positive_float_env(
        "CUSTOMER_LOOKUP_TIMEOUT_SECONDS",
        _CUSTOMER_LOOKUP_TIMEOUT_DEFAULT_SECONDS,
    )


def _voice_greeting_lookup_timeout_seconds() -> float:
    return _positive_float_env(
        "VOICE_GREETING_LOOKUP_TIMEOUT_SECONDS",
        _VOICE_GREETING_LOOKUP_TIMEOUT_DEFAULT_SECONDS,
    )


def _run_chat_with_fresh_session(chat_func, chat_request: ChatRequest):
    with Session(_db_engine) as db:
        return chat_func(chat_request, db)


def _pending_response_ttl_seconds() -> float:
    return _positive_float_env(
        "VOICE_PENDING_RESPONSE_TTL_SECONDS",
        _PENDING_RESPONSE_TTL_DEFAULT_SECONDS,
    )


def _pending_response_done_grace_seconds() -> float:
    return _positive_float_env(
        "VOICE_PENDING_RESPONSE_DONE_GRACE_SECONDS",
        _PENDING_RESPONSE_DONE_GRACE_DEFAULT_SECONDS,
    )


def _prewarm_request_timeout_seconds() -> float:
    return _positive_float_env("VOICE_PREWARM_REQUEST_TIMEOUT_SECONDS", 4.0)


def _prewarm_total_budget_seconds() -> float:
    return _positive_float_env("VOICE_PREWARM_TOTAL_BUDGET_SECONDS", 18.0)


def _cleanup_stale_pending_responses(*, force: bool = False) -> int:
    global _pending_response_last_cleanup
    now = time.time()
    if not force and now - _pending_response_last_cleanup < 10.0:
        return 0
    _pending_response_last_cleanup = now

    ttl = _pending_response_ttl_seconds()
    removed = 0
    for session_id, task in list(_pending_responses.items()):
        created_at = _pending_response_created_at.get(session_id, now)
        if now - created_at <= ttl:
            continue
        current = _pending_responses.pop(session_id, None)
        _pending_response_created_at.pop(session_id, None)
        if current is task:
            removed += 1
            if not task.done():
                task.cancel()
    if removed:
        print(f"[Voice] Pending filler task scaduti rimossi: {removed}")
    return removed


async def _drop_pending_response_after_grace(session_id: str, task: asyncio.Task) -> None:
    await asyncio.sleep(_pending_response_done_grace_seconds())
    current = _pending_responses.get(session_id)
    if current is task and task.done():
        _pending_responses.pop(session_id, None)
        _pending_response_created_at.pop(session_id, None)
        print(f"[Voice] Pending filler task completato rimosso dopo grace: session={session_id!r}")


async def _resolve_customer_lookup_task(
    lookup_task: asyncio.Task | None,
    phone: str | None,
    *,
    timeout_seconds: float | None = None,
    cancel_on_timeout: bool = True,
) -> dict | None:
    if lookup_task is None:
        return None
    timeout = timeout_seconds if timeout_seconds is not None else _customer_lookup_timeout_seconds()
    started = time.perf_counter()
    try:
        awaitable = lookup_task if cancel_on_timeout else asyncio.shield(lookup_task)
        result = await asyncio.wait_for(awaitable, timeout=timeout)
        record_latency(
            "voice",
            "customer_lookup",
            (time.perf_counter() - started) * 1000,
            result="found" if result else "missing",
            timeout_seconds=timeout,
        )
        return result
    except asyncio.TimeoutError:
        record_latency(
            "voice",
            "customer_lookup",
            (time.perf_counter() - started) * 1000,
            result="timeout",
            timeout_seconds=timeout,
        )
        if cancel_on_timeout:
            lookup_task.cancel()
        print(f"[Customer] Lookup timeout per {mask_phone(phone)}, saluto senza profilo")
    except Exception as exc:
        record_latency(
            "voice",
            "customer_lookup",
            (time.perf_counter() - started) * 1000,
            result="error",
            timeout_seconds=timeout,
            error=type(exc).__name__,
        )
        print(f"[Customer] Lookup errore per {mask_phone(phone)}: {type(exc).__name__}: {exc}")
    return None


def _apply_customer_profile_to_conversation(
    conversation: ConversationSession,
    customer: dict,
) -> tuple[str | None, bool]:
    found_name = (customer.get("full_name") or "").strip()
    raw_fav = customer.get("favorite_pizzas") or []
    if isinstance(raw_fav, str):
        raw_fav = [p.strip() for p in raw_fav.split(",") if p.strip()]
    favorite_pizzas = raw_fav[:5] if isinstance(raw_fav, list) else []

    changed = False
    if found_name and not conversation.customer_name:
        conversation.customer_name = found_name
        changed = True
    if favorite_pizzas:
        fav_json = json.dumps(favorite_pizzas, ensure_ascii=False)
        if conversation.favorite_pizzas_json != fav_json:
            conversation.favorite_pizzas_json = fav_json
            changed = True
    return found_name or None, changed


def _store_customer_profile_sync(session_id: str, customer: dict) -> None:
    from sqlmodel import select as _select

    with Session(_db_engine) as db:
        conversation = db.exec(
            _select(ConversationSession).where(ConversationSession.session_id == session_id)
        ).first()
        if not conversation or conversation.completed:
            return
        found_name, changed = _apply_customer_profile_to_conversation(conversation, customer)
        if not changed:
            return
        db.add(conversation)
        db.commit()
        print(
            f"[Voice] Profilo cliente aggiornato in background: "
            f"session={session_id!r} name={mask_name(found_name)}"
        )


async def _store_customer_profile_from_task(session_id: str, lookup_task: asyncio.Task) -> None:
    try:
        customer = lookup_task.result()
    except asyncio.CancelledError:
        return
    except Exception as exc:
        print(f"[Voice] Lookup cliente background errore session={session_id!r}: {type(exc).__name__}: {exc}")
        return
    if not customer:
        return
    await asyncio.to_thread(_store_customer_profile_sync, session_id, customer)


def _defer_customer_profile_update(session_id: str, lookup_task: asyncio.Task) -> None:
    loop = asyncio.get_running_loop()

    def _on_done(done_task: asyncio.Task) -> None:
        loop.create_task(_store_customer_profile_from_task(session_id, done_task))

    lookup_task.add_done_callback(_on_done)


def _drop_audio_cache_entry(text: str, *, remove_file: bool = True) -> None:
    filename = _AUDIO_CACHE.pop(text, None)
    _AUDIO_CACHE_META.pop(text, None)
    if remove_file and filename:
        try:
            (AUDIO_DIR / filename).unlink(missing_ok=True)
        except OSError as exc:
            print(f"[ElevenLabs] Errore rimozione cache audio {filename}: {type(exc).__name__}: {exc}")


def _prune_audio_cache(now: float | None = None) -> None:
    now = time.time() if now is None else now
    ttl_seconds = _audio_cache_ttl_seconds()

    for text, filename in list(_AUDIO_CACHE.items()):
        meta = _AUDIO_CACHE_META.get(text, {})
        path = AUDIO_DIR / filename
        is_pinned = bool(meta.get("pinned", False))
        created_at = float(meta.get("created_at", now))
        if not path.exists() or (not is_pinned and now - created_at > ttl_seconds):
            _drop_audio_cache_entry(text)

    max_items = _audio_cache_max_items()
    overflow = len(_AUDIO_CACHE) - max_items
    if overflow <= 0:
        return

    candidates = [
        (
            float(_AUDIO_CACHE_META.get(text, {}).get("last_used_at", 0.0)),
            text,
        )
        for text in _AUDIO_CACHE
        if not bool(_AUDIO_CACHE_META.get(text, {}).get("pinned", False))
    ]
    for _, text in sorted(candidates)[:overflow]:
        _drop_audio_cache_entry(text)


def _audio_cache_get(text: str) -> str | None:
    with _AUDIO_CACHE_LOCK:
        filename = _AUDIO_CACHE.get(text)
        if not filename:
            return None
        path = AUDIO_DIR / filename
        now = time.time()
        meta = _AUDIO_CACHE_META.get(text, {})
        is_pinned = bool(meta.get("pinned", False))
        created_at = float(meta.get("created_at", now))
        if not path.exists() or (not is_pinned and now - created_at > _audio_cache_ttl_seconds()):
            _drop_audio_cache_entry(text)
            return None
        meta["last_used_at"] = now
        _AUDIO_CACHE_META[text] = meta
        return filename


def _audio_cache_put(text: str, filename: str, *, pinned: bool = False) -> str:
    with _AUDIO_CACHE_LOCK:
        now = time.time()
        previous = _AUDIO_CACHE_META.get(text, {})
        old_filename = _AUDIO_CACHE.get(text)
        created_at = previous.get("created_at", now) if old_filename == filename else now
        _AUDIO_CACHE[text] = filename
        _AUDIO_CACHE_META[text] = {
            "created_at": float(created_at),
            "last_used_at": now,
            "pinned": bool(pinned or previous.get("pinned", False)),
        }
        if old_filename and old_filename != filename:
            try:
                (AUDIO_DIR / old_filename).unlink(missing_ok=True)
            except OSError as exc:
                print(f"[ElevenLabs] Errore rimozione vecchia cache audio {old_filename}: {type(exc).__name__}: {exc}")
        _prune_audio_cache(now)
        return filename


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


def _synthesize(text: str, *, pinned: bool = False, timeout_seconds: float | None = None) -> str | None:
    """Chiama ElevenLabs TTS (sync). Controlla la cache prima.
    Usata solo per il prewarm all'avvio; i route handler usano _synthesize_async."""
    cached = _audio_cache_get(text)
    if cached:
        print(f"[ElevenLabs] Cache hit: {describe_text_for_log(text)}")
        if pinned:
            _audio_cache_put(text, cached, pinned=True)
        return cached

    api_key = os.getenv("ELEVENLABS_API_KEY")
    voice_id = os.getenv("ELEVENLABS_VOICE_ID")
    if not api_key or not voice_id:
        print("[ElevenLabs] Credenziali mancanti, fallback a Polly")
        return None
    model_id = os.getenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    payload = {"text": text, "model_id": model_id, "language_code": "it"}
    timeout = timeout_seconds or _positive_float_env("ELEVENLABS_TTS_TIMEOUT_SECONDS", 15.0)
    print(f"[ElevenLabs] POST sync model={model_id} {describe_text_for_log(text)}")
    try:
        resp = httpx.post(
            url,
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        print(f"[ElevenLabs] HTTP {resp.status_code} size={len(resp.content)}")
        if resp.status_code != 200:
            print(f"[ElevenLabs] Errore risposta status={resp.status_code}")
            return None
        resp.raise_for_status()
        filename = f"{uuid.uuid4()}.mp3"
        (AUDIO_DIR / filename).write_bytes(resp.content)
        print(f"[ElevenLabs] Audio salvato: {filename} ({len(resp.content)} bytes)")
        return _audio_cache_put(text, filename, pinned=pinned)
    except httpx.TimeoutException:
        print(f"[ElevenLabs] Timeout dopo {timeout:g}s — fallback a Polly")
        return None
    except httpx.HTTPStatusError as e:
        print(f"[ElevenLabs] HTTPStatusError {e.response.status_code} — fallback a Polly")
        return None
    except Exception as e:
        print(f"[ElevenLabs] Errore inatteso {type(e).__name__}: {e} — fallback a Polly")
        return None


async def _synthesize_async(text: str, *, pinned: bool = False) -> str | None:
    """Chiama ElevenLabs TTS (async). Controlla la cache prima."""
    cached = _audio_cache_get(text)
    if cached:
        print(f"[ElevenLabs] Cache hit: {describe_text_for_log(text)}")
        if pinned:
            _audio_cache_put(text, cached, pinned=True)
        return cached

    api_key = os.getenv("ELEVENLABS_API_KEY")
    voice_id = os.getenv("ELEVENLABS_VOICE_ID")
    if not api_key or not voice_id:
        print("[ElevenLabs] Credenziali mancanti, fallback a Polly")
        return None
    model_id = os.getenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    payload = {"text": text, "model_id": model_id, "language_code": "it"}
    timeout = _positive_float_env("ELEVENLABS_TTS_TIMEOUT_SECONDS", 15.0)
    print(f"[ElevenLabs] POST async model={model_id} {describe_text_for_log(text)}")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                headers={"xi-api-key": api_key, "Content-Type": "application/json"},
                json=payload,
            )
        print(f"[ElevenLabs] HTTP {resp.status_code} size={len(resp.content)}")
        if resp.status_code != 200:
            print(f"[ElevenLabs] Errore risposta status={resp.status_code}")
            return None
        resp.raise_for_status()
        filename = f"{uuid.uuid4()}.mp3"
        (AUDIO_DIR / filename).write_bytes(resp.content)
        print(f"[ElevenLabs] Audio salvato: {filename} ({len(resp.content)} bytes)")
        return _audio_cache_put(text, filename, pinned=pinned)
    except httpx.TimeoutException:
        print(f"[ElevenLabs] Timeout dopo {timeout:g}s — fallback a Polly")
        return None
    except httpx.HTTPStatusError as e:
        print(f"[ElevenLabs] HTTPStatusError {e.response.status_code} — fallback a Polly")
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
    print(f"[TTS] Testo finale: {describe_text_for_log(text)}")

    # Cache hit: file già pronto su disco → nessuna latenza ElevenLabs
    cached = _audio_cache_get(text)
    if cached:
        print(f"[ElevenLabs] Cache hit: {describe_text_for_log(text)}")
        url = f"{_public_base_url()}/voice/audio/{cached}"
        return f"<Play>{escape(url)}</Play>"

    # ElevenLabs non configurato → fallback Polly (nessuna latenza di rete)
    if not os.getenv("ELEVENLABS_API_KEY") or not os.getenv("ELEVENLABS_VOICE_ID"):
        return f'<Say voice="{_POLLY_FALLBACK}">{escape(text)}</Say>'

    # Registra lo stream e ritorna subito: Twilio recupererà l'audio in streaming
    stream_id = str(uuid.uuid4())
    _pending_streams[stream_id] = (text, time.time())
    url = f"{_public_base_url()}/voice/stream/{stream_id}"
    print(f"[ElevenLabs] Stream registrato {stream_id}: {describe_text_for_log(text)}")
    return f"<Play>{escape(url)}</Play>"


def _prewarm_audio_cache_sync() -> None:
    """Pre-genera gli audio frequenti con un budget totale limitato."""
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        print("[ElevenLabs] Prewarm skip: ELEVENLABS_API_KEY non configurata")
        return
    deadline = time.monotonic() + _prewarm_total_budget_seconds()
    print(f"[ElevenLabs] Prewarm di {len(_CACHED_PHRASES)} frasi frequenti...")
    cached_count = 0
    for phrase in _CACHED_PHRASES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print("[ElevenLabs] Prewarm interrotto: budget tempo esaurito")
            break
        timeout = min(_prewarm_request_timeout_seconds(), max(0.5, remaining))
        filename = _synthesize(phrase, pinned=True, timeout_seconds=timeout)
        if filename:
            cached_count += 1
            print(f"[ElevenLabs] Cached: {describe_text_for_log(phrase)} → {filename}")
        else:
            print(f"[ElevenLabs] Prewarm fallito per: {describe_text_for_log(phrase)}")
    print(f"[ElevenLabs] Prewarm completato: {cached_count}/{len(_CACHED_PHRASES)} frasi in cache")


def prewarm_audio_cache(*, background: bool = True) -> None:
    """Avvia il prewarm TTS senza bloccare la readiness dell'app."""
    global _PREWARM_THREAD_STARTED
    if not background:
        _prewarm_audio_cache_sync()
        return

    if not os.getenv("ELEVENLABS_API_KEY"):
        print("[ElevenLabs] Prewarm skip: ELEVENLABS_API_KEY non configurata")
        return

    with _PREWARM_THREAD_LOCK:
        if _PREWARM_THREAD_STARTED:
            print("[ElevenLabs] Prewarm già avviato")
            return
        _PREWARM_THREAD_STARTED = True

    thread = threading.Thread(
        target=_prewarm_audio_cache_sync,
        name="voice-audio-prewarm",
        daemon=True,
    )
    thread.start()
    print("[ElevenLabs] Prewarm avviato in background")


async def _get_tts_stream_client() -> httpx.AsyncClient:
    global _tts_stream_client
    if _tts_stream_client is None or _tts_stream_client.is_closed:
        _tts_stream_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0),
            limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
        )
    return _tts_stream_client


async def close_tts_stream_client() -> None:
    global _tts_stream_client
    if _tts_stream_client is not None and not _tts_stream_client.is_closed:
        await _tts_stream_client.aclose()
    _tts_stream_client = None


@router.get("/audio/{filename}")
def serve_audio(filename: str):
    """Serve i file MP3 generati da ElevenLabs a Twilio."""
    if not filename.endswith(".mp3"):
        raise HTTPException(status_code=400, detail="Invalid filename")
    resolved = (AUDIO_DIR / filename).resolve()
    if not str(resolved).startswith(str(AUDIO_DIR.resolve())):
        raise HTTPException(status_code=403, detail="Access denied")
    if not resolved.exists():
        raise HTTPException(status_code=404, detail="Audio not found")
    return FileResponse(resolved, media_type="audio/mpeg")


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
    print(f"[ElevenLabs] Avvio stream {stream_id}: model={model_id} {describe_text_for_log(text)}")

    async def generate():
        started = time.perf_counter()
        first_chunk_seen = False
        try:
            http = await _get_tts_stream_client()
            async with http.stream(
                "POST",
                el_url,
                headers={"xi-api-key": api_key, "Content-Type": "application/json"},
                json={"text": text, "model_id": model_id, "language_code": "it"},
            ) as resp:
                if resp.status_code != 200:
                    await resp.aread()
                    record_latency(
                        "voice",
                        "tts_stream",
                        (time.perf_counter() - started) * 1000,
                        result="provider_error",
                        status_code=resp.status_code,
                    )
                    print(f"[ElevenLabs] Stream error status={resp.status_code}")
                    return
                async for chunk in resp.aiter_bytes(chunk_size=4096):
                    if not first_chunk_seen:
                        first_chunk_seen = True
                        record_latency(
                            "voice",
                            "tts_first_byte",
                            (time.perf_counter() - started) * 1000,
                            result="success",
                            status_code=resp.status_code,
                        )
                        print(f"[ElevenLabs] Stream {stream_id}: primo chunk in arrivo")
                    yield chunk
        except Exception as e:
            record_latency(
                "voice",
                "tts_stream",
                (time.perf_counter() - started) * 1000,
                result="error",
                error=type(e).__name__,
            )
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
    cached = _audio_cache_get(_FILLER_PHRASE)
    if cached:
        url = f"{_public_base_url()}/voice/audio/{cached}"
        return f"<Play>{escape(url)}</Play>"
    return f'<Say voice="{_POLLY_FALLBACK}">{escape(_FILLER_PHRASE)}</Say>'


async def _build_response_twiml(result, session_id: str) -> str:
    """Costruisce il TwiML finale dal risultato di chat()."""
    reply = result.response_message
    cache_hit = _audio_cache_get(reply) is not None
    print(
        f"[Voice] Risposta agente: {describe_text_for_log(reply)} "
        f"stato={result.state!r} cache_hit={cache_hit}"
    )
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


async def _build_retry_gather_twiml(
    session_id: str,
    message: str = "Scusi, non ho capito. Può ripetere?",
) -> str:
    audio, no_input = await asyncio.gather(
        _audio_element_async(message),
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
    _cleanup_stale_pending_responses()
    task = _pending_responses.pop(session_id, None)
    _pending_response_created_at.pop(session_id, None)
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
    if result.state == "completed":
        _has_order = result.order_id is not None
        asyncio.create_task(_call_log_update(
            session_id,
            "ordine" if _has_order else "nessun_ordine",
            order_id=result.order_id,
            summary=f"Ordine #{result.order_id} confermato" if _has_order else "Chiamata terminata senza ordine",
        ))
    else:
        asyncio.create_task(_prefetch_openai_connection())
    return Response(content=twiml, media_type="application/xml")


@router.post("/incoming")
async def voice_incoming(
    request: Request,
    From: str = Form(default=""),
    To: str = Form(default=""),
    session: Session = Depends(get_session),
):
    """Webhook Twilio Voice: crea sessione, lookup cliente, risponde con saluto + Gather speech."""
    started = time.perf_counter()
    await _verify_twilio_request(request)
    caller_phone = From.strip() or None
    print(f"[Voice] Chiamata in arrivo da: {mask_phone(caller_phone)}")

    # Risolvi il ristorante dal numero chiamato (To)
    _restaurant, restaurant_id = await asyncio.to_thread(resolve_restaurant_from_phone, To)
    print(f"[Voice] To={To!r} → restaurant_id={restaurant_id!r}")

    # Controlla agent_active prima di qualsiasi altra operazione
    if not is_agent_active(restaurant_id=restaurant_id):
        print("[Voice] agent_active=False → chiusura chiamata")
        # CallLog fuori_orario — fire-and-forget
        asyncio.create_task(_call_log_create_instant(
            restaurant_id=restaurant_id,
            caller_phone=caller_phone,
            outcome="fuori_orario",
            summary="Chiamata fuori orario di apertura",
        ))
        closed_audio = await _audio_element_async(build_closed_message(restaurant_id=restaurant_id))
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<Response>\n"
            f"  {closed_audio}\n"
            "  <Hangup/>\n"
            "</Response>"
        )
        record_latency(
            "voice",
            "incoming",
            (time.perf_counter() - started) * 1000,
            result="closed",
        )
        return Response(content=twiml, media_type="application/xml")

    # Avvia il lookup cliente subito in background: il phone è già disponibile
    # prima ancora della sessione DB, così i ~500ms di Base44 si sovrappongono.
    lookup_task = None
    if caller_phone:
        print(f"[Voice] Customer lookup per {mask_phone(caller_phone)}")
        lookup_task = asyncio.create_task(asyncio.to_thread(lookup_customer, caller_phone))

    session_id = str(uuid.uuid4())
    conversation = ConversationSession(
        session_id=session_id,
        customer_phone=caller_phone,
        items_json="[]",
        state="collecting_items",
        completed=False,
        restaurant_id=restaurant_id or None,
    )
    session.add(conversation)
    session.commit()
    print(f"[Voice] Sessione creata: {session_id}")

    # CallLog: crea con outcome=abbandonata di default (verrà aggiornato a fine chiamata)
    asyncio.create_task(_call_log_create(session_id, restaurant_id, caller_phone))

    greeting = get_agent_greeting(restaurant_id=restaurant_id)

    # Attendi il lookup (si sovrappone alle operazioni DB sopra)
    customer = await _resolve_customer_lookup_task(
        lookup_task,
        caller_phone,
        timeout_seconds=_voice_greeting_lookup_timeout_seconds(),
        cancel_on_timeout=False,
    )
    if customer:
        found_name, changed = _apply_customer_profile_to_conversation(conversation, customer)
        if found_name:
            print(f"[Voice] Cliente trovato: {mask_name(found_name)}")
            # Saluta direttamente per nome — il numero è conferma sufficiente
            first_name = found_name.split()[0]
            greeting = f"Ciao {first_name}! Come posso aiutarti?"
            if changed:
                session.add(conversation)
                session.commit()
        else:
            print("[Voice] Cliente non trovato")
    elif lookup_task and not lookup_task.done():
        _defer_customer_profile_update(session_id, lookup_task)
        print("[Voice] Lookup cliente ancora in corso: aggiorno la sessione se arriva")
    elif caller_phone:
        print("[Voice] Cliente non trovato")

    print(f"[Voice] Saluto: {describe_text_for_log(greeting)}")

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
    record_latency(
        "voice",
        "incoming",
        (time.perf_counter() - started) * 1000,
        result="gather",
        customer_profile=bool(customer),
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
    started = time.perf_counter()
    await _verify_twilio_request(request)
    # Import locale per evitare import circolare (chat importa da conversation_service)
    from app.routes.chat import chat  # noqa: PLC0415

    speech = SpeechResult.strip()
    print(f"[Voice] Gather session={session_id!r} speech={describe_text_for_log(speech)}")

    from sqlmodel import select as _select
    _conv = session.exec(_select(ConversationSession).where(ConversationSession.session_id == session_id)).first()
    if _conv is None:
        print(f"[Voice] voice_gather: sessione {session_id!r} non trovata, hangup")
        record_latency(
            "voice",
            "gather",
            (time.perf_counter() - started) * 1000,
            result="session_missing",
        )
        return Response(
            content=(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                "<Response>\n"
                '  <Say voice="Polly.Giorgio">Mi dispiace, sessione non trovata. Arrivederci.</Say>\n'
                "  <Hangup/>\n"
                "</Response>"
            ),
            media_type="application/xml",
        )
    _phone = _conv.customer_phone
    _masked_phone = mask_phone(_phone)
    _state = _conv.state
    print(f"[Voice] Sessione {session_id}: customer_phone={_masked_phone} stato={_state!r}")

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
            asyncio.create_task(_call_log_update(
                session_id, "nessun_ordine",
                summary="Chiamata terminata: nessun input ricevuto",
            ))
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
        async def _chat_task():
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(_run_chat_with_fresh_session, chat, chat_request),
                    timeout=25.0,
                )
                print(
                    f"[Voice] _chat_bg completato: session={session_id!r} stato={result.state!r} "
                    f"order_id={result.order_id!r} phone={_masked_phone}"
                )
                if result.state == "completed":
                    print(f"[SMS] Tentativo invio dopo filler: session={session_id!r} phone={_masked_phone}")
                return result
            except asyncio.TimeoutError:
                print(f"[Voice] _chat_bg TIMEOUT session={session_id!r}")
                raise
            except Exception as exc:
                print(f"[Voice] _chat_bg ERRORE session={session_id!r}: {type(exc).__name__}: {exc}")
                raise

        _cleanup_stale_pending_responses()
        previous_task = _pending_responses.get(session_id)
        if previous_task and not previous_task.done():
            previous_task.cancel()
            print(f"[Voice] Filler path: task precedente cancellato per session={session_id!r}")

        task = asyncio.create_task(_chat_task())
        _pending_responses[session_id] = task
        _pending_response_created_at[session_id] = time.time()
        task.add_done_callback(
            lambda done_task, sid=session_id: asyncio.create_task(
                _drop_pending_response_after_grace(sid, done_task)
            )
        )

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
            f"[Voice] Filler path: task avviato per session={session_id!r} phone={_masked_phone} "
            f"filler={'ON' if _cooldown_ok else f'SKIP (cooldown {int(_FILLER_COOLDOWN - (_now - _last))}s)'}"
        )
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<Response>\n"
            f"  {_filler_el}\n"
            f'  <Redirect method="POST">/voice/process?session_id={session_id}</Redirect>\n'
            "</Response>"
        )
        record_latency(
            "voice",
            "gather",
            (time.perf_counter() - started) * 1000,
            result="filler_redirect",
            state=_state,
        )
        return Response(content=twiml, media_type="application/xml")

    _timeout_twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        '  <Say voice="Polly.Giorgio">Mi dispiace, riprovare.</Say>\n'
        "  <Hangup/>\n"
        "</Response>"
    )
    try:
        if _state in _SPECULATIVE_STATES:
            # Parallelismo speculativo: OpenAI + pre-generazione "Ok!" in parallelo.
            print(f"[Voice] Parallel speculative: OpenAI + ElevenLabs('Ok!') per stato={_state!r}")
            result, _ = await asyncio.wait_for(
                asyncio.gather(
                    asyncio.to_thread(_run_chat_with_fresh_session, chat, chat_request),
                    _synthesize_async("Ok!"),
                ),
                timeout=25.0,
            )
        else:
            result = await asyncio.wait_for(
                asyncio.to_thread(_run_chat_with_fresh_session, chat, chat_request),
                timeout=25.0,
            )
    except asyncio.TimeoutError:
        print(f"[Voice] voice_gather TIMEOUT session={session_id!r}")
        record_latency(
            "voice",
            "gather",
            (time.perf_counter() - started) * 1000,
            result="timeout",
            state=_state,
        )
        return Response(content=_timeout_twiml, media_type="application/xml")
    except Exception as exc:
        print(f"[Voice] voice_gather ERRORE session={session_id!r}: {type(exc).__name__}: {exc}")
        record_latency(
            "voice",
            "gather",
            (time.perf_counter() - started) * 1000,
            result="error",
            state=_state,
            error=type(exc).__name__,
        )
        twiml = await _build_retry_gather_twiml(session_id)
        return Response(content=twiml, media_type="application/xml")

    twiml = await _build_response_twiml(result, session_id)
    if result.state == "completed":
        _has_order = result.order_id is not None
        asyncio.create_task(_call_log_update(
            session_id,
            "ordine" if _has_order else "nessun_ordine",
            order_id=result.order_id,
            summary=f"Ordine #{result.order_id} confermato" if _has_order else "Chiamata terminata senza ordine",
        ))
    else:
        asyncio.create_task(_prefetch_openai_connection())
    record_latency(
        "voice",
        "gather",
        (time.perf_counter() - started) * 1000,
        result="response",
        state=result.state,
    )
    return Response(content=twiml, media_type="application/xml")
