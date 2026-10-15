"""Latenza per turno delle chiamate vocali, accumulata in memoria e salvata a fine chiamata.

Ogni chiamata ha una traccia (chiave: CallSid Twilio) con l'istante di inizio
(time.monotonic() all'arrivo di /voice/incoming). Ogni turno del cliente registra
timestamp in ms relativi a quell'istante. Durante la chiamata si scrive solo in
memoria; /voice/status, a chiamata finita, salva i turni sul CallLog Base44
(campo `latency_turns`) in un task in background.

Il turno corrente viaggia in una ContextVar: asyncio.create_task e asyncio.to_thread
copiano il contesto, quindi chat() e i servizi chiamati dal suo thread registrano
LLM e chiamate esterne senza cambiare firma.
"""
import asyncio
import contextvars
import functools
import threading
import time
from typing import Any, Callable

TURN_MARKS = (
    "user_speech_end",
    "stt_final",
    "llm_request_sent",
    "llm_first_token",
    "llm_complete",
    "tts_request_sent",
    "tts_first_byte",
    "first_audio_sent_to_twilio",
)

# Tracce più vecchie di così (status callback mai arrivato) vengono scartate
_TRACE_TTL_SECONDS = 2 * 60 * 60
_MAX_STREAM_LINKS = 1000

_lock = threading.Lock()
_calls: dict[str, dict[str, Any]] = {}       # call_sid → traccia
_session_calls: dict[str, str] = {}          # session_id → call_sid
_stream_turns: dict[str, dict[str, Any]] = {}  # stream_id → turno

_current_turn: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "voice_current_turn", default=None
)


def _now() -> float:
    return time.monotonic()


def _prune(now: float) -> None:
    for call_sid, trace in list(_calls.items()):
        if now - trace["t0"] > _TRACE_TTL_SECONDS:
            _calls.pop(call_sid, None)
            _session_calls.pop(trace["session_id"], None)
    if len(_stream_turns) > _MAX_STREAM_LINKS:
        for stream_id in list(_stream_turns)[: len(_stream_turns) - _MAX_STREAM_LINKS]:
            _stream_turns.pop(stream_id, None)


def start_call(call_sid: str, session_id: str, started_at: float | None = None) -> None:
    """Apre la traccia della chiamata. `started_at` è un valore di time.monotonic()."""
    if not call_sid:
        return
    t0 = _now() if started_at is None else started_at
    with _lock:
        _prune(t0)
        _calls[call_sid] = {"t0": t0, "session_id": session_id, "partial": False, "turns": []}
        _session_calls[session_id] = call_sid


def start_turn(
    call_sid: str,
    session_id: str,
    *,
    state: str,
    stt_final_at: float | None = None,
) -> dict[str, Any] | None:
    """Apre un turno del cliente e lo rende il turno corrente del contesto."""
    if not call_sid:
        return None
    now = _now()
    with _lock:
        trace = _calls.get(call_sid)
        if trace is None:
            # Processo riavviato a chiamata in corso: tempi relativi al primo turno visto
            trace = {"t0": stt_final_at or now, "session_id": session_id, "partial": True, "turns": []}
            _calls[call_sid] = trace
            _session_calls[session_id] = call_sid
        turn: dict[str, Any] = {
            "turn": len(trace["turns"]) + 1,
            "state": state,
            "path": "direct",
            "audio_source": None,
            "tools": [],
            "_t0": trace["t0"],
        }
        for mark_name in TURN_MARKS:
            turn[mark_name] = None
        trace["turns"].append(turn)
    _set_mark(turn, "stt_final", stt_final_at if stt_final_at is not None else now)
    _current_turn.set(turn)
    return turn


def resume_turn(session_id: str) -> dict[str, Any] | None:
    """Rende corrente l'ultimo turno della sessione (es. /voice/process dopo il filler)."""
    with _lock:
        trace = _calls.get(_session_calls.get(session_id, ""))
        turn = trace["turns"][-1] if trace and trace["turns"] else None
    _current_turn.set(turn)
    return turn


def current_turn() -> dict[str, Any] | None:
    return _current_turn.get()


def _set_mark(turn: dict[str, Any], name: str, at: float, *, overwrite: bool = False) -> None:
    if overwrite or turn.get(name) is None:
        turn[name] = int(round((at - turn["_t0"]) * 1000))


def mark(name: str, *, turn: dict[str, Any] | None = None, overwrite: bool = False) -> None:
    """Registra `name` sul turno (default: quello corrente). Il primo valore vince."""
    turn = turn if turn is not None else _current_turn.get()
    if turn is not None:
        _set_mark(turn, name, _now(), overwrite=overwrite)


def annotate(**fields: Any) -> None:
    turn = _current_turn.get()
    if turn is not None:
        turn.update(fields)


def link_stream(stream_id: str) -> None:
    """Collega uno stream TTS registrato al turno corrente."""
    turn = _current_turn.get()
    if turn is not None:
        with _lock:
            _stream_turns[stream_id] = turn


def stream_turn(stream_id: str) -> dict[str, Any] | None:
    with _lock:
        return _stream_turns.pop(stream_id, None)


def traced_tool(name: str) -> Callable:
    """Decoratore: registra nome, inizio e fine della funzione nel turno corrente."""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            turn = _current_turn.get()
            if turn is None:
                return func(*args, **kwargs)
            start = _now()
            try:
                return func(*args, **kwargs)
            finally:
                end = _now()
                turn["tools"].append({
                    "name": name,
                    "start": int(round((start - turn["_t0"]) * 1000)),
                    "end": int(round((end - turn["_t0"]) * 1000)),
                })

        return wrapper

    return decorator


def _diff(turn: dict[str, Any], end: str, start: str) -> int | None:
    if turn.get(end) is None or turn.get(start) is None:
        return None
    return turn[end] - turn[start]


def summarize_turn(turn: dict[str, Any]) -> dict[str, Any]:
    """Turno pronto da salvare: timestamp, durate per stadio e latenza percepita."""
    out = {key: value for key, value in turn.items() if not key.startswith("_")}
    out["tools"] = [dict(tool, duration=tool["end"] - tool["start"]) for tool in turn.get("tools", [])]
    # user_speech_end non è osservabile con <Gather>: la latenza percepita parte da
    # stt_final quando manca (vedi docs/pipeline-audit.md)
    speech_end = "user_speech_end" if turn.get("user_speech_end") is not None else "stt_final"
    out["durations"] = {
        "stt": _diff(turn, "stt_final", "user_speech_end"),
        "pre_llm": _diff(turn, "llm_request_sent", "stt_final"),
        "llm_ttft": _diff(turn, "llm_first_token", "llm_request_sent"),
        "llm_total": _diff(turn, "llm_complete", "llm_request_sent"),
        "llm_to_tts": _diff(turn, "tts_request_sent", "llm_complete"),
        "tts_ttfb": _diff(turn, "tts_first_byte", "tts_request_sent"),
        "tts_to_twilio": _diff(turn, "first_audio_sent_to_twilio", "tts_first_byte"),
        "tts_stream_total": _diff(turn, "tts_complete", "tts_request_sent"),
        "tools_total": sum(tool["end"] - tool["start"] for tool in turn.get("tools", [])) or None,
    }
    out["perceived_from"] = speech_end
    out["perceived_latency"] = _diff(turn, "first_audio_sent_to_twilio", speech_end)
    return out


def pop_call(call_sid: str) -> dict[str, Any] | None:
    """Toglie la traccia dalla memoria e la restituisce pronta da salvare."""
    with _lock:
        trace = _calls.pop(call_sid, None)
        if trace is None:
            return None
        _session_calls.pop(trace["session_id"], None)
        turn_ids = {id(turn) for turn in trace["turns"]}
        for stream_id, turn in list(_stream_turns.items()):
            if id(turn) in turn_ids:
                _stream_turns.pop(stream_id, None)
    return {
        "partial": trace["partial"],
        "turns": [summarize_turn(turn) for turn in trace["turns"]],
    }


async def flush_call(call_sid: str) -> None:
    """A chiamata finita: salva i turni sul CallLog (campo latency_turns)."""
    trace = pop_call(call_sid)
    if not trace or not trace["turns"]:
        return
    from app.services.base44_client import find_call_log_by_sid, update_call_log

    try:
        record = await asyncio.to_thread(find_call_log_by_sid, call_sid)
        if not record or not record.get("id"):
            print(f"[Latency] Nessun CallLog per call_sid={call_sid!r}: {len(trace['turns'])} turni persi")
            return
        await asyncio.to_thread(update_call_log, record["id"], {"latency_turns": trace["turns"]})
        print(f"[Latency] Salvati {len(trace['turns'])} turni call_sid={call_sid!r}")
    except Exception as exc:
        print(f"[Latency] Errore salvataggio call_sid={call_sid!r}: {type(exc).__name__}: {exc}")


def clear() -> None:
    with _lock:
        _calls.clear()
        _session_calls.clear()
        _stream_turns.clear()
