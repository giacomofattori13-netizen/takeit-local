"""
Scarica gli impasti da Base44 e li salva in app/dough_data.json.

Uso:
    python scripts/export_doughs.py

Richiede BASE44_TOKEN nel file .env (o come variabile d'ambiente).
"""

import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE44_DOUGH_URL = (
    "https://app.base44.com/api/apps/69c54bc5c44250d7da397903/entities/DoughType"
)
OUTPUT_PATH = Path(__file__).parent.parent / "app" / "dough_data.json"

EXCLUDED_CODES = {"senza_glutine"}


def main() -> None:
    token = os.getenv("BASE44_TOKEN")
    if not token:
        print("Errore: BASE44_TOKEN non configurato nel .env", file=sys.stderr)
        sys.exit(1)

    print("Scarico impasti da Base44...")
    try:
        response = httpx.get(
            BASE44_DOUGH_URL,
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

    body = response.json()
    raw = body.get("entities", []) if isinstance(body, dict) else body
    if not isinstance(raw, list):
        print(f"Formato risposta inatteso: {type(raw).__name__}", file=sys.stderr)
        sys.exit(1)
    print(f"Impasti ricevuti: {len(raw)}")

    # Deduplica per code, escludi senza_glutine
    seen: set[str] = set()
    doughs = []
    for item in raw:
        code = item.get("code", "")
        if code in EXCLUDED_CODES or code in seen:
            continue
        seen.add(code)
        doughs.append({
            "name": item["name"],
            "code": code,
            "surcharge": float(item.get("surcharge", 0.0)),
            "available": item.get("available", True),
        })

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(doughs, f, ensure_ascii=False, indent=2)

    print(f"Salvati {len(doughs)} impasti in {OUTPUT_PATH}:")
    for d in doughs:
        print(f"  {d['code']:20} surcharge=€{d['surcharge']:.2f}  available={d['available']}")


if __name__ == "__main__":
    main()
