import json
import os

import httpx
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

BASE44_ORDER_URL = "https://app.base44.com/api/apps/69c54bc5c44250d7da397903/entities/Order"


def save_order_to_base44(
    customer_name: str,
    customer_phone: str | None,
    pickup_time: str,
    total_amount: float,
    items: list[dict],
) -> None:
    api_key = os.getenv("BASE44_API_KEY")
    if not api_key:
        print("WARNING: BASE44_API_KEY not set, skipping Base44 sync")
        return

    payload = {
        "customer_name": customer_name,
        "customer_phone": customer_phone,
        "status": "nuovo",
        "source": "telefono",
        "pickup_time": pickup_time,
        "total_amount": total_amount,
        "items": items,
    }

    print(f"[Base44] Payload inviato: {json.dumps(payload, ensure_ascii=False, indent=2)}")

    try:
        response = httpx.post(
            BASE44_ORDER_URL,
            params={"api_key": api_key},
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        print(f"[Base44] Status code: {response.status_code}")
        print(f"[Base44] Response body: {response.text}")
        response.raise_for_status()
        print(f"[Base44] Ordine sincronizzato, id={response.json().get('id')}")
    except httpx.HTTPStatusError as e:
        print(f"[Base44] HTTP error {e.response.status_code}: {e.response.text}")
    except Exception as e:
        print(f"[Base44] Errore generico: {type(e).__name__}: {e}")

print("DEBUG conversation_service loaded")

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
MODEL_NAME = os.getenv("OPENAI_MODEL", "gpt-4o-mini")


def build_system_prompt(menu_text: str) -> str:
    return f"""
You extract takeaway pizza orders from Italian customer messages.

You MUST use this menu as the source of truth.

MENU:
{menu_text}

Return ONLY valid JSON with this exact structure:
{{
  "intent": string,
  "customer_name": string | null,
  "pickup_time": string | null,
  "items": [
    {{
      "pizza_name": string,
      "pizza_type": string,
      "quantity": number,
      "add_ingredients": [string],
      "remove_ingredients": [string]
    }}
  ]
}}

Allowed intent values:
- "add_items"
- "set_customer_name"
- "set_pickup_time"
- "modify_items"
- "remove_items"
- "replace_items"
- "cancel_order"
- "unknown"

Rules:
- Output JSON only. No markdown.
- pizza_type must be exactly one of these two values: "Normale" or "Senza glutine".
- If the user explicitly says "senza glutine", "gluten free", or clearly asks for gluten-free pizza, then pizza_type MUST be "Senza glutine".
- If the user does NOT explicitly request gluten free, then pizza_type MUST be "Normale".
- Never convert an explicit gluten-free request into "Normale".
- quantity must be an integer > 0.
- Normalize pizza names with first letter uppercase when possible.
- If a pizza exists in the MENU, use exactly the same name as in the MENU.
- If the user explicitly requests a pizza name that is NOT in the MENU, you must STILL include that pizza in items so the backend can validate it.
- Never drop an explicitly requested pizza just because it is not present in the MENU.
- If the user says something like "vorrei una gustosa", "una diavola", "due capricciose", you must extract that pizza request even if it is not in the MENU.
- If no pizza is clearly mentioned, return an empty items array.
- pickup_time should be a simple string like "20:30" when present.
- customer_name should be extracted when clearly present.
- Each item must always include "add_ingredients" and "remove_ingredients".
- If there are no ingredient changes, use empty arrays.
- If the user says "senza pomodoro", put "pomodoro" in remove_ingredients.
- If the user says "con patatine", put "patatine" in add_ingredients.
- If the user says "bianca", interpret it as remove_ingredients = ["pomodoro"] unless a better pizza base is clearly specified.
- Never invent ingredients not mentioned by the user.
- If the user describes a pizza by ingredients but does not clearly name a pizza from the MENU, you may use "Pizza personalizzata" as pizza_name.
- Use "Pizza personalizzata" especially for phrases like "una pizza con würstel e patatine", "una bianca con prosciutto", "una rossa con olive".
- If using "Pizza personalizzata", still fill add_ingredients and remove_ingredients correctly.
- If the user says "bianca", interpret it as remove_ingredients = ["pomodoro"].
- If the user says "rossa", do not add anything automatically unless specific ingredients are mentioned.

Intent rules:
- Use "add_items" when the user is adding pizzas.
- Use "set_customer_name" when the user is mainly providing the customer name.
- Use "set_pickup_time" when the user is mainly providing the pickup time.
- Use "modify_items" when the user is clearly correcting previously mentioned pizzas but the action is ambiguous.
- Use "remove_items" when the user wants to remove one or more pizzas already present in the order.
- Use "replace_items" when the user wants to replace previous pizzas with new ones.
- Use "cancel_order" when the user wants to cancel the whole order.
- Use "unknown" if the message is unclear.

Examples:
- "togli la margherita" -> remove_items
- "leva una pizza" -> remove_items
- "fai due capricciose invece" -> replace_items
- "al posto della margherita metti una diavola" -> replace_items
- "annulla tutto" -> cancel_order
- "voglio una margherita" -> add_items
- "voglio una cascina" -> add_items
- "voglio 2 margherite" -> add_items
"""


def extract_order_from_text(message: str, menu_items: list[dict]) -> dict:
    menu_lines = []
    for item in menu_items:
        line = (
            f'- {item["name"]} | tipo: {item["pizza_type"]} | '
            f'categoria: {item["category"]} | prezzo: {item["price"]} | '
            f'disponibile: {item["available"]}'
        )
        menu_lines.append(line)

    menu_text = "\n".join(menu_lines) if menu_lines else "No menu items available."

    response = client.responses.create(
        model=MODEL_NAME,
        input=[
            {
                "role": "system",
                "content": build_system_prompt(menu_text),
            },
            {
                "role": "user",
                "content": message,
            },
        ],
    )

    raw_text = response.output_text.strip()
    parsed = json.loads(raw_text)

    if "intent" not in parsed:
        parsed["intent"] = "unknown"
    if "customer_name" not in parsed:
        parsed["customer_name"] = None
    if "pickup_time" not in parsed:
        parsed["pickup_time"] = None
    if "items" not in parsed:
        parsed["items"] = []

    return parsed