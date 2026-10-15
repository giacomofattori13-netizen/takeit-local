"""
Report di latenza delle ultime N chiamate vocali: mediana e p95 per stadio.

Legge il campo `latency_turns` dei CallLog su Base44 (scritto a fine chiamata da
app/turn_latency.py). Tempi in millisecondi.

Uso:
    python scripts/latency_report.py            # ultime 20 chiamate con dati
    python scripts/latency_report.py -n 50
    python scripts/latency_report.py --restaurant <restaurant_id>
    python scripts/latency_report.py --file calllogs.json   # export JSON di CallLog

Richiede BASE44_TOKEN nel file .env (o come variabile d'ambiente), tranne con --file.
"""

import argparse
import json
import math
import os
import statistics
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

BASE44_APP = "https://app.base44.com/api/apps/69c54bc5c44250d7da397903/entities"

# (etichetta, funzione turno → valore in ms o None)
STAGES = [
    ("STT (fine parlato → trascrizione)", lambda t: t["durations"].get("stt")),
    ("Pre-LLM (trascrizione → richiesta LLM)", lambda t: t["durations"].get("pre_llm")),
    ("LLM primo token", lambda t: t["durations"].get("llm_ttft")),
    ("LLM totale", lambda t: t["durations"].get("llm_total")),
    ("LLM fine → richiesta TTS", lambda t: t["durations"].get("llm_to_tts")),
    ("TTS primo byte", lambda t: t["durations"].get("tts_ttfb")),
    ("TTS primo byte → Twilio", lambda t: t["durations"].get("tts_to_twilio")),
    ("TTS stream completo (richiesta → ultimo byte)", lambda t: t["durations"].get("tts_stream_total")),
    ("Tool call (somma per turno)", lambda t: t["durations"].get("tools_total")),
    ("LATENZA PERCEPITA", lambda t: t.get("perceived_latency")),
]


def fetch_call_logs(token: str, limit: int, restaurant_id: str | None) -> list[dict]:
    params: dict = {"sort": "-created_date", "limit": str(limit)}
    if restaurant_id:
        params["q"] = json.dumps({"restaurant_id": restaurant_id})
    response = httpx.get(
        f"{BASE44_APP}/CallLog",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=20,
    )
    response.raise_for_status()
    body = response.json()
    records = body.get("entities", []) if isinstance(body, dict) else body
    return [r for r in records if isinstance(r, dict)]


def load_turns(records: list[dict], calls: int) -> tuple[list[dict], int]:
    """Turni delle ultime `calls` chiamate che hanno dati di latenza."""
    with_data = [r for r in records if r.get("latency_turns")]
    with_data.sort(key=lambda r: r.get("started_at") or r.get("created_date") or "", reverse=True)
    selected = with_data[:calls]
    turns: list[dict] = []
    for record in selected:
        raw = record["latency_turns"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        turns.extend(t for t in raw if isinstance(t, dict))
    return turns, len(selected)


def p95(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def print_report(turns: list[dict], calls: int) -> None:
    print(f"Chiamate: {calls}   Turni: {len(turns)}\n")
    print(f"{'Stadio':<48}{'n':>5}{'mediana':>10}{'p95':>10}")
    print("-" * 73)
    for label, getter in STAGES:
        values = [v for v in (getter(t) for t in turns) if v is not None]
        if not values:
            print(f"{label:<48}{0:>5}{'—':>10}{'—':>10}")
            continue
        print(f"{label:<48}{len(values):>5}{statistics.median(values):>10.0f}{p95(values):>10}")

    tools: dict[str, list[int]] = {}
    for turn in turns:
        for tool in turn.get("tools", []):
            tools.setdefault(tool["name"], []).append(tool["end"] - tool["start"])
    if tools:
        print(f"\n{'Tool call':<48}{'n':>5}{'mediana':>10}{'p95':>10}")
        print("-" * 73)
        for name, values in sorted(tools.items()):
            print(f"{name:<48}{len(values):>5}{statistics.median(values):>10.0f}{p95(values):>10}")

    sources: dict[str, int] = {}
    for turn in turns:
        sources[turn.get("audio_source") or "nessuno"] = sources.get(turn.get("audio_source") or "nessuno", 0) + 1
    fillers = sum(1 for t in turns if t.get("path") == "filler")
    print(f"\nAudio risposta: {sources}   Turni con filler: {fillers}")
    if any(t.get("perceived_from") == "stt_final" for t in turns):
        print(
            "Nota: user_speech_end non è misurabile con <Gather>; la latenza percepita parte "
            "dall'arrivo della trascrizione al server ed esclude l'endpointing di Twilio."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-n", "--calls", type=int, default=20, help="numero di chiamate (default 20)")
    parser.add_argument("--restaurant", help="filtra per restaurant_id")
    parser.add_argument("--file", help="legge i CallLog da un file JSON invece che da Base44")
    args = parser.parse_args()

    if args.file:
        records = json.loads(Path(args.file).read_text())
        if isinstance(records, dict):
            records = records.get("entities", [])
        if args.restaurant:
            records = [r for r in records if r.get("restaurant_id") == args.restaurant]
    else:
        token = os.getenv("BASE44_TOKEN", "").strip()
        if not token:
            print("BASE44_TOKEN mancante", file=sys.stderr)
            return 1
        # Le chiamate senza dati (fuori orario, precedenti alla strumentazione) vengono scartate
        records = fetch_call_logs(token, max(args.calls * 5, 100), args.restaurant)

    turns, calls = load_turns(records, args.calls)
    if not turns:
        print("Nessuna chiamata con latency_turns.")
        return 0
    print_report(turns, calls)
    return 0


if __name__ == "__main__":
    sys.exit(main())
