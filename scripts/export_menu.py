"""
Scarica menu e configurazione ristorante da Base44 e li salva in:
  - app/menu_data.json
  - app/restaurant_data.json

Uso:
    python scripts/export_menu.py

Richiede BASE44_TOKEN nel file .env (o come variabile d'ambiente).
Nota: Base44 risponde sempre con {"entities": [...], "count": N}.
"""

import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE44_APP = "https://app.base44.com/api/apps/69c54bc5c44250d7da397903/entities"
APP_DIR = Path(__file__).parent.parent / "app"
MENU_OUTPUT = APP_DIR / "menu_data.json"
RESTAURANT_OUTPUT = APP_DIR / "restaurant_data.json"


def fetch_entities(token: str, entity: str) -> list[dict]:
    """Scarica tutti i record di un'entità Base44."""
    url = f"{BASE44_APP}/{entity}"
    try:
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        print(f"  {entity}: HTTP {response.status_code}")
        response.raise_for_status()
        body = response.json()
        if isinstance(body, dict):
            return body.get("entities", [])
        if isinstance(body, list):
            return body
        return []
    except httpx.HTTPStatusError as e:
        print(f"  Errore HTTP {e.response.status_code}: {e.response.text[:200]}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"  Errore: {e}", file=sys.stderr)
        sys.exit(1)


def export_menu(token: str) -> None:
    print("Scarico MenuItem...")
    all_items = fetch_entities(token, "MenuItem")
    print(f"  Voci ricevute: {len(all_items)}")

    menu = [
        {
            "name": item["name"],
            "category": item.get("category", ""),
            "dough_type": item.get("dough_type", "classica"),
            "price": item.get("price", 0.0),
            "available": item.get("available", True),
            "ingredients": item.get("ingredients", []),
        }
        for item in all_items
        if item.get("available", False)
    ]
    menu.sort(key=lambda x: (x["dough_type"], x["name"]))

    MENU_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(MENU_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(menu, f, ensure_ascii=False, indent=2)
    print(f"  Salvate {len(menu)} voci in {MENU_OUTPUT}")


def export_restaurant(token: str) -> None:
    print("Scarico Restaurant...")
    records = fetch_entities(token, "Restaurant")
    if not records:
        print("  Nessun record Restaurant trovato", file=sys.stderr)
        return

    restaurant = records[0]
    # Salva tutti i campi disponibili
    data = {
        "agent_greeting": restaurant.get("agent_greeting", ""),
        "opening_hours": restaurant.get("opening_hours", ""),
        "agent_tone": restaurant.get("agent_tone", ""),
        "name": restaurant.get("name", ""),
        "phone": restaurant.get("phone", ""),
        "address": restaurant.get("address", ""),
    }
    # Includi eventuali altri campi presenti
    for key, value in restaurant.items():
        if key not in data:
            data[key] = value

    RESTAURANT_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(RESTAURANT_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  Salvato in {RESTAURANT_OUTPUT}")
    print(f"  Campi: {list(data.keys())}")
    print(f"  agent_greeting: {data.get('agent_greeting')!r}")


def main() -> None:
    token = os.getenv("BASE44_TOKEN")
    if not token:
        print("Errore: BASE44_TOKEN non configurato nel .env", file=sys.stderr)
        sys.exit(1)

    export_menu(token)
    export_restaurant(token)
    print("Export completato.")


if __name__ == "__main__":
    main()
