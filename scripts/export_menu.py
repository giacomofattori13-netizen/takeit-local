"""
Scarica il menu da Base44 e lo salva in app/menu_data.json.

Uso:
    python scripts/export_menu.py

Richiede BASE44_TOKEN nel file .env (o come variabile d'ambiente).
Nota: l'endpoint pubblico è app.base44.com (api.base44.com restituisce 404).
"""

import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE44_MENU_URL = (
    "https://app.base44.com/api/apps/69c54bc5c44250d7da397903/entities/MenuItem"
)
OUTPUT_PATH = Path(__file__).parent.parent / "app" / "menu_data.json"


def main() -> None:
    token = os.getenv("BASE44_TOKEN")
    if not token:
        print("Errore: BASE44_TOKEN non configurato nel .env", file=sys.stderr)
        sys.exit(1)

    print(f"Scarico menu da Base44...")
    try:
        response = httpx.get(
            BASE44_MENU_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        print(f"Status: {response.status_code}")
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        print(f"Errore HTTP {e.response.status_code}: {e.response.text[:200]}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Errore: {e}", file=sys.stderr)
        sys.exit(1)

    all_items = response.json()
    print(f"Voci ricevute: {len(all_items)}")

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

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(menu, f, ensure_ascii=False, indent=2)

    print(f"Salvate {len(menu)} voci disponibili in {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
