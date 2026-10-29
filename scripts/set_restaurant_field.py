"""
Aggiorna UN solo campo di un Restaurant su Base44, senza toccare gli altri.

Uso:
    python scripts/set_restaurant_field.py <restaurant_id> <campo> <valore> [--string]

Il valore si legge come JSON quando è possibile (true, false, null, 3, 2.5,
["a","b"]), altrimenti come testo ("11:00", "2026-10-29"). Con --string è sempre testo.
Mostra il valore prima e dopo. Richiede BASE44_TOKEN nel file .env (o come variabile d'ambiente).

Esempi:
    python scripts/set_restaurant_field.py 6a22d781b615baedb412be35 daily_summary_enabled true
    python scripts/set_restaurant_field.py 6a22d781b615baedb412be35 daily_summary_time 10:30
"""

import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

BASE44_APP = "https://app.base44.com/api/apps/69c54bc5c44250d7da397903/entities"


def parse_value(raw: str, force_string: bool = False):
    if force_string:
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def get_restaurant(token: str, restaurant_id: str) -> dict:
    response = httpx.get(
        f"{BASE44_APP}/Restaurant/{restaurant_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def main(argv: list[str]) -> int:
    force_string = "--string" in argv
    args = [a for a in argv if a != "--string"]
    if len(args) != 3:
        print(__doc__)
        return 2
    restaurant_id, field, raw_value = args
    value = parse_value(raw_value, force_string)

    token = os.getenv("BASE44_TOKEN", "").strip()
    if not token:
        print("BASE44_TOKEN mancante")
        return 1

    before = get_restaurant(token, restaurant_id)
    print(f"Restaurant: {before.get('name')!r} (id={restaurant_id})")
    if field not in before:
        print(f"Attenzione: il campo {field!r} non è nella risposta di Base44 (non impostato o nome sbagliato)")
    print(f"Prima: {field} = {json.dumps(before.get(field), ensure_ascii=False)}")

    response = httpx.put(
        f"{BASE44_APP}/Restaurant/{restaurant_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={field: value},
        timeout=15,
    )
    response.raise_for_status()

    after = get_restaurant(token, restaurant_id)
    print(f"Dopo:  {field} = {json.dumps(after.get(field), ensure_ascii=False)}")
    changed = sorted(k for k in set(before) | set(after) if k != field and k != "updated_date" and before.get(k) != after.get(k))
    if changed:
        print(f"Attenzione: sono cambiati anche altri campi: {changed}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
