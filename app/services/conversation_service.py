import datetime
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.privacy import describe_text_for_log, mask_name, mask_phone
from app.telemetry import record_latency

load_dotenv()

BASE44_ORDER_URL = "https://app.base44.com/api/apps/69c54bc5c44250d7da397903/entities/Order"
BASE44_CUSTOMER_URL = "https://app.base44.com/api/apps/69c54bc5c44250d7da397903/entities/Customer"
BASE44_RESERVATION_URL = "https://app.base44.com/api/apps/69c54bc5c44250d7da397903/entities/Reservation"
BASE44_TABLE_URL = "https://app.base44.com/api/apps/69c54bc5c44250d7da397903/entities/Table"

MENU_JSON_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "menu_data.json")
)
DOUGH_JSON_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dough_data.json")
)
RESTAURANT_JSON_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "restaurant_data.json")
)

_menu_cache: dict[str, list[dict]] = {}           # "" = default JSON file; restaurant_id = per-restaurant
_dough_cache: list[dict] = []
_dough_refresh_inflight = False
_dough_refresh_lock = threading.Lock()
_restaurant_cache: dict[str, dict] = {}            # "" = default; restaurant_id = per-restaurant
_restaurant_cache_ts: dict[str, float] = {}        # "" = default
_restaurant_refresh_inflight: set[str] = set()     # restaurant_id keys being refreshed
_restaurant_refresh_lock = threading.Lock()
_customer_lookup_cache: dict[str, tuple[float, dict | None, float]] = {}
_customer_lookup_cache_lock = threading.Lock()
_system_prompt_cache: dict[str, str | None] = {}   # "" = default; restaurant_id = per-restaurant
_system_prompt_slim_cache: dict[str, dict[str, str]] = {}  # "" = default; restaurant_id → {state: prompt}

RESTAURANT_CACHE_TTL = 600  # 10 minuti
RESTAURANT_REFRESH_TIMEOUT_DEFAULT_SECONDS = 3.0
DOUGH_REFRESH_TIMEOUT_DEFAULT_SECONDS = 3.0
CUSTOMER_LOOKUP_CACHE_TTL_DEFAULT_SECONDS = 300.0
CUSTOMER_LOOKUP_MISS_CACHE_TTL_DEFAULT_SECONDS = 30.0
CUSTOMER_LOOKUP_CACHE_MAX_ITEMS_DEFAULT = 256

BASE44_APP = "https://app.base44.com/api/apps/69c54bc5c44250d7da397903/entities"

INGREDIENT_EXTRA_PRICE = 2.0
SIZE_MINI_DISCOUNT = 1.50
SIZE_DOPPIO_SURCHARGE = 2.00
CUSTOMER_LOOKUP_HTTP_TIMEOUT_DEFAULT_SECONDS = 2.0


class ReservationAvailabilityError(RuntimeError):
    """Availability non verificabile in modo affidabile."""


def _positive_float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, "").strip())
    except ValueError:
        return default
    return value if value > 0 else default


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, "").strip())
    except ValueError:
        return default
    return value if value > 0 else default


def _customer_lookup_http_timeout_seconds() -> float:
    return _positive_float_env(
        "CUSTOMER_LOOKUP_HTTP_TIMEOUT_SECONDS",
        CUSTOMER_LOOKUP_HTTP_TIMEOUT_DEFAULT_SECONDS,
    )


def _dough_refresh_timeout_seconds() -> float:
    return _positive_float_env(
        "DOUGH_REFRESH_TIMEOUT_SECONDS",
        DOUGH_REFRESH_TIMEOUT_DEFAULT_SECONDS,
    )


def _customer_lookup_cache_ttl_seconds() -> float:
    return _positive_float_env(
        "CUSTOMER_LOOKUP_CACHE_TTL_SECONDS",
        CUSTOMER_LOOKUP_CACHE_TTL_DEFAULT_SECONDS,
    )


def _customer_lookup_miss_cache_ttl_seconds() -> float:
    return _positive_float_env(
        "CUSTOMER_LOOKUP_MISS_CACHE_TTL_SECONDS",
        CUSTOMER_LOOKUP_MISS_CACHE_TTL_DEFAULT_SECONDS,
    )


def _customer_lookup_cache_max_items() -> int:
    return _positive_int_env(
        "CUSTOMER_LOOKUP_CACHE_MAX_ITEMS",
        CUSTOMER_LOOKUP_CACHE_MAX_ITEMS_DEFAULT,
    )


def _restaurant_refresh_timeout_seconds() -> float:
    return _positive_float_env(
        "RESTAURANT_REFRESH_TIMEOUT_SECONDS",
        RESTAURANT_REFRESH_TIMEOUT_DEFAULT_SECONDS,
    )


def reset_menu_cache(restaurant_id: str | None = None) -> None:
    global _menu_cache, _system_prompt_cache, _system_prompt_slim_cache
    if restaurant_id is None:
        _menu_cache.clear()
        _system_prompt_cache.clear()
        _system_prompt_slim_cache.clear()
    else:
        _menu_cache.pop(restaurant_id, None)
        _system_prompt_cache.pop(restaurant_id, None)
        _system_prompt_slim_cache.pop(restaurant_id, None)


def reset_dough_cache() -> None:
    global _dough_cache, _dough_refresh_inflight
    _dough_cache = []
    _dough_refresh_inflight = False
    _system_prompt_cache.clear()
    _system_prompt_slim_cache.clear()


def reset_restaurant_cache(restaurant_id: str | None = None) -> None:
    global _restaurant_cache, _restaurant_cache_ts, _restaurant_refresh_inflight
    with _restaurant_refresh_lock:
        if restaurant_id is None:
            _restaurant_cache.clear()
            _restaurant_cache_ts.clear()
            _restaurant_refresh_inflight.clear()
        else:
            _restaurant_cache.pop(restaurant_id, None)
            _restaurant_cache_ts.pop(restaurant_id, None)
            _restaurant_refresh_inflight.discard(restaurant_id)


def get_proposable_menu(menu_items: list[dict] | None = None, restaurant_id: str = "") -> list[dict]:
    """Returns menu items the agent can proactively offer.

    An item is proposable iff: available != False AND no ingredient appears in
    restaurant.sold_out_ingredients (case-insensitive, trimmed comparison).
    """
    if menu_items is None:
        menu_items = load_menu_from_base44(restaurant_id=restaurant_id)

    restaurant = load_restaurant(restaurant_id=restaurant_id)
    sold_out_raw = restaurant.get("sold_out_ingredients") or []
    if not sold_out_raw:
        return menu_items

    sold_out_set = {s.lower().strip() for s in sold_out_raw if isinstance(s, str) and s.strip()}
    proposable = []
    for item in menu_items:
        item_ings = {ing.lower().strip() for ing in item.get("ingredients", []) if ing}
        if item_ings & sold_out_set:
            continue
        proposable.append(item)

    hidden = len(menu_items) - len(proposable)
    if hidden:
        print(f"[Menu] {hidden} voci nascoste per ingredienti finiti: {sold_out_set}")
    return proposable


def get_sold_out_item_names(menu_items: list[dict] | None = None, restaurant_id: str = "") -> set[str]:
    """Returns lowercase names of items hidden due to sold_out_ingredients."""
    if menu_items is None:
        menu_items = load_menu_from_base44(restaurant_id=restaurant_id)

    restaurant = load_restaurant(restaurant_id=restaurant_id)
    sold_out_raw = restaurant.get("sold_out_ingredients") or []
    if not sold_out_raw:
        return set()

    sold_out_set = {s.lower().strip() for s in sold_out_raw if isinstance(s, str) and s.strip()}
    hidden: set[str] = set()
    for item in menu_items:
        item_ings = {ing.lower().strip() for ing in item.get("ingredients", []) if ing}
        if item_ings & sold_out_set:
            hidden.add(item["name"].lower())
    return hidden


def reset_customer_lookup_cache() -> None:
    with _customer_lookup_cache_lock:
        _customer_lookup_cache.clear()


def _prune_customer_lookup_cache(now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    expired = [
        key
        for key, (cached_at, _customer, ttl_seconds) in _customer_lookup_cache.items()
        if now - cached_at >= ttl_seconds
    ]
    for key in expired:
        _customer_lookup_cache.pop(key, None)

    max_items = _customer_lookup_cache_max_items()
    while len(_customer_lookup_cache) > max_items:
        oldest_key = next(iter(_customer_lookup_cache))
        _customer_lookup_cache.pop(oldest_key, None)


def _customer_lookup_cache_ttl_for(customer: dict | None) -> float:
    if customer:
        return _customer_lookup_cache_ttl_seconds()
    return _customer_lookup_miss_cache_ttl_seconds()

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


def load_menu_from_base44(restaurant_id: str = "") -> list[dict]:
    """
    Carica il menu.
    - restaurant_id=="" → legge app/menu_data.json (comportamento legacy)
    - restaurant_id!="" → fetcha da Base44 filtrando per restaurant_id;
                          fallback al file JSON se Base44 non ritorna nulla.
    """
    global _menu_cache

    if restaurant_id in _menu_cache:
        return _menu_cache[restaurant_id]

    if restaurant_id:
        # Fetch dal Base44 per questo ristorante specifico
        try:
            from app.services.base44_client import get_menu_items as _b44_get_menu_items
            raw_items = _b44_get_menu_items(restaurant_id=restaurant_id)
            if raw_items:
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
                        "sale_unit": item.get("sale_unit", "piece"),
                        "restaurant_id": item.get("restaurant_id"),
                    }
                    for item in raw_items
                    if item.get("available", True)
                ]
                _menu_cache[restaurant_id] = menu
                first_names = [item["name"] for item in menu[:3]]
                print(f"[Menu] Caricato da Base44: {len(menu)} voci (restaurant_id={restaurant_id!r})")
                print(f"[Menu] Prime 3 voci: {first_names}")
                return menu
            print(f"[Menu] Base44 non ha restituito voci per restaurant_id={restaurant_id!r}, fallback a file")
        except Exception as e:
            print(f"[Menu] Errore fetch Base44 per restaurant_id={restaurant_id!r}: {type(e).__name__}: {e}")
        # fallback al file

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
                "sale_unit": item.get("sale_unit", "piece"),
            }
            for item in raw
            if item.get("available", True)
        ]

        _menu_cache[restaurant_id] = menu
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


def _parse_doughs_from_base44_body(body: Any) -> list[dict]:
    raw = body.get("entities", body) if isinstance(body, dict) else body
    if not isinstance(raw, list):
        print(f"[Dough] Formato Base44 inatteso: {type(raw).__name__}")
        return []
    return [
        {
            "name": item["name"],
            "code": item["code"],
            "surcharge": float(item.get("surcharge", 0.0)),
            "available": item.get("available", True),
        }
        for item in raw
        if isinstance(item, dict)
    ]


def _fetch_doughs_from_base44(timeout_seconds: float | None = None) -> list[dict]:
    token = os.getenv("BASE44_TOKEN")
    if not token:
        print("[Dough] BASE44_TOKEN non configurato, skip refresh")
        return []

    url = f"{BASE44_APP}/DoughType"
    try:
        timeout = timeout_seconds or _dough_refresh_timeout_seconds()
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        response.raise_for_status()
        body = response.json()
        print(f"[Dough] Body keys: {list(body.keys()) if isinstance(body, dict) else type(body).__name__}")
        return _parse_doughs_from_base44_body(body)
    except Exception as e:
        print(f"[Dough] Errore fetch Base44: {type(e).__name__}: {e}")
        return []


def _cache_doughs(doughs: list[dict], *, save_to_file: bool) -> list[dict]:
    global _dough_cache
    if save_to_file:
        with open(DOUGH_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(doughs, f, ensure_ascii=False, indent=2)
    _dough_cache = _filter_doughs([d for d in doughs if d.get("available", True)])
    print(f"[Dough] Cache aggiornata. Impasti attivi: {[d['code'] for d in _dough_cache]}")
    return _dough_cache


def _refresh_doughs_from_base44_blocking(*, save_to_file: bool = True) -> list[dict]:
    started = time.perf_counter()
    doughs = _fetch_doughs_from_base44()
    elapsed_ms = (time.perf_counter() - started) * 1000
    if not doughs:
        record_latency("dough", "refresh", elapsed_ms, result="failed")
        return []
    record_latency("dough", "refresh", elapsed_ms, result="success")
    return _cache_doughs(doughs, save_to_file=save_to_file)


def _dough_refresh_worker() -> None:
    global _dough_refresh_inflight
    try:
        print("[Dough] Refresh Base44 in background")
        _refresh_doughs_from_base44_blocking(save_to_file=True)
    finally:
        with _dough_refresh_lock:
            _dough_refresh_inflight = False


def _start_dough_refresh_background() -> bool:
    global _dough_refresh_inflight
    with _dough_refresh_lock:
        if _dough_refresh_inflight:
            return False
        _dough_refresh_inflight = True

    thread = threading.Thread(
        target=_dough_refresh_worker,
        name="dough-refresh",
        daemon=True,
    )
    thread.start()
    return True


def fetch_and_save_doughs() -> list[dict]:
    """Carica subito gli impasti locali e aggiorna Base44 in background."""
    local = load_doughs()
    if local:
        _start_dough_refresh_background()
        return local

    print("[Dough] Nessun file locale disponibile: provo refresh Base44 breve")
    fresh = _refresh_doughs_from_base44_blocking(save_to_file=True)
    return fresh or []


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
    restaurant_id: str = "",
    pickup_date: str | None = None,
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

    base44_items = []
    for item in items:
        b44_item = {
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
        if item.get("sale_unit") == "kg":
            kg_size = item.get("size", "normale")
            if kg_size in ("piena", "mezza"):
                b44_item["size"] = kg_size
        base44_items.append(b44_item)

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
    if restaurant_id:
        payload["restaurant_id"] = restaurant_id
    if pickup_date:
        payload["pickup_date"] = pickup_date

    print(
        f"[Base44] Payload ordine=#{order_number} customer={mask_name(customer_name)} "
        f"phone={mask_phone(customer_phone)} items={len(base44_items)} total={total_amount}"
        f" pickup_date={pickup_date!r}"
    )

    try:
        response = httpx.post(
            BASE44_ORDER_URL,
            params={"api_key": api_key},
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        print(f"[Base44] Status code: {response.status_code}")
        print(f"[Base44] Response body_len={len(response.text)}")
        response.raise_for_status()
        print(f"[Base44] Ordine sincronizzato, id={response.json().get('id')}")
    except httpx.HTTPStatusError as e:
        print(f"[Base44] HTTP error {e.response.status_code}: body_len={len(e.response.text)}")
        return
    except Exception as e:
        print(f"[Base44] Errore generico: {type(e).__name__}: {e}")
        return



def _normalize_phone(raw: str | None) -> str | None:
    """Normalizza un numero di telefono in formato E.164 (+39...).
    Ritorna None se il numero è assente, fisso, o non normalizzabile."""
    phone = re.sub(r"[\s\-\(\)]", "", raw or "")
    if not phone:
        return None
    if phone.startswith("0") or phone.startswith("+390"):
        return None  # numero fisso
    if not phone.startswith("+"):
        phone = f"+39{phone}"
    return phone


def format_weight_display(kg: float) -> str:
    """Convert a kg float to a compact Italian display string.

    0.1 → "100g", 0.5 → "500g", 1.0 → "1 kg", 1.5 → "1.5 kg"
    """
    grams = round(kg * 1000)
    if grams <= 0:
        return "0g"
    if grams < 1000:
        return f"{grams}g"
    kg_val = grams / 1000
    return f"{kg_val:g} kg"


def _build_pizza_lines(items: list[dict]) -> list[str]:
    """Costruisce le righe descrittive delle pizze per i messaggi di conferma.

    Formato:
        - 1x Margherita
            # integrale          (solo se impasto != classica)
            + patatine fritte    (solo se presenti)
            - mozzarella         (solo se presenti)

        - 500g Porchetta         (per items al kg)
    """
    lines = []
    for item in items:
        qty = item.get("quantity", 1)
        name = item.get("pizza_name", "")
        sale_unit = item.get("sale_unit", "piece")

        if sale_unit == "kg":
            temperature = item.get("temperature") or "fredda"
            temp_str = " (calda)" if temperature == "calda" else " (fredda)"
            kg_size = item.get("size", "normale")
            size_str = f" — {kg_size}" if kg_size in ("piena", "mezza") else ""
            lines.append(f"- {format_weight_display(float(qty))} {name}{size_str}{temp_str}")
            continue

        dough = item.get("dough_type", "classica")
        add_ings = item.get("add_ingredients", [])
        rem_ings = item.get("remove_ingredients", [])

        display_name = "Margherita" if name == "Personalizzata" else name
        lines.append(f"- {qty}x {display_name}")
        if dough and dough != "classica":
            lines.append(f"    # {dough}")
        for ing in add_ings:
            lines.append(f"    + {ing}")
        for ing in rem_ings:
            lines.append(f"    - {ing}")
    return lines


def _send_sms(
    phone: str,
    items: list[dict],
    pickup_time: str,
    total_amount: float,
    account_sid: str,
    auth_token: str,
) -> str:
    """Invia SMS di conferma ordine (canale principale).
    Restituisce 'sms_inviato:<status>', 'sms_skip:<motivo>', 'sms_errore:<msg>'."""
    raw_sms_from = os.getenv("TWILIO_NUMBER")
    print(f"[SMS] TWILIO_NUMBER={mask_phone(raw_sms_from)}")
    if not raw_sms_from:
        print("[SMS] TWILIO_NUMBER non configurato, skip")
        return "sms_skip:TWILIO_NUMBER_mancante"

    sms_from = raw_sms_from.removeprefix("whatsapp:")
    print(f"[SMS] From={mask_phone(sms_from)} To={mask_phone(phone)}")

    pizzeria_name = os.getenv("PIZZERIA_NAME", "La Pizzeria")
    pizzeria_phone = os.getenv("PIZZERIA_PHONE", "")
    pizza_lines = _build_pizza_lines(items)
    pizza_block = "\n".join(pizza_lines)
    total_str = f"\u20ac{total_amount:.2f}"
    time_str = pickup_time or "da definire"
    contact_line = f"Per modifiche chiama il {pizzeria_phone}" if pizzeria_phone else ""

    parts = [
        f"{pizzeria_name} \u2705",
        "Ordine confermato!",
        "",
        pizza_block,
        "",
        f"Totale: {total_str} \u2014 Ritiro alle {time_str}",
    ]
    if contact_line:
        parts.append(contact_line)
    body = "\n".join(parts)

    print(f"[SMS] Body {describe_text_for_log(body)}")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    try:
        resp = httpx.post(
            url,
            auth=(account_sid, auth_token),
            data={"From": sms_from, "To": phone, "Body": body},
            timeout=10,
        )
        print(f"[SMS] Risposta Twilio: status={resp.status_code} body_len={len(resp.text)}")
        resp.raise_for_status()
        print(f"[SMS] Inviato con successo a {mask_phone(phone)}")
        return f"sms_inviato:{resp.status_code}"
    except httpx.HTTPStatusError as e:
        print(f"[SMS] Errore HTTP {e.response.status_code}: body_len={len(e.response.text)}")
        return f"sms_errore:HTTP_{e.response.status_code}"
    except Exception as e:
        import traceback
        print(f"[SMS] Errore inatteso {type(e).__name__}: {e}")
        print(f"[SMS] Traceback:\n{traceback.format_exc()}")
        return f"sms_errore:{type(e).__name__}"


def _send_whatsapp(
    phone: str,
    pickup_time: str,
    account_sid: str,
    auth_token: str,
    from_number: str,
    pizzeria_name: str,
) -> str:
    """Invia conferma WhatsApp (canale secondario opzionale).
    Restituisce 'wa_inviato:<status>' o 'wa_errore:<msg>'."""
    clean_from = from_number.removeprefix("whatsapp:")
    wa_from = f"whatsapp:{clean_from}"
    wa_to = f"whatsapp:{phone}"
    content_sid = "HXb5b62575e6e4ff6129ad7c8efe1f983e"
    content_variables = json.dumps({"1": pizzeria_name, "2": pickup_time})

    print(
        f"[WhatsApp] POST Messages.json → "
        f"From={mask_phone(wa_from)} To={mask_phone(wa_to)}"
    )
    print(f"[WhatsApp] ContentSid={content_sid} ContentVariables={describe_text_for_log(content_variables)}")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    try:
        response = httpx.post(
            url,
            auth=(account_sid, auth_token),
            data={
                "From": wa_from,
                "To": wa_to,
                "ContentSid": content_sid,
                "ContentVariables": content_variables,
            },
            timeout=10,
        )
        print(f"[WhatsApp] Risposta: status={response.status_code} body_len={len(response.text)}")
        response.raise_for_status()
        print(f"[WhatsApp] Inviato con successo")
        return f"wa_inviato:{response.status_code}"
    except httpx.HTTPStatusError as e:
        print(f"[WhatsApp] Errore HTTP {e.response.status_code}: body_len={len(e.response.text)}")
        return f"wa_errore:HTTP_{e.response.status_code}"
    except Exception as e:
        import traceback
        print(f"[WhatsApp] Errore inatteso {type(e).__name__}: {e}")
        print(f"[WhatsApp] Traceback:\n{traceback.format_exc()}")
        return f"wa_errore:{type(e).__name__}"


def send_whatsapp_confirmation(
    customer_name: str,
    customer_phone: str | None,
    pickup_time: str,
    items: list[dict],
    total_amount: float,
) -> str:
    """Invia la conferma ordine: SMS come canale principale, WhatsApp come fallback opzionale.
    Restituisce una stringa di stato con i risultati dei canali usati."""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    wa_from_number = os.getenv("TWILIO_WHATSAPP_FROM")
    pizzeria_name = os.getenv("PIZZERIA_NAME", "La Pizzeria")

    print(f"[Conferma] === INIZIO INVIO CONFERMA ORDINE ===")
    print(f"[Conferma] customer_name={mask_name(customer_name)} customer_phone={mask_phone(customer_phone)}")
    print(f"[Conferma] pickup_time={pickup_time!r} total_amount={total_amount} items={len(items)}")
    print(
        f"[Conferma] ACCOUNT_SID={'✓' if account_sid else '✗'} "
        f"AUTH_TOKEN={'✓' if auth_token else '✗'} "
        f"TWILIO_NUMBER={mask_phone(os.getenv('TWILIO_NUMBER'))} "
        f"TWILIO_WHATSAPP_FROM={mask_phone(wa_from_number)}"
    )

    if not all([account_sid, auth_token]):
        print(f"[Conferma] Credenziali Twilio mancanti, skip")
        return "skip:credenziali_mancanti"

    phone = _normalize_phone(customer_phone)
    print(
        f"[Conferma] Numero raw={mask_phone(customer_phone)} "
        f"→ normalizzato={mask_phone(phone)}"
    )
    if not phone:
        print(f"[Conferma] Numero non valido o fisso, skip invio")
        return "skip:numero_non_valido"

    # ── Canale principale: SMS ────────────────────────────────────────────────
    sms_status = _send_sms(phone, items, pickup_time, total_amount, account_sid, auth_token)
    print(f"[Conferma] SMS risultato: {sms_status}")

    # ── Canale secondario: WhatsApp (solo se SMS fallisce e credenziali disponibili) ──
    if not sms_status.startswith("sms_inviato") and wa_from_number:
        print(f"[Conferma] SMS fallito → tentativo WhatsApp")
        wa_status = _send_whatsapp(phone, pickup_time, account_sid, auth_token, wa_from_number, pizzeria_name)
        print(f"[Conferma] WhatsApp risultato: {wa_status}")
        return f"{sms_status}|{wa_status}"

    return sms_status


def _fetch_restaurant_from_base44(timeout_seconds: float | None = None) -> dict | None:
    """Fa la GET a Base44 e restituisce il dict del ristorante, o None in caso di errore."""
    token = os.getenv("BASE44_TOKEN")
    if not token:
        print("[Restaurant] BASE44_TOKEN non configurato, skip fetch")
        return None
    url = f"{BASE44_APP}/Restaurant"
    try:
        timeout = timeout_seconds or _restaurant_refresh_timeout_seconds()
        response = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
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


def _load_restaurant_from_file() -> dict:
    """Carica app/restaurant_data.json come fallback offline per orari e saluto."""
    try:
        with open(RESTAURANT_JSON_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            print(f"[Restaurant] Fallback file caricato: {RESTAURANT_JSON_PATH}")
            return data
        print(f"[Restaurant] Fallback file non valido: {type(data).__name__}")
    except FileNotFoundError:
        print(f"[Restaurant] Fallback file non trovato: {RESTAURANT_JSON_PATH}")
    except Exception as e:
        print(f"[Restaurant] Errore fallback file: {type(e).__name__}: {e}")
    return {}


def _cache_restaurant_data(data: dict, source: str, restaurant_id: str = "") -> dict:
    global _restaurant_cache, _restaurant_cache_ts
    _restaurant_cache[restaurant_id] = data
    _restaurant_cache_ts[restaurant_id] = time.monotonic()
    print(f"[Restaurant] Cache aggiornata da {source} (restaurant_id={restaurant_id!r}). Campi: {list(data.keys())}")
    print(f"[Restaurant] agent_greeting: {data.get('agent_greeting')!r}")
    return _restaurant_cache[restaurant_id]


def _fetch_restaurant_from_base44_for(restaurant_id: str = "", timeout_seconds: float | None = None) -> dict | None:
    """Fetches the right Restaurant from Base44 depending on restaurant_id."""
    if restaurant_id:
        from app.services.base44_client import get_restaurant_by_id as _b44_get_by_id
        return _b44_get_by_id(restaurant_id, timeout=timeout_seconds or _restaurant_refresh_timeout_seconds())
    return _fetch_restaurant_from_base44(timeout_seconds=timeout_seconds)


def _refresh_restaurant_cache_blocking(restaurant_id: str = "") -> dict | None:
    started = time.perf_counter()
    fresh = _fetch_restaurant_from_base44_for(restaurant_id)
    elapsed_ms = (time.perf_counter() - started) * 1000

    if fresh is not None:
        record_latency("restaurant", "refresh", elapsed_ms, result="success")
        return _cache_restaurant_data(fresh, "Base44", restaurant_id)

    record_latency("restaurant", "refresh", elapsed_ms, result="failed")
    return None


def _restaurant_refresh_worker(reason: str, restaurant_id: str = "") -> None:
    try:
        print(f"[Restaurant] Refresh Base44 in background ({reason}) restaurant_id={restaurant_id!r}")
        _refresh_restaurant_cache_blocking(restaurant_id)
    finally:
        with _restaurant_refresh_lock:
            _restaurant_refresh_inflight.discard(restaurant_id)


def _start_restaurant_refresh_background(reason: str, restaurant_id: str = "") -> bool:
    with _restaurant_refresh_lock:
        if restaurant_id in _restaurant_refresh_inflight:
            return False
        _restaurant_refresh_inflight.add(restaurant_id)

    thread = threading.Thread(
        target=_restaurant_refresh_worker,
        args=(reason, restaurant_id),
        name=f"restaurant-refresh-{restaurant_id or 'default'}",
        daemon=True,
    )
    thread.start()
    return True


def load_restaurant(restaurant_id: str = "") -> dict:
    """Restituisce i dati ristorante senza bloccare il turno cliente su Base44.

    Usa stale-while-revalidate: cache/file locale rispondono subito; Base44 aggiorna
    in background quando il TTL scade o al primo cold load.
    """
    now = time.monotonic()
    cached = _restaurant_cache.get(restaurant_id)
    cached_ts = _restaurant_cache_ts.get(restaurant_id, 0.0)

    if cached and (now - cached_ts) < RESTAURANT_CACHE_TTL:
        return cached

    if cached:
        age = int(now - cached_ts)
        print(f"[Restaurant] Uso cache stale (età {age}s), refresh in background (restaurant_id={restaurant_id!r})")
        _start_restaurant_refresh_background("cache_stale", restaurant_id)
        return cached

    # For non-default restaurants, don't use the local file fallback (it's only for Corte del Sole)
    if not restaurant_id:
        local = _load_restaurant_from_file()
        if local:
            cached = _cache_restaurant_data(local, "file", restaurant_id)
            _start_restaurant_refresh_background("cold_file_fallback", restaurant_id)
            return cached

    _start_restaurant_refresh_background("cold_empty", restaurant_id)
    if restaurant_id:
        print(f"[Restaurant] Nessun dato immediato per restaurant_id={restaurant_id!r}: avvio refresh")
    else:
        print("[Restaurant] Nessun dato immediato disponibile: né cache, né file")
    return {}


def is_agent_active(restaurant_id: str = "") -> bool:
    """Restituisce True se l'agente è attivo (agent_active != False).
    In caso di dati mancanti o errori, assume attivo per sicurezza."""
    restaurant = load_restaurant(restaurant_id=restaurant_id)
    active = restaurant.get("agent_active", True)
    # Base44 può restituire bool o stringa
    if isinstance(active, str):
        active = active.lower() not in ("false", "0", "no")
    result = bool(active)
    print(f"[Agent] agent_active={result!r} (raw={restaurant.get('agent_active')!r})")
    return result


def is_reservations_enabled(restaurant_id: str = "") -> bool:
    """Restituisce True se le prenotazioni tavolo sono abilitate (default True).
    Quando False il ristorante è in modalità asporto puro: nessun flusso prenotazione."""
    restaurant = load_restaurant(restaurant_id=restaurant_id)
    value = restaurant.get("reservations_enabled", True)
    if isinstance(value, str):
        value = value.lower() not in ("false", "0", "no")
    result = bool(value)
    print(f"[Reservation] reservations_enabled={result!r} (raw={restaurant.get('reservations_enabled')!r})")
    return result


def fetch_and_save_restaurant(restaurant_id: str = "") -> dict:
    """Alias usato all'avvio in main.py: prova un refresh Base44 breve, poi fallback locale."""
    fresh = _refresh_restaurant_cache_blocking(restaurant_id)
    if fresh:
        return fresh
    return load_restaurant(restaurant_id=restaurant_id)


def resolve_restaurant_from_phone(to_number: str) -> tuple[dict, str, str]:
    """Find the Restaurant whose agent_phone matches to_number (the Twilio To field).

    Returns (restaurant_dict, restaurant_id_str, match_method) where match_method is
    one of: "agent_phone" | "default_restaurant_id" | "global_fallback".
    Falls back to DEFAULT_RESTAURANT_ID env, then empty-string default.
    Never raises — always returns a usable triple.
    """
    from app.services.base44_client import get_restaurant_by_phone as _b44_by_phone

    to_clean = to_number.strip()

    # 1. Match by agent_phone
    if to_clean:
        try:
            matched = _b44_by_phone(to_clean)
        except Exception as exc:
            print(f"[Restaurant] Errore risoluzione per To={to_clean!r}: {type(exc).__name__}: {exc}")
            matched = None

        if matched:
            rid = matched.get("id", "")
            print(f"[Restaurant] To={to_clean!r} → restaurant_id={rid!r} (match=agent_phone)")
            _cache_restaurant_data(matched, "phone_lookup", rid)
            return matched, rid, "agent_phone"

    print(f"[Restaurant] To={to_clean!r} → nessun match agent_phone")

    # 2. Fallback: DEFAULT_RESTAURANT_ID env
    default_id = os.getenv("DEFAULT_RESTAURANT_ID", "").strip()
    if default_id:
        restaurant = load_restaurant(restaurant_id=default_id)
        if restaurant:
            print(f"[Restaurant] To={to_clean!r} → restaurant_id={default_id!r} (match=default_restaurant_id)")
            return restaurant, default_id, "default_restaurant_id"
        print(f"[Restaurant] DEFAULT_RESTAURANT_ID={default_id!r} non trovato in cache/Base44")

    # 3. Last resort: empty-string default (existing global behaviour)
    print(f"[Restaurant] To={to_clean!r} → nessun ristorante risolto (match=global_fallback)")
    return load_restaurant(""), "", "global_fallback"


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


def get_agent_greeting(restaurant_id: str = "") -> str:
    """Restituisce il saluto dell'agente con saluto temporale corretto.
    Priorità: 1) Restaurant.agent_greeting da Base44
               2) env var AGENT_GREETING
               3) stringa hardcoded di emergenza
    """
    # 1. Base44
    restaurant = load_restaurant(restaurant_id=restaurant_id)
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


def get_opening_hours(restaurant_id: str = "") -> dict | str | None:
    """Restituisce gli orari di apertura da Restaurant.opening_hours in Base44."""
    restaurant = load_restaurant(restaurant_id=restaurant_id)
    return restaurant.get("opening_hours")


_WEEKDAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_WEEKDAY_IT   = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]


def build_closed_message(restaurant_id: str = "") -> str:
    """Genera un messaggio di chiusura dinamico in base agli orari di apertura.

    Casi:
    1. Giorno di chiusura settimanale → 'Siamo chiusi oggi. Riapriamo [giorno] alle [ora].'
    2. Prima dell'orario di apertura odierno → 'Al momento siamo chiusi. Apriamo alle [ora] di oggi.'
    3. Dopo la chiusura odierna → 'Siamo chiusi per questa sera. Riapriamo [giorno] alle [ora].'
    4. Dentro gli orari ma agent_active=False (chiusura straordinaria) →
       'Siamo temporaneamente chiusi per questa sera. Riapriamo regolarmente [giorno].'
    5. Fallback (nessun dato orari) → messaggio generico.
    """
    rome = ZoneInfo("Europe/Rome")
    now = datetime.datetime.now(tz=rome)
    now_total = now.hour * 60 + now.minute
    today_idx = now.weekday()  # 0 = lunedì

    opening_hours = get_opening_hours(restaurant_id=restaurant_id)
    if not opening_hours or not isinstance(opening_hours, dict):
        return "Siamo temporaneamente chiusi. Richiameremo appena possibile. Grazie!"

    def _fmt(minutes: int) -> str:
        """Converte minuti dall'inizio della giornata in stringa parlata: 'le 19' o 'le 19:30'."""
        h, m = divmod(minutes, 60)
        return str(h) if m == 0 else f"{h}:{m:02d}"

    def _next_open(offset_start: int = 1) -> tuple[int | None, int | None]:
        """Restituisce (weekday_index, open_minutes) del prossimo giorno aperto."""
        for i in range(offset_start, 7):
            day_idx = (today_idx + i) % 7
            slot = opening_hours.get(_WEEKDAY_NAMES[day_idx], "")
            r = _parse_opening_range(slot)
            if r:
                return day_idx, r[0]
        return None, None

    today_range = _parse_opening_range(opening_hours.get(_WEEKDAY_NAMES[today_idx], ""))

    # Caso 1 — giorno di chiusura settimanale
    if today_range is None:
        next_idx, next_open_min = _next_open(1)
        if next_idx is not None:
            return (
                f"Siamo chiusi oggi. Riapriamo {_WEEKDAY_IT[next_idx]} "
                f"alle {_fmt(next_open_min)}. La aspettiamo!"
            )
        return "Siamo chiusi oggi. Riapriamo presto. La aspettiamo!"

    open_min, close_min = today_range

    # Caso 2 — prima dell'apertura odierna
    if now_total < open_min:
        return f"Al momento siamo chiusi. Apriamo alle {_fmt(open_min)} di oggi. La aspettiamo!"

    # Caso 3 — dopo la chiusura odierna
    if now_total >= close_min:
        next_idx, next_open_min = _next_open(1)
        if next_idx is not None:
            return (
                f"Siamo chiusi per questa sera. Riapriamo {_WEEKDAY_IT[next_idx]} "
                f"alle {_fmt(next_open_min)}. La aspettiamo!"
            )
        return "Siamo chiusi per questa sera. Riapriamo presto. La aspettiamo!"

    # Caso 4 — dentro gli orari ma agent_active=False (chiusura straordinaria)
    next_idx, next_open_min = _next_open(1)
    if next_idx is not None:
        return (
            f"Siamo temporaneamente chiusi per questa sera. "
            f"Riapriamo regolarmente {_WEEKDAY_IT[next_idx]} alle {_fmt(next_open_min)}. Grazie!"
        )
    return "Siamo temporaneamente chiusi per questa sera. Riapriamo presto. Grazie!"


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

    # Regola 1: orario ambiguo (1-11) → se siamo nel pomeriggio/sera (ora >= 12),
    # interpretiamo sempre come PM (7 → 19, 8 → 20). Un cliente che chiama la sera
    # non vuole ritirare alle 7 di mattina.
    # Usa now_total (minuti dall'inizio della giornata in Europe/Rome) come fonte di verità.
    is_afternoon = now_total >= 12 * 60
    original_hour = hour
    if 1 <= hour <= 11 and is_afternoon:
        hour += 12
    print(
        f"[Hours] ora cliente={original_hour} ora attuale={now.hour:02d}:{now.minute:02d} "
        f"(Rome) is_afternoon={is_afternoon} → convertita={hour:02d}:{minute:02d}"
    )

    # Regola 3: arrotonda ai 15 minuti più vicini
    total = _round_to_nearest_15(hour * 60 + minute)
    if total >= 24 * 60:
        total -= 24 * 60

    h, m = divmod(total, 60)
    result = f"{h:02d}:{m:02d}"
    if result != raw.strip():
        print(f"[Hours] Orario normalizzato: '{raw}' → '{result}'")
    return result


def validate_pickup_time(pickup_time: str, restaurant_id: str = "") -> tuple[bool, str | None, str | None]:
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

    opening_hours = get_opening_hours(restaurant_id=restaurant_id)
    if not opening_hours or not isinstance(opening_hours, dict):
        return True, None, None

    try:
        pickup_minutes = _parse_minutes(pickup_time)
    except (ValueError, IndexError):
        return True, None, None

    today_name = _WEEKDAY_NAMES[datetime.date.today().weekday()]
    today_slot = opening_hours.get(today_name, "")
    today_range = _parse_opening_range(today_slot)

    if today_range is None:
        # Oggi chiusi — cerca il prossimo giorno aperto
        for i in range(1, 7):
            next_day = _WEEKDAY_NAMES[(datetime.date.today().weekday() + i) % 7]
            next_slot = opening_hours.get(next_day, "")
            next_range = _parse_opening_range(next_slot)
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

    # Dopo la chiusura → suggerisci l'ultimo slot (chiusura - 15 min).
    # L'orario limite stesso (chiusura - 15 min) è valido: condizione > non >=.
    if pickup_minutes > close_min - 15:
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


def get_next_open_day(restaurant_id: str = "") -> tuple[datetime.date, str]:
    """Return (date, weekday_it_name) of the first open business day starting from tomorrow.

    Checks opening_hours from Base44; if unavailable or no slot is closed,
    returns tomorrow unconditionally.
    """
    rome = ZoneInfo("Europe/Rome")
    today = datetime.datetime.now(tz=rome).date()
    opening_hours = get_opening_hours(restaurant_id=restaurant_id)
    for i in range(1, 8):
        candidate = today + datetime.timedelta(days=i)
        if opening_hours and isinstance(opening_hours, dict):
            slot = opening_hours.get(_WEEKDAY_NAMES[candidate.weekday()], "")
            if _parse_opening_range(slot) is None:
                continue  # this day is closed
        return candidate, _WEEKDAY_IT[candidate.weekday()]
    # Fallback: tomorrow regardless
    tomorrow = today + datetime.timedelta(days=1)
    return tomorrow, _WEEKDAY_IT[tomorrow.weekday()]


def lookup_customer(phone: str) -> dict | None:
    """
    Cerca il cliente su Base44 per numero di telefono.
    Scarica tutti i Customer e filtra in Python (Base44 non supporta query params).
    Restituisce il primo match (o None). Usa lookup_all_customers per i duplicati.
    """
    cache_key = re.sub(r"[\s\-\(\)]", "", phone or "")
    now = time.monotonic()
    with _customer_lookup_cache_lock:
        _prune_customer_lookup_cache(now)
        cached = _customer_lookup_cache.get(cache_key)
        if cached:
            print(f"[Customer] Lookup cache hit per {mask_phone(cache_key)}")
            return cached[1]

    matches = _fetch_customers_by_phone(
        phone,
        timeout_seconds=_customer_lookup_http_timeout_seconds(),
    )
    customer = matches[0] if matches else None
    with _customer_lookup_cache_lock:
        _customer_lookup_cache[cache_key] = (
            time.monotonic(),
            customer,
            _customer_lookup_cache_ttl_for(customer),
        )
        _prune_customer_lookup_cache()
    return customer


def _fetch_customers_by_phone(
    phone: str,
    timeout_seconds: float = 10.0,
) -> list[dict]:
    """Restituisce TUTTI i record Customer con quel numero di telefono."""
    masked_phone = mask_phone(phone)
    print(f"[Customer] Inizio lookup per {masked_phone}")
    token = os.getenv("BASE44_TOKEN")
    if not token:
        print("[Customer] BASE44_TOKEN non configurato, lookup saltato")
        return []

    try:
        print(f"[Customer] GET {BASE44_CUSTOMER_URL} timeout={timeout_seconds}s")
        response = httpx.get(
            BASE44_CUSTOMER_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout_seconds,
        )
        print(f"[Customer] HTTP {response.status_code}")
        response.raise_for_status()
        data = response.json()
        entities = data.get("entities", []) if isinstance(data, dict) else data
        if not isinstance(entities, list):
            print(f"[Customer] Formato inatteso per entities: {type(entities).__name__}")
            return []
        print(f"[Customer] Totale record: {len(entities)}")
        phone_norm = re.sub(r"[\s\-\(\)]", "", phone)
        print(f"[Customer] Cerco phone normalizzato: {mask_phone(phone_norm)}")
        matches = []
        for record in entities:
            rec_phone_raw = record.get("phone") or ""
            rec_phone = re.sub(r"[\s\-\(\)]", "", rec_phone_raw)
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
        print(
            f"[Customer] Trovati {len(all_matches)} duplicati per "
            f"{mask_phone(phone)} — unificazione in corso"
        )
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
                f"[Customer] Aggiornato: {mask_name(full_name)} | ordini={new_total_orders} "
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
            print(f"[Customer] Creato: {mask_name(full_name)} | total_spend={payload['total_spend']}")
        except Exception as e:
            print(f"[Customer] Errore create: {type(e).__name__}: {e}")


_RESERVATION_INTENT_PATTERNS = re.compile(
    r"vorrei\s+prenotar|prenotar|prenot|ho\s+un\s+tavolo|posso\s+prenotar"
    r"|un\s+tavolo|tavolo\s+per|posto\s+per|cena\s+per|pranzo\s+per"
    r"|riservare?\s+un\s+tavolo|riservazione",
    re.IGNORECASE,
)


def detect_reservation_intent(message: str) -> bool:
    """Restituisce True se il messaggio indica una richiesta di prenotazione tavolo."""
    return bool(_RESERVATION_INTENT_PATTERNS.search(message))


def _fetch_tables_from_base44(required: bool = False) -> list[dict]:
    """Recupera tutti i tavoli configurati su Base44. Restituisce [] in caso di errore."""
    token = os.getenv("BASE44_TOKEN")
    if not token:
        if required:
            raise ReservationAvailabilityError("BASE44_TOKEN non configurato")
        return []
    try:
        response = httpx.get(
            BASE44_TABLE_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        response.raise_for_status()
        data = response.json()
        entities = data.get("entities", []) if isinstance(data, dict) else data
        tables = [t for t in (entities if isinstance(entities, list) else []) if t.get("status") != "maintenance"]
        print(f"[Table] Recuperati {len(tables)} tavoli da Base44")
        return tables
    except Exception as e:
        print(f"[Table] Errore fetch tavoli: {type(e).__name__}: {e}")
        if required:
            raise ReservationAvailabilityError(f"fetch tavoli fallito: {type(e).__name__}") from e
        return []


def _fetch_reservations_for_date(date: str, required: bool = False) -> list[dict]:
    """Recupera le prenotazioni per una data specifica."""
    token = os.getenv("BASE44_TOKEN")
    if not token:
        if required:
            raise ReservationAvailabilityError("BASE44_TOKEN non configurato")
        return []
    try:
        response = httpx.get(
            BASE44_RESERVATION_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        response.raise_for_status()
        data = response.json()
        entities = data.get("entities", []) if isinstance(data, dict) else data
        if not isinstance(entities, list):
            return []
        return [r for r in entities if r.get("date") == date and r.get("status") == "confermata"]
    except Exception as e:
        print(f"[Reservation] Errore fetch prenotazioni: {type(e).__name__}: {e}")
        if required:
            raise ReservationAvailabilityError(f"fetch prenotazioni fallito: {type(e).__name__}") from e
        return []


def _slot_overlaps(r_time_str: str, req_start: int, req_end: int, slot_minutes: int) -> bool:
    try:
        rh, rm = map(int, r_time_str.split(":"))
        r_start = rh * 60 + rm
        r_end = r_start + slot_minutes
        return r_start < req_end and r_end > req_start
    except Exception:
        return False


def _parse_minutes(time_str: str) -> int:
    parts = time_str.strip().split(":")
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"orario non valido: {time_str}")
    return hour * 60 + minute


def _format_minutes(total_minutes: int) -> str:
    total_minutes %= 24 * 60
    hour, minute = divmod(total_minutes, 60)
    return f"{hour:02d}:{minute:02d}"


def _parse_opening_range(slot: str | None) -> tuple[int, int] | None:
    if not slot or slot.strip().lower() == "closed":
        return None
    halves = slot.strip().split("-", 1)
    if len(halves) != 2:
        return None
    try:
        return _parse_minutes(halves[0]), _parse_minutes(halves[1])
    except Exception:
        return None


def validate_reservation_time(date: str, time: str, restaurant_id: str = "") -> tuple[bool, str | None]:
    """Valida data/orario prenotazione contro apertura e tempo corrente."""
    restaurant = load_restaurant(restaurant_id=restaurant_id)
    opening_hours = restaurant.get("opening_hours")
    if not opening_hours or not isinstance(opening_hours, dict):
        return True, None

    try:
        reservation_date = datetime.date.fromisoformat(date)
        requested_minutes = _parse_minutes(time)
    except Exception:
        return False, "Non ho capito bene giorno o orario della prenotazione. Può ripeterli?"

    rome = ZoneInfo("Europe/Rome")
    now = datetime.datetime.now(tz=rome)
    today = now.date()
    if reservation_date < today:
        return False, "Mi dispiace, quella data è già passata. Per quale altro giorno vuole prenotare?"

    weekday_idx = reservation_date.weekday()
    weekday_it = _WEEKDAY_IT[weekday_idx]
    day_range = _parse_opening_range(opening_hours.get(_WEEKDAY_NAMES[weekday_idx], ""))
    if not day_range:
        return False, f"Mi dispiace, {weekday_it} siamo chiusi. Per quale altro giorno vuole prenotare?"

    open_min, close_min = day_range
    candidate_minutes = requested_minutes
    display_close = close_min
    if close_min <= open_min:
        close_min += 24 * 60
        if candidate_minutes < open_min:
            candidate_minutes += 24 * 60

    if candidate_minutes < open_min or candidate_minutes >= close_min:
        return (
            False,
            f"Mi dispiace, {weekday_it} prendiamo prenotazioni dalle "
            f"{_format_minutes(open_min)} alle {_format_minutes(display_close)}.",
        )

    if reservation_date == today:
        now_minutes = now.hour * 60 + now.minute
        if close_min > 24 * 60 and now_minutes < open_min:
            now_minutes += 24 * 60
        if candidate_minutes <= now_minutes:
            return False, "Mi dispiace, quell'orario è già passato. A che ora vuole prenotare?"

    return True, None


def assign_table(
    date: str,
    time: str,
    party_size: int,
    all_reservations: list[dict] | None = None,
    slot_minutes: int = 90,
    tables: list[dict] | None = None,
) -> dict | None:
    """
    Assegna il tavolo migliore disponibile per lo slot richiesto.

    Strategia (in ordine):
    1. Tavolo singolo più piccolo che basta (least waste)
    2. Tavolo singolo con prolunga (extended_capacity)
    3. Combinazione di tavoli (combinable_with)

    Ritorna {table_id, table_name, extended, combined_tables} oppure None.
    """
    if tables is None:
        tables = _fetch_tables_from_base44()
    if not tables:
        print("[Table] Nessun tavolo configurato, skip assign_table")
        return None

    if all_reservations is None:
        all_reservations = _fetch_reservations_for_date(date)

    # Calcola intervallo richiesto in minuti
    try:
        h, m = map(int, time.split(":"))
        req_start = h * 60 + m
        req_end = req_start + slot_minutes
    except Exception:
        return None

    # Tavoli già occupati in questo slot
    occupied_ids: set[str] = set()
    for r in all_reservations:
        r_time = r.get("time") or ""
        if _slot_overlaps(r_time, req_start, req_end, slot_minutes):
            if r.get("table_id"):
                occupied_ids.add(r["table_id"])
            for tid in (r.get("combined_tables") or []):
                occupied_ids.add(tid)

    available = [t for t in tables if t.get("id") not in occupied_ids]
    avail_by_id = {t["id"]: t for t in available}

    # 1. Tavolo singolo (least waste)
    single_fits = [t for t in available if (t.get("capacity") or 0) >= party_size]
    if single_fits:
        best = min(single_fits, key=lambda t: t.get("capacity", 0))
        print(f"[Table] Assegnato tavolo singolo: {best['name']} cap={best.get('capacity')}")
        return {
            "table_id": best["id"],
            "table_name": best["name"],
            "extended": False,
            "combined_tables": [],
        }

    # 2. Tavolo con prolunga
    extendable = [
        t for t in available
        if t.get("extendable") and (t.get("extended_capacity") or 0) >= party_size
    ]
    if extendable:
        best = min(extendable, key=lambda t: t.get("extended_capacity", 0))
        print(f"[Table] Assegnato con prolunga: {best['name']} ext_cap={best.get('extended_capacity')}")
        return {
            "table_id": best["id"],
            "table_name": best["name"],
            "extended": True,
            "combined_tables": [],
        }

    # 3. Combinazione di tavoli
    for table in sorted(available, key=lambda t: t.get("capacity", 0), reverse=True):
        for partner_id in (table.get("combinable_with") or []):
            partner = avail_by_id.get(partner_id)
            if not partner:
                continue
            combined_cap = (
                table.get("combined_capacity")
                or (table.get("capacity", 0) + partner.get("capacity", 0))
            )
            if combined_cap >= party_size:
                name = f"{table['name']} + {partner['name']}"
                print(f"[Table] Assegnata combinazione: {name} cap={combined_cap}")
                return {
                    "table_id": table["id"],
                    "table_name": name,
                    "extended": False,
                    "combined_tables": [partner_id],
                }

    print(f"[Table] Nessun tavolo disponibile per party_size={party_size} date={date} time={time}")
    return None


def check_reservation_availability(
    date: str, time: str, party_size: int, restaurant_id: str = ""
) -> tuple[bool, str | None, dict | None]:
    """
    Controlla disponibilità per lo slot richiesto tramite assegnazione tavolo reale.
    Fallback su max_covers quando nessun Table è configurato su Base44.

    Ritorna (available, next_slot_suggestion, table_info).
    table_info è {table_id, table_name, extended, combined_tables} oppure None.
    """
    restaurant = load_restaurant(restaurant_id=restaurant_id)
    slot_minutes = int(restaurant.get("reservation_slot_minutes") or 90)

    # Tavoli e prenotazioni sono indipendenti: li carichiamo in parallelo per dimezzare
    # la latenza Base44 (worst-case 5s → ~5s invece di ~10s su errore di rete lento).
    with ThreadPoolExecutor(max_workers=2) as _pool:
        _f_reservations = _pool.submit(_fetch_reservations_for_date, date, required=True)
        _f_tables = _pool.submit(_fetch_tables_from_base44, required=True)
        all_reservations = _f_reservations.result()   # ri-lancia ReservationAvailabilityError se avvenuto
        tables = _f_tables.result()                   # idem
    tables_configured = bool(tables)
    table_info = assign_table(
        date,
        time,
        party_size,
        all_reservations,
        slot_minutes,
        tables=tables,
    )

    if table_info is not None:
        return True, None, table_info

    # Parse richiesto una sola volta; usato sia nel fallback max_covers che nella scansione next-slot.
    try:
        req_start = _parse_minutes(time)
    except Exception:
        return False, None, None
    req_end = req_start + slot_minutes

    if not tables_configured:
        # Fallback: logica max_covers originale
        max_covers = restaurant.get("max_covers")
        if not max_covers:
            return True, None, None
        max_covers = int(max_covers)

        booked = sum(
            int(r.get("party_size") or 0)
            for r in all_reservations
            if _slot_overlaps(r.get("time") or "", req_start, req_end, slot_minutes)
        )
        if booked + party_size <= max_covers:
            return True, None, None

    # Nessuna disponibilità nello slot richiesto: cerca i prossimi slot
    for delta in range(1, 4):
        next_start = req_start + delta * slot_minutes
        next_h, next_m = divmod(next_start, 60)
        next_time = f"{next_h:02d}:{next_m:02d}"
        next_time_valid, _ = validate_reservation_time(date, next_time)
        if not next_time_valid:
            continue
        if tables_configured:
            next_table = assign_table(
                date,
                next_time,
                party_size,
                all_reservations,
                slot_minutes,
                tables=tables,
            )
            if next_table is not None:
                return False, next_time, next_table
        else:
            max_covers = int(restaurant.get("max_covers") or 0)
            next_end = next_start + slot_minutes
            next_booked = sum(
                int(r.get("party_size") or 0)
                for r in all_reservations
                if _slot_overlaps(r.get("time") or "", next_start, next_end, slot_minutes)
            )
            if max_covers and next_booked + party_size <= max_covers:
                return False, next_time, None

    return False, None, None


def save_reservation_to_base44(
    customer_name: str,
    customer_phone: str | None,
    date: str,
    time: str,
    party_size: int,
    session_id: str,
    notes: str = "",
    table_id: str | None = None,
    table_name: str | None = None,
    combined_tables: list[str] | None = None,
    extended: bool = False,
    restaurant_id: str = "",
) -> str | None:
    """Salva la prenotazione su Base44. Ritorna l'id creato o None in caso di errore."""
    api_key = os.getenv("BASE44_API_KEY")
    if not api_key:
        print("[Reservation] BASE44_API_KEY non configurato, skip salvataggio")
        return None

    payload = {
        "customer_name": customer_name,
        "customer_phone": customer_phone,
        "date": date,
        "time": time,
        "party_size": party_size,
        "status": "confermata",
        "source": "telefono",
        "notes": notes,
        "session_id": session_id,
        "table_id": table_id,
        "table_name": table_name,
        "combined_tables": combined_tables or [],
        "extended": extended,
    }
    if restaurant_id:
        payload["restaurant_id"] = restaurant_id
    print(
        f"[Reservation] Salvo: customer={mask_name(customer_name)} "
        f"phone={mask_phone(customer_phone)} date={date} time={time} party={party_size} "
        f"table={table_name!r} session={session_id}"
    )
    try:
        response = httpx.post(
            BASE44_RESERVATION_URL,
            params={"api_key": api_key},
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        response.raise_for_status()
        reservation_id = response.json().get("id")
        print(f"[Reservation] Salvata con id={reservation_id}")
        return reservation_id
    except Exception as e:
        print(f"[Reservation] Errore salvataggio: {type(e).__name__}: {e}")
        return None


def send_reservation_sms(
    customer_name: str,
    customer_phone: str | None,
    date: str,
    time: str,
    party_size: int,
    table_name: str | None = None,
) -> str:
    """Invia SMS di conferma prenotazione. Ritorna stringa di stato."""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not all([account_sid, auth_token]):
        print("[ReservationSMS] Credenziali Twilio mancanti, skip")
        return "skip:credenziali_mancanti"

    phone = _normalize_phone(customer_phone)
    if not phone:
        print("[ReservationSMS] Numero non valido o fisso, skip")
        return "skip:numero_non_valido"

    raw_sms_from = os.getenv("TWILIO_NUMBER")
    if not raw_sms_from:
        print("[ReservationSMS] TWILIO_NUMBER non configurato, skip")
        return "skip:TWILIO_NUMBER_mancante"

    sms_from = raw_sms_from.removeprefix("whatsapp:")
    pizzeria_phone = os.getenv("PIZZERIA_PHONE", "")
    pizzeria_name = os.getenv("PIZZERIA_NAME", "La Pizzeria")

    # Formato data leggibile: 2026-05-21 → "21/05/2026"
    try:
        y, mo, d = date.split("-")
        date_str = f"{d}/{mo}/{y}"
    except Exception:
        date_str = date

    persons_str = f"{party_size} {'persona' if party_size == 1 else 'persone'}"
    table_part = f", {table_name}" if table_name else ""
    parts = [
        f"{pizzeria_name} ✅",
        f"Prenotazione confermata — {customer_name}, {date_str} alle {time}, {persons_str}{table_part}.",
    ]
    if pizzeria_phone:
        parts.append(f"Per modifiche chiama il {pizzeria_phone}.")
    body = "\n".join(parts)

    print(f"[ReservationSMS] To={mask_phone(phone)} From={mask_phone(sms_from)}")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    try:
        resp = httpx.post(
            url,
            auth=(account_sid, auth_token),
            data={"From": sms_from, "To": phone, "Body": body},
            timeout=10,
        )
        resp.raise_for_status()
        print(f"[ReservationSMS] Inviato con successo a {mask_phone(phone)}")
        return f"sms_inviato:{resp.status_code}"
    except httpx.HTTPStatusError as e:
        print(f"[ReservationSMS] Errore HTTP {e.response.status_code}")
        return f"sms_errore:HTTP_{e.response.status_code}"
    except Exception as e:
        print(f"[ReservationSMS] Errore inatteso {type(e).__name__}: {e}")
        return f"sms_errore:{type(e).__name__}"


MODEL_NAME = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
LLM_RESPONSE_FORMAT = {"type": "json_object"}
_http_client: httpx.Client | None = None
_openai_client: OpenAI | None = None


def get_openai_client() -> OpenAI:
    """Restituisce un client OpenAI lazy con keep-alive.

    Il client viene creato solo al primo uso, così l'app può importare e avviarsi
    anche quando la variabile d'ambiente manca in un contesto locale/test.
    """
    global _http_client, _openai_client
    if _openai_client is not None:
        return _openai_client

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY non configurata")

    _http_client = httpx.Client(
        limits=httpx.Limits(
            max_connections=10,
            max_keepalive_connections=5,
            keepalive_expiry=60.0,
        ),
        timeout=httpx.Timeout(connect=3.0, read=12.0, write=5.0, pool=2.0),
    )
    _openai_client = OpenAI(
        api_key=api_key,
        http_client=_http_client,
    )
    return _openai_client


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
      "size": string,
      "add_ingredients": [string],
      "remove_ingredients": [string],
      "temperature": "fredda" | "calda" | ""
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
- "set_kg_temperature"
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
- QUANTITÀ E UNITÀ DI VENDITA:
  * Voci SENZA "[al kg]": quantity è un intero ≥ 1 (numero di pezzi/trance).
    "due trance" → quantity=2, "una" → quantity=1.
  * Voci con "[al kg, €X/kg]": quantity è il peso in kg (numero decimale).
    Conversioni: "un etto"→0.1, "due etti"→0.2, "tre etti"→0.3, "quattro etti"→0.4,
    "cinque etti"→0.5, "mezzo chilo"→0.5, "un chilo"→1.0, "un chilo e mezzo"→1.5,
    "due chili"→2.0, "100 grammi"→0.1, "200 grammi"→0.2, "500 grammi"→0.5.
    Se il peso NON è specificato dal cliente per una voce al kg, usa quantity=0
    (il backend chiederà al cliente quanta ne vuole).
- quantity deve essere ≥ 0.
- Always use the exact pizza name as it appears in the MENU.
- If the user explicitly requests a pizza name that is NOT in the MENU, you must STILL include that pizza in items so the backend can validate it.
- Never drop an explicitly requested pizza just because it is not present in the MENU.
- If the user says something like "vorrei una gustosa", "una diavola", "due capricciose", you must extract that pizza request even if it is not in the MENU.
- If no pizza is clearly mentioned, return an empty items array.
- REGOLA ASSOLUTA — MESSAGGIO CORRENTE ONLY: Analizza ESCLUSIVAMENTE l'ultimo messaggio dell'utente (l'ultimo turno). NON guardare i messaggi precedenti per estrarre pizze. Gli item già ordinati sono salvati dal backend — non riestrarre mai pizze dalla storia. Se l'ultimo messaggio non contiene nuove pizze (es. è un orario, un nome, una conferma, una risposta sì/no), restituisci items=[]. Esempi: "alle 20:30" → items=[], "Mario" → items=[], "sì" → items=[], "una capricciosa" → items=[Capricciosa].
- pickup_time should be a simple string in 24h format like "20:30" when present. If the user says "prima possibile", "il prima possibile", "subito" or similar expressions meaning "as soon as possible", output "prima_possibile" as pickup_time. TIME RULE: this is an evening-only pizzeria (18:30-22:30). Hours 6-11 with no explicit AM/PM → ALWAYS add 12: "alle 7"→"19:00", "alle 8"→"20:00", "alle 8:30"→"20:30", "alle 9"→"21:00". Hours 12-22 → use as-is. Never output a time before 18:00 unless the customer explicitly says "di mattina".
- customer_name should be extracted when clearly present.
- Each item must always include "add_ingredients" and "remove_ingredients".
- If there are no ingredient changes, use empty arrays.
- REGOLA CRITICA SUL NOME PIZZA: pizza_name deve contenere ESCLUSIVAMENTE il nome base della pizza
  così come appare nel MENU (es. "Margherita", "Capricciosa", "4 Formaggi").
  Tutto ciò che segue "con" o "senza" sono MODIFICATORI e vanno in add_ingredients o remove_ingredients,
  mai nel pizza_name. NON scrivere mai pizza_name come "Margherita con patatine" o "Capricciosa senza olive".
  Esempi corretti:
  * "una margherita con patatine fritte" → pizza_name="Margherita", add_ingredients=["patatine fritte"]
  * "una capricciosa senza olive con würstel" → pizza_name="Capricciosa", remove_ingredients=["olive"], add_ingredients=["würstel"]
  * "una margherita integrale con uovo" → pizza_name="Margherita", dough_type="integrale", add_ingredients=["uovo"]
  * "una diavola con würstel e patatine" → pizza_name="Diavola", add_ingredients=["würstel", "patatine"]
  * "una margherita con le patatine fritte impasto integrale" → pizza_name="Margherita", dough_type="integrale", add_ingredients=["patatine fritte"]
  * "capricciosa senza funghi integrale" → pizza_name="Capricciosa", remove_ingredients=["funghi"], dough_type="integrale"
- If the user says "senza pomodoro", put "pomodoro" in remove_ingredients.
- If the user says "con patatine", put "patatine" in add_ingredients.
- If the user says "bianca", interpret it as remove_ingredients = ["pomodoro"] unless a better pizza base is clearly specified.
- Never invent ingredients not mentioned by the user.
- If the customer names a pizza that does NOT appear in the MENU (e.g. "una würstel", "una melanzane e gorgonzola", "una pizza al salmone"), use pizza_name="Personalizzata" and put the unknown pizza name as the FIRST element of add_ingredients, followed by any other ingredients mentioned.
  * "una würstel" → pizza_name="Personalizzata", add_ingredients=["würstel"]
  * "una melanzane e gorgonzola" → pizza_name="Personalizzata", add_ingredients=["melanzane", "gorgonzola"]
  * "una pizza con würstel e patatine" → pizza_name="Personalizzata", add_ingredients=["würstel", "patatine"]
  * "una bianca con prosciutto" → pizza_name="Personalizzata", add_ingredients=["prosciutto"], remove_ingredients=["pomodoro"]
- If the user describes a pizza by ingredients without naming it, use pizza_name="Personalizzata" with the ingredients in add_ingredients.
- If using "Personalizzata", still fill add_ingredients and remove_ingredients correctly. Never use "Pizza personalizzata" — always just "Personalizzata".
- If the user says "bianca", interpret it as remove_ingredients = ["pomodoro"].
- If the user says "rossa", do not add anything automatically unless specific ingredients are mentioned.
- SIZE MODIFIERS — il campo size indica la dimensione della pizza (solo per pizze al PEZZO, NON per voci al kg):
  * "normale" → default, nessun modificatore di prezzo.
  * "mini" → pizza più piccola (-€1.50 sul prezzo base). Parole chiave: "mini", "piccola".
  * "doppio" → pizza con doppio impasto (+€2.00 extra). Parole chiave: "doppio impasto", "doppia pasta", "doppia", "doppio".
  Se il cliente non specifica, usa size="normale".
  NON mettere "mini" o "doppio" nel pizza_name o in add_ingredients — vanno SOLO nel campo size.
  Esempi:
  * "una margherita mini" → pizza_name="Margherita", size="mini"
  * "una capricciosa doppio impasto" → pizza_name="Capricciosa", size="doppio"
  * "una diavola doppia" → pizza_name="Diavola", size="doppio"
  * "una margherita doppia pasta integrale" → pizza_name="Margherita", size="doppio", dough_type="integrale"
  * "una margherita" → pizza_name="Margherita", size="normale"

DIMENSIONE TRANCIO (solo per voci "[al kg]"):
- Le voci al kg hanno due formati di taglio: "piena" (trancio 15×20 cm) e "mezza" (trancio 7.5×10 cm).
- Se il cliente dice "piena", "intera", "grande" → size="piena".
- Se dice "mezza", "mezza porzione", "piccola" → size="mezza".
- Se NON specificata, usa size="normale" (il backend chiederà piena o mezza).
- NON confondere "mezza" come dimensione trancio con "mezza" come orario (es. "alle otto e mezza").
- Esempi:
  * "300g di porchetta piena" → pizza_name="Porchetta", quantity=0.3, size="piena"
  * "200g di pizza bianca mezza" → pizza_name="Pizza Bianca", quantity=0.2, size="mezza"
  * "mezzo chilo di porchetta" → quantity=0.5, size="normale" ("mezzo" qui è il peso, non la dimensione)
- Se risponde SOLO con "piena" o "mezza" senza pizze nuove → intent="set_kg_size", items=[].
- NON usare "piena"/"mezza" nel pizza_name o in add_ingredients — va SOLO nel campo size.

Intent rules:
- Use "add_items" when the user is adding pizzas.
- Use "set_customer_name" when the user is mainly providing the customer name.
- Use "set_pickup_time" when the user is mainly providing the pickup time.
- Use "modify_items" when the user is clearly correcting previously mentioned pizzas but the action is ambiguous.
- Use "remove_items" when the user wants to remove one or more specific pizzas already present in the order (togliete, rimuovi, non voglio più, cancella la ...).
- Use "replace_items" when the user wants to replace previous pizzas with new ones.
- Use "cancel_order" when the user wants to cancel the whole order including name and time (annulla l'ordine, voglio annullare).
- Use "clear_cart" when the user wants to reset only the pizzas and start over, keeping name and pickup time (cancella tutto e ricominciamo, ricominciamo da capo, azzera le pizze, voglio ricominciare).
- Use "set_kg_temperature" when the user is answering a temperature question (fredda/calda/da portar via/da mangiare) with NO new pizza items. Return items=[].
- Use "set_kg_size" when the user is answering a slice-size question (piena/mezza/intera/metà porzione) for a kg item with NO new pizza items. Return items=[].
- Use "ask_kg_price" when the user is asking about the price of a kg item (quanto costa, quanto viene, che prezzo, il prezzo) with NO new pizza items. Return items=[].
- Use "unknown" if the message is unclear.

TEMPERATURA PER PIZZE AL KG:
- Le voci segnate "[al kg]" nel menu possono essere servite fredde (da asporto) o calde (scaldate subito).
- Se il cliente specifica la temperatura per una voce al kg, usa temperature="fredda" o temperature="calda".
- Se NON specificata, usa temperature="" (il backend usa il default della sessione).
- Se il cliente dice "fredde"/"fredda"/"da portar via"/"da asporto" → temperature="fredda".
- Se dice "calde"/"calda"/"scaldata"/"da mangiare subito" → temperature="calda".
- Se risponde SOLO con una preferenza temperatura senza pizze (es. "calde per favore") → intent="set_kg_temperature", items=[].

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


def _build_slim_system_prompt(
    menu_items: list[dict],
    dough_items: list[dict] | None,
    state: str,
) -> str:
    """Prompt compatto per stati dove l'estrazione pizza è secondaria.
    Omette ingredienti e le ~140 righe di regole impasto/size — ~10x più corto
    del prompt completo, risparmiando 100-200ms di latenza OpenAI su input tokens.
    """
    menu_lines = [
        f'- {item["name"]}{"  [al kg]" if item.get("sale_unit") == "kg" else ""}'
        for item in menu_items
    ]
    menu_text = "\n".join(menu_lines) if menu_lines else "No menu items available."

    dough_codes = [d["code"] for d in (dough_items or [])]
    dough_section = f'\nDOUGH TYPES (codes): {", ".join(dough_codes)}\n' if dough_codes else ""

    if state == "collecting_name":
        primary_goal = (
            "PRIMARY GOAL: extract customer_name. "
            "The customer is providing their name. They may also add/modify pizzas."
        )
        intent_hint = (
            'Use "set_customer_name" if mainly providing a name, '
            '"add_items" if adding pizzas, "unknown" if unclear.'
        )
    else:  # collecting_pickup_time
        primary_goal = (
            'PRIMARY GOAL: extract pickup_time (format "HH:MM" or "prima_possibile"). '
            "The customer is providing a pickup time. They may also add/modify pizzas."
        )
        intent_hint = (
            'Use "set_pickup_time" if mainly providing a time, '
            '"add_items" if adding pizzas, "unknown" if unclear.\n'
            '"prima_possibile" for "subito"/"prima possibile"/"appena posso".\n'
            "CRITICAL TIME RULES (pizzeria is open 18:30-22:30, evenings only):\n"
            "- Hours 6-11 with no explicit AM/PM marker → ALWAYS convert to evening: "
            'add 12. "alle 7"→19:00, "alle 8"→20:00, "alle 8:30"→20:30, "alle 9"→21:00.\n'
            "- Hours 12-22 → use as-is.\n"
            "- Never output a time before 18:00 unless the customer explicitly says 'di mattina'.\n"
            "- Output format: HH:MM (24h, zero-padded)."
        )

    return f"""You extract takeaway pizza orders from Italian customer messages.
{primary_goal}

MENU:
{menu_text}
{dough_section}
Return ONLY valid JSON — no markdown:
{{"intent": string, "customer_name": string|null, "pickup_time": string|null, "items": [{{"pizza_name": string, "dough_type": string, "quantity": number, "size": string, "add_ingredients": [string], "remove_ingredients": [string]}}]}}

Rules:
- CURRENT MESSAGE ONLY: extract only from the current message, never from history.
- dough_type: one of the codes above, default "classica".
- pizza_name: exact MENU name, or "Personalizzata" for unknown pizzas.
- If the user provides only a name or time with no pizzas, return items=[].
- {intent_hint}
"""


def _get_system_prompt(
    menu_items: list[dict],
    dough_items: list[dict] | None,
    state: str = "collecting_items",
    restaurant_id: str = "",
) -> str:
    # Stati leggeri: prompt compatto keyed by (restaurant_id, state)
    if state in ("collecting_name", "collecting_pickup_time"):
        slim_key = restaurant_id
        slim_by_state = _system_prompt_slim_cache.get(slim_key, {})
        if state in slim_by_state:
            return slim_by_state[state]
        prompt = _build_slim_system_prompt(menu_items, dough_items, state)
        slim_by_state[state] = prompt
        _system_prompt_slim_cache[slim_key] = slim_by_state
        print(f"[LLM] Slim prompt per stato={state!r} restaurant_id={restaurant_id!r} ({len(prompt)} chars)")
        return prompt

    # collecting_items: prompt completo, cache per restaurant_id
    cached_full = _system_prompt_cache.get(restaurant_id)
    if cached_full is not None:
        return cached_full

    menu_lines = []
    for item in menu_items:
        sale_unit = item.get("sale_unit", "piece")
        price = item.get("price", 0.0)
        if sale_unit == "kg":
            ings_str = f" [al kg, €{price:.2f}/kg]"
        else:
            ingredients = item.get("ingredients") or []
            ings_str = f' [{", ".join(ingredients)}]' if ingredients else ""
        menu_lines.append(f'- {item["name"]}{ings_str}')
    menu_text = "\n".join(menu_lines) if menu_lines else "No menu items available."

    dough_lines = []
    for d in (dough_items or []):
        surcharge = d.get("surcharge", 0.0)
        surcharge_text = f"+€{surcharge:.2f}" if surcharge > 0 else "incluso"
        dough_lines.append(f'- {d["name"]} (code: {d["code"]}) | supplemento: {surcharge_text}')
    dough_text = "\n".join(dough_lines)

    full_prompt = build_system_prompt(menu_text, dough_text)
    _system_prompt_cache[restaurant_id] = full_prompt
    print(f"[LLM] System prompt completo costruito (restaurant_id={restaurant_id!r}, {len(full_prompt)} chars)")
    return full_prompt


def prewarm_system_prompt(restaurant_id: str = "") -> None:
    """Costruisce e cachea il system prompt all'avvio del server."""
    menu_items = load_menu_from_base44(restaurant_id=restaurant_id)
    dough_items = load_doughs()
    if not menu_items:
        print("[LLM] Prewarm system prompt: menu vuoto, skip")
        return
    _get_system_prompt(menu_items, dough_items, state="collecting_items", restaurant_id=restaurant_id)
    _get_system_prompt(menu_items, dough_items, state="collecting_name", restaurant_id=restaurant_id)
    _get_system_prompt(menu_items, dough_items, state="collecting_pickup_time", restaurant_id=restaurant_id)


# Alias fonetici: parole comuni trascritte male dal riconoscimento vocale → termine corretto.
# Applicati al testo del cliente PRIMA della chiamata OpenAI.
PIZZA_ALIASES: dict[str, str] = {
    "booster": "würstel",
    "vurstel": "würstel",
    "wurstel": "würstel",
    "wurster": "würstel",
    "formaggio verde": "gorgonzola",
    "verde": "gorgonzola",
    "bondola": "mortadella",
}

_ALLOWED_INTENTS = {
    "add_items",
    "set_customer_name",
    "set_pickup_time",
    "modify_items",
    "remove_items",
    "replace_items",
    "cancel_order",
    "clear_cart",
    "set_kg_temperature",
    "set_kg_size",
    "ask_kg_price",
    "unknown",
}
_ALLOWED_SIZES = {"normale", "mini", "doppio", "piena", "mezza"}


class _ExtractedItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pizza_name: str = ""
    dough_type: str = "classica"
    # float supports both piece counts (1, 2, …) and kg weights (0.1, 0.5, …).
    # 0.0 is a sentinel meaning "kg item, weight not yet specified".
    quantity: float = Field(default=1.0, ge=0.0)
    size: str = "normale"
    add_ingredients: list[str] = Field(default_factory=list)
    remove_ingredients: list[str] = Field(default_factory=list)
    # For kg items: "fredda"|"calda"|"" (empty = use session default)
    temperature: str = ""

    @field_validator("pizza_name", "dough_type", "size", mode="before")
    @classmethod
    def _coerce_string(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("temperature", mode="before")
    @classmethod
    def _coerce_temperature(cls, value: Any) -> str:
        if value is None:
            return ""
        t = str(value).lower().strip()
        if re.search(r"cald", t):
            return "calda"
        if re.search(r"fredd", t):
            return "fredda"
        return ""

    @field_validator("quantity", mode="before")
    @classmethod
    def _coerce_quantity(cls, value: Any) -> float:
        try:
            quantity = float(value)
        except (TypeError, ValueError):
            return 1.0
        return max(quantity, 0.0)

    @field_validator("add_ingredients", "remove_ingredients", mode="before")
    @classmethod
    def _coerce_ingredients(cls, value: Any) -> list[str]:
        if value is None:
            return []
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, list):
            return []
        result = []
        for item in values:
            if item is None:
                continue
            text = str(item).strip()
            if text:
                result.append(text)
        return result


class _ExtractedOrderPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    intent: str = "unknown"
    customer_name: str | None = None
    pickup_time: str | None = None
    items: list[_ExtractedItem] = Field(default_factory=list)

    @field_validator("intent", mode="before")
    @classmethod
    def _coerce_intent(cls, value: Any) -> str:
        intent = "" if value is None else str(value).strip()
        return intent if intent in _ALLOWED_INTENTS else "unknown"

    @field_validator("customer_name", "pickup_time", mode="before")
    @classmethod
    def _coerce_optional_string(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("items", mode="before")
    @classmethod
    def _coerce_items(cls, value: Any) -> list[Any]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]


def _fallback_extracted_payload() -> dict:
    return {
        "intent": "add_items",
        "items": [],
        "customer_name": None,
        "pickup_time": None,
        "_llm_fallback": True,
    }


def _parse_llm_json_payload(raw_text: str) -> dict | None:
    text = (raw_text or "").strip()
    if not text:
        print("[OpenAI] JSON vuoto dal modello")
        return None

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        decoder = json.JSONDecoder()
        object_start = text.find("{")
        if object_start >= 0:
            try:
                parsed, object_end = decoder.raw_decode(text[object_start:])
            except json.JSONDecodeError:
                print(
                    f"[OpenAI] JSON non valido: {type(e).__name__}: {e}; "
                    f"raw={text[:500]!r}"
                )
                return None
            if isinstance(parsed, dict):
                trailing = text[object_start + object_end:].strip()
                if object_start > 0 or trailing:
                    print("[OpenAI] JSON recuperato da risposta con testo extra")
                return parsed
        print(
            f"[OpenAI] JSON non valido: {type(e).__name__}: {e}; "
            f"raw={text[:500]!r}"
        )
        return None

    if not isinstance(parsed, dict):
        print(f"[OpenAI] JSON top-level non oggetto: {type(parsed).__name__}")
        return None
    return parsed


def _normalize_extracted_payload(payload: Any, dough_items: list[dict] | None = None) -> dict:
    """Valida e normalizza l'output LLM prima che entri nello stato ordine."""
    if not isinstance(payload, dict):
        print(f"[OpenAI] Payload non oggetto: {type(payload).__name__}")
        return _fallback_extracted_payload()

    try:
        parsed = _ExtractedOrderPayload.model_validate(payload)
    except ValidationError as e:
        print(f"[OpenAI] Payload non valido: {e}")
        return _fallback_extracted_payload()

    valid_dough_codes = {"classica"}
    valid_dough_codes.update(
        str(item.get("code", "")).strip()
        for item in (dough_items or [])
        if str(item.get("code", "")).strip()
    )

    items = []
    for item in parsed.items:
        data = item.model_dump()
        if not data["pizza_name"]:
            continue
        if data["dough_type"] not in valid_dough_codes:
            print(f"[OpenAI] dough_type non valido: {data['dough_type']!r} → classica")
            data["dough_type"] = "classica"
        if data["size"] not in _ALLOWED_SIZES:
            data["size"] = "normale"
        items.append(data)

    return {
        "intent": parsed.intent,
        "customer_name": parsed.customer_name,
        "pickup_time": parsed.pickup_time,
        "items": items,
    }


def _apply_aliases(text: str) -> str:
    """Sostituisce gli alias fonetici nel testo (case-insensitive, parola intera)."""
    for alias, canonical in PIZZA_ALIASES.items():
        text = re.sub(rf"\b{re.escape(alias)}\b", canonical, text, flags=re.IGNORECASE)
    return text


def extract_order_from_text(
    message: str,
    menu_items: list[dict],
    dough_items: list[dict] | None = None,
    state: str = "collecting_items",
    existing_items: list[dict] | None = None,
    customer_name: str | None = None,
    restaurant_id: str = "",
) -> dict:
    system_prompt = _get_system_prompt(menu_items, dough_items, state, restaurant_id=restaurant_id)

    normalized = _apply_aliases(message)
    if normalized != message:
        print(
            "[LLM] Alias applicati: "
            f"before={describe_text_for_log(message)} "
            f"after={describe_text_for_log(normalized)}"
        )

    # Per collecting_items inietta il contesto di sessione nel messaggio utente
    # (non nel system prompt, che rimane statico per attivare il prefix caching OpenAI).
    if state == "collecting_items":
        items_str = json.dumps(existing_items or [], ensure_ascii=False)
        nome_str = customer_name or "non ancora raccolto"
        user_content = (
            f"[Stato sessione]\n"
            f"Items già ordinati: {items_str}\n"
            f"Nome cliente: {nome_str}\n\n"
            f"[Trascrizione cliente]\n"
            f"{normalized}"
        )
    else:
        user_content = normalized

    input_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    print(f"[LLM] Estrazione da messaggio corrente: {describe_text_for_log(normalized)}")

    # Chat Completions (non Responses API) per attivare il prefix-caching automatico:
    # OpenAI cachea il system message prefix dopo la prima chiamata (~5 min TTL),
    # riducendo la latenza da ~4s a ~300-500ms sulle chiamate successive.
    # La Responses API usa invece previous_response_id per il caching, che non
    # si applica al pattern stateless che usiamo qui.
    _t0 = time.time()
    try:
        response = get_openai_client().chat.completions.create(
            model=MODEL_NAME,
            max_tokens=200,
            messages=input_messages,
            response_format=LLM_RESPONSE_FORMAT,
            temperature=0,
            timeout=8,
        )
    except Exception as e:
        _elapsed_ms = int((time.time() - _t0) * 1000)
        record_latency(
            "llm",
            "extract_order",
            _elapsed_ms,
            state=state,
            model=MODEL_NAME,
            result="error",
            error_type=type(e).__name__,
        )
        print(f"[OpenAI] elapsed={_elapsed_ms}ms stato={state} ERROR={type(e).__name__}: {e} → fallback Ok!")
        return _fallback_extracted_payload()
    _elapsed_ms = int((time.time() - _t0) * 1000)
    record_latency(
        "llm",
        "extract_order",
        _elapsed_ms,
        state=state,
        model=MODEL_NAME,
        result="ok",
    )
    _usage = response.usage
    _tokens_in = getattr(_usage, "prompt_tokens", -1)
    _cached = getattr(getattr(_usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
    print(f"[OpenAI] elapsed={_elapsed_ms}ms stato={state} tokens_in={_tokens_in} cached={_cached}")

    raw_text = (response.choices[0].message.content or "").strip()
    parsed = _parse_llm_json_payload(raw_text)
    if parsed is None:
        return _fallback_extracted_payload()

    parsed = _normalize_extracted_payload(parsed, dough_items)
    if parsed.get("_llm_fallback"):
        return parsed

    # Normalizza dough_type → pizza_type per compatibilità con il DB locale
    for item in parsed["items"]:
        add_ing = item.get("add_ingredients", [])
        rem_ing = item.get("remove_ingredients", [])
        dough_log = item.get("dough_type", "classica")
        size = item.get("size", "normale").lower().strip()
        if size not in ("normale", "mini", "doppio", "piena", "mezza"):
            size = "normale"
        item["size"] = size
        add_count = len(add_ing) if isinstance(add_ing, list) else 0
        remove_count = len(rem_ing) if isinstance(rem_ing, list) else 0
        print(
            "[LLM] estratto: "
            f"pizza={describe_text_for_log(str(item.get('pizza_name') or ''))} "
            f"dough={dough_log!r} size={size!r} "
            f"add_count={add_count} remove_count={remove_count}"
        )
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
            print(
                "[LLM] Nome corretto: "
                f"from={describe_text_for_log(item['pizza_name'])} "
                f"to={describe_text_for_log(canonical)}"
            )
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
