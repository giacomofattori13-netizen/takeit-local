"""
Script una tantum: corregge i CallLog degli ultimi giorni che hanno una prenotazione o un
ordine con lo stesso session_id ma un outcome diverso (es. "nessun_ordine" scritto dal
silenzio dopo la conferma).

Uso:
    python scripts/recalc_call_log_outcomes.py            # mostra l'elenco, non scrive niente
    python scripts/recalc_call_log_outcomes.py --apply    # mostra l'elenco e scrive
    python scripts/recalc_call_log_outcomes.py --days 7   # finestra diversa (default 30)

Scrive solo outcome, summary e, per gli ordini, order_id. I CallLog senza session_id non si
possono collegare e restano com'erano. Richiede BASE44_TOKEN nel file .env.
"""

import argparse
import datetime
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from app.privacy import mask_phone  # noqa: E402
from app.services import base44_client  # noqa: E402
from app.services.call_log_outcome import order_summary, reservation_summary  # noqa: E402

PAGE_SIZE = 200


def fetch_all_call_logs() -> list[dict]:
    """Tutti i CallLog, dal più recente. Si ferma se una pagina non porta record nuovi."""
    logs: dict[str, dict] = {}
    skip = 0
    while True:
        response = httpx.get(
            f"{base44_client._BASE}/CallLog",
            headers=base44_client.auth_headers(),
            params={"sort": "-created_date", "limit": PAGE_SIZE, "skip": skip},
            timeout=30,
        )
        response.raise_for_status()
        page = [e for e in base44_client._parse_entities(response.json()) if isinstance(e, dict)]
        new = [e for e in page if e.get("id") not in logs]
        logs.update((e.get("id"), e) for e in new)
        if len(page) < PAGE_SIZE or not new:
            return list(logs.values())
        skip += PAGE_SIZE


def _parse_time(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)


def in_window(log: dict, since: datetime.datetime) -> bool:
    started = _parse_time(log.get("started_at")) or _parse_time(log.get("created_date"))
    return started is not None and started >= since


def expected_patch(orders: list[dict], reservations: list[dict]) -> dict | None:
    """Campi da scrivere secondo ciò che la sessione ha salvato; None se non ha salvato niente."""
    if orders:
        order = orders[0]
        return {
            "outcome": "ordine",
            "summary": order_summary(order.get("order_number")),
            "order_id": order.get("id"),
        }
    if reservations:
        res = reservations[0]
        return {
            "outcome": "prenotazione",
            "summary": reservation_summary(
                res.get("party_size"),
                res.get("date"),
                res.get("time"),
                pending=res.get("status") == "da_confermare",
                reference=res.get("id"),
            ),
        }
    return None


def build_plan(logs: list[dict], since: datetime.datetime) -> tuple[list[tuple[dict, dict]], int]:
    """(CallLog da correggere con la patch, CallLog nella finestra senza session_id)."""
    plan = []
    no_session = 0
    for log in logs:
        if not in_window(log, since):
            continue
        session_id = log.get("session_id")
        if not session_id:
            no_session += 1
            continue
        orders = base44_client.query_entities("Order", {"session_id": session_id}, timeout=20)
        reservations = base44_client.query_entities("Reservation", {"session_id": session_id}, timeout=20)
        patch = expected_patch(orders, reservations)
        if patch and log.get("outcome") != patch["outcome"]:
            plan.append((log, patch))
    return plan, no_session


def print_plan(plan: list[tuple[dict, dict]]) -> None:
    for log, patch in plan:
        print(
            f"- CallLog {log.get('id')}  {log.get('started_at')}  {mask_phone(log.get('caller_phone'))}\n"
            f"    session: {log.get('session_id')}\n"
            f"    outcome: {log.get('outcome')!r} -> {patch['outcome']!r}\n"
            f"    summary: {log.get('summary')!r} -> {patch['summary']!r}"
            + (f"\n    order_id: {log.get('order_id')!r} -> {patch['order_id']!r}" if "order_id" in patch else "")
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Ricalcola outcome/summary dei CallLog esistenti.")
    parser.add_argument("--days", type=int, default=30, help="quanti giorni guardare (default 30)")
    parser.add_argument("--apply", action="store_true", help="scrive le correzioni; senza, mostra solo l'elenco")
    args = parser.parse_args(argv)

    if not base44_client.base44_token():
        print("BASE44_TOKEN mancante")
        return 1

    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=args.days)
    plan, no_session = build_plan(fetch_all_call_logs(), since)

    print(f"CallLog da correggere negli ultimi {args.days} giorni: {len(plan)}")
    if no_session:
        print(f"CallLog senza session_id, non collegabili e non toccati: {no_session}")
    print_plan(plan)

    if not args.apply:
        if plan:
            print("\nNessuna scrittura. Per applicare: python scripts/recalc_call_log_outcomes.py --apply")
        return 0

    failures = 0
    for log, patch in plan:
        try:
            base44_client.update_entity("CallLog", log["id"], patch)
            print(f"Scritto CallLog {log['id']}")
        except httpx.HTTPError as exc:
            failures += 1
            print(f"ERRORE CallLog {log['id']}: {type(exc).__name__}")
    print(f"Scritti {len(plan) - failures} di {len(plan)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
