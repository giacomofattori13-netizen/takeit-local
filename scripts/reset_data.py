"""
Elimina tutti i record di Order, Customer, PrintJob e OwnerCommand da Base44.

Uso:
    python scripts/reset_data.py

Richiede BASE44_TOKEN nel file .env (o come variabile d'ambiente).
"""

import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

BASE44_APP = "https://app.base44.com/api/apps/69c54bc5c44250d7da397903/entities"
ENTITIES = ["Order", "Customer", "PrintJob", "OwnerCommand"]


def fetch_all(token: str, entity: str) -> list[dict]:
    response = httpx.get(
        f"{BASE44_APP}/{entity}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    response.raise_for_status()
    body = response.json()
    if isinstance(body, dict):
        return body.get("entities", [])
    if isinstance(body, list):
        return body
    return []


def delete_record(token: str, entity: str, record_id: str) -> bool:
    response = httpx.delete(
        f"{BASE44_APP}/{entity}/{record_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    return response.status_code in (200, 204)


def main() -> None:
    token = os.getenv("BASE44_TOKEN")
    if not token:
        print("Errore: BASE44_TOKEN non configurato", file=sys.stderr)
        sys.exit(1)

    totals = {}
    for entity in ENTITIES:
        print(f"\n[{entity}] Recupero record...")
        try:
            records = fetch_all(token, entity)
        except httpx.HTTPStatusError as e:
            print(f"  HTTP {e.response.status_code}: {e.response.text[:200]}")
            totals[entity] = 0
            continue
        except Exception as e:
            print(f"  Errore fetch: {e}")
            totals[entity] = 0
            continue

        print(f"  Trovati: {len(records)}")
        deleted = 0
        for record in records:
            rid = record.get("id") or record.get("_id")
            if not rid:
                print(f"  SKIP: record senza id — {record}")
                continue
            ok = delete_record(token, entity, rid)
            if ok:
                deleted += 1
                print(f"  Eliminato {entity}/{rid}")
            else:
                print(f"  ERRORE eliminazione {entity}/{rid}")

        totals[entity] = deleted

    print("\n=== RIEPILOGO ===")
    for entity, count in totals.items():
        print(f"  {entity}: {count} eliminati")


if __name__ == "__main__":
    main()
