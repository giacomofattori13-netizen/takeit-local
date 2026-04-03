import json
import os
import re

import httpx
from openai import OpenAI

BASE44_ORDER_URL = "https://app.base44.com/api/apps/69c54bc5c44250d7da397903/entities/Order"

MENU_JSON_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "menu_data.json")
)
DOUGH_JSON_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dough_data.json")
)

_menu_cache: list[dict] = []
_dough_cache: list[dict] = []
_restaurant_cache: dict | None = None

BASE44_APP = "https://app.base44.com/api/apps/69c54bc5c44250d7da397903/entities"

INGREDIENT_EXTRA_PRICE = 2.0


def reset_menu_cache() -> None:
    global _menu_cache
    _menu_cache = []


def reset_dough_cache() -> None:
    global _dough_cache
    _dough_cache = []


def reset_restaurant_cache() -> None:
    global _restaurant_cache
    _restaurant_cache = None

# Mappature bidirezionali tra dough_type Base44 e pizza_type interno (usato nel DB locale)
_DOUGH_TO_PIZZA_TYPE: dict[str, str] = {
    "classica": "Normale",
    "integrale": "Integrale",
    "senza_glutine": "Senza glutine",
}

_PIZZA_TYPE_TO_DOUGH: dict[str, str] = {
    "Normale": "classica",
    "Integrale": "integrale",
    "Senza glutine": "senza_glutine",
}


def load_menu_from_base44() -> list[dict]:
    """
    Carica il menu dal file statico app/menu_data.json (generato da Base44).
    Il file viene letto una sola volta e tenuto in memoria per l'intera durata
    del processo — nessun token JWT, nessuna chiamata di rete.
    Per aggiornare il menu: sostituire menu_data.json e riavviare il server.
    """
    global _menu_cache

    if _menu_cache:
        return _menu_cache

    menu_path = os.path.normpath(MENU_JSON_PATH)
    try:
        with open(menu_path, encoding="utf-8") as f:
            raw = json.load(f)

        menu = [
            {
                "name": item["name"],
                "category": item.get("category", ""),
                "dough_type": item.get("dough_type", "classica"),
                "pizza_type": _DOUGH_TO_PIZZA_TYPE.get(
                    item.get("dough_type", "classica"), "Normale"
                ),
                "price": item.get("price", 0.0),
                "available": item.get("available", True),
                "ingredients": item.get("ingredients", []),
            }
            for item in raw
            if item.get("available", True)
        ]

        _menu_cache = menu
        first_names = [item["name"] for item in menu[:3]]
        print(f"[Menu] Caricato da file: {len(menu)} voci ({menu_path})")
        print(f"[Menu] Prime 3 voci: {first_names}")
        return menu

    except FileNotFoundError:
        print(f"[Menu] File non trovato: {menu_path} — verrà usato il DB locale come fallback")
        return []
    except Exception as e:
        print(f"[Menu] Errore lettura menu_data.json: {type(e).__name__}: {e}")
        return []


def _filter_doughs(raw: list[dict]) -> list[dict]:
    """Deduplica per code (primo trovato) ed esclude senza_glutine."""
    seen: set[str] = set()
    result = []
    for d in raw:
        code = d.get("code", "")
        if code == "senza_glutine" or code in seen:
            continue
        seen.add(code)
        result.append(d)
    return result


def load_doughs() -> list[dict]:
    """Carica gli impasti dal file dough_data.json (fallback statico)."""
    global _dough_cache
    if _dough_cache:
        return _dough_cache
    try:
        with open(DOUGH_JSON_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        _dough_cache = _filter_doughs([d for d in raw if d.get("available", True)])
        print(f"[Dough] Caricati {len(_dough_cache)} impasti da file: {[d['code'] for d in _dough_cache]}")
        return _dough_cache
    except FileNotFoundError:
        print(f"[Dough] File non trovato: {DOUGH_JSON_PATH}")
        return []
    except Exception as e:
        print(f"[Dough] Errore: {type(e).__name__}: {e}")
        return []


def fetch_and_save_doughs() -> list[dict]:
    """
    Scarica gli impasti dall'endpoint DoughType di Base44, salva su
    dough_data.json e aggiorna la cache. Se il fetch fallisce, carica
    da file.
    """
    global _dough_cache
    token = os.getenv("BASE44_TOKEN")
    if not token:
        print("[Dough] BASE44_TOKEN non configurato, carico da file")
        return load_doughs()

    url = f"{BASE44_APP}/DoughType"
    try:
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        response.raise_for_status()
        raw = response.json()
        doughs = [
            {
                "name": item["name"],
                "code": item["code"],
                "surcharge": float(item.get("surcharge", 0.0)),
                "available": item.get("available", True),
            }
            for item in raw
        ]
        with open(DOUGH_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(doughs, f, ensure_ascii=False, indent=2)
        _dough_cache = _filter_doughs([d for d in doughs if d.get("available", True)])
        print(f"[Dough] Scaricati da Base44, salvati. Impasti attivi: {[d['code'] for d in _dough_cache]}")
        return _dough_cache
    except Exception as e:
        print(f"[Dough] Errore fetch Base44: {type(e).__name__}: {e} — carico da file")
        return load_doughs()


def get_dough_surcharge(dough_code: str) -> float:
    """Restituisce il supplemento dell'impasto; 0.0 se non trovato."""
    print(f"[Dough] get_dough_surcharge('{dough_code}') — cache: {[d['code'] for d in _dough_cache]}")
    for dough in _dough_cache:
        if dough["code"] == dough_code:
            surcharge = float(dough.get("surcharge", 0.0))
            print(f"[Dough] Trovato '{dough_code}' → surcharge={surcharge}")
            return surcharge
    print(f"[Dough] '{dough_code}' non trovato in cache → surcharge=0.0")
    return 0.0


def is_dough_available(dough_code: str) -> bool:
    """Restituisce True se l'impasto è disponibile (o se non è in cache = non gestito)."""
    for dough in _dough_cache:
        if dough["code"] == dough_code:
            return bool(dough.get("available", True))
    return True  # impasti non elencati sono considerati validi (es. default classica)


def save_order_to_base44(
    customer_name: str,
    customer_phone: str | None,
    pickup_time: str,
    order_number: int,
    ai_confidence: float,
    items: list[dict],
) -> None:
    """
    Invia l'ordine a Base44.
    Ogni item deve già contenere base_price, extras_price, total_price.
    total_amount viene calcolato come somma dei total_price.
    dough_type viene mappato dal campo pizza_type se non già presente.
    """
    api_key = os.getenv("BASE44_API_KEY")
    if not api_key:
        print("WARNING: BASE44_API_KEY not set, skipping Base44 sync")
        return

    needs_review = ai_confidence < 0.8
    review_reason = "Bassa confidenza AI" if needs_review else None
    total_amount = round(sum(item.get("total_price", 0.0) for item in items), 2)

    base44_items = [
        {
            "pizza_name": item["pizza_name"],
            "quantity": item["quantity"],
            "dough_type": (
                item.get("dough_type")
                or _PIZZA_TYPE_TO_DOUGH.get(item.get("pizza_type", ""), "classica")
            ),
            "add_ingredients": item.get("add_ingredients", []),
            "remove_ingredients": item.get("remove_ingredients", []),
            "base_price": item.get("base_price", 0.0),
            "extras_price": item.get("extras_price", 0.0),
            "total_price": item.get("total_price", 0.0),
        }
        for item in items
    ]

    payload = {
        "order_number": order_number,
        "customer_name": customer_name,
        "customer_phone": customer_phone,
        "status": "nuovo",
        "source": "telefono",
        "pickup_time": pickup_time,
        "total_amount": total_amount,
        "ai_confidence": ai_confidence,
        "needs_review": needs_review,
        "review_reason": review_reason,
        "items": base44_items,
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
        return
    except Exception as e:
        print(f"[Base44] Errore generico: {type(e).__name__}: {e}")
        return

    send_whatsapp_confirmation(
        customer_name=customer_name,
        customer_phone=customer_phone,
        pickup_time=pickup_time,
        items=items,
        total_amount=total_amount,
    )


def send_whatsapp_confirmation(
    customer_name: str,
    customer_phone: str | None,
    pickup_time: str,
    items: list[dict],
    total_amount: float,
) -> None:
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_WHATSAPP_FROM")
    pizzeria_name = os.getenv("PIZZERIA_NAME", "La Pizzeria")

    if not all([account_sid, auth_token, from_number]):
        print("[WhatsApp] Variabili Twilio non configurate, skip")
        return

    # Normalizza il numero: rimuovi spazi/trattini/parentesi
    phone = re.sub(r"[\s\-\(\)]", "", customer_phone or "")
    if not phone:
        print("[WhatsApp] Numero non disponibile, skip")
        return
    # Numeri fissi: formato locale (0...) o internazionale italiano (+390...)
    if phone.startswith("0") or phone.startswith("+390"):
        print(f"[WhatsApp] Numero fisso ({phone}), skip")
        return
    if not phone.startswith("+"):
        phone = f"+39{phone}"

    pizza_lines = []
    for item in items:
        qty = item.get("quantity", 1)
        name = item.get("pizza_name", "")
        dough = item.get("dough_type", "classica")
        extras = []
        if dough != "classica":
            extras.append(dough)
        for ing in item.get("add_ingredients", []):
            extras.append(f"+{ing}")
        for ing in item.get("remove_ingredients", []):
            extras.append(f"-{ing}")
        line = f"{qty}x {name}"
        if extras:
            line += f" ({', '.join(extras)})"
        pizza_lines.append(line)

    content_sid = "HXb5b62575e6e4ff6129ad7c8efe1f983e"
    content_variables = json.dumps({"1": pizzeria_name, "2": pickup_time})

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    try:
        response = httpx.post(
            url,
            auth=(account_sid, auth_token),
            data={
                "From": f"whatsapp:{from_number}",
                "To": f"whatsapp:{phone}",
                "ContentSid": content_sid,
                "ContentVariables": content_variables,
            },
            timeout=10,
        )
        print(f"[WhatsApp] Risposta Twilio: status={response.status_code} body={response.text}")
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        print(f"[WhatsApp] Errore HTTP {e.response.status_code}: {e.response.text}")
    except Exception as e:
        print(f"[WhatsApp] Errore invio: {type(e).__name__}: {e}")


def load_restaurant() -> dict:
    """Carica i dati del ristorante dall'entità Restaurant di Base44 (con cache).
    Prova prima BASE44_TOKEN (Bearer auth), poi BASE44_API_KEY (query param).
    I fallimenti di rete NON vengono cachati — viene ritentato ad ogni chiamata
    finché non si ottengono dati reali.
    """
    global _restaurant_cache
    # Usa la cache solo se contiene dati reali (non None e non vuota)
    if _restaurant_cache:
        return _restaurant_cache

    token = os.getenv("BASE44_TOKEN")
    api_key = os.getenv("BASE44_API_KEY")
    if not token and not api_key:
        print("[Restaurant] Nessun token Base44 (BASE44_TOKEN / BASE44_API_KEY) — skip")
        # Non carichiamo la cache: se il token viene aggiunto dopo non vogliamo bloccarci
        return {}

    url = f"{BASE44_APP}/Restaurant"
    attempts = []
    if token:
        attempts.append({"headers": {"Authorization": f"Bearer {token}"}})
    if api_key:
        attempts.append({"params": {"api_key": api_key}})

    for kwargs in attempts:
        try:
            response = httpx.get(url, timeout=10, **kwargs)
            print(f"[Restaurant] HTTP {response.status_code} ({list(kwargs.keys())[0]})")
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list) and data:
                result = data[0]
            elif isinstance(data, dict) and data:
                result = data
            else:
                print("[Restaurant] Risposta vuota o formato inatteso, non cacheato")
                continue
            _restaurant_cache = result
            print(f"[Restaurant] Campi disponibili: {list(_restaurant_cache.keys())}")
            print(f"[Restaurant] agent_greeting: {_restaurant_cache.get('agent_greeting')!r}")
            return _restaurant_cache
        except Exception as e:
            print(f"[Restaurant] Tentativo fallito: {type(e).__name__}: {e}")
            continue

    # Tutti i tentativi falliti — non cacheamo, si riproverà alla prossima chiamata
    print("[Restaurant] Tutti i tentativi falliti, verrà riprovato alla prossima richiesta")
    return {}


def get_agent_greeting() -> str:
    """Restituisce il saluto dell'agente.
    Priorità: 1) Restaurant.agent_greeting da Base44
               2) env var AGENT_GREETING
               3) stringa hardcoded di emergenza
    """
    # 1. Base44
    restaurant = load_restaurant()
    greeting = restaurant.get("agent_greeting")
    if greeting and isinstance(greeting, str) and greeting.strip():
        result = greeting.strip()
        print(f"[Agent] Saluto: {result!r} (fonte: Base44)")
        return result

    # 2. Env var (configurabile su Railway senza codice)
    env_greeting = os.getenv("AGENT_GREETING")
    if env_greeting and env_greeting.strip():
        result = env_greeting.strip()
        print(f"[Agent] Saluto: {result!r} (fonte: env AGENT_GREETING)")
        return result

    # 3. Fallback di emergenza
    result = "Pizzeria Corte Del Sole, buonasera. Come posso aiutarla?"
    print(f"[Agent] Saluto: {result!r} (fonte: fallback hardcoded)")
    return result


def get_opening_hours() -> dict | str | None:
    """Restituisce gli orari di apertura da Restaurant.opening_hours in Base44."""
    restaurant = load_restaurant()
    return restaurant.get("opening_hours")


def validate_pickup_time(pickup_time: str) -> tuple[bool, str | None]:
    """
    Controlla se pickup_time rientra negli orari di apertura.
    Restituisce (is_valid, nearest_slot) dove nearest_slot è il primo slot aperto successivo.
    Se opening_hours non è configurato o non parsabile, restituisce sempre (True, None).
    """
    opening_hours = get_opening_hours()
    if not opening_hours:
        return True, None

    try:
        parts = pickup_time.strip().split(":")
        pickup_minutes = int(parts[0]) * 60 + (int(parts[1]) if len(parts) > 1 else 0)
    except (ValueError, IndexError):
        return True, None

    def parse_time(t: str) -> int:
        p = t.strip().split(":")
        return int(p[0]) * 60 + (int(p[1]) if len(p) > 1 else 0)

    # Collect ranges as list of (open_min, close_min)
    ranges: list[tuple[int, int]] = []

    if isinstance(opening_hours, str):
        for segment in opening_hours.split(","):
            segment = segment.strip()
            if "-" in segment:
                halves = segment.split("-", 1)
                try:
                    ranges.append((parse_time(halves[0]), parse_time(halves[1])))
                except Exception:
                    pass
    elif isinstance(opening_hours, dict):
        if "open" in opening_hours and "close" in opening_hours:
            try:
                ranges.append((parse_time(opening_hours["open"]), parse_time(opening_hours["close"])))
            except Exception:
                pass
        else:
            for slot in opening_hours.values():
                if isinstance(slot, str) and "-" in slot:
                    halves = slot.split("-", 1)
                    try:
                        ranges.append((parse_time(halves[0]), parse_time(halves[1])))
                    except Exception:
                        pass
                elif isinstance(slot, dict) and "open" in slot and "close" in slot:
                    try:
                        ranges.append((parse_time(slot["open"]), parse_time(slot["close"])))
                    except Exception:
                        pass

    if not ranges:
        return True, None

    for open_m, close_m in ranges:
        if open_m <= pickup_minutes <= close_m:
            return True, None

    # Not valid — find nearest open slot after requested time
    ranges.sort()
    for open_m, _ in ranges:
        if open_m > pickup_minutes:
            h, m = divmod(open_m, 60)
            return False, f"{h:02d}:{m:02d}"

    # All ranges are before the requested time — suggest first opening
    open_m = ranges[0][0]
    h, m = divmod(open_m, 60)
    return False, f"{h:02d}:{m:02d}"


print("DEBUG conversation_service loaded")

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
MODEL_NAME = os.getenv("OPENAI_MODEL", "gpt-4o-mini")


def build_system_prompt(menu_text: str, dough_text: str = "") -> str:
    dough_section = f"\nDOUGH TYPES AVAILABLE:\n{dough_text}\n" if dough_text else ""
    return f"""
You extract takeaway pizza orders from Italian customer messages.

You MUST use this menu as the source of truth.

MENU:
{menu_text}
{dough_section}

Return ONLY valid JSON with this exact structure:
{{
  "intent": string,
  "customer_name": string | null,
  "pickup_time": string | null,
  "items": [
    {{
      "pizza_name": string,
      "dough_type": string,
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
- Each pizza in the MENU is a standalone product. Gluten-free versions have "(SG)" in their name and are distinct products, not variants.
- dough_type MUST be one of the exact codes listed in DOUGH TYPES above (e.g. "classica", "integrale", "napoletana", "pinsa_romana", "senza_lievito").
- NEVER use "Normale", "Standard", "Normal", or any value not in the DOUGH TYPES list.
- If the user does NOT specify the dough, dough_type MUST be "classica".
- Always use the exact "code" value from DOUGH TYPES, never the "name".
- If the user says "senza glutine", use the "(SG)" version of the pizza name from the MENU (e.g. "Pusteria (SG)") and set dough_type to "classica" (the SG pizza has its own price).
- If the user asks which doughs are available or their prices, answer using the DOUGH TYPES list.
- Never propose a "(SG)" pizza as an alternative when the user asked for a specific dough type.
- Reject (set dough_type to "classica") any dough not present in DOUGH TYPES.
- quantity must be an integer > 0.
- Always use the exact pizza name as it appears in the MENU.
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


def extract_order_from_text(
    message: str,
    menu_items: list[dict],
    dough_items: list[dict] | None = None,
) -> dict:
    menu_lines = []
    for item in menu_items:
        ingredients_text = ", ".join(item.get("ingredients", [])) or "n.d."
        line = (
            f'- {item["name"]} | impasto: {item.get("dough_type", "classica")} | '
            f'categoria: {item["category"]} | prezzo: €{item["price"]} | '
            f'ingredienti: {ingredients_text}'
        )
        menu_lines.append(line)

    menu_text = "\n".join(menu_lines) if menu_lines else "No menu items available."

    dough_lines = []
    for d in (dough_items or []):
        surcharge = d.get("surcharge", 0.0)
        surcharge_text = f"+€{surcharge:.2f}" if surcharge > 0 else "incluso"
        dough_lines.append(f'- {d["name"]} (code: {d["code"]}) | supplemento: {surcharge_text}')
    dough_text = "\n".join(dough_lines)

    print(f"[LLM] menu_items ricevuti: {len(menu_items)}")
    print(f"[LLM] Prime 3 righe menu_text:\n" + "\n".join(menu_lines[:3]) if menu_lines else "[LLM] menu_text vuoto")

    response = client.responses.create(
        model=MODEL_NAME,
        input=[
            {
                "role": "system",
                "content": build_system_prompt(menu_text, dough_text),
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

    # Normalizza dough_type → pizza_type per compatibilità con il DB locale
    for item in parsed["items"]:
        dough = item.get("dough_type", "classica")
        item["dough_type"] = dough
        item["pizza_type"] = _DOUGH_TO_PIZZA_TYPE.get(dough, "Normale")

    # Correggi i nomi: l'LLM può estrarre "Pusteria" + dough_type="senza_glutine"
    # mentre nel menu la voce si chiama "Pusteria (SG)". Usiamo un indice
    # (nome_base, dough_type) → nome_canonico costruito dal menu.
    name_lookup = _build_name_lookup(menu_items)
    for item in parsed["items"]:
        key = (item["pizza_name"].lower(), item["dough_type"])
        canonical = name_lookup.get(key)
        if canonical and canonical != item["pizza_name"]:
            print(f"[LLM] Nome corretto: '{item['pizza_name']}' → '{canonical}'")
            item["pizza_name"] = canonical

    return parsed


def _build_name_lookup(menu_items: list[dict]) -> dict:
    """
    Costruisce un indice {(nome_lower, dough_type): nome_canonico}.

    Regole implementate:
    - Corrispondenza esatta: ("pusteria (sg)", "senza_glutine") → "Pusteria (SG)"
    - Nome base + dough: ("pusteria", "senza_glutine") → "Pusteria (SG)"
    - Regola 1 fallback: se "X classica" non esiste ma "X (SG)" esiste,
      ("x", "classica") → "X (SG)" — per richieste senza impasto specificato.
    """
    lookup: dict[tuple[str, str], str] = {}
    base_to_doughs: dict[str, dict[str, str]] = {}  # base_lower → {dough: full_name}

    for item in menu_items:
        full_name = item["name"]
        dough = item.get("dough_type", "classica")
        lookup[(full_name.lower(), dough)] = full_name
        # Nome base senza suffissi tra parentesi: "Pusteria (SG)" → "pusteria"
        base = re.sub(r"\s*\([^)]+\)\s*$", "", full_name).strip().lower()
        if base != full_name.lower():
            lookup.setdefault((base, dough), full_name)
        base_to_doughs.setdefault(base, {})[dough] = full_name

    # Regola 1: se classica non esiste ma senza_glutine sì, il fallback
    # silenzioso usa la versione SG (cliente non ha specificato impasto)
    for base, doughs in base_to_doughs.items():
        if "classica" not in doughs and "senza_glutine" in doughs:
            lookup.setdefault((base, "classica"), doughs["senza_glutine"])

    return lookup
