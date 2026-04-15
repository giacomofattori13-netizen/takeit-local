import datetime
import json
import os
import re
import time
from zoneinfo import ZoneInfo

import httpx
from openai import OpenAI

BASE44_ORDER_URL = "https://app.base44.com/api/apps/69c54bc5c44250d7da397903/entities/Order"
BASE44_CUSTOMER_URL = "https://app.base44.com/api/apps/69c54bc5c44250d7da397903/entities/Customer"

MENU_JSON_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "menu_data.json")
)
DOUGH_JSON_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dough_data.json")
)

_menu_cache: list[dict] = []
_dough_cache: list[dict] = []
_restaurant_cache: dict | None = None
_restaurant_cache_ts: float = 0.0  # epoch seconds dell'ultimo fetch riuscito

RESTAURANT_CACHE_TTL = 600  # 10 minuti

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
        body = response.json()
        print(f"[Dough] Body keys: {list(body.keys()) if isinstance(body, dict) else type(body).__name__}")
        raw = body.get("entities", body) if isinstance(body, dict) else body
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
    """Restituisce il supplemento dell'impasto; 0.0 se non trovato.
    Se la cache è vuota (es. worker riavviato) ricarica da file prima di cercare.
    """
    cache = _dough_cache if _dough_cache else load_doughs()
    for dough in cache:
        if dough["code"] == dough_code:
            surcharge = float(dough.get("surcharge", 0.0))
            print(f"[Dough] {dough_code} → surcharge={surcharge}")
            return surcharge
    print(f"[Dough] '{dough_code}' non trovato in cache ({[d['code'] for d in cache]}) → surcharge=0.0")
    return 0.0


def is_dough_available(dough_code: str) -> bool:
    """Restituisce True se l'impasto è disponibile (o se non è in cache = non gestito)."""
    for dough in _dough_cache:
        if dough["code"] == dough_code:
            return bool(dough.get("available", True))
    return True  # impasti non elencati sono considerati validi (es. default classica)


def get_next_order_number() -> int:
    """
    Restituisce il prossimo numero ordine progressivo: conta gli Order su Base44 e aggiunge 1.
    Fallback: numero random a 4 cifre.
    """
    import random as _random
    token = os.getenv("BASE44_TOKEN")
    if token:
        try:
            response = httpx.get(
                BASE44_ORDER_URL,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
            entities = data.get("entities", []) if isinstance(data, dict) else data
            count = len(entities) if isinstance(entities, list) else 0
            next_num = count + 1
            print(f"[Order] Ordini esistenti: {count} → prossimo numero: {next_num}")
            return next_num
        except Exception as e:
            print(f"[Order] Errore conteggio ordini: {type(e).__name__}: {e} → uso fallback random")
    fallback = _random.randint(1000, 9999)
    print(f"[Order] Fallback numero ordine random: {fallback}")
    return fallback


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



def send_whatsapp_confirmation(
    customer_name: str,
    customer_phone: str | None,
    pickup_time: str,
    items: list[dict],
    total_amount: float,
) -> str:
    """Invia la conferma WhatsApp. Restituisce una stringa di stato:
    'inviato:<status_code>', 'skip:<motivo>', 'errore:<msg>'."""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_WHATSAPP_FROM")
    pizzeria_name = os.getenv("PIZZERIA_NAME", "La Pizzeria")

    print(f"[WhatsApp] Invio a customer_phone={customer_phone!r} customer_name={customer_name!r}")
    print(f"[WhatsApp] Variabili Twilio — ACCOUNT_SID={'✓' if account_sid else '✗'} AUTH_TOKEN={'✓' if auth_token else '✗'} FROM={'✓ ' + from_number if from_number else '✗'}")

    if not all([account_sid, auth_token, from_number]):
        print("[WhatsApp] Variabili Twilio mancanti, skip")
        return "skip:credenziali_mancanti"

    # Normalizza il numero: rimuovi spazi/trattini/parentesi
    phone = re.sub(r"[\s\-\(\)]", "", customer_phone or "")
    print(f"[WhatsApp] Numero normalizzato: {phone!r}")
    if not phone:
        print("[WhatsApp] Numero non disponibile, skip")
        return "skip:numero_mancante"
    # Numeri fissi: formato locale (0...) o internazionale italiano (+390...)
    if phone.startswith("0") or phone.startswith("+390"):
        print(f"[WhatsApp] Numero fisso ({phone}), skip")
        return "skip:numero_fisso"
    if not phone.startswith("+"):
        phone = f"+39{phone}"
    print(f"[WhatsApp] Numero destinatario finale: {phone!r}")

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
        return f"inviato:{response.status_code}"
    except httpx.HTTPStatusError as e:
        print(f"[WhatsApp] Errore HTTP {e.response.status_code}: {e.response.text}")
        return f"errore:HTTP_{e.response.status_code}"
    except Exception as e:
        print(f"[WhatsApp] Errore invio: {type(e).__name__}: {e}")
        return f"errore:{type(e).__name__}"


def _fetch_restaurant_from_base44() -> dict | None:
    """Fa la GET a Base44 e restituisce il dict del ristorante, o None in caso di errore."""
    token = os.getenv("BASE44_TOKEN")
    if not token:
        print("[Restaurant] BASE44_TOKEN non configurato, skip fetch")
        return None
    url = f"{BASE44_APP}/Restaurant"
    try:
        response = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        response.raise_for_status()
        body = response.json()
        entities = body.get("entities", body) if isinstance(body, dict) else body
        data = entities[0] if isinstance(entities, list) and entities else (body if isinstance(body, dict) else None)
        if not isinstance(data, dict):
            print("[Restaurant] Risposta Base44 non valida")
            return None
        return data
    except Exception as e:
        print(f"[Restaurant] Errore fetch Base44: {type(e).__name__}: {e}")
        return None


def load_restaurant() -> dict:
    """Restituisce i dati del ristorante con cache in memoria e TTL di 10 minuti.

    - Se la cache è valida (< 10 min), la restituisce direttamente.
    - Se è scaduta, tenta un refresh da Base44.
    - Se Base44 non risponde, usa i dati cached precedenti come fallback.
    - Non dipende da nessun file su disco.
    """
    global _restaurant_cache, _restaurant_cache_ts
    now = time.monotonic()

    if _restaurant_cache and (now - _restaurant_cache_ts) < RESTAURANT_CACHE_TTL:
        return _restaurant_cache

    fresh = _fetch_restaurant_from_base44()
    if fresh is not None:
        _restaurant_cache = fresh
        _restaurant_cache_ts = now
        print(f"[Restaurant] Cache aggiornata da Base44. Campi: {list(_restaurant_cache.keys())}")
        print(f"[Restaurant] agent_greeting: {_restaurant_cache.get('agent_greeting')!r}")
        return _restaurant_cache

    # Base44 non disponibile — usa cache precedente se esiste
    if _restaurant_cache:
        age = int(now - _restaurant_cache_ts)
        print(f"[Restaurant] Base44 non disponibile, uso cache precedente (età {age}s)")
        return _restaurant_cache

    print("[Restaurant] Nessun dato disponibile: né Base44 né cache")
    return {}


def is_agent_active() -> bool:
    """Restituisce True se l'agente è attivo (agent_active != False).
    In caso di dati mancanti o errori, assume attivo per sicurezza."""
    restaurant = load_restaurant()
    active = restaurant.get("agent_active", True)
    # Base44 può restituire bool o stringa
    if isinstance(active, str):
        active = active.lower() not in ("false", "0", "no")
    result = bool(active)
    print(f"[Agent] agent_active={result!r} (raw={restaurant.get('agent_active')!r})")
    return result


def fetch_and_save_restaurant() -> dict:
    """Alias usato all'avvio in main.py: forza un refresh da Base44 ignorando il TTL."""
    global _restaurant_cache_ts
    _restaurant_cache_ts = 0.0  # azzera il TTL così load_restaurant fa sempre il fetch
    return load_restaurant()


_GREETING_PATTERN = re.compile(r"buon pomeriggio|buonasera|buongiorno", re.IGNORECASE)


def _time_greeting() -> str:
    """Restituisce il saluto corretto in base all'ora corrente (Europe/Rome)."""
    hour = datetime.datetime.now(ZoneInfo("Europe/Rome")).hour
    if 6 <= hour < 12:
        return "buongiorno"
    if 12 <= hour < 18:
        return "buon pomeriggio"
    return "buonasera"  # 18-05


def _apply_time_greeting(text: str) -> str:
    """Sostituisce buongiorno/buon pomeriggio/buonasera nel testo con il saluto
    corretto per l'orario corrente. Se nessuno è presente, antepone il saluto."""
    greeting = _time_greeting()
    if _GREETING_PATTERN.search(text):
        return _GREETING_PATTERN.sub(greeting, text)
    return f"{greeting.capitalize()}, {text}"


def get_agent_greeting() -> str:
    """Restituisce il saluto dell'agente con saluto temporale corretto.
    Priorità: 1) Restaurant.agent_greeting da Base44
               2) env var AGENT_GREETING
               3) stringa hardcoded di emergenza
    """
    # 1. Base44
    restaurant = load_restaurant()
    greeting = restaurant.get("agent_greeting")
    if greeting and isinstance(greeting, str) and greeting.strip():
        result = _apply_time_greeting(greeting.strip())
        print(f"[Agent] Saluto: {result!r} (fonte: Base44)")
        return result

    # 2. Env var (configurabile su Railway senza codice)
    env_greeting = os.getenv("AGENT_GREETING")
    if env_greeting and env_greeting.strip():
        result = _apply_time_greeting(env_greeting.strip())
        print(f"[Agent] Saluto: {result!r} (fonte: env AGENT_GREETING)")
        return result

    # 3. Fallback di emergenza
    result = _apply_time_greeting("Pizzeria Corte Del Sole, buonasera. Come posso aiutarla?")
    print(f"[Agent] Saluto: {result!r} (fonte: fallback hardcoded)")
    return result


def get_opening_hours() -> dict | str | None:
    """Restituisce gli orari di apertura da Restaurant.opening_hours in Base44."""
    restaurant = load_restaurant()
    return restaurant.get("opening_hours")


_WEEKDAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _round_to_nearest_15(total_minutes: int) -> int:
    """Arrotonda ai 15 minuti più vicini (es. 19:07 → 19:00, 19:08 → 19:15)."""
    return ((total_minutes + 7) // 15) * 15


def _round_up_to_15(total_minutes: int) -> int:
    """Arrotonda al successivo multiplo di 15 minuti (ceiling)."""
    return ((total_minutes + 14) // 15) * 15


def resolve_pickup_time(raw: str) -> str:
    """
    Normalizza un orario di ritiro grezzo in "HH:MM" (timezone Europe/Rome).

    Regole applicate:
    1. Orario ambiguo (ore 1-12): scegli il più vicino nel futuro tra AM e PM.
    2. "prima_possibile" / "prima possibile" / "subito": ora corrente + 20 min,
       arrotondato al prossimo multiplo di 15 min.
    3. Arrotonda sempre ai 15 minuti più vicini.
    """
    rome = ZoneInfo("Europe/Rome")
    now = datetime.datetime.now(tz=rome)
    now_total = now.hour * 60 + now.minute

    lowered = raw.strip().lower()
    if (
        re.search(r"prima.{0,10}possibile|appena.{0,5}possibile", lowered)
        or lowered in ("prima_possibile", "asap", "subito")
    ):
        target_total = _round_up_to_15(now_total + 20)
        if target_total >= 24 * 60:
            target_total -= 24 * 60
        h, m = divmod(target_total, 60)
        print(f"[Hours] 'prima possibile' → {h:02d}:{m:02d} (ora attuale: {now.hour:02d}:{now.minute:02d})")
        return f"{h:02d}:{m:02d}"

    m_match = re.match(r"^(\d{1,2})(?::(\d{2}))?$", raw.strip())
    if not m_match:
        return raw  # formato non riconosciuto, ritorna as-is

    hour = int(m_match.group(1))
    minute = int(m_match.group(2)) if m_match.group(2) else 0

    # Regola 1: orario ambiguo (1-12) → AM vs PM, scegli il più vicino nel futuro
    if 1 <= hour <= 12:
        am_total = hour * 60 + minute
        pm_total = (hour + 12) * 60 + minute

        def _minutes_until(target: int) -> int:
            diff = target % (24 * 60) - now_total
            if diff < 0:
                diff += 24 * 60
            return diff

        if _minutes_until(pm_total) < _minutes_until(am_total):
            hour = (hour + 12) % 24
            print(f"[Hours] Orario ambiguo '{raw}' risolto come {hour:02d}:{minute:02d} (ora: {now.hour:02d}:{now.minute:02d})")

    # Regola 3: arrotonda ai 15 minuti più vicini
    total = _round_to_nearest_15(hour * 60 + minute)
    if total >= 24 * 60:
        total -= 24 * 60

    h, m = divmod(total, 60)
    result = f"{h:02d}:{m:02d}"
    if result != raw.strip():
        print(f"[Hours] Orario normalizzato: '{raw}' → '{result}'")
    return result


def validate_pickup_time(pickup_time: str) -> tuple[bool, str | None, str | None]:
    """
    Controlla se pickup_time rientra negli orari di apertura del giorno corrente
    ed è nel futuro rispetto all'orario attuale (timezone Europe/Rome).

    opening_hours atteso: {"monday": "closed"|"HH:MM-HH:MM", "tuesday": ..., ...}

    Restituisce (is_valid, nearest_open_slot, closing_time).
    - is_valid: True se l'orario è valido
    - nearest_open_slot: orario suggerito (HH:MM) se non valido
    - closing_time: orario di chiusura (HH:MM), valorizzato solo quando il problema
      è che l'orario richiesto supera la chiusura di oggi
    """
    rome = ZoneInfo("Europe/Rome")
    now = datetime.datetime.now(tz=rome)
    now_total = now.hour * 60 + now.minute

    opening_hours = get_opening_hours()
    if not opening_hours or not isinstance(opening_hours, dict):
        return True, None, None

    def parse_time(t: str) -> int:
        p = t.strip().split(":")
        return int(p[0]) * 60 + (int(p[1]) if len(p) > 1 else 0)

    def parse_range(slot: str) -> tuple[int, int] | None:
        if not slot or slot.strip().lower() == "closed":
            return None
        halves = slot.strip().split("-", 1)
        if len(halves) != 2:
            return None
        try:
            return parse_time(halves[0]), parse_time(halves[1])
        except Exception:
            return None

    try:
        parts = pickup_time.strip().split(":")
        pickup_minutes = int(parts[0]) * 60 + (int(parts[1]) if len(parts) > 1 else 0)
    except (ValueError, IndexError):
        return True, None, None

    today_name = _WEEKDAY_NAMES[datetime.date.today().weekday()]
    today_slot = opening_hours.get(today_name, "")
    today_range = parse_range(today_slot)

    if today_range is None:
        # Oggi chiusi — cerca il prossimo giorno aperto
        for i in range(1, 7):
            next_day = _WEEKDAY_NAMES[(datetime.date.today().weekday() + i) % 7]
            next_slot = opening_hours.get(next_day, "")
            next_range = parse_range(next_slot)
            if next_range:
                h, m = divmod(next_range[0], 60)
                print(f"[Hours] Oggi ({today_name}) chiusi, prossima apertura: {next_day} {h:02d}:{m:02d}")
                return False, f"{h:02d}:{m:02d}", None
        return False, None, None

    open_min, close_min = today_range

    # Prima dell'apertura
    if pickup_minutes < open_min:
        h, m = divmod(open_min, 60)
        return False, f"{h:02d}:{m:02d}", None

    # Dopo la chiusura → suggerisci l'ultimo slot (chiusura - 15 min)
    if pickup_minutes > close_min:
        last_slot = max(close_min - 15, open_min)
        close_h, close_m = divmod(close_min, 60)
        last_h, last_m = divmod(last_slot, 60)
        print(f"[Hours] Richiesta {pickup_time} dopo chiusura {close_h:02d}:{close_m:02d}")
        return False, f"{last_h:02d}:{last_m:02d}", f"{close_h:02d}:{close_m:02d}"

    # Dentro gli orari — ma già passato per oggi
    if pickup_minutes < now_total:
        next_slot = _round_up_to_15(now_total)
        if next_slot <= close_min:
            h, m = divmod(next_slot, 60)
            print(f"[Hours] Orario {pickup_time} già passato, prossimo slot: {h:02d}:{m:02d}")
            return False, f"{h:02d}:{m:02d}", None
        # Nessun slot rimasto oggi
        last_slot = max(close_min - 15, open_min)
        close_h, close_m = divmod(close_min, 60)
        last_h, last_m = divmod(last_slot, 60)
        return False, f"{last_h:02d}:{last_m:02d}", f"{close_h:02d}:{close_m:02d}"

    return True, None, None


def lookup_customer(phone: str) -> dict | None:
    """
    Cerca il cliente su Base44 per numero di telefono.
    Scarica tutti i Customer e filtra in Python (Base44 non supporta query params).
    Restituisce il primo match (o None). Usa lookup_all_customers per i duplicati.
    """
    matches = _fetch_customers_by_phone(phone)
    return matches[0] if matches else None


def _fetch_customers_by_phone(phone: str) -> list[dict]:
    """Restituisce TUTTI i record Customer con quel numero di telefono."""
    print(f"[Customer] Inizio lookup per {phone!r}")
    token = os.getenv("BASE44_TOKEN")
    if not token:
        print("[Customer] BASE44_TOKEN non configurato, lookup saltato")
        return []

    try:
        print(f"[Customer] GET {BASE44_CUSTOMER_URL}")
        response = httpx.get(
            BASE44_CUSTOMER_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        print(f"[Customer] HTTP {response.status_code}")
        print(f"[Customer] Body raw: {response.text[:800]}")
        response.raise_for_status()
        data = response.json()
        entities = data.get("entities", []) if isinstance(data, dict) else data
        if not isinstance(entities, list):
            print(f"[Customer] Formato inatteso per entities: {type(entities).__name__}")
            return []
        print(f"[Customer] Totale record: {len(entities)}")
        phone_norm = re.sub(r"[\s\-\(\)]", "", phone)
        print(f"[Customer] Cerco phone normalizzato: {phone_norm!r}")
        matches = []
        for record in entities:
            rec_phone_raw = record.get("phone") or ""
            rec_phone = re.sub(r"[\s\-\(\)]", "", rec_phone_raw)
            print(f"[Customer]   record phone raw={rec_phone_raw!r} norm={rec_phone!r}")
            if rec_phone == phone_norm:
                matches.append(record)
        print(f"[Customer] Match trovati: {len(matches)}")
        return matches
    except Exception as e:
        print(f"[Customer] Errore lookup: {type(e).__name__}: {e}")
        return []


def _delete_customer(customer_id: str, auth_kwargs: dict) -> None:
    try:
        response = httpx.delete(
            f"{BASE44_CUSTOMER_URL}/{customer_id}",
            **auth_kwargs,
            timeout=10,
        )
        response.raise_for_status()
        print(f"[Customer] Eliminato duplicato id={customer_id}")
    except Exception as e:
        print(f"[Customer] Errore eliminazione duplicato id={customer_id}: {type(e).__name__}: {e}")


def upsert_customer(
    full_name: str,
    phone: str | None,
    pizzas: list[str],
    total_amount: float = 0.0,
) -> None:
    """
    Crea o aggiorna il cliente su Base44 dopo un ordine confermato.
    Aggiorna: total_orders, last_order_date, favorite_pizzas, total_spend, average_spend.
    Crea con: is_repeat=False.
    """
    api_key = os.getenv("BASE44_API_KEY")
    token = os.getenv("BASE44_TOKEN")
    if not api_key and not token:
        print("[Customer] Nessun token, skip upsert")
        return

    auth_kwargs: dict = (
        {"params": {"api_key": api_key}}
        if api_key
        else {"headers": {"Authorization": f"Bearer {token}"}}
    )
    today = datetime.date.today().isoformat()

    all_matches = _fetch_customers_by_phone(phone) if phone else []

    # Deduplicazione: se ci sono più record con lo stesso phone, tieni quello
    # con più ordini e cancella gli altri, sommandone i dati.
    if len(all_matches) > 1:
        print(f"[Customer] Trovati {len(all_matches)} duplicati per {phone!r} — unificazione in corso")
        all_matches.sort(key=lambda r: int(r.get("total_orders") or 0), reverse=True)
        primary = all_matches[0]
        duplicates = all_matches[1:]

        # Somma total_orders e total_spend dai duplicati nel primario
        combined_orders = int(primary.get("total_orders") or 0) + sum(
            int(r.get("total_orders") or 0) for r in duplicates
        )
        combined_spend = round(
            float(primary.get("total_spend") or 0.0)
            + sum(float(r.get("total_spend") or 0.0) for r in duplicates),
            2,
        )
        prev_pizzas = primary.get("favorite_pizzas") or []
        if isinstance(prev_pizzas, str):
            prev_pizzas = [p.strip() for p in prev_pizzas.split(",") if p.strip()]
        for dup in duplicates:
            dup_pizzas = dup.get("favorite_pizzas") or []
            if isinstance(dup_pizzas, str):
                dup_pizzas = [p.strip() for p in dup_pizzas.split(",") if p.strip()]
            for p in dup_pizzas:
                if p not in prev_pizzas:
                    prev_pizzas.append(p)

        # Aggiorna il primario con i dati unificati
        combined_avg = round(combined_spend / combined_orders, 2) if combined_orders else 0.0
        try:
            merge_payload = {
                "total_orders": combined_orders,
                "total_spend": combined_spend,
                "average_spend": combined_avg,
                "favorite_pizzas": prev_pizzas,
                "is_repeat": True,
            }
            resp = httpx.put(
                f"{BASE44_CUSTOMER_URL}/{primary['id']}",
                **auth_kwargs,
                json=merge_payload,
                timeout=10,
            )
            resp.raise_for_status()
            print(f"[Customer] Primario aggiornato dopo merge: ordini={combined_orders} spend={combined_spend}")
        except Exception as e:
            print(f"[Customer] Errore merge primario: {type(e).__name__}: {e}")

        # Cancella i duplicati
        for dup in duplicates:
            _delete_customer(dup["id"], auth_kwargs)

        # Usa il primario (aggiornato) come existing
        primary["total_orders"] = combined_orders
        primary["total_spend"] = combined_spend
        primary["average_spend"] = combined_avg
        primary["favorite_pizzas"] = prev_pizzas
        existing = primary
    else:
        existing = all_matches[0] if all_matches else None

    if existing:
        customer_id = existing.get("id")

        # Merge favorite_pizzas senza duplicati
        prev = existing.get("favorite_pizzas") or []
        if isinstance(prev, str):
            prev = [p.strip() for p in prev.split(",") if p.strip()]
        merged_pizzas = list(dict.fromkeys(prev + [p for p in pizzas if p not in prev]))

        new_total_orders = int(existing.get("total_orders") or 0) + 1
        new_total_spend = round(float(existing.get("total_spend") or 0.0) + total_amount, 2)
        new_average_spend = round(new_total_spend / new_total_orders, 2)

        payload = {
            "full_name": full_name,
            "phone": phone,
            "last_order_date": today,
            "total_orders": new_total_orders,
            "favorite_pizzas": merged_pizzas,
            "total_spend": new_total_spend,
            "average_spend": new_average_spend,
            "is_repeat": True,
        }
        try:
            response = httpx.put(
                f"{BASE44_CUSTOMER_URL}/{customer_id}",
                **auth_kwargs,
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
            print(
                f"[Customer] Aggiornato: {full_name} | ordini={new_total_orders} "
                f"total_spend={new_total_spend} avg={new_average_spend}"
            )
        except Exception as e:
            print(f"[Customer] Errore update: {type(e).__name__}: {e}")
    else:
        payload = {
            "full_name": full_name,
            "phone": phone,
            "last_order_date": today,
            "total_orders": 1,
            "favorite_pizzas": pizzas,
            "total_spend": round(total_amount, 2),
            "average_spend": round(total_amount, 2),
            "is_repeat": False,
        }
        try:
            response = httpx.post(
                BASE44_CUSTOMER_URL,
                **auth_kwargs,
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
            print(f"[Customer] Creato: {full_name} | total_spend={payload['total_spend']}")
        except Exception as e:
            print(f"[Customer] Errore create: {type(e).__name__}: {e}")


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
- "clear_cart"
- "unknown"

Rules:
- Output JSON only. No markdown.
- Each pizza in the MENU is a standalone product. Gluten-free versions have "(SG)" in their name and are distinct products, not variants.
- dough_type MUST be one of the exact codes listed in DOUGH TYPES above (e.g. "classica", "integrale", "napoletana", "pinsa_romana", "senza_lievito").
- NEVER use "Normale", "Standard", "Normal", or any value not in the DOUGH TYPES list.
- If the user does NOT specify the dough for a pizza, dough_type MUST be "classica".
- REGOLA CRITICA SULL'IMPASTO: Se il cliente dice il nome di una pizza seguito (o preceduto) da un tipo di impasto, quell'impasto va assegnato SOLO a quella pizza. Tutte le altre pizze nella stessa frase senza impasto specificato devono avere dough_type "classica". NON propagare mai l'impasto da una pizza all'altra.
- Esempi espliciti:
  * "capricciosa integrale" → pizza_name="Capricciosa", dough_type="integrale"
  * "margherita napoletana" → pizza_name="Margherita", dough_type="napoletana"
  * "una contadina e una capricciosa integrale" → Contadina dough_type="classica", Capricciosa dough_type="integrale"
  * "una capricciosa, una appia e una margherita impasto integrale" → capricciosa dough_type="classica", appia dough_type="classica", margherita dough_type="integrale"
  * "due margherite integrali e una capricciosa" → margherite dough_type="integrale", capricciosa dough_type="classica"
- REGOLA IMPASTO GLOBALE: Se il cliente usa un impasto in modo globale per tutte le pizze appena ordinate (es. "tutte napoletane", "tutte integrali", "impasto napoletano per tutte"), assegna quell'impasto a TUTTE le pizze menzionate in quel messaggio.
- Esempi impasto globale:
  * "due margherite e una capricciosa tutte napoletane" → Margherita dough_type="napoletana", Capricciosa dough_type="napoletana"
  * "una marinara e una diavola impasto integrale per tutte" → Marinara dough_type="integrale", Diavola dough_type="integrale"
  * "tre pizze tutte senza lievito" → tutte dough_type="senza_lievito"
  * "una capricciosa, una tirolese e una appia tutte integrali" → Capricciosa dough_type="integrale", Tirolese dough_type="integrale", Appia dough_type="integrale"
  ATTENZIONE: "tutte [impasto]" significa OGNI pizza elencata nel messaggio, non solo l'ultima.
- IMPORTANTE: le parole "tutte", "entrambe", "tutti" quando seguite da un tipo di impasto (integrale, napoletana, pinsa, senza lievito) NON sono nomi di pizze. Applica quell'impasto a tutte le pizze estratte in questo messaggio e non aggiungere nessun item extra. Esempio: "una margherita e una baita tutte e due integrali" → estrai solo Margherita e Baita entrambe con dough_type="integrale", senza aggiungere nessun terzo item.
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
- REGOLA ASSOLUTA — MESSAGGIO CORRENTE ONLY: Analizza ESCLUSIVAMENTE l'ultimo messaggio dell'utente (l'ultimo turno). NON guardare i messaggi precedenti per estrarre pizze. Gli item già ordinati sono salvati dal backend — non riestrarre mai pizze dalla storia. Se l'ultimo messaggio non contiene nuove pizze (es. è un orario, un nome, una conferma, una risposta sì/no), restituisci items=[]. Esempi: "alle 20:30" → items=[], "Mario" → items=[], "sì" → items=[], "una capricciosa" → items=[Capricciosa].
- pickup_time should be a simple string like "20:30" when present. If the user says "prima possibile", "il prima possibile", "subito" or similar expressions meaning "as soon as possible", output "prima_possibile" as pickup_time.
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
- Use "remove_items" when the user wants to remove one or more specific pizzas already present in the order (togliete, rimuovi, non voglio più, cancella la ...).
- Use "replace_items" when the user wants to replace previous pizzas with new ones.
- Use "cancel_order" when the user wants to cancel the whole order including name and time (annulla l'ordine, voglio annullare).
- Use "clear_cart" when the user wants to reset only the pizzas and start over, keeping name and pickup time (cancella tutto e ricominciamo, ricominciamo da capo, azzera le pizze, voglio ricominciare).
- Use "unknown" if the message is unclear.

Remove_items rules:
- Put the pizza to remove in items[] with pizza_name set to the exact name mentioned.
- If the user says "l'ultima pizza", "l'ultima", "quella appena aggiunta" → set pizza_name to "__last__" so the backend resolves it.
- If multiple pizzas to remove, list each as a separate item.

Examples:
- "togli la margherita" -> remove_items, items=[{{pizza_name:"Margherita",...}}]
- "togliete la capricciosa" -> remove_items, items=[{{pizza_name:"Capricciosa",...}}]
- "non voglio più la margherita" -> remove_items, items=[{{pizza_name:"Margherita",...}}]
- "rimuovi l'ultima pizza" -> remove_items, items=[{{pizza_name:"__last__",...}}]
- "leva una pizza" -> remove_items, items=[{{pizza_name:"__last__",...}}]
- "fai due capricciose invece" -> replace_items
- "al posto della margherita metti una diavola" -> replace_items
- "annulla l'ordine" -> cancel_order
- "cancella tutto e ricominciamo" -> clear_cart
- "voglio ricominciare da capo" -> clear_cart
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
        line = f'- {item["name"]} | {item["category"]} | €{item["price"]}'
        menu_lines.append(line)

    menu_text = "\n".join(menu_lines) if menu_lines else "No menu items available."

    dough_lines = []
    for d in (dough_items or []):
        surcharge = d.get("surcharge", 0.0)
        surcharge_text = f"+€{surcharge:.2f}" if surcharge > 0 else "incluso"
        dough_lines.append(f'- {d["name"]} (code: {d["code"]}) | supplemento: {surcharge_text}')
    dough_text = "\n".join(dough_lines)

    input_messages = [
        {"role": "system", "content": build_system_prompt(menu_text, dough_text)},
        {"role": "user", "content": message},
    ]
    print(f"[LLM] Estrazione da messaggio corrente: {message!r}")

    response = client.responses.create(
        model=MODEL_NAME,
        max_output_tokens=512,
        input=input_messages,
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
        add_ing = item.get("add_ingredients", [])
        rem_ing = item.get("remove_ingredients", [])
        dough_log = item.get("dough_type", "classica")
        print(f"[LLM] estratto: pizza={item.get('pizza_name')!r} dough={dough_log!r} add={add_ing} remove={rem_ing}")
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
