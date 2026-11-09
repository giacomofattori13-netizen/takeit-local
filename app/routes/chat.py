import copy
import datetime
import json
import os
import random
import threading
import time
import unicodedata
import uuid
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

from typing import Annotated, Any
from zoneinfo import ZoneInfo

from difflib import SequenceMatcher

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db import engine as _db_engine
from app.db import get_session
from app.models import (
    MenuItem,
    Order,
    OrderItem,
    OrderSideEffect,
    ConversationSession,
    ConversationLog,
)
from app.privacy import describe_text_for_log, mask_name, mask_phone
from app.schemas import ChatRequest, ChatResponse, ChatStartResponse
from app.services.conversation_service import (
    SERVICE_UNAVAILABLE_MESSAGE,
    build_closed_message,
    ensure_restaurant_config,
    extract_order_from_text,
    load_menu_from_base44,
    get_proposable_menu,
    get_sold_out_item_names,
    load_restaurant,
    restaurant_flag,
    load_doughs,
    send_whatsapp_confirmation,
    send_owner_alert,
    build_order_not_saved_alert,
    build_new_order_owner_message,
    get_dough_surcharge,
    is_dough_available,
    is_agent_active,
    is_reservations_enabled,
    is_custom_pizza_enabled,
    is_phone_orders_next_day_only,
    get_opening_hours,
    get_opening_range_text,
    _WEEKDAY_IT,
    INGREDIENT_EXTRA_PRICE,
    SIZE_MINI_DISCOUNT,
    SIZE_DOPPIO_SURCHARGE,
    _PIZZA_TYPE_TO_DOUGH,
    ORDER_VOCAB_NAME_BLOCKERS,
    get_agent_greeting,
    validate_pickup_time,
    resolve_pickup_time,
    get_next_open_day,
    earliest_pickup_minutes_today,
    first_pickup_slot,
    reservation_slots_left_today,
    lookup_customer,
    upsert_customer,
    ReservationAvailabilityError,
    _OFF_MENU_COUNT,
    classify_call_intent,
    is_ingredient,
    is_only_greeting,
    mentions_product,
    mentions_table,
    strip_restaurant_name,
    check_reservation_availability,
    validate_reservation_time,
    save_reservation_to_base44,
    update_reservation_on_base44,
    get_large_party_settings,
    customer_reservation_notes,
    send_reservation_sms,
)
from app.services.order_sync import rome_today, save_order_to_base44
from app.telemetry import record_latency

router = APIRouter(prefix="/chat", tags=["chat"])

SessionDep = Annotated[Session, Depends(get_session)]
_CUSTOMER_LOOKUP_EXECUTOR = ThreadPoolExecutor(max_workers=4)
_ORDER_SIDE_EFFECTS_EXECUTOR = ThreadPoolExecutor(max_workers=4)
_ORDER_SIDE_EFFECT_TIMERS: dict[int, threading.Timer] = {}
_ORDER_SIDE_EFFECT_TIMERS_LOCK = threading.Lock()
_SIDE_EFFECT_MAX_ATTEMPTS = 5
_SIDE_EFFECT_RETRY_BASE_SECONDS = 30.0
_SIDE_EFFECT_RETRY_MAX_SECONDS = 15 * 60.0
_CUSTOMER_LOOKUP_TIMEOUT_DEFAULT_SECONDS = 1.0


def _positive_float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, "").strip())
    except ValueError:
        return default
    return value if value > 0 else default


def _customer_lookup_timeout_seconds() -> float:
    return _positive_float_env(
        "CUSTOMER_LOOKUP_TIMEOUT_SECONDS",
        _CUSTOMER_LOOKUP_TIMEOUT_DEFAULT_SECONDS,
    )


def _resolve_customer_lookup_future(lookup_future, phone: str | None) -> dict | None:
    if lookup_future is None:
        return None
    try:
        return lookup_future.result(timeout=_customer_lookup_timeout_seconds())
    except FutureTimeoutError:
        lookup_future.cancel()
        print(f"[Customer] Lookup timeout per {mask_phone(phone)}, saluto senza profilo")
    except Exception as exc:
        print(f"[Customer] Lookup errore per {mask_phone(phone)}: {type(exc).__name__}: {exc}")
    return None


def build_missing_item_message(session: Session, item: dict, restaurant_id: str = "") -> tuple[str, list[str]]:
    pizza_name = item["pizza_name"]
    is_sg = pizza_name.upper().endswith("(SG)")

    same_name_statement = select(MenuItem).where(MenuItem.name == pizza_name)
    same_name_item = session.exec(same_name_statement).first()

    if same_name_item:
        # La pizza esiste ma non è disponibile
        if is_sg:
            base_name = pizza_name[:-4].strip()
            return f'{base_name} non è disponibile in versione senza glutine.', []
        return f'{pizza_name} non è disponibile al momento.', []

    # La pizza non esiste: prova correzione typo
    typo_suggestions = get_typo_correction_suggestions(
        session=session,
        requested_name=pizza_name,
        limit=2,
        restaurant_id=restaurant_id,
    )
    if typo_suggestions:
        suggestions_text = ", ".join(typo_suggestions)
        return (
            f'{pizza_name} non è presente nel menu. '
            f'Forse intendevi {suggestions_text}?',
            typo_suggestions,
        )

    if is_sg:
        base_name = pizza_name[:-4].strip()
        return f'{base_name} non è disponibile in versione senza glutine.', []

    suggestions = get_available_menu_suggestions(
        session=session,
        exclude_name=pizza_name,
        limit=3,
    )
    if suggestions:
        suggestions_text = ", ".join(suggestions)
        return (
            f'{pizza_name} non è presente nel menu. '
            f'Posso proporti {suggestions_text}.',
            suggestions,
        )

    return f'{pizza_name} non è presente nel menu.', []

def get_available_menu_suggestions(
    session: Session,
    exclude_name: str | None = None,
    limit: int = 3,
) -> list[str]:
    statement = select(MenuItem).where(MenuItem.available == True)  # noqa: E712
    items = session.exec(statement).all()

    seen = set()
    suggestions = []

    for item in items:
        if exclude_name and item.name.lower() == exclude_name.lower():
            continue

        if item.name not in seen:
            seen.add(item.name)
            suggestions.append(item.name)

        if len(suggestions) >= limit:
            break

    return suggestions

def merge_items(existing_items: list[dict], new_items: list[dict]) -> list[dict]:
    merged = existing_items.copy()

    for new_item in new_items:
        found = False
        for existing_item in merged:
            same_name = existing_item["pizza_name"] == new_item["pizza_name"]
            same_extras = (
                existing_item.get("add_ingredients", []) == new_item.get("add_ingredients", [])
                and existing_item.get("remove_ingredients", []) == new_item.get("remove_ingredients", [])
                and existing_item.get("size", "normale") == new_item.get("size", "normale")
            )
            if same_name and same_extras:
                if existing_item["pizza_type"] == new_item["pizza_type"]:
                    # Stesso impasto: accumula quantità
                    existing_item["quantity"] += new_item["quantity"]
                else:
                    # Impasto diverso: sostituisce invece di aggiungere un duplicato.
                    # Es. capricciosa/classica + capricciosa/integrale → capricciosa/integrale.
                    existing_item["pizza_type"] = new_item["pizza_type"]
                    existing_item["dough_type"] = new_item.get("dough_type", "classica")
                    existing_item["size"] = new_item.get("size", "normale")
                    existing_item["quantity"] = new_item["quantity"]
                found = True
                break

        if not found:
            merged.append(new_item)

    return merged

# Parole che aggiungono davvero: "altri due", "aggiungi", "anche", "un'altra"
_EXPLICIT_ADD_RE = re.compile(r"\b(?:altr[aoie]|aggiung\w*|anche|ancora|in\s+piu)\b")


def _repeated_cart_item(cart: list[dict], new_item: dict, same_quantity: bool = True) -> dict | None:
    """La voce del carrello che `new_item` ripete: stesso prodotto, stessa quantità (se
    `same_quantity`), stesse aggiunte e nessun dettaglio in contrasto (porzione,
    caldo/freddo, unità). None se è una voce nuova."""
    def same_or_missing(a, b, missing=("",)) -> bool:
        return a == b or a in missing or b in missing

    for existing in cart:
        if (
            existing.get("pizza_name") != new_item.get("pizza_name")
            or existing.get("pizza_type") != new_item.get("pizza_type")
            or (same_quantity and float(existing.get("quantity") or 0) != float(new_item.get("quantity") or 0))
            or float(new_item.get("quantity") or 0) <= 0
            or (new_item.get("add_ingredients") or []) not in ([], existing.get("add_ingredients") or [])
            or (new_item.get("remove_ingredients") or []) not in ([], existing.get("remove_ingredients") or [])
        ):
            continue
        if existing.get("sale_unit") == "kg" or new_item.get("sale_unit") == "kg":
            unit_ok = same_or_missing(existing.get("order_unit", ""), new_item.get("order_unit", ""))
            size_ok = same_or_missing(existing.get("size"), new_item.get("size"), ("", None, "normale"))
            temperature_ok = same_or_missing(existing.get("temperature", ""), new_item.get("temperature", ""))
            if unit_ok and size_ok and temperature_ok:
                return existing
        elif existing.get("size", "normale") == new_item.get("size", "normale"):
            return existing
    return None


def _fill_repeated_item(existing: dict, new_item: dict) -> None:
    """Dettagli detti ripetendo il prodotto ("2 tranci di zucca interi") che nel carrello
    mancavano."""
    if existing.get("sale_unit") != "kg":
        return
    if not existing.get("order_unit") and new_item.get("order_unit"):
        existing["order_unit"] = new_item["order_unit"]
    if existing.get("size") not in ("piena", "mezza") and new_item.get("size") in ("piena", "mezza"):
        existing["size"] = new_item["size"]
        _clear_kg_attempts(existing, "portion")
    if existing.get("temperature") not in ("calda", "fredda") and new_item.get("temperature") in ("calda", "fredda"):
        existing["temperature"] = new_item["temperature"]
        _clear_kg_attempts(existing, "temperature")


def remove_items_from_order(existing_items: list[dict], items_to_remove: list[dict]) -> list[dict]:
    updated_items = []

    for existing_item in existing_items:
        remaining_quantity = existing_item["quantity"]

        for item_to_remove in items_to_remove:
            same_item = (
                existing_item["pizza_name"] == item_to_remove["pizza_name"]
                and existing_item["pizza_type"] == item_to_remove["pizza_type"]
            )

            if same_item:
                remaining_quantity -= item_to_remove["quantity"]

        if remaining_quantity > 0:
            updated_items.append({**existing_item, "quantity": remaining_quantity})

    return updated_items

def replace_items_in_order(existing_items: list[dict], new_items: list[dict], session: Session) -> list[dict]:
    valid_existing_items = keep_only_valid_existing_items(session, existing_items)
    return merge_items([], new_items) if not valid_existing_items else new_items

def cancel_order_items() -> list[dict]:
    return []

def apply_intent_to_items(
    existing_items: list[dict],
    new_items: list[dict],
    intent: str,
) -> list[dict]:
    if intent in {"modify_items", "replace_items"}:
        return new_items

    if intent == "add_items":
        return merge_items(existing_items, new_items)

    if intent == "remove_items":
        return remove_items_from_order(existing_items, new_items)

    if intent == "cancel_order":
        return cancel_order_items()

    return existing_items

# Voci al taglio (sale_unit="kg"): il cliente ordina a peso (order_unit="kg",
# quantity = kg) oppure a tranci (order_unit="tranci", quantity = numero di tranci,
# size = porzione "piena"/"mezza"). order_unit vuoto = quantità non ancora detta.
_COUNT_WORDS = {1: "un", 2: "due", 3: "tre", 4: "quattro", 5: "cinque", 6: "sei",
                7: "sette", 8: "otto", 9: "nove", 10: "dieci"}
_PORTION_WORDS = {"piena": ("intero", "interi"), "mezza": ("mezzo", "mezzi")}
_SLICE_TEMPERATURE_WORDS = {"calda": ("caldo", "caldi"), "fredda": ("freddo", "freddi")}


def kg_order_unit(item: dict) -> str:
    """'kg', 'tranci' o '' (quantità non detta). Le voci salvate prima di
    order_unit con un peso valgono come ordini a peso."""
    unit = item.get("order_unit")
    if unit in ("kg", "tranci"):
        return unit
    return "kg" if float(item.get("quantity") or 0) > 0 else ""


def slice_count(item: dict) -> int:
    return int(round(float(item.get("quantity") or 0)))


def format_weight_spoken(kg: float) -> str:
    """0.1 → 'un etto', 0.3 → 'tre etti', 0.5 → 'mezzo chilo', 1.5 → 'un chilo e mezzo'."""
    grams = round(kg * 1000)
    if grams == 500:
        return "mezzo chilo"
    if grams % 1000 == 0:
        kilos = grams // 1000
        return "un chilo" if kilos == 1 else f"{_COUNT_WORDS.get(kilos, kilos)} chili"
    if grams % 1000 == 500:
        kilos = grams // 1000
        return f"{'un chilo' if kilos == 1 else f'{_COUNT_WORDS.get(kilos, kilos)} chili'} e mezzo"
    if grams < 1000 and grams % 100 == 0:
        hectos = grams // 100
        return "un etto" if hectos == 1 else f"{_COUNT_WORDS.get(hectos, hectos)} etti"
    return f"{grams} grammi"


def format_kg_item(item: dict) -> str:
    """Voce al taglio per il cliente: quantità (peso o tranci con porzione),
    gusto e caldo/freddo.

    Mostra esattamente ciò che viene salvato: un campo non detto dal cliente
    compare come "da definire", mai con un valore predefinito.
    """
    name = item["pizza_name"].lower()
    unit = kg_order_unit(item)
    temperature = item.get("temperature")
    if unit == "tranci":
        count = slice_count(item)
        plural = count != 1
        noun = "tranci" if plural else "trancio"
        portion = _PORTION_WORDS.get(item.get("size"))
        if portion:
            line = f"{_COUNT_WORDS.get(count, count)} {noun} {portion[plural]} di {name}"
        else:
            line = f"{_COUNT_WORDS.get(count, count)} {noun} di {name}, porzione da definire"
        temp = _SLICE_TEMPERATURE_WORDS.get(temperature)
        temp_text = temp[plural] if temp else "caldo o freddo da definire"
    elif unit == "kg":
        line = f"{format_weight_spoken(float(item['quantity']))} di {name}"
        temp_text = temperature if temperature in ("calda", "fredda") else "caldo o freddo da definire"
    else:
        line = f"{name}, quantità da definire"
        temp_text = temperature if temperature in ("calda", "fredda") else "caldo o freddo da definire"
    line = f"{line}, {temp_text}"
    if item.get("add_ingredients"):
        line += " con " + ", ".join(item["add_ingredients"])
    if item.get("remove_ingredients"):
        line += " senza " + ", ".join(item["remove_ingredients"])
    return line


def format_single_item_for_customer(item: dict) -> str:
    quantity = item["quantity"]
    pizza_name = item["pizza_name"]
    pizza_type = item["pizza_type"]
    add_ingredients = item.get("add_ingredients", [])
    remove_ingredients = item.get("remove_ingredients", [])
    size = item.get("size", "normale")

    if item.get("sale_unit") == "kg":
        return format_kg_item(item)

    # Pizza fuori menu: è una Margherita base con il nome detto come aggiunta
    is_margherita_base = pizza_name in ("Margherita", "Personalizzata", "Pizza personalizzata")

    is_plain_margherita = (
        is_margherita_base
        and not add_ingredients
        and not remove_ingredients
    )

    is_margherita_with_variants = (
        is_margherita_base
        and (add_ingredients or remove_ingredients)
    )

    is_bianca_style = (
        is_margherita_base
        and "pomodoro" in remove_ingredients
    )

    # Caso 1: margherita normale
    if is_plain_margherita:
        if quantity == 1:
            line = "una margherita"
        else:
            line = f"{quantity} margherite"

    # Caso 2: margherita con varianti -> risposta naturale cliente
    elif is_margherita_with_variants:
        visible_remove_ingredients = [
            ingredient for ingredient in remove_ingredients
            if ingredient != "pomodoro"
        ]

        if is_bianca_style:
            if quantity == 1:
                if add_ingredients:
                    line = f"una bianca con {', '.join(add_ingredients)}"
                else:
                    line = "una bianca"
            else:
                if add_ingredients:
                    line = f"{quantity} bianche con {', '.join(add_ingredients)}"
                else:
                    line = f"{quantity} bianche"
        else:
            line = "una margherita" if quantity == 1 else f"{quantity} margherite"
            if add_ingredients:
                line += f" con {_format_pizza_list(add_ingredients)}"

        if visible_remove_ingredients:
            line += " senza " + ", ".join(visible_remove_ingredients)

    # Caso 3: altre pizze menu
    else:
        if quantity == 1:
            line = f"una {pizza_name.lower()}"
        else:
            line = f"{quantity} {pluralize_pizza_name(pizza_name, quantity)}"

        if add_ingredients:
            line += " con " + ", ".join(add_ingredients)

        if remove_ingredients:
            line += " senza " + ", ".join(remove_ingredients)

    if size == "mini":
        line += " (mini)"
    elif size == "doppio":
        line += " (doppio impasto)"

    if pizza_type == "Senza glutine":
        line += " senza glutine"

    return line

def format_items_for_customer(items: list[dict]) -> str:
    if not items:
        return ""

    return ", ".join(format_single_item_for_customer(item) for item in items)

def pluralize_pizza_name(name: str, quantity: int) -> str:
    if quantity == 1:
        return name.lower()

    irregulars = {
        "Margherita": "margherite",
        "Diavola": "diavole",
        "Capricciosa": "capricciose",
        "Quattro Formaggi": "quattro formaggi",
    }

    if name in irregulars:
        return irregulars[name]

    return name.lower()

def split_valid_and_invalid_items(
    session: Session,
    items: list[dict],
) -> tuple[list[dict], list[dict], list[str]]:
    valid_items = []
    invalid_items = []
    missing_messages = []

    for item in items:
        # Pizza libera: sempre valida, usa prezzo Margherita come base
        if item["pizza_name"] == "Personalizzata":
            valid_items.append(item)
            continue

        statement = select(MenuItem).where(
            MenuItem.name == item["pizza_name"],
            MenuItem.pizza_type == item["pizza_type"],
        )
        menu_item = session.exec(statement).first()

        # Fallback: il tipo di impasto (integrale, napoletana…) è una preferenza,
        # non una voce separata nel DB. Se non trovato con pizza_type specifico,
        # cerca per solo nome (qualsiasi pizza_type disponibile).
        if not menu_item:
            fallback = select(MenuItem).where(MenuItem.name == item["pizza_name"])
            menu_item = session.exec(fallback).first()
            if menu_item:
                item["pizza_type"] = menu_item.pizza_type

        if not menu_item or not menu_item.available:
            invalid_items.append(item)
            missing_messages.append(build_missing_item_message(session, item))
        else:
            valid_items.append(item)

    return valid_items, invalid_items, missing_messages

_WORD_TO_DIGIT = {
    "uno": "1", "una": "1", "due": "2", "tre": "3", "quattro": "4",
    "cinque": "5", "sei": "6", "sette": "7", "otto": "8", "nove": "9",
}


def normalize_pizza_name(name: str) -> str:
    """Minuscolo → rimuovi accenti → numeri in lettere → cifre."""
    s = name.lower().strip()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    for word, digit in _WORD_TO_DIGIT.items():
        s = re.sub(rf"\b{word}\b", digit, s)
    return s


def fuzzy_find_pizza(
    requested_name: str,
    pizza_type: str,
    db_session: "Session",
) -> tuple["MenuItem | None", float]:
    """
    Cerca la pizza più simile nel DB usando distanza di Levenshtein (SequenceMatcher).
    Priorità: stessa pizza_type; fallback: qualsiasi tipo.
    Ritorna (menu_item, similarity) dove similarity è 0.0–1.0.
    """
    norm_req = normalize_pizza_name(requested_name)
    all_items = db_session.exec(select(MenuItem).where(MenuItem.available == True)).all()  # noqa: E712

    best_item: "MenuItem | None" = None
    best_sim = 0.0

    for mi in all_items:
        norm_mi = normalize_pizza_name(mi.name)
        sim = SequenceMatcher(None, norm_req, norm_mi).ratio()
        # Preferisci stesso pizza_type con un piccolo boost
        if mi.pizza_type == pizza_type:
            sim = min(sim + 0.02, 1.0)
        if sim > best_sim:
            best_sim = sim
            best_item = mi

    return best_item, best_sim


_DONE_SIGNALS = {
    "è tutto", "e' tutto", "basta così", "basta cosi", "basta",
    "ho finito", "per ora è tutto", "per ora e' tutto", "ok è tutto",
    "okay è tutto", "è tutto qui", "per adesso è tutto", "fine",
}

_ORDINALS_IT = {
    2: "seconda", 3: "terza", 4: "quarta", 5: "quinta",
    6: "sesta", 7: "settima", 8: "ottava", 9: "nona", 10: "decima",
}

_NUMBER_WORDS = {
    "una": 1, "un": 1, "uno": 1,
    "due": 2, "tre": 3, "quattro": 4, "cinque": 5, "sei": 6, "sette": 7,
}

_LOCAL_NAME_BLOCKERS = {
    "ok", "si", "sì", "io",
} | ORDER_VOCAB_NAME_BLOCKERS

_LOCAL_PICKUP_BLOCKERS = {
    "pizza", "pizze", "margherita", "diavola", "capricciosa", "aggiungi", "metti",
    "toglimi", "rimuovi", "senza glutine", "impasto", "mini", "doppio", "nome",
    "sono", "mi chiamo",
}

_ITALIAN_HOUR_WORDS = {
    "una": 1,
    "uno": 1,
    "due": 2,
    "tre": 3,
    "quattro": 4,
    "cinque": 5,
    "sei": 6,
    "sette": 7,
    "otto": 8,
    "nove": 9,
    "dieci": 10,
    "undici": 11,
    "dodici": 12,
    "diciotto": 18,
    "diciannove": 19,
    "venti": 20,
    "ventuno": 21,
    "ventidue": 22,
    "ventitre": 23,
}


def extract_intended_quantity(message: str) -> int | None:
    """
    Estrae il numero totale di pizze dichiarate in frasi come
    'vorrei 2 pizze' o 'ordino tre pizze d'asporto'.
    Viene usato SOLO quando il messaggio non contiene nomi di pizze specifiche.
    """
    msg = message.lower()
    pattern = r"(\b\d+\b|\b(?:una|un|uno|due|tre|quattro|cinque|sei|sette)\b)\s+pizze?\b"
    match = re.search(pattern, msg)
    if not match:
        return None
    word = match.group(1).strip()
    if word.isdigit():
        return int(word)
    return _NUMBER_WORDS.get(word)


def is_done_signal(message: str) -> bool:
    """True se il cliente segnala che ha finito di ordinare."""
    msg = message.lower().strip()
    return any(signal in msg for signal in _DONE_SIGNALS)


def _normalize_for_match(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in normalized if unicodedata.category(c) != "Mn")


_RESERVATION_CANCEL_RE = re.compile(
    r"\b(?:"
    r"no|annulla|annullare|cancella|cancellare|stop|"
    r"non\s+(?:confermo|va\s+bene|e\s+giusto|e\s+corretto|ok|okay|certo|esatto|perfetto)"
    r")\b",
    re.IGNORECASE,
)
_RESERVATION_CONFIRM_RE = re.compile(
    r"\b(?:si|sì|ok|okay|va\s+bene|confermo|certo|esatto|perfetto|yes)\b",
    re.IGNORECASE,
)


def _reservation_confirmation_intent(message: str) -> str | None:
    normalized = _normalize_for_match(message)
    if _RESERVATION_CANCEL_RE.search(normalized):
        return "cancel"
    if _RESERVATION_CONFIRM_RE.search(normalized):
        return "confirm"
    return None


def _contains_any_word(text: str, words: set[str]) -> bool:
    return any(re.search(rf"\b{re.escape(word)}\b", text) for word in words)


def _format_customer_name(name: str) -> str:
    return " ".join(part[:1].upper() + part[1:].lower() for part in name.split())


def _apply_evening_pickup_hour_rule(hour: int, text: str, evening_only: bool = True) -> int:
    """Regola locale coerente col prompt: 6-11 senza mattina esplicita => sera,
    solo per i locali aperti solo la sera."""
    has_morning_marker = re.search(r"\b(?:mattina|di\s+mattina)\b", text) is not None
    has_evening_marker = re.search(r"\b(?:sera|di\s+sera|stasera)\b", text) is not None
    if 1 <= hour <= 11 and has_evening_marker:
        return hour + 12
    if evening_only and 6 <= hour <= 11 and not has_morning_marker:
        return hour + 12
    return hour


def _spoken_day_part(text: str) -> str | None:
    """"mattina" o "sera" se il cliente lo dice ("di sera", "stasera", "di pomeriggio")."""
    normalized = _normalize_for_match(text)
    if re.search(r"\bmattin[ao]\b", normalized):
        return "mattina"
    if re.search(r"\b(?:sera|stasera|pomeriggio)\b", normalized):
        return "sera"
    return None


def _normalize_spoken_time(text: str) -> str:
    """Ripara le storpiature più comuni dello STT sugli orari:
    "l'otto"/"all'otto" → "alle otto", "allotto"/"ottoe" → "alle otto"/"otto",
    "8pm" → "8". "ottobre" resta com'è."""
    text = re.sub(r"\b(?:all|l)'\s*", "alle ", text)
    text = re.sub(r"\b(alle|all|al)?otto(?!bre)[a-z]*", lambda m: "alle otto" if m.group(1) else "otto", text)
    return re.sub(r"\b8[a-z]+\b", "8", text)


_YES_ANSWER_RE = re.compile(
    r"\b(?:si|ok|okay|va\s+bene|certo|esatto|giusto|perfetto|confermo|d'accordo|benissimo|volentieri)\b"
)
_NO_ANSWER_RE = re.compile(r"\b(?:no|non|sbagliato)\b")


def _yes_no_answer(message: str) -> str | None:
    """"yes" / "no" per una risposta a una proposta sì/no; None se ambigua o assente."""
    normalized = _normalize_for_match(message)
    is_yes = _YES_ANSWER_RE.search(normalized) is not None
    is_no = _NO_ANSWER_RE.search(normalized) is not None
    if is_yes != is_no:
        return "yes" if is_yes else "no"
    return None


# ── Domande obbligatorie non capite: nome e orario ──────────────────────────
# Mai la stessa domanda due volte di fila: 1° fallimento frase diversa, 2° proposta
# sì/no, 3° valore di ripiego e ordine da verificare (needs_review). Tentativi,
# proposta e motivi stanno in ConversationSession.slot_attempts_json.
PICKUP_TIME_MISS_MESSAGE = "Non ho capito l'orario. Mi dica per esempio: alle otto, oppure alle otto e mezza."
NAME_MISS_MESSAGE = "Non ho capito il nome. Me lo ripete, per favore?"
NAME_MISS_NO_GUESS_MESSAGE = "Mi dica solo il nome, per favore. Per esempio: Mario."
UNKNOWN_CUSTOMER_NAME = "Cliente"
PICKUP_TIME_ASK_AFTER_NO_MESSAGE = "A che ora le va bene?"
PICKUP_TIME_UNDEFINED_MESSAGE = "Segno l'ordine, il locale la richiama per l'orario. Grazie, a presto!"


def _slot_state(conversation: ConversationSession) -> dict:
    try:
        data = json.loads(conversation.slot_attempts_json or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _store_slot_state(conversation: ConversationSession, data: dict) -> None:
    conversation.slot_attempts_json = json.dumps(data, ensure_ascii=False)


def _clear_slot_attempts(conversation: ConversationSession, slot: str) -> None:
    data = _slot_state(conversation)
    if data.pop(slot, None) is not None:
        _store_slot_state(conversation, data)


def _log_pickup_time_turn(recognized: str, conversation: ConversationSession) -> None:
    """Una riga per ogni risposta alla domanda sull'orario, senza il testo del cliente.
    recognized: "regole" | "llm" | "nessuno"."""
    misses = int(_slot_state(conversation).get("pickup_time", {}).get("misses") or 0)
    print(f"[PickupTime] riconosciuto={recognized} fallimenti={misses} stato={conversation.state}")


def _slot_review_reasons(conversation: ConversationSession) -> list[str]:
    return list(_slot_state(conversation).get("review") or [])


def _name_guess(message: str) -> str | None:
    """Il nome come è stato trascritto (max 2 parole), da proporre con sì/no."""
    raw = re.sub(r"[^A-Za-zÀ-ÖØ-öø-ÿ' ]+", " ", message)
    raw = re.sub(r"^\s*(?:sono|mi chiamo|chiamami|il nome [eè]|a nome di|nome|per)\s+", "", raw, flags=re.IGNORECASE)
    words = [word for word in raw.split() if len(word) >= 2][:2]
    if not words or _yes_no_answer(" ".join(words)):
        return None
    return _format_customer_name(" ".join(words))


def _extract_local_customer_name(message: str) -> str | None:
    """Fast path prudente: accetta solo risposte che sembrano essere un nome puro."""
    raw = re.sub(r"[.!?,;:]+$", "", message.strip())
    if not raw:
        return None

    candidate = re.sub(
        r"^(?:sono|mi chiamo|chiamami|il nome\s+(?:e|è)|nome|a nome di|per)\s+",
        "",
        raw,
        flags=re.IGNORECASE,
    ).strip()
    candidate = re.sub(r"\b(?:grazie|per favore)\b", "", candidate, flags=re.IGNORECASE).strip()
    candidate = re.sub(r"\s+", " ", candidate)
    normalized = _normalize_for_match(candidate)

    if (
        not candidate
        or any(char.isdigit() for char in candidate)
        or _contains_any_word(normalized, _LOCAL_NAME_BLOCKERS)
        or not re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ' -]{2,60}", candidate)
    ):
        return None

    words = candidate.split()
    if len(words) > 4:
        return None

    return _format_customer_name(candidate)


# "e mezza", "e un quarto", "meno un quarto" dopo l'ora (gruppi: e/meno, parola)
_PICKUP_MINUTE_WORDS = r"(?:\s+(e|meno)\s+(mezza|mezzo|un\s+quarto|quarto))"


def _apply_minute_words(hour: int, minute: int, sign: str | None, word: str | None) -> tuple[int, int]:
    """(8, 0, "e", "mezza") → (8, 30); (8, 0, "meno", "un quarto") → (7, 45)."""
    if not word:
        return hour, minute
    if sign == "meno":
        return (hour - 1) % 24, 45
    return hour, 30 if word in {"mezza", "mezzo"} else 15


def _extract_local_pickup_time(message: str, evening_only: bool = True) -> str | None:
    """Estrae orari semplici senza LLM quando il bot sta già chiedendo solo l'orario.

    evening_only: le ore 6-11 senza "mattina" valgono come sera (locali aperti solo la sera).
    """
    normalized = _normalize_for_match(message).strip()
    compact = _normalize_spoken_time(re.sub(r"[.!?,;:]+$", "", normalized))
    if not compact or _contains_any_word(compact, _LOCAL_PICKUP_BLOCKERS):
        return None

    if re.search(r"\b(?:prima\s+possibile|appena\s+possibile|subito|asap)\b", compact):
        return "prima_possibile"

    numeric = re.search(
        r"(?:\b(?:alle|le|ore|verso|per(?:\s+le)?)\s+)?(\d{1,2})(?:[:.](\d{1,2}))?"
        rf"{_PICKUP_MINUTE_WORDS}?\b",
        compact,
    )
    if numeric:
        has_time_marker = re.search(r"\b(?:alle|le|ore|verso|per(?:\s+le)?)\b", compact)
        is_only_time = re.fullmatch(
            rf"\d{{1,2}}(?:[:.]\d{{1,2}})?{_PICKUP_MINUTE_WORDS}?",
            compact,
        )
        if numeric.group(2) or has_time_marker or is_only_time:
            hour = int(numeric.group(1))
            minute = int(numeric.group(2) or 0)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                hour, minute = _apply_minute_words(hour, minute, numeric.group(3), numeric.group(4))
                hour = _apply_evening_pickup_hour_rule(hour, compact, evening_only)
                return f"{hour}:{minute:02d}"

    word_pattern = "|".join(sorted(_ITALIAN_HOUR_WORDS, key=len, reverse=True))
    word_match = re.search(
        rf"(?:\b(?:alle|le|ore|verso|per(?:\s+le)?)\s+)?\b({word_pattern})\b"
        rf"{_PICKUP_MINUTE_WORDS}?",
        compact,
    )
    if word_match:
        has_time_marker = re.search(r"\b(?:alle|le|ore|verso|per(?:\s+le)?)\b", compact)
        is_only_time = re.fullmatch(
            rf"({word_pattern}){_PICKUP_MINUTE_WORDS}?",
            compact,
        )
        if has_time_marker or is_only_time:
            hour, minute = _apply_minute_words(
                _ITALIAN_HOUR_WORDS[word_match.group(1)], 0, word_match.group(2), word_match.group(3),
            )
            hour = _apply_evening_pickup_hour_rule(hour, compact, evening_only)
            return f"{hour}:{minute:02d}"

    return None


_ITALIAN_MONTHS = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
    "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
    "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}

_ITALIAN_WEEKDAYS = {
    "lunedì": 0, "lunedi": 0, "martedì": 1, "martedi": 1,
    "mercoledì": 2, "mercoledi": 2, "giovedì": 3, "giovedi": 3,
    "venerdì": 4, "venerdi": 4, "sabato": 5, "domenica": 6,
}


def _extract_reservation_date(message: str) -> str | None:
    """Estrae una data dal testo e la restituisce in formato YYYY-MM-DD."""
    _dt = datetime
    normalized = _normalize_for_match(message).strip()
    today = _dt.datetime.now(tz=ZoneInfo("Europe/Rome")).date()

    if re.search(r"\b(oggi|stasera|sta\s+sera|questa\s+sera|stanotte)\b", normalized):
        return today.isoformat()
    if re.search(r"\b(domani)\b", normalized):
        return (today + _dt.timedelta(days=1)).isoformat()
    if re.search(r"\b(dopodomani)\b", normalized):
        return (today + _dt.timedelta(days=2)).isoformat()

    # Una data detta conta più del giorno della settimana: se non coincidono lo chiede
    # prima chi chiama (_weekday_date_conflict)
    dated = _date_in_message(normalized, today, bare_day=True)
    if dated:
        return dated.isoformat()

    # Giorno della settimana: "sabato", "domenica prossima" ecc.
    for it_name, weekday in _ITALIAN_WEEKDAYS.items():
        if re.search(rf"\b{re.escape(it_name)}\b", normalized):
            days_ahead = (weekday - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            return (today + _dt.timedelta(days=days_ahead)).isoformat()

    # "21 maggio", "21/05", "21/05/2026", "21-05-2026"
    match = re.search(
        r"(\d{1,2})[/\-\s](\d{1,2})(?:[/\-\s](\d{2,4}))?",
        normalized,
    )
    if match:
        day = int(match.group(1))
        month = int(match.group(2))
        year_raw = match.group(3)
        year = int(year_raw) if year_raw else today.year
        if year < 100:
            year += 2000
        try:
            d = _dt.date(year, month, day)
            if d < today:
                d = _dt.date(year + 1, month, day)
            return d.isoformat()
        except ValueError:
            pass

    # "21 maggio"
    match2 = re.search(
        r"(\d{1,2})\s+(" + "|".join(_ITALIAN_MONTHS.keys()) + r")\b",
        normalized,
    )
    if match2:
        day = int(match2.group(1))
        month = _ITALIAN_MONTHS[match2.group(2)]
        year = today.year
        try:
            d = _dt.date(year, month, day)
            if d < today:
                d = _dt.date(year + 1, month, day)
            return d.isoformat()
        except ValueError:
            pass

    return None


def _extract_party_size(message: str) -> int | None:
    """Estrae il numero di persone dal testo."""
    normalized = _normalize_for_match(message)
    word_to_num = {
        "una": 1, "uno": 1, "un": 1, "due": 2, "tre": 3, "quattro": 4,
        "cinque": 5, "sei": 6, "sette": 7, "otto": 8, "nove": 9, "dieci": 10,
    }
    # Cifra numerica
    m = re.search(r"\b(\d+)\b", normalized)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 30:
            return n
    # Parola
    for word, n in word_to_num.items():
        if re.search(rf"\b{word}\b", normalized):
            return n
    return None


_PARTY_WORDS = {
    "una": 1, "uno": 1, "un": 1, "due": 2, "tre": 3, "quattro": 4, "cinque": 5, "sei": 6,
    "sette": 7, "otto": 8, "nove": 9, "dieci": 10, "undici": 11, "dodici": 12,
    "tredici": 13, "quattordici": 14, "quindici": 15, "venti": 20,
}
_PARTY_NUMBER = r"(\d{1,2}|" + "|".join(_PARTY_WORDS) + r")"
# "tavolo per N": senza "un/una" ("un tavolo per una cena") e senza mese o minuti dopo
# ("tavolo per 12 ottobre", "per 8:30"); "per le 8", "per il 12", "per sabato" non hanno il numero.
_TABLE_FOR_NUMBER = r"(\d{1,2}|" + "|".join(w for w in _PARTY_WORDS if w not in ("un", "una", "uno")) + r")"
_PARTY_IN_SENTENCE_RE = re.compile(
    rf"\b{_PARTY_NUMBER}\s+person[ae]\b"
    rf"|\bsiamo\s+in\s+{_PARTY_NUMBER}\b"
    rf"|\btavol[oi]\s+(?:per|da)\s+{_TABLE_FOR_NUMBER}\b"
    rf"(?!\s*(?:[:.]\d|(?:{'|'.join(_ITALIAN_MONTHS)})\b))"
)
_TIME_IN_SENTENCE_RE = re.compile(
    r"\b(?:alle|per\s+le|verso\s+le|ore)\s+\d{1,2}(?:[:.]\d{2})?(?:\s+e\s+mezza)?"
)


def _extract_reservation_details(message: str) -> dict:
    """Giorno, ora e persone già detti nella frase con cui il cliente chiede il tavolo
    ("un tavolo per domani alle 20 per 4 persone"). Solo i dati espliciti: l'ora
    deve seguire "alle"/"per le", il numero di persone segue _PARTY_IN_SENTENCE_RE."""
    normalized = _normalize_for_match(message)
    details: dict = {}
    date = _extract_reservation_date(message)
    if date:
        details["date"] = date
    time_match = _TIME_IN_SENTENCE_RE.search(normalized)
    if time_match:
        local_time = _extract_local_pickup_time(time_match.group(0))
        if local_time:
            details["time"] = local_time
    party = _extract_party_in_sentence(normalized)
    if party:
        details["party_size"] = party
    return details


def _extract_party_in_sentence(message: str) -> int | None:
    """Numero di persone detto esplicitamente in una frase qualsiasi ("un tavolo per 4",
    "per 12 persone", "siamo in 6"); None se il numero non è chiaramente di persone."""
    match = _PARTY_IN_SENTENCE_RE.search(_normalize_for_match(message))
    if not match:
        return None
    token = next(g for g in match.groups() if g)
    party = int(token) if token.isdigit() else _PARTY_WORDS[token]
    return party if party >= 1 else None


def _reservation_day_error(date_str: str, restaurant_id: str = "") -> str | None:
    """Messaggio se nel giorno scelto non si può prenotare: locale chiuso quel giorno,
    oppure oggi senza più orari. None se il giorno va bene (qualsiasi giorno futuro aperto)."""
    _dt = datetime
    try:
        day = _dt.date.fromisoformat(date_str)
    except (TypeError, ValueError):
        return None
    today = _dt.datetime.now(tz=ZoneInfo("Europe/Rome")).date()
    if day < today:
        return "Mi dispiace, quella data è già passata. Per quale altro giorno vuole prenotare?"
    if get_opening_range_text(restaurant_id, day) is None and load_restaurant(restaurant_id=restaurant_id).get("opening_hours"):
        return f"Mi dispiace, {_WEEKDAY_IT[day.weekday()]} siamo chiusi. Per quale altro giorno vuole prenotare?"
    if day == today and not reservation_slots_left_today(restaurant_id):
        return "Per stasera non è più possibile prenotare. Per quale altro giorno?"
    return None


def _format_reservation_date_it(date_str: str) -> str:
    """Formatta una data YYYY-MM-DD in italiano con il giorno: 'stasera, giovedì 1 ottobre',
    'domani, venerdì 2 ottobre', 'sabato 21 maggio 2027' (anno solo se diverso)."""
    _dt = datetime
    months_it = [
        "", "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
        "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
    ]
    try:
        d = _dt.date.fromisoformat(date_str)
    except Exception:
        return date_str
    today = _dt.datetime.now(tz=ZoneInfo("Europe/Rome")).date()
    text = f"{_WEEKDAY_IT[d.weekday()]} {d.day} {months_it[d.month]}"
    if d.year != today.year:
        text += f" {d.year}"
    if d == today:
        return f"stasera, {text}"
    if d == today + _dt.timedelta(days=1):
        return f"domani, {text}"
    return text


def _reservation_date_retry_message(restaurant_id: str = "") -> str:
    """Richiesta di ripetere la data, con esempi vicini a oggi e in giorni di apertura:
    'domani' e il prossimo sabato (o, se è chiuso, il primo giorno aperto).

    Si cerca da 2 a 7 giorni da oggi: così il giorno della settimana detto dal cliente
    ripetendo l'esempio è proprio quella data."""
    _dt = datetime
    today = _dt.datetime.now(tz=ZoneInfo("Europe/Rome")).date()

    def is_open(day) -> bool:
        return _reservation_day_error(day.isoformat(), restaurant_id) is None

    open_days = [today + _dt.timedelta(days=n) for n in range(2, 8)]
    open_days = [day for day in open_days if is_open(day)]
    examples = []
    if is_open(today + _dt.timedelta(days=1)):
        examples.append("domani")
    if open_days:
        day = next((d for d in open_days if d.weekday() == 5), open_days[0])
        examples.append(_format_reservation_date_it(day.isoformat()))
    question = "Non ho capito la data. Può dirmi il giorno?"
    return f"{question} Per esempio {', o '.join(examples)}." if examples else question


def _apply_reservation_table_info(res_data: dict, table_info: dict | None) -> None:
    """Aggiorna il riepilogo prenotazione con l'ultima assegnazione tavolo."""
    if not table_info:
        for key in ("table_id", "table_name", "combined_tables", "extended"):
            res_data.pop(key, None)
        return

    res_data["table_id"] = table_info.get("table_id")
    res_data["table_name"] = table_info.get("table_name")
    res_data["combined_tables"] = table_info.get("combined_tables", [])
    res_data["extended"] = table_info.get("extended", False)


RESERVATION_PREFERENCE_ACK = "Lo segnalo, cerchiamo di accontentarla."

# Preferenza di sala detta dal cliente: si segnala, mai garantita.
_ROOM_PREFERENCE_RE = re.compile(
    r"(?:\b(?:in|nella|nel|alla|al|sulla|sul|vicino\s+alla|vicino\s+al)\s+|\ball')?"
    r"(?:\bsala\s+\w+|\bsaletta\b|\bveranda\b|\bterrazz[ao]\b|\bgiardino\b|\bdehors\b"
    r"|\ball'?aperto\b|\bfuori\b|\bdentro\b|\binterno\b|\bfinestra\b|\bcamino\b)",
    re.IGNORECASE,
)
# Richieste particolari da riportare nelle note della prenotazione.
_RESERVATION_NOTE_RE = re.compile(
    r"\b(seggiolon\w*|passeggin\w*|carrozzin\w*|sedia\s+a\s+rotelle|compleann\w*|anniversari\w*"
    r"|allergi\w*|intolleran\w*|celiac\w*|senza\s+glutine|vegan\w*|vegetarian\w*|cane|bambin\w*)\b",
    re.IGNORECASE,
)


def _extract_reservation_preferences(message: str) -> tuple[str | None, str | None]:
    """(preferenza di sala, nota) dette dal cliente nel messaggio, se ci sono."""
    room_match = _ROOM_PREFERENCE_RE.search(message)
    room = room_match.group(0).strip() if room_match else None
    note = message.strip() if _RESERVATION_NOTE_RE.search(message) else None
    return room, note


def _format_reservation_summary(
    name: str,
    date_it: str,
    time_str: str,
    party: int,
    table_name: str | None,
    preferred_room: str | None = None,
) -> str:
    """Produce 'Nome, giorno data alle HH:MM, N persona/persone[, Tavolo X][, preferenza]'."""
    table_part = f", {table_name}" if table_name else ""
    room_part = f", preferenza {preferred_room}: lo segnalo, cerchiamo di accontentarla" if preferred_room else ""
    persons = "persona" if party == 1 else "persone"
    return f"{name}, {date_it} alle {time_str}, {party} {persons}{table_part}{room_part}"


def _reservation_notes_text(res_data: dict) -> str:
    return " · ".join(res_data.get("notes") or [])


def _normalize_callback_phone(message: str, caller_phone: str | None) -> str | None:
    """Numero a cui richiamare: quello della chiamata se il cliente conferma,
    altrimenti le cifre dette (con +39 per i numeri italiani senza prefisso)."""
    digits = re.sub(r"[^\d+]", "", message)
    if len(re.sub(r"\D", "", digits)) >= 8:
        if digits.startswith("+"):
            return digits
        if digits.startswith("00"):
            return "+" + digits[2:]
        return "+39" + digits
    if caller_phone and re.search(r"\b(s[iì]|ok|va bene|certo|esatto|quest[oa]|stesso)\b", message.lower()):
        return caller_phone
    return None


def large_group_callback_prompt(conversation: ConversationSession, intro: str) -> tuple[str, str]:
    """Il locale non ha risposto (o non c'è un numero di trasferimento): l'agente
    raccoglie i dati per farsi richiamare. Aggiorna lo stato e restituisce
    (stato, messaggio)."""
    if not conversation.customer_name:
        state, question = "collecting_reservation_callback_name", "A nome di chi?"
    elif conversation.customer_phone:
        state, question = "collecting_reservation_callback_phone", "La richiamiamo al numero da cui sta chiamando?"
    else:
        state, question = "collecting_reservation_callback_phone", "A che numero possiamo richiamarla?"
    conversation.state = state
    return state, f"{intro} Prendo i dati e la richiamiamo noi per confermare. {question}"


def _callback_request_details(conversation: ConversationSession) -> dict:
    """Quello che il cliente ha detto finora: intento, prodotti, giorno, ora, persone."""
    res = json.loads(conversation.reservation_json or "{}")
    items = json.loads(conversation.items_json or "[]")
    in_reservation = conversation.state.startswith(("collecting_reservation", "awaiting_reservation")) or bool(
        res.get("date") or res.get("time") or res.get("party_size")
    )
    intent = "prenotazione" if in_reservation else "ordine" if (
        items or conversation.pickup_date or conversation.pickup_time
    ) else "non chiaro"
    date = res.get("date") or conversation.pickup_date
    time = res.get("time") or conversation.pickup_time
    party = res.get("party_size")
    products = ", ".join(format_single_item_for_customer(item) for item in items)
    summary = (
        f"Intento: {intent}. Prodotti: {products or 'nessuno'}. "
        f"Giorno: {_format_reservation_date_it(date) if date else 'non detto'}. "
        f"Ora: {time or 'non detta'}. Persone: {party or 'non dette'}."
    )
    return {"intent": intent, "date": date, "time": time, "party_size": party, "summary": summary}


def save_callback_request(conversation: ConversationSession, session_id: str, restaurant_id: str) -> str | None:
    """Richiesta di richiamata su Base44, una sola per chiamata. Id o None se non riesce.

    Reservation vuole sempre giorno, ora e persone: se il cliente non li ha detti si
    usano oggi, 00:00 e 0, e le note dicono che non sono stati detti."""
    res = json.loads(conversation.reservation_json or "{}")
    if res.get("pending_reservation_id"):
        return res["pending_reservation_id"]
    details = _callback_request_details(conversation)
    today = datetime.datetime.now(tz=ZoneInfo("Europe/Rome")).date().isoformat()
    phone_note = f"Richiamare al {conversation.customer_phone}" if conversation.customer_phone else "Numero non disponibile"
    reservation_id = save_reservation_to_base44(
        customer_name=conversation.customer_name or "Ospite",
        customer_phone=conversation.customer_phone,
        date=details["date"] or today,
        time=details["time"] or "00:00",
        party_size=int(details["party_size"] or 0),
        session_id=session_id,
        notes=f"Richiesta di richiamata. {details['summary']} {phone_note}.",
        restaurant_id=restaurant_id,
        status="da_confermare",
        review_reason=CALLBACK_REVIEW_REASON,
    )
    res["callback_request"] = {"intent": details["intent"], "summary": details["summary"]}
    if reservation_id:
        res["pending_reservation_id"] = reservation_id
        res["pending_review_reason"] = CALLBACK_REVIEW_REASON
    conversation.reservation_json = json.dumps(res, ensure_ascii=False)
    return reservation_id


def finish_callback_request(conversation: ConversationSession, session_id: str, restaurant_id: str) -> str:
    """Il locale non ha risposto: si registra la richiesta e si saluta. La richiamata si
    promette solo se è registrata e c'è un numero a cui richiamare."""
    saved = save_callback_request(conversation, session_id, restaurant_id)
    conversation.state = "completed"
    return CALLBACK_GOODBYE if saved and conversation.customer_phone else CALLBACK_NOT_POSSIBLE


def _build_pickup_time_error(
    pickup_time: str,
    suggestion: str | None,
    closing_time: str | None,
) -> str:
    if closing_time:
        return (
            f"Mi dispiace, chiudiamo alle {closing_time}. L'ultimo orario disponibile è le {suggestion}."
            if suggestion else f"Mi dispiace, alle {pickup_time} siamo già chiusi."
        )
    return (
        f"Mi dispiace, alle {pickup_time} siamo chiusi. Il prossimo orario disponibile è le {suggestion}."
        if suggestion else f"Mi dispiace, alle {pickup_time} siamo chiusi."
    )


def _log_chat_timing(
    session_id: str,
    path: str,
    started_at: float,
    **fields: Any,
) -> None:
    elapsed_ms = round((time.perf_counter() - started_at) * 1000)
    record_latency("chat", path, elapsed_ms, **fields)
    details = " ".join(f"{key}={value}" for key, value in fields.items())
    suffix = f" {details}" if details else ""
    print(f"[Latency] chat path={path} session={session_id} elapsed_ms={elapsed_ms}{suffix}")


def _menu_select(restaurant_id: str, *conditions):
    """select(MenuItem) limitato alle voci del locale; senza restaurant_id (flusso
    legacy) nessun filtro. Evita che nomi uguali prendano prezzo/unità di un altro locale."""
    statement = select(MenuItem).where(*conditions)
    if restaurant_id:
        statement = statement.where(MenuItem.restaurant_id == restaurant_id)
    return statement


def enrich_items_with_pricing(
    session: Session,
    items: list[dict[str, Any]],
    restaurant_id: str = "",
) -> tuple[list[dict[str, Any]], float | None]:
    """Aggiunge base/extras/total price agli item usando una sola logica condivisa."""
    margherita = session.exec(
        _menu_select(restaurant_id, MenuItem.name == "Margherita")
    ).first()
    base_personalizzata = round(margherita.price, 2) if margherita else 0.0

    # Tariffe al kg dal Restaurant (se configurate)
    restaurant = load_restaurant(restaurant_id=restaurant_id)
    _price_cold_raw = restaurant.get("price_per_kg_cold")
    _price_hot_raw = restaurant.get("price_per_kg_hot")
    price_per_kg_cold: float | None = float(_price_cold_raw) if _price_cold_raw else None
    price_per_kg_hot: float | None = float(_price_hot_raw) if _price_hot_raw else None

    enriched_items: list[dict[str, Any]] = []
    for item in items:
        if item["pizza_name"] == "Personalizzata":
            base_price = base_personalizzata
            item_sale_unit = "piece"
        else:
            menu_item = session.exec(
                _menu_select(
                    restaurant_id,
                    MenuItem.name == item["pizza_name"],
                    MenuItem.pizza_type == item["pizza_type"],
                )
            ).first()
            if not menu_item:
                menu_item = session.exec(
                    _menu_select(restaurant_id, MenuItem.name == item["pizza_name"])
                ).first()
            base_price = round(menu_item.price, 2) if menu_item else 0.0
            item_sale_unit = getattr(menu_item, "sale_unit", "piece") if menu_item else "piece"

        quantity = float(item.get("quantity") or 1)

        if item_sale_unit == "kg":
            # Al taglio 0 = quantità non capita (needs_review): resta 0, mai 1
            quantity = float(item.get("quantity") or 0)
            # Al peso: usa tariffa Restaurant se disponibile, altrimenti MenuItem.price.
            # Caldo/freddo non indicato (ordine in needs_review): tariffa fredda come stima.
            if item.get("temperature") == "calda" and price_per_kg_hot is not None:
                effective_price = round(price_per_kg_hot, 2)
            elif price_per_kg_cold is not None:
                effective_price = round(price_per_kg_cold, 2)
            else:
                effective_price = base_price  # fallback: MenuItem.price
            extras_price = 0.0
            # Al taglio il prezzo si fa a peso al ritiro, anche per un peso detto
            # ("mezzo chilo" è una stima): nessun totale, né della voce né dell'ordine
            total_price = None
            base_price = effective_price
        else:
            is_sg_pizza = "(SG)" in item.get("pizza_name", "")
            dough_code = item.get("dough_type", "classica")
            dough_surcharge = 0.0 if is_sg_pizza else get_dough_surcharge(dough_code)
            size = item.get("size", "normale")
            if size == "mini":
                base_price = round(max(0.0, base_price - SIZE_MINI_DISCOUNT), 2)
            elif size == "doppio":
                dough_surcharge = round(dough_surcharge + SIZE_DOPPIO_SURCHARGE, 2)
            extras_price = round(
                dough_surcharge + len(item.get("add_ingredients", [])) * INGREDIENT_EXTRA_PRICE,
                2,
            )
            total_price = round((base_price + extras_price) * quantity, 2)

        enriched_items.append({
            **item,
            "quantity": quantity,
            "sale_unit": item_sale_unit,
            "base_price": base_price,
            "extras_price": extras_price,
            "total_price": total_price,
        })

    return enriched_items, order_total_amount(enriched_items)


def order_total_amount(items: list[dict[str, Any]]) -> float | None:
    """Somma dei totali; None se una voce non ha prezzo (tranci: si pesa al ritiro)."""
    totals = [item.get("total_price") for item in items]
    if any(total is None for total in totals):
        return None
    return round(sum(totals), 2)


def _execute_order_side_effect(kind: str, payload: dict[str, Any]) -> None:
    if kind == "base44_order":
        rid = payload.get("restaurant_id") or os.getenv("DEFAULT_RESTAURANT_ID", "")
        print(f"[SideEffects] base44_order restaurant_id={rid!r}")
        result = save_order_to_base44(
            session_id=payload.get("session_id"),
            restaurant_id=rid,
            customer_name=payload["customer_name"],
            customer_phone=payload["customer_phone"],
            pickup_time=payload["pickup_time"],
            ai_confidence=payload["ai_confidence"],
            items=payload["items"],
            pickup_date=payload.get("pickup_date"),
            order_date=payload.get("order_date"),
            review_reasons=payload.get("review_reasons"),
        )
        if payload.get("session_id"):
            local_order_id = _store_order_number(payload["session_id"], result)
        else:
            local_order_id = None
        if not result:
            return
        # Qui l'ordine è già su Base44: un errore della notifica non deve far ritentare
        # questo job (creerebbe un doppione), quindi si registra e basta.
        try:
            _enqueue_owner_new_order(
                _owner_new_order_payload(payload, local_order_id, result.get("order_number"), rid),
            )
        except Exception as exc:
            print(
                f"[OwnerNotify] accodamento fallito restaurant_id={rid!r} order_id={local_order_id} "
                f"order_number={result.get('order_number')}: {type(exc).__name__}: {exc}"
            )
        return

    if kind == "owner_new_order":
        _send_owner_new_order(payload)
        return

    if kind == "whatsapp_confirmation":
        print(
            f"[SMS] Tentativo invio async: phone={mask_phone(payload['customer_phone'])} "
            f"name={mask_name(payload['customer_name'])}"
        )
        send_whatsapp_confirmation(
            customer_name=payload["customer_name"],
            customer_phone=payload["customer_phone"],
            pickup_time=payload["pickup_time"],
            items=payload["items"],
            total_amount=payload["total_amount"],
            pickup_date=payload.get("pickup_date") or payload.get("order_date"),
            restaurant_id=payload.get("restaurant_id") or "",
        )
        return

    if kind == "customer_upsert":
        if payload["customer_name"]:
            upsert_customer(
                full_name=payload["customer_name"],
                phone=payload["customer_phone"],
                pizzas=payload["pizza_names"],
                total_amount=payload["total_amount"],
                restaurant_id=payload.get("restaurant_id") or "",
            )
        return

    if kind == "reservation_sms":
        print(
            f"[ReservationSMS] Tentativo invio outbox: phone={mask_phone(payload['customer_phone'])} "
            f"name={mask_name(payload['customer_name'])}"
        )
        status = send_reservation_sms(
            customer_name=payload["customer_name"],
            customer_phone=payload["customer_phone"],
            date=payload["date"],
            time=payload["time"],
            party_size=payload["party_size"],
            table_name=payload.get("table_name"),
            restaurant_name=payload.get("restaurant_name"),
            restaurant_phone=payload.get("restaurant_phone"),
        )
        if status.startswith("sms_errore"):
            raise RuntimeError(status)
        return

    raise ValueError(f"Side effect sconosciuto: {kind}")


def _run_order_side_effects(payload: dict[str, Any]) -> None:
    """Compatibilità/test: esegue i side effect senza persistenza outbox."""
    started_at = time.perf_counter()
    order_number = payload.get("session_id") or payload.get("order_number")
    side_effects = [
        ("base44_order", payload),
        ("whatsapp_confirmation", payload),
        ("customer_upsert", payload),
    ]
    for kind, item_payload in side_effects:
        try:
            _execute_order_side_effect(kind, item_payload)
            print(f"[SideEffects] {kind} ordine #{order_number} completato")
        except Exception as exc:
            print(f"[SideEffects] {kind} errore ordine #{order_number}: {type(exc).__name__}: {exc}")
    elapsed_ms = round((time.perf_counter() - started_at) * 1000)
    print(f"[SideEffects] ordine #{order_number} completato elapsed_ms={elapsed_ms}")


def _side_effect_retry_delay(attempts: int) -> float:
    return min(_SIDE_EFFECT_RETRY_BASE_SECONDS * (2 ** max(attempts - 1, 0)), _SIDE_EFFECT_RETRY_MAX_SECONDS)


def _process_order_side_effect_job(job_id: int) -> None:
    with Session(_db_engine) as db_session:
        job = db_session.get(OrderSideEffect, job_id)
        if not job or job.status == "done":
            return
        if job.status == "failed" and job.attempts >= _SIDE_EFFECT_MAX_ATTEMPTS:
            return

        now = time.time()
        if job.next_attempt_at and job.next_attempt_at > now and job.status != "running":
            _schedule_order_side_effect_job(job.id, job.next_attempt_at - now)
            return

        job.status = "running"
        job.attempts += 1
        job.updated_at = now
        db_session.add(job)
        db_session.commit()

        try:
            payload = json.loads(job.payload_json)
        except json.JSONDecodeError as exc:
            job.status = "failed"
            job.last_error = f"JSONDecodeError: {exc}"
            job.updated_at = time.time()
            db_session.add(job)
            db_session.commit()
            print(f"[SideEffects] job={job_id} kind={job.kind} payload_json corrotto, fallito definitivamente: {job.last_error}")
            if job.kind == "base44_order":
                _alert_owner_order_not_saved({}, job.attempts, f"job={job_id} {job.last_error}")
            return

        try:
            _execute_order_side_effect(job.kind, payload)
        except Exception as exc:
            now = time.time()
            job = db_session.get(OrderSideEffect, job_id)
            if not job:
                return
            job.last_error = f"{type(exc).__name__}: {exc}"
            job.updated_at = now
            if job.attempts >= _SIDE_EFFECT_MAX_ATTEMPTS:
                job.status = "failed"
                print(f"[SideEffects] job={job_id} kind={job.kind} fallito definitivamente: {job.last_error}")
                if job.kind == "base44_order":
                    _alert_owner_order_not_saved(payload, job.attempts, job.last_error)
            else:
                job.status = "retry"
                job.next_attempt_at = now + _side_effect_retry_delay(job.attempts)
                print(
                    f"[SideEffects] job={job_id} kind={job.kind} retry "
                    f"attempt={job.attempts} next={int(job.next_attempt_at - now)}s error={job.last_error}"
                )
            db_session.add(job)
            db_session.commit()
            if job.status == "retry":
                _schedule_order_side_effect_job(job.id, max(0.0, job.next_attempt_at - time.time()))
            return

        now = time.time()
        job = db_session.get(OrderSideEffect, job_id)
        if not job:
            return
        job.status = "done"
        job.last_error = None
        job.next_attempt_at = 0.0
        job.updated_at = now
        db_session.add(job)
        db_session.commit()
        print(f"[SideEffects] job={job_id} kind={job.kind} completato")


def _schedule_order_side_effect_job(job_id: int, delay_seconds: float = 0.0) -> None:
    delay_seconds = max(0.0, delay_seconds)
    with _ORDER_SIDE_EFFECT_TIMERS_LOCK:
        old_timer = _ORDER_SIDE_EFFECT_TIMERS.pop(job_id, None)
        if old_timer:
            old_timer.cancel()

    if delay_seconds <= 0:
        _ORDER_SIDE_EFFECTS_EXECUTOR.submit(_process_order_side_effect_job, job_id)
        return

    def _submit_due_job() -> None:
        with _ORDER_SIDE_EFFECT_TIMERS_LOCK:
            _ORDER_SIDE_EFFECT_TIMERS.pop(job_id, None)
        _ORDER_SIDE_EFFECTS_EXECUTOR.submit(_process_order_side_effect_job, job_id)

    timer = threading.Timer(delay_seconds, _submit_due_job)
    timer.daemon = True
    with _ORDER_SIDE_EFFECT_TIMERS_LOCK:
        _ORDER_SIDE_EFFECT_TIMERS[job_id] = timer
    timer.start()


def recover_order_side_effects(limit: int = 100) -> int:
    """Riaccoda job pendenti/running dopo restart del processo, in batch ordinati."""
    scheduled = 0
    batch_size = max(1, limit)
    offset = 0
    now = time.time()
    while True:
        with Session(_db_engine) as db_session:
            jobs = db_session.exec(
                select(OrderSideEffect)
                .where(OrderSideEffect.status.in_(["pending", "retry", "running"]))
                .order_by(OrderSideEffect.next_attempt_at, OrderSideEffect.id)
                .offset(offset)
                .limit(batch_size)
            ).all()
        if not jobs:
            break

        for job in jobs:
            if job.id is None:
                continue
            delay = 0.0 if job.status == "running" else max(0.0, (job.next_attempt_at or 0.0) - now)
            _schedule_order_side_effect_job(job.id, delay)
            scheduled += 1

        if len(jobs) < batch_size:
            break
        offset += batch_size

    if scheduled:
        print(f"[SideEffects] recuperati {scheduled} job pendenti")
    return scheduled


def _alert_owner_order_not_saved(order: dict[str, Any], attempts: int, last_error: str | None) -> None:
    """Un ordine confermato al cliente non è arrivato su Base44: avvisa subito il titolare del locale."""
    rid = order.get("restaurant_id") or ""
    try:
        status = send_owner_alert(build_order_not_saved_alert(order, attempts, last_error), restaurant_id=rid)
        print(f"[SideEffects] avviso titolare ordine non salvato restaurant_id={rid!r}: {status}")
        if "inviato" not in status:
            print(
                f"[OwnerNotify] avviso ordine non salvato NON inviato restaurant_id={rid!r} "
                f"order_id={order.get('session_id')!r} status={status}"
            )
    except Exception as exc:
        print(
            f"[OwnerNotify] avviso ordine non salvato fallito restaurant_id={rid!r} "
            f"order_id={order.get('session_id')!r}: {type(exc).__name__}: {exc}"
        )


_OWNER_NEW_ORDER_FIELDS = (
    "customer_name", "customer_phone", "pickup_time", "pickup_date", "order_date",
    "items", "total_amount", "review_reasons", "ai_confidence",
)


def _owner_new_order_payload(
    order: dict[str, Any], local_order_id: int | None, order_number: Any, restaurant_id: str,
) -> dict[str, Any]:
    payload = {key: order.get(key) for key in _OWNER_NEW_ORDER_FIELDS}
    payload.update(restaurant_id=restaurant_id, local_order_id=local_order_id, order_number=order_number)
    return payload


def _enqueue_owner_new_order(payload: dict[str, Any]) -> None:
    """Accoda la notifica al titolare per un ordine salvato su Base44 da un job di retry."""
    now = time.time()
    with Session(_db_engine) as db_session:
        job = OrderSideEffect(
            order_number=payload.get("local_order_id") or 0,
            kind="owner_new_order",
            payload_json=json.dumps(payload, ensure_ascii=False),
            status="pending",
            attempts=0,
            next_attempt_at=now,
            created_at=now,
            updated_at=now,
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)
        job_id = job.id
    _schedule_order_side_effect_job(job_id)


def _owner_new_order_wanted(payload: dict[str, Any], restaurant_id: str) -> bool:
    """True se la notifica per questo nuovo ordine va inviata. Gli ordini da
    verificare (motivi o bassa confidenza AI, come needs_review su Base44) partono sempre."""
    confidence = payload.get("ai_confidence")
    if payload.get("review_reasons") or (confidence is not None and confidence < 0.8):
        return True
    restaurant = load_restaurant(restaurant_id=restaurant_id) if restaurant_id else {}
    return restaurant_flag(restaurant, "owner_order_sms_enabled", default=True)


def _send_owner_new_order(payload: dict[str, Any]) -> None:
    """Notifica al titolare del locale un nuovo ordine. Un invio fallito si registra
    con restaurant_id e order_id e si ritenta tramite l'outbox; la configurazione
    mancante non si ritenta.

    Con Restaurant.owner_order_sms_enabled = false (null vale true) partono solo
    gli ordini da verificare: gli altri il titolare li trova nel riepilogo giornaliero."""
    rid = payload.get("restaurant_id") or ""
    order_ref = f"order_id={payload.get('local_order_id')} order_number={payload.get('order_number')}"
    if not _owner_new_order_wanted(payload, rid):
        print(f"[OwnerNotify] owner_order_sms_enabled=false: notifica saltata restaurant_id={rid!r} {order_ref}")
        return
    try:
        status = send_owner_alert(build_new_order_owner_message(payload), restaurant_id=rid)
    except Exception as exc:
        print(f"[OwnerNotify] invio fallito restaurant_id={rid!r} {order_ref}: {type(exc).__name__}: {exc}")
        raise
    if "inviato" in status:
        print(f"[OwnerNotify] nuovo ordine notificato restaurant_id={rid!r} {order_ref}: {status}")
        return
    print(f"[OwnerNotify] invio fallito restaurant_id={rid!r} {order_ref} status={status}")
    if not status.startswith("skip"):
        raise RuntimeError(f"notifica titolare non inviata: {status}")


def _store_order_number(session_id: str, result: dict[str, Any]) -> int | None:
    """Salva sull'Order locale il numero definitivo e l'id Base44; ritorna l'id locale."""
    with Session(_db_engine) as db_session:
        order = _get_order_for_session(db_session, session_id)
        if order is None:
            return None
        order.order_number = int(result["order_number"])
        order.base44_id = str(result["id"])
        db_session.add(order)
        db_session.commit()
        return order.id


def _finalize_new_order(
    *,
    session: Session,
    order: Order,
    conversation: ConversationSession,
    merged_order: dict[str, Any],
    enriched_items: list[dict[str, Any]],
    total_amount: float | None,
    restaurant_id: str,
    ai_confidence: float,
    review_reasons: list[str] | None = None,
) -> None:
    """Salva l'ordine su Base44 PRIMA della risposta al cliente, così il numero
    è definitivo prima di qualsiasi conferma. Se Base44 non risponde, il salvataggio
    passa al job con retry (e all'avviso al titolare se fallisce del tutto)."""
    base44_payload: dict[str, Any] = {
        "session_id": conversation.session_id,
        "restaurant_id": restaurant_id or os.getenv("DEFAULT_RESTAURANT_ID", ""),
        "customer_name": merged_order["customer_name"],
        "customer_phone": conversation.customer_phone,
        "pickup_time": merged_order["pickup_time"],
        "ai_confidence": ai_confidence,
        "items": copy.deepcopy(enriched_items),
        "total_amount": total_amount,
        "order_date": rome_today(),
    }
    if merged_order.get("pickup_date"):
        base44_payload["pickup_date"] = merged_order["pickup_date"]
    if review_reasons:
        base44_payload["review_reasons"] = list(review_reasons)
    try:
        result = save_order_to_base44(**{k: v for k, v in base44_payload.items() if k != "total_amount"})
    except Exception as exc:
        print(
            f"[Order] Base44 non disponibile alla conferma ({type(exc).__name__}: {exc}): "
            f"ordine locale id={order.id} passa al retry"
        )
        result = None

    if result:
        order.order_number = int(result["order_number"])
        order.base44_id = str(result["id"])
        session.add(order)
        session.commit()

    _enqueue_order_side_effects(
        session=session,
        local_order_id=order.id,
        customer_name=merged_order["customer_name"],
        customer_phone=conversation.customer_phone,
        pickup_time=merged_order["pickup_time"],
        items=enriched_items,
        total_amount=total_amount,
        pizza_names=list(dict.fromkeys(item["pizza_name"] for item in merged_order["items"])),
        restaurant_id=base44_payload["restaurant_id"],
        pickup_date=base44_payload.get("pickup_date"),
        order_date=base44_payload["order_date"],
        base44_order_payload=None if result else base44_payload,
        owner_notification=(
            _owner_new_order_payload(base44_payload, order.id, result["order_number"], base44_payload["restaurant_id"])
            if result else None
        ),
    )


def _enqueue_order_side_effects(
    *,
    session: Session,
    local_order_id: int,
    customer_name: str,
    customer_phone: str | None,
    pickup_time: str,
    items: list[dict[str, Any]],
    total_amount: float | None,
    pizza_names: list[str],
    restaurant_id: str = "",
    pickup_date: str | None = None,
    order_date: str | None = None,
    base44_order_payload: dict[str, Any] | None = None,
    owner_notification: dict[str, Any] | None = None,
) -> None:
    """Accoda i side effect dell'ordine. `base44_order_payload` è presente solo se
    l'Order non è già stato creato su Base44 alla conferma; in quel caso la
    notifica al titolare la accoda il job base44_order quando l'ordine è salvato."""
    now = time.time()
    item_payload = copy.deepcopy(items)
    job_specs: list[tuple[str, dict[str, Any]]] = []
    if base44_order_payload is not None:
        job_specs.append(("base44_order", base44_order_payload))
    job_specs += [
        ("whatsapp_confirmation", {
            "customer_name": customer_name,
            "customer_phone": customer_phone,
            "pickup_time": pickup_time,
            "items": item_payload,
            "total_amount": total_amount,
            "pickup_date": pickup_date,
            "order_date": order_date,
            "restaurant_id": restaurant_id,
        }),
    ]
    if owner_notification is not None:
        job_specs.append(("owner_new_order", owner_notification))
    if customer_name:
        job_specs.append(("customer_upsert", {
            "customer_name": customer_name,
            "customer_phone": customer_phone,
            "pizza_names": [name for name in pizza_names if name not in _OFF_MENU_PIZZA_NAMES],
            "total_amount": total_amount,
            "restaurant_id": restaurant_id,
        }))

    jobs: list[OrderSideEffect] = []
    for kind, payload in job_specs:
        job = OrderSideEffect(
            order_number=local_order_id,  # riferimento all'Order locale, non il numero Base44
            kind=kind,
            payload_json=json.dumps(payload, ensure_ascii=False),
            status="pending",
            attempts=0,
            next_attempt_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        jobs.append(job)
    session.commit()

    for job in jobs:
        session.refresh(job)
        _schedule_order_side_effect_job(job.id)
    print(f"[SideEffects] ordine locale id={local_order_id} accodato: {len(jobs)} job")


def _enqueue_reservation_sms_side_effect(
    *,
    session: Session,
    customer_name: str,
    customer_phone: str | None,
    date: str,
    reservation_time: str,
    party_size: int,
    table_name: str | None,
    restaurant_name: str | None = None,
    restaurant_phone: str | None = None,
) -> None:
    if not _is_mobile_phone(customer_phone):
        return

    now = time.time()
    job = OrderSideEffect(
        order_number=0,
        kind="reservation_sms",
        payload_json=json.dumps({
            "customer_name": customer_name,
            "customer_phone": customer_phone,
            "date": date,
            "time": reservation_time,
            "party_size": party_size,
            "table_name": table_name,
            "restaurant_name": restaurant_name,
            "restaurant_phone": restaurant_phone,
        }, ensure_ascii=False),
        status="pending",
        attempts=0,
        next_attempt_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    _schedule_order_side_effect_job(job.id)
    print(f"[ReservationSMS] job accodato reservation date={date} time={reservation_time} table={table_name!r}")


def _build_sold_out_item_message(
    session: Session,
    pizza_name: str,
    sold_out_names: set[str],
) -> str:
    """Return 'finita' message with best proposable alternative in the same category."""
    item_db = session.exec(select(MenuItem).where(MenuItem.name == pizza_name)).first()
    category = item_db.category if item_db else None

    alternatives: list[str] = []
    if category:
        candidates = session.exec(
            select(MenuItem).where(
                MenuItem.available == True,  # noqa: E712
                MenuItem.category == category,
            )
        ).all()
        seen: set[str] = set()
        for c in candidates:
            if c.name.lower() == pizza_name.lower():
                continue
            if c.name.lower() in sold_out_names:
                continue
            if c.name not in seen:
                seen.add(c.name)
                alternatives.append(c.name)

    if alternatives:
        return f"{pizza_name} è temporaneamente esaurita. Posso proporle {alternatives[0]}?"
    return f"{pizza_name} è temporaneamente esaurita."


def _format_euro(amount: float) -> str:
    return f"{amount:.2f}".replace(".", ",") + " euro"


def _kg_price_answer(restaurant_id: str = "") -> str:
    restaurant = load_restaurant(restaurant_id=restaurant_id)
    cold = restaurant.get("price_per_kg_cold")
    hot = restaurant.get("price_per_kg_hot")
    if cold and hot:
        return (
            f"Al taglio costa {_format_euro(float(cold))} al chilo fredda e "
            f"{_format_euro(float(hot))} calda. Il totale lo facciamo alla bilancia."
        )
    return "Il prezzo è al chilo: il totale lo facciamo alla bilancia."


_PRICE_QUESTION_RE = re.compile(
    r"\bquanto\s+(?:costa|costano|costerebbe|viene|vengono|verrebbe|pago|spendo|fa|fanno)\b|\bprezz[oi]\b|\bcosto\b"
)
RECAP_CONFIRM_QUESTION = "Confermo l'ordine?"


def _recap_price_answer(session: Session, items: list[dict], restaurant_id: str = "") -> str:
    """Prezzo dell'ordine al riepilogo. Al taglio: prezzo al chilo (caldo/freddo come
    nell'ordine) e si paga a peso al ritiro, nessun totale. A pezzo: il totale."""
    enriched, _ = enrich_items_with_pricing(session, items, restaurant_id)
    kg_items = [item for item in enriched if item.get("sale_unit") == "kg"]
    piece_items = [item for item in enriched if item.get("sale_unit") != "kg"]
    parts = []
    if piece_items:
        piece_total = round(sum(float(item.get("total_price") or 0) for item in piece_items), 2)
        parts.append(
            f"In tutto sono {_format_euro(piece_total)}." if not kg_items
            else f"Le voci a pezzo fanno {_format_euro(piece_total)}."
        )
    if kg_items:
        restaurant = load_restaurant(restaurant_id=restaurant_id)
        cold, hot = restaurant.get("price_per_kg_cold"), restaurant.get("price_per_kg_hot")
        temperatures = {item.get("temperature") for item in kg_items}
        if cold and hot:
            rates = []
            if temperatures != {"calda"}:
                rates.append(f"{_format_euro(float(cold))} al chilo freddo")
            if temperatures != {"fredda"}:
                rates.append(f"{_format_euro(float(hot))} al chilo caldo")
            rate_text = " e ".join(rates)
        else:
            rate_text = f"{_format_euro(float(kg_items[0].get('base_price') or 0))} al chilo"
        parts.append(f"Al taglio costa {rate_text}: si paga a peso al ritiro.")
    return " ".join(parts)


def determine_state(
    merged_order: dict,
    missing_messages: list[str],
    completed: bool,
    intended_quantity: int | None = None,
    done_signal: bool = False,
) -> str:
    if completed:
        return "completed"

    if missing_messages:
        return "collecting_items"

    if not merged_order["items"]:
        return "collecting_items"

    # Rimani in collecting_items finché non viene raggiunto il numero dichiarato
    # o il cliente segnala che ha finito
    if intended_quantity and not done_signal:
        current_count = sum(item.get("quantity", 1) for item in merged_order["items"])
        if current_count < intended_quantity:
            return "collecting_items"

    # Voci al taglio: quantità (peso o tranci), porzione dei tranci e caldo/freddo
    # sono obbligatorie e mai predefinite
    for slot in _KG_SLOTS:
        if _kg_items_missing(merged_order["items"], slot):
            return _KG_STATE_BY_SLOT[slot]

    if not merged_order.get("customer_name"):
        return "collecting_name"

    if not merged_order.get("pickup_time"):
        return "collecting_pickup_time"

    return "awaiting_confirmation"

def _is_mobile_phone(phone: str | None) -> bool:
    """True se il numero è un cellulare italiano."""
    if not phone:
        return False
    p = re.sub(r"[\s\-\(\)]", "", phone)
    return (
        p.startswith("+393")
        or p.startswith("393")
        or (p.startswith("3") and not p.startswith("+390") and not p.startswith("0"))
    )


def build_assistant_response(
    merged_order: dict,
    state: str,
    missing_messages: list[str],
    order_saved: bool,
    intent: str,
    new_valid_items: list[dict],
    customer_phone: str | None = None,
    pickup_time_error: str | None = None,
    removed_names: list[str] | None = None,
    not_found_names: list[str] | None = None,
    intended_quantity: int | None = None,
    day_phrase: str | None = None,
    restaurant_id: str = "",
    day_said: bool = False,
) -> str:
    """`day_phrase` ("domani, giovedì") è presente solo per i preordini.
    `day_said`: il giorno di ritiro è già stato detto in questa chiamata, non si ripete."""
    customer_name = merged_order.get("customer_name")
    pickup_time = merged_order.get("pickup_time")
    items_text = format_items_for_customer(merged_order["items"])

    # Prezzo al taglio: tariffe al kg, il totale si fa alla bilancia
    if intent == "ask_kg_price":
        answer = _kg_price_answer(restaurant_id)
        if state in _KG_SLOT_STATES:
            return f"{answer} {_kg_slot_question(merged_order.get('items', []))}"
        return answer

    # Pickup time closed-hours error — ritorna solo il messaggio di errore,
    # senza accodare missing_messages (che già contiene pickup_time_error).
    # Se è aperta la domanda su porzione o caldo/freddo, la ripete: l'orario
    # verrà chiesto dopo.
    if pickup_time_error:
        if state in _KG_SLOT_STATES:
            error = pickup_time_error.removesuffix(" A che ora passa?")
            return f"{error} {_kg_slot_question(merged_order['items'], _KG_SLOT_STATES[state])}"
        return pickup_time_error

    # Missing/invalid items
    if missing_messages:
        return " ".join(missing_messages)

    # Cancellation / cart reset
    if intent == "cancel_order":
        return "Va bene, ordine annullato. Mi dica pure se vuole ricominciare."

    if intent == "clear_cart":
        return "Ho cancellato tutto, mi dica pure cosa desidera!"

    if intent == "remove_items":
        parts = []
        if not_found_names:
            for name in not_found_names:
                parts.append(f"Non ho trovato {name} nel suo ordine.")
        if removed_names:
            parts.append("Rimosso!")
        return " ".join(parts) if parts else "Non ho trovato quella pizza nel suo ordine."

    # Order completed
    if state == "completed" and order_saved:
        name_part = f", {customer_name}" if customer_name else ""
        if _is_mobile_phone(customer_phone):
            return f"Perfetto{name_part}! Le arriva un SMS di conferma."
        else:
            return f"Perfetto{name_part}, a presto!"

    # Awaiting confirmation — riepilogo completo di ciò che verrà salvato
    if state == "awaiting_confirmation":
        return build_confirmation_recap(merged_order, day_phrase)

    if state in _KG_SLOT_STATES:
        return _kg_slot_question(merged_order.get("items", []), _KG_SLOT_STATES[state])

    if state == "confirming_next_day":
        return pickup_time_error or _next_day_question(restaurant_id)

    # Collecting pickup time (ha già il nome, manca solo l’ora)
    if state == "collecting_pickup_time":
        if day_phrase:
            return "A che ora passa?" if day_said else f"{_pickup_day_intro(day_phrase)} A che ora passa?"
        return "Per che ora?"

    # Collecting name
    if state == "collecting_name":
        return "A che nome?"

    # Collecting items — risposte brevissime, niente domande
    if intent in ("add_items", "modify_items", "replace_items"):
        if not new_valid_items:
            # Il cliente ha dichiarato una quantità senza specificare le pizze
            return "Certo, mi dica pure!"
        # Se c'è una quantità dichiarata e non ancora raggiunta, chiedi esplicitamente la prossima
        if intended_quantity is not None:
            items_count = sum(int(item.get("quantity", 1)) for item in merged_order.get("items", []))
            remaining = intended_quantity - items_count
            if remaining > 0:
                next_num = items_count + 1
                ordinal = _ORDINALS_IT.get(next_num)
                if remaining == 1:
                    if ordinal:
                        return f"Perfetto! E la {ordinal} pizza?"
                    return "Perfetto! E l'ultima pizza?"
                else:
                    return f"Perfetto! Ne mancano ancora {remaining}, mi dica pure."
        return random.choice(["Ok!", "Aggiunto!", "Perfetto!", "Certo!"])

    # Segnale "ho finito" senza items: chiedi nome e ora
    if not merged_order["items"]:
        return "Certo, mi dica pure!"

    # Natural ordering intent (e.g. "vorrei ordinare")
    return "Certo, mi dica pure!"

def has_invalid_items(session: Session, items: list[dict]) -> bool:
    for item in items:
        statement = select(MenuItem).where(
            MenuItem.name == item["pizza_name"],
            MenuItem.pizza_type == item["pizza_type"],
        )
        menu_item = session.exec(statement).first()

        # Fallback per impasto non-standard: cerca per solo nome
        if not menu_item:
            fallback = select(MenuItem).where(MenuItem.name == item["pizza_name"])
            menu_item = session.exec(fallback).first()

        if not menu_item or not menu_item.available:
            return True

    return False

def extract_choice_from_suggestions(message: str, suggestions: list[str]) -> str | None:
    message_lower = message.lower().strip()

    for suggestion in suggestions:
        if suggestion.lower() in message_lower:
            return suggestion

    ordinal_map = {
        "prima": 0,
        "primo": 0,
        "seconda": 1,
        "secondo": 1,
        "terza": 2,
        "terzo": 2,
    }

    for word, index in ordinal_map.items():
        if word in message_lower and index < len(suggestions):
            return suggestions[index]

    implicit_choice_markers = [
        "quella",
        "quella lì",
        "va bene quella",
        "ok quella",
        "prendo quella",
        "fai quella",
        "fai quella lì",
        "sì",
        "si",
        "va bene",
        "ok",
        "perfetto",
        "confermo",
    ]

    if any(marker in message_lower for marker in implicit_choice_markers):
        if suggestions:
            return suggestions[0]

    return None

def keep_only_valid_existing_items(session: Session, items: list[dict]) -> list[dict]:
    valid_items = []

    for item in items:
        statement = select(MenuItem).where(
            MenuItem.name == item["pizza_name"],
            MenuItem.pizza_type == item["pizza_type"],
        )
        menu_item = session.exec(statement).first()

        # Fallback per impasto non-standard: cerca per solo nome
        if not menu_item:
            fallback = select(MenuItem).where(MenuItem.name == item["pizza_name"])
            menu_item = session.exec(fallback).first()

        if menu_item and menu_item.available:
            valid_items.append(item)

    return valid_items

def get_menu_names(session: Session) -> list[str]:
    statement = select(MenuItem).where(MenuItem.available == True)  # noqa: E712
    items = session.exec(statement).all()

    seen = set()
    names = []

    for item in items:
        if item.name not in seen:
            seen.add(item.name)
            names.append(item.name)

    return names

def get_typo_correction_suggestions(
    session: Session,
    requested_name: str,
    limit: int = 2,
    restaurant_id: str = "",
) -> list[str]:
    # Solo il menu del locale della chiamata: nello stesso DB ci sono i menu di tutti
    menu_names = (
        [entry.name for entry in session.exec(_menu_select(restaurant_id)).all() if entry.available]
        if restaurant_id
        else get_menu_names(session)
    )

    scored_matches = []
    requested_lower = requested_name.lower().strip()

    for name in menu_names:
        similarity = SequenceMatcher(None, requested_lower, name.lower()).ratio()

        # soglia alta = solo typo molto vicini
        if similarity >= 0.82:
            scored_matches.append((name, similarity))

    scored_matches.sort(key=lambda x: x[1], reverse=True)

    return [name for name, _ in scored_matches[:limit]]

class ChatStartRequest(BaseModel):
    test_phone: str | None = None


def _italian_title(full_name: str) -> str:
    """Restituisce 'il signor' o 'la signora' in base al nome (euristica sul finale)."""
    first = full_name.strip().split()[0] if full_name.strip() else full_name
    if first.lower().endswith("a"):
        return f"la signora {full_name}"
    return f"il signor {full_name}"


def _format_pizza_list(pizzas: list[str]) -> str:
    """Formatta una lista di pizze in italiano: 'A', 'A e B', 'A, B e C'."""
    if not pizzas:
        return ""
    if len(pizzas) == 1:
        return pizzas[0]
    if len(pizzas) == 2:
        return f"{pizzas[0]} e {pizzas[1]}"
    return ", ".join(pizzas[:-1]) + f" e {pizzas[-1]}"


def _get_order_for_session(session: Session, session_id: str) -> Order | None:
    return session.exec(
        select(Order).where(Order.conversation_session_id == session_id)
    ).first()


def _mark_conversation_completed(
    session: Session,
    conversation: ConversationSession,
) -> None:
    conversation.completed = True
    conversation.state = "completed"
    session.add(conversation)
    session.commit()


def _persist_order_once(
    session: Session,
    conversation: ConversationSession,
    merged_order: dict,
) -> tuple[Order, bool]:
    """Crea l'ordine una sola volta per sessione conversazionale.

    Ritorna (order, created). Se l'ordine esiste già, non risalva item né
    rilancia sync esterne: è la guardia contro retry webhook/doppi "sì".
    """
    existing_order = _get_order_for_session(session, conversation.session_id)
    if existing_order:
        _mark_conversation_completed(session, conversation)
        return existing_order, False

    # Il giorno di ritiro è sempre scritto, anche per "stasera": vuoto non vuol dire oggi
    merged_order["pickup_date"] = (
        merged_order.get("pickup_date")
        or conversation.pickup_date
        or datetime.datetime.now(tz=ZoneInfo("Europe/Rome")).date().isoformat()
    )
    order = Order(
        conversation_session_id=conversation.session_id,
        customer_name=merged_order["customer_name"],
        pickup_time=merged_order["pickup_time"],
        pickup_date=merged_order.get("pickup_date"),
        status="new",
    )
    session.add(order)
    try:
        session.commit()
        session.refresh(order)
    except IntegrityError:
        session.rollback()
        existing_order = _get_order_for_session(session, conversation.session_id)
        if existing_order:
            _mark_conversation_completed(session, conversation)
            return existing_order, False
        raise

    for item in merged_order["items"]:
        # sale_unit: read from item dict (set during extraction) or fallback to DB
        _item_sale_unit = item.get("sale_unit")
        if not _item_sale_unit:
            _mi = session.exec(
                _menu_select(conversation.restaurant_id or "", MenuItem.name == item["pizza_name"])
            ).first()
            _item_sale_unit = getattr(_mi, "sale_unit", "piece") if _mi else "piece"
        _kg_size = item.get("size") if _item_sale_unit == "kg" else None
        _portion = _kg_size if _kg_size in ("piena", "mezza") else None
        session.add(OrderItem(
            order_id=order.id,
            pizza_name=item["pizza_name"],
            pizza_type=item["pizza_type"],
            quantity=float(item.get("quantity") or 1),
            add_ingredients_json=json.dumps(item.get("add_ingredients", []), ensure_ascii=False),
            remove_ingredients_json=json.dumps(item.get("remove_ingredients", []), ensure_ascii=False),
            dough_type=item.get("dough_type", "classica"),
            size=item.get("size", "normale"),
            sale_unit=_item_sale_unit,
            temperature=item.get("temperature") or None,
            portion=_portion,
        ))
    session.commit()

    _mark_conversation_completed(session, conversation)
    return order, True


def _confirm_order(
    session: Session,
    conversation: ConversationSession,
    merged_order: dict,
    restaurant_id: str,
) -> Order:
    """Salva l'ordine confermato (una sola volta per sessione) con i motivi
    needs_review delle voci al taglio e delle domande non capite."""
    order, order_created = _persist_order_once(session, conversation, merged_order)
    enriched_items, order_total = enrich_items_with_pricing(session, merged_order["items"], restaurant_id=restaurant_id)
    if order_created:
        _finalize_new_order(
            session=session,
            order=order,
            conversation=conversation,
            merged_order=merged_order,
            enriched_items=enriched_items,
            total_amount=order_total,
            restaurant_id=restaurant_id,
            ai_confidence=0.95,
            review_reasons=_kg_review_reasons(merged_order["items"]) + _slot_review_reasons(conversation),
        )
    return order


@router.post("/start", response_model=ChatStartResponse)
def start_chat(body: ChatStartRequest, session: SessionDep):
    print(f"[ChatStart] test_phone={mask_phone(body.test_phone)}")
    if not is_agent_active():
        print("[Chat] agent_active=False → rifiuto start_chat")
        return ChatStartResponse(
            session_id="",
            state="closed",
            completed=False,
            response_message=build_closed_message(),
        )
    new_session_id = str(uuid.uuid4())
    phone = body.test_phone or None

    # Avvia il lookup cliente in parallelo: il phone è già disponibile dal body
    # prima ancora di creare la sessione DB, così i ~500ms di latenza Base44
    # si sovrappongono alle operazioni di commit locale.
    lookup_future = None
    if phone:
        print(f"[Customer] Avvio lookup in parallelo per {mask_phone(phone)}")
        lookup_future = _CUSTOMER_LOOKUP_EXECUTOR.submit(
            lookup_customer, phone, os.getenv("DEFAULT_RESTAURANT_ID", "")
        )

    conversation = ConversationSession(
        session_id=new_session_id,
        customer_name=None,
        customer_phone=phone,
        pickup_time=None,
        items_json="[]",
        state="collecting_items",
        completed=False,
    )
    session.add(conversation)
    session.commit()
    session.refresh(conversation)
    print(f"[Customer] customer_phone in sessione: {mask_phone(conversation.customer_phone)}")

    # Riconoscimento cliente dal numero di telefono
    greeting = get_agent_greeting()
    if lookup_future is not None:
        customer = _resolve_customer_lookup_future(lookup_future, phone)
        if customer:
            found_name = customer.get("full_name", "").strip()
            if found_name:
                # Saluta direttamente per nome — il numero è conferma sufficiente
                first_name = found_name.split()[0]
                greeting = f"Salve {first_name}! Come posso aiutarla?"
                conversation.customer_name = found_name
                # Salva le pizze preferite per il flusso "solite"
                raw_fav = customer.get("favorite_pizzas") or []
                if isinstance(raw_fav, str):
                    raw_fav = [p.strip() for p in raw_fav.split(",") if p.strip()]
                conversation.favorite_pizzas_json = json.dumps(raw_fav[:5], ensure_ascii=False)
                session.add(conversation)
                session.commit()
    else:
        print("[Customer] phone non disponibile, lookup saltato")

    print(f"[Agent] Saluto: {describe_text_for_log(greeting)}")

    return ChatStartResponse(
        session_id=conversation.session_id,
        state=conversation.state,
        completed=conversation.completed,
        response_message=greeting,
    )

def _extract_temperature(message: str) -> str | None:
    """Return 'calda' or 'fredda' from a bare temperature response, or None."""
    m = message.lower()
    if re.search(r'\bcald[aeio]\b|scaldat|mangiare\s+subito', m):
        return "calda"
    if re.search(r'\bfredd[aeio]\b|portar?\s+via|asporto', m):
        return "fredda"
    return None


# "mezzo" in un peso ("mezzo chilo", "un chilo e mezzo") o in un orario ("e mezza")
# non è una porzione.
_NOT_PORTION_MEZZO_RE = re.compile(r"\bmezz[oa]\s+(?:chil[oi]|kg|etto)\b|\be\s+mezz[oa]\b")


def _extract_kg_size(message: str) -> str | None:
    """Return 'piena' or 'mezza' from a slice-portion answer, or None.

    intera/intero/inter/tutto/pieno/piena → piena; mezza/mezzo/metà → mezza.
    """
    m = _NOT_PORTION_MEZZO_RE.sub(" ", message.lower())
    if re.search(r'\binter[aoie]?\b|\bpien[aoie]\b|\btutt[oa]\b|\bgrand[ei]\b', m):
        return "piena"
    if re.search(r'\bmezz[aoie]\b|\bmetà\b|\bmeta\b', m):
        return "mezza"
    return None


_KG_NUMBER_WORDS = {
    "un": 1, "uno": 1, "una": 1, "due": 2, "tre": 3, "quattro": 4, "cinque": 5,
    "sei": 6, "sette": 7, "otto": 8, "nove": 9, "dieci": 10,
}
_KG_NUM = r"(\d+(?:[.,]\d+)?|un|uno|una|due|tre|quattro|cinque|sei|sette|otto|nove|dieci)"


def _kg_number(token: str) -> float:
    return float(_KG_NUMBER_WORDS.get(token, token.replace(",", ".") if token[0].isdigit() else 0))


def _extract_kg_quantity(message: str, bare_number_ok: bool = False) -> tuple[str, float] | None:
    """('kg', peso in kg) o ('tranci', numero) da una risposta sulla quantità.

    bare_number_ok: un numero da solo ("due", "tre interi") vale come numero di
    tranci; solo quando l'agente ha appena chiesto "Quanti tranci?".
    """
    m = message.lower()
    # "Intrancio di pepe rosa": il riconoscimento vocale attacca "un" a "trancio"
    m = re.sub(r"\bin\s?trancio\b", "un trancio", m)
    if re.search(r"\bmezzo\s+(?:chilo|kg)\b", m):
        return "kg", 0.5
    match = re.search(rf"\b{_KG_NUM}\s*(?:chil[oi]|kg)\b(\s+e\s+mezzo)?", m)
    if match:
        return "kg", _kg_number(match.group(1)) + (0.5 if match.group(2) else 0.0)
    match = re.search(rf"\b{_KG_NUM}\s*(?:ett[oi]|hg)\b", m)
    if match:
        return "kg", round(_kg_number(match.group(1)) * 0.1, 3)
    match = re.search(r"\b(\d+)\s*(?:grammi|gr|g)\b", m)
    if match:
        return "kg", int(match.group(1)) / 1000
    match = re.search(rf"\b{_KG_NUM}\s+(?:tranci|trancio|pezzi|pezzo|fette|fetta)\b", m)
    if match:
        return "tranci", _kg_number(match.group(1))
    if bare_number_ok:
        # "E 2 interi?", "Allora due": la congiunzione davanti non toglie il numero
        m = re.sub(r"^[\s,.]*(?:(?:e|ed|allora|si|sì|ok|facciamo|ne|mi\s+dia|me\s+ne\s+dia)\b[\s,]*)+", "", m)
        match = re.match(rf"^\s*{_KG_NUM}\b", m)
        if match and _kg_number(match.group(1)) >= 1:
            return "tranci", _kg_number(match.group(1))
    return None


def _normalize_kg_order_unit(item: dict, message: str) -> None:
    """Fissa order_unit di una voce al taglio appena estratta dall'LLM.

    Se l'LLM non lo indica: un peso o dei tranci detti nel messaggio decidono;
    una porzione detta senza numero vale come tranci (numero da chiedere); una
    quantità senza unità vale come peso, come prima di order_unit.
    """
    unit = item.get("order_unit") if item.get("order_unit") in ("kg", "tranci") else ""
    quantity = float(item.get("quantity") or 0)
    if not unit and quantity > 0:
        said = _extract_kg_quantity(message)
        unit = said[0] if said else "kg"
    if not unit and item.get("size") in ("piena", "mezza"):
        unit = "tranci"
    item["order_unit"] = unit
    if unit == "kg":
        item["size"] = "normale"
    if unit == "tranci" and quantity > 0:
        item["quantity"] = float(int(round(quantity)))


# Una frase con più prodotti si divide dove inizia un nuovo prodotto: "e" o virgola seguiti
# da un numero o da una porzione ("... verde e noci caldo | e mezzo di salmone e limone").
_ITEM_CLAUSE_SPLIT_RE = re.compile(
    r"(?:,|\be)\s+(?=(?:un|uno|una|due|tre|quattro|cinque|sei|\d+|mezz[oaie]|inter[oaie])\b)"
)
_WEIGHT_WORDS_RE = re.compile(r"\b(?:chil[oi]|kg|ett[oi]|hg|grammi|grammo|gr)\b")
# "fritto": il riconoscimento vocale lo scrive al posto di "freddo"
_CLAUSE_HOT_RE = re.compile(r"\bcald[aeio]\b")
_CLAUSE_COLD_RE = re.compile(r"\b(?:fredd[aeio]|fritt[aeio])\b")
_CLAUSE_SLICE_RE = re.compile(r"\b(?:intrancio|tranci|trancio|pezzi|pezzo|fette|fetta|inter[aoie]|mezz[aoie])\b")


def _kg_item_clauses(items: list[dict], message: str) -> dict[int, str]:
    """{indice voce al taglio: parte della frase che parla solo di lei}. Una parte che
    nomina più prodotti non si assegna: lì decide l'LLM."""
    clauses = _ITEM_CLAUSE_SPLIT_RE.split(_normalize_for_match(message))
    found: dict[int, str] = {}
    for clause in clauses:
        named = [
            index for index, item in enumerate(items)
            if item.get("pizza_name")
            and re.search(rf"\b{re.escape(_normalize_for_match(item['pizza_name']))}\b", clause)
        ]
        if len(named) == 1 and items[named[0]].get("sale_unit") == "kg" and named[0] not in found:
            found[named[0]] = clause
    return found


def _apply_kg_clause_values(items: list[dict], message: str, temperature_only: bool = False) -> set[str]:
    """Caldo/freddo e "mezzo" detti nella stessa parte di frase del prodotto valgono
    sempre, anche sopra quanto ha estratto l'LLM. "Mezzo" senza chilo, etti o grammi è
    mezzo trancio, mai mezzo chilo. Restituisce i campi applicati."""
    applied: set[str] = set()
    for index, clause in _kg_item_clauses(items, message).items():
        item = items[index]
        hot, cold = bool(_CLAUSE_HOT_RE.search(clause)), bool(_CLAUSE_COLD_RE.search(clause))
        if hot != cold:
            temperature = "calda" if hot else "fredda"
            if item.get("temperature") != temperature:
                print(f"[KgClause] {item['pizza_name']!r}: temperatura dalla frase {temperature!r}")
            item["temperature"] = temperature
            _clear_kg_attempts(item, "temperature")
            applied.add("temperature")
        if (
            not temperature_only
            and kg_order_unit(item) == "kg"
            and not _WEIGHT_WORDS_RE.search(clause)
            and _CLAUSE_SLICE_RE.search(clause)
        ):
            said = _extract_kg_quantity(clause)
            item["order_unit"] = "tranci"
            item["quantity"] = said[1] if said and said[0] == "tranci" else 1
            size = _extract_kg_size(clause)
            item["size"] = size or "normale"
            print(f"[KgClause] {item['pizza_name']!r}: nessun peso detto → tranci, porzione={size!r}")
            applied.add("quantity")
            if size:
                applied.add("portion")
        elif (
            not temperature_only
            and kg_order_unit(item) == "tranci"
            and float(item.get("quantity") or 0) <= 0
            and not _WEIGHT_WORDS_RE.search(clause)
            and re.search(r"\bmezz[oa]\b", clause)
            and not _extract_kg_quantity(clause)
        ):
            # "e mezzo di salmone": un mezzo trancio
            item["quantity"] = 1
            if item.get("size") not in ("piena", "mezza"):
                item["size"] = "mezza"
            _clear_kg_attempts(item, "quantity")
            print(f"[KgClause] {item['pizza_name']!r}: \"mezzo\" senza numero → un mezzo trancio")
            applied.add("quantity")
    return applied


# ── Quantità, porzione e caldo/freddo per le voci al taglio ──────────────────
# Nessun valore predefinito: se il cliente non li dice, l'agente li chiede.
# La porzione (intero/mezzo) serve solo per gli ordini a tranci, non a peso.
# Dopo KG_SLOT_MAX_ATTEMPTS risposte non capite il campo resta vuoto e l'ordine
# viene salvato con needs_review (vedi _kg_review_reasons). Porzione e caldo/freddo
# hanno un passo in più: al secondo fallimento si propone un valore (sì/no).
_KG_SLOTS = ("quantity", "portion", "temperature")
_KG_SLOT_STATES = {
    "collecting_kg_quantity": "quantity",
    "collecting_kg_portion": "portion",
    "collecting_kg_temperature": "temperature",
}
_KG_STATE_BY_SLOT = {slot: state for state, slot in _KG_SLOT_STATES.items()}
KG_SLOT_MAX_ATTEMPTS = 2
_KG_SLOT_MAX_ATTEMPTS_BY_SLOT = {"quantity": KG_SLOT_MAX_ATTEMPTS, "portion": 3, "temperature": 3}
# Valore proposto al secondo fallimento; con "no" vale l'altro
_KG_PROPOSALS = {"portion": ("piena", "mezza"), "temperature": ("calda", "fredda")}


def _kg_slot_missing(item: dict, slot: str) -> bool:
    if item.get("sale_unit") != "kg" or item.get(f"_{slot}_unresolved"):
        return False
    unit = kg_order_unit(item)
    if slot == "quantity":
        quantity = float(item.get("quantity") or 0)
        # G4: i tranci sono sempre un numero intero
        return unit == "" or quantity <= 0 or (unit == "tranci" and quantity != int(quantity))
    if slot == "portion":
        return unit == "tranci" and item.get("size") not in ("piena", "mezza")
    return item.get("temperature") not in ("calda", "fredda")


def _kg_items_missing(items: list[dict], slot: str) -> list[dict]:
    """Voci al taglio a cui manca il campo `slot` e per cui va ancora chiesto."""
    return [item for item in items if _kg_slot_missing(item, slot)]


def _clear_kg_attempts(item: dict, slot: str) -> None:
    item.pop(f"_{slot}_attempts", None)
    item.pop(f"_{slot}_unresolved", None)
    item.pop(f"_{slot}_proposed", None)


def _apply_kg_proposal_answer(items: list[dict], slot: str, message: str) -> bool:
    """Sì/no alla proposta "Va bene interi?" / "Va bene caldi?": sì → valore
    proposto, no → l'altro. True se la risposta è stata applicata."""
    pending = [item for item in _kg_items_missing(items, slot) if item.get(f"_{slot}_proposed")]
    answer = _yes_no_answer(message) if pending else None
    if answer is None:
        return False
    for item in pending:
        proposed = item[f"_{slot}_proposed"]
        value = proposed if answer == "yes" else next(v for v in _KG_PROPOSALS[slot] if v != proposed)
        if slot == "portion":
            item["order_unit"] = "tranci"
            item["size"] = value
        else:
            item["temperature"] = value
        _clear_kg_attempts(item, slot)
    return True


def _apply_kg_answers_from_message(
    items: list[dict], message: str, bare_number_ok: bool = False
) -> set[str]:
    """Quantità, porzione e caldo/freddo detti nel messaggio → voci che li aspettano.
    Ogni campo riconosciuto si applica anche se la risposta è parziale."""
    applied: set[str] = set()
    quantity = _extract_kg_quantity(message, bare_number_ok=bare_number_ok)
    if quantity:
        unit, value = quantity
        for item in _kg_items_missing(items, "quantity"):
            item["order_unit"] = unit
            item["quantity"] = value
            if unit == "kg":
                item["size"] = "normale"
            _clear_kg_attempts(item, "quantity")
            applied.add("quantity")
    size = _extract_kg_size(message)
    if size:
        for item in items:
            if (
                item.get("sale_unit") == "kg"
                and kg_order_unit(item) != "kg"
                and item.get("size") not in ("piena", "mezza")
                and not item.get("_portion_unresolved")
            ):
                # "Interi" senza numero: è un ordine a tranci, il numero si chiede dopo
                item["order_unit"] = "tranci"
                item["size"] = size
                _clear_kg_attempts(item, "portion")
                applied.add("portion")
    temperature = _extract_temperature(message)
    if temperature:
        for item in _kg_items_missing(items, "temperature"):
            item["temperature"] = temperature
            _clear_kg_attempts(item, "temperature")
            applied.add("temperature")
    return applied


def _register_kg_miss(items: list[dict], slot: str) -> bool:
    """Risposta non capita alla domanda `slot`: conta il tentativo. Porzione e
    caldo/freddo al secondo propongono un valore; all'ultimo il campo resta vuoto
    (niente default) e si passa oltre. True se il campo è appena diventato da verificare."""
    max_attempts = _KG_SLOT_MAX_ATTEMPTS_BY_SLOT[slot]
    unresolved = False
    for item in _kg_items_missing(items, slot):
        attempts = int(item.get(f"_{slot}_attempts") or 0) + 1
        item[f"_{slot}_attempts"] = attempts
        if attempts >= max_attempts:
            item[f"_{slot}_unresolved"] = True
            item.pop(f"_{slot}_proposed", None)
            unresolved = True
            print(f"[KgSlot] {slot} non capito dopo {attempts} tentativi per {item.get('pizza_name')!r}: needs_review")
        elif slot in _KG_PROPOSALS and attempts == max_attempts - 1:
            item[f"_{slot}_proposed"] = _KG_PROPOSALS[slot][0]
    return unresolved


_KG_REVIEW_LABELS = {
    "quantity": "Quantità (peso o numero di tranci)",
    "portion": "Porzione (intero/mezzo)",
    "temperature": "Caldo/freddo",
}


def _kg_review_reasons(items: list[dict]) -> list[str]:
    reasons = []
    for slot in _KG_SLOTS:
        names = [item["pizza_name"] for item in items if item.get(f"_{slot}_unresolved")]
        if names:
            reasons.append(
                f"{_KG_REVIEW_LABELS[slot]} non capito dopo {_KG_SLOT_MAX_ATTEMPTS_BY_SLOT[slot]} tentativi per "
                f"{_format_pizza_list(names)}: chiedere al cliente"
            )
    return reasons


def _kg_temperature_question(items: list[dict]) -> str:
    if all(kg_order_unit(item) == "kg" for item in items):
        return "Calda o fredda?"
    if len(items) == 1 and kg_order_unit(items[0]) == "tranci" and slice_count(items[0]) == 1:
        return "Caldo o freddo?"
    return "Caldi o freddi?"


def _kg_slot_question(items: list[dict], slot: str | None = None) -> str:
    """Una sola domanda breve per tutto ciò che manca alle voci al taglio,
    es. "Quanti tranci, interi o mezzi? Caldi o freddi?". Il nome del prodotto
    compare solo se nel carrello ci sono più voci al taglio."""
    missing = {s: _kg_items_missing(items, s) for s in _KG_SLOTS}
    parts = []
    if missing["quantity"]:
        if all(item.get("size") in ("piena", "mezza") for item in missing["quantity"]):
            parts.append("Quanti tranci?")
        else:
            parts.append("Quanti tranci, interi o mezzi?")
    elif missing["portion"]:
        single = len(missing["portion"]) == 1 and slice_count(missing["portion"][0]) == 1
        parts.append("Intero o mezzo?" if single else "Interi o mezzi?")
    if missing["temperature"]:
        parts.append(_kg_temperature_question(missing["temperature"]))
    question = " ".join(parts)
    if not question:
        return ""

    first_slot = next(s for s in _KG_SLOTS if missing[s])
    retry = any(int(item.get(f"_{first_slot}_attempts") or 0) > 0 for item in missing[first_slot])
    proposed = [item for item in missing[first_slot] if item.get(f"_{first_slot}_proposed")]
    if proposed:
        question = _kg_proposal_question(first_slot, proposed)
        retry = False

    asked = list(dict.fromkeys(
        item["pizza_name"].lower() for s in _KG_SLOTS for item in missing[s]
    ))
    kg_names = {item["pizza_name"] for item in items if item.get("sale_unit") == "kg"}
    if len(kg_names) > 1:
        target = f"la {asked[0]}" if len(asked) == 1 else _format_pizza_list(asked)
        question = f"Per {target}, {question[0].lower()}{question[1:]}"
    if retry:
        question = f"Scusi, {question[0].lower()}{question[1:]}"
    return question


def _kg_proposal_question(slot: str, items: list[dict]) -> str:
    """ "Va bene interi? Mi dica sì o no." con l'accordo di _kg_temperature_question."""
    value = items[0][f"_{slot}_proposed"]
    if slot == "portion":
        single = len(items) == 1 and slice_count(items[0]) == 1
        word = {"piena": "intero" if single else "interi", "mezza": "mezzo" if single else "mezzi"}[value]
    else:
        temperature_question = _kg_temperature_question(items)  # "Calda o fredda?"
        word = temperature_question.split()[0 if value == "calda" else 2].rstrip("?").lower()
    return f"Va bene {word}? Mi dica sì o no."


# ── Correzioni al riepilogo ("Confermo?") ─────────────────────────────────────
# Parole di chi vuole cambiare qualcosa senza dire subito cosa, o con un altro gusto
_RECAP_CHANGE_WORDS_RE = re.compile(
    r"\b(?:invece|anziche|piuttosto|cambi\w*|modific\w*|sostitu\w*|togli\w*|correggi\w*|sbagliat\w*"
    r"|metti\w*|fammi\w*|facciamo\w*|fanne|aggiung\w*|pero)\b"
)
# "Facciamone tre", "mettine due": la nuova quantità della voce
_RECAP_COUNT_RE = re.compile(
    rf"\b(?:facciamone|fanne|mettine|fammene|dammene|ne\s+(?:voglio|vorrei|prendo|metta|metti|faccia|fai))\s+{_KG_NUM}\b"
)
RECAP_CHANGE_QUESTION_KG = "Va bene, cosa cambio? Il gusto, i tranci, intero o mezzo, caldo o freddo, o l'orario?"
RECAP_CHANGE_QUESTION = "Va bene, cosa cambio? Le pizze, l'orario o il nome?"


def _recap_change_question(items: list[dict]) -> str:
    return RECAP_CHANGE_QUESTION_KG if any(i.get("sale_unit") == "kg" for i in items) else RECAP_CHANGE_QUESTION


def _names_product_outside_cart(message: str, items: list[dict], menu_items: list[dict]) -> bool:
    """La frase nomina un prodotto del menu che non è nel carrello ("mettimi pepe rosa")."""
    said = " ".join(re.findall(r"[a-z0-9]+", _normalize_for_match(message)))
    in_cart = {_normalize_for_match(i.get("pizza_name") or "") for i in items}
    for entry in menu_items:
        name = " ".join(re.findall(r"[a-z0-9]+", _normalize_for_match(entry.get("name") or "")))
        if name and _normalize_for_match(entry.get("name") or "") not in in_cart and re.search(rf"\b{re.escape(name)}\b", said):
            return True
    return False


def _apply_recap_item_changes(items: list[dict], message: str) -> bool:
    """Intero/mezzo, caldo/freddo e quantità detti al riepilogo sostituiscono quelli del
    carrello. Valgono per il prodotto nominato, altrimenti per tutte le voci al taglio
    (la quantità solo se nel carrello c'è una voce). True se qualcosa è cambiato."""
    message = re.sub(r"\bnon\s+\w+", " ", _normalize_for_match(message))  # "mezzi, non interi"
    named = _kg_item_clauses(items, message)
    whole = message
    if named:
        targets = list(named.items())
    else:
        targets = [(index, whole) for index, item in enumerate(items) if item.get("sale_unit") == "kg"]
    changed = False
    for index, clause in targets:
        item = items[index]
        size = _extract_kg_size(clause)
        if size and kg_order_unit(item) != "kg" and item.get("size") != size:
            item["order_unit"], item["size"] = "tranci", size
            _clear_kg_attempts(item, "portion")
            changed = True
        hot, cold = bool(_CLAUSE_HOT_RE.search(clause)), bool(_CLAUSE_COLD_RE.search(clause))
        if hot != cold and item.get("temperature") != ("calda" if hot else "fredda"):
            item["temperature"] = "calda" if hot else "fredda"
            _clear_kg_attempts(item, "temperature")
            changed = True
        quantity = _extract_kg_quantity(clause) if named or len(targets) == 1 else None
        if quantity and (kg_order_unit(item), float(item.get("quantity") or 0)) != quantity:
            item["order_unit"], item["quantity"] = quantity
            if quantity[0] == "kg":
                item["size"] = "normale"
            _clear_kg_attempts(item, "quantity")
            changed = True
    count = _RECAP_COUNT_RE.search(whole)
    if count and len(items) == 1 and _kg_number(count.group(1)) >= 1:
        item = items[0]
        value = _kg_number(count.group(1))
        if item.get("sale_unit") == "kg":
            value = float(int(value))
            if kg_order_unit(item) != "kg" and float(item.get("quantity") or 0) != value:
                item["order_unit"], item["quantity"] = "tranci", value
                changed = True
        elif int(item.get("quantity") or 1) != int(value):
            item["quantity"] = int(value)
            changed = True
    return changed


def pickup_day_phrase(pickup_date: datetime.date) -> str:
    """'domani, giovedì' oppure solo il giorno ('martedì') se domani il locale è chiuso."""
    today = datetime.datetime.now(tz=ZoneInfo("Europe/Rome")).date()
    weekday = _WEEKDAY_IT[pickup_date.weekday()]
    if pickup_date == today + datetime.timedelta(days=1):
        return f"domani, {weekday}"
    if pickup_date >= today + datetime.timedelta(days=7):
        # Oltre la settimana il solo giorno della settimana sarebbe ambiguo
        return f"{weekday} {pickup_date.day} {list(_ITALIAN_MONTHS)[pickup_date.month - 1]}"
    return weekday


def _pickup_day_intro(day_phrase: str) -> str:
    if day_phrase.startswith("domani"):
        return f"Il ritiro è per {day_phrase}."
    return f"Domani siamo chiusi, il ritiro è per {day_phrase}."


def _dated_day(today: datetime.date, day: int, month: int, year: int | None = None) -> datetime.date | None:
    """Data detta dal cliente; senza anno e già passata vale per l'anno prossimo."""
    try:
        date = datetime.date(year or today.year, month, day)
        return date if year or date >= today else datetime.date(today.year + 1, month, day)
    except ValueError:
        return None


# "Sono Domenica", "a nome Sabato": non sono giorni
_NOT_A_DAY_LOOKBEHIND = r"(?<!sono )(?<!nome )(?<!chiamo )(?<!signora )(?<!signor )"
_WEEKDAY_NAMES_RE = "|".join(_ITALIAN_WEEKDAYS)
# "sabato 14", "sabato il 14": il numero è un giorno solo se la frase finisce lì o prosegue
# con l'ora ("sabato 4 persone" e "sabato 2 margherite" non sono date)
_WEEKDAY_AND_DAY_RE = re.compile(
    rf"\b(?:{_WEEKDAY_NAMES_RE})\s+(?:il\s+|l')?(\d{{1,2}}|primo)(?=\s*(?:$|[,.;!?]|(?:alle|per)\b|a\s+(?:pranzo|cena)\b))"
)
# "il 21", "l'8": solo quando si parla di una data (prenotazioni)
_BARE_DAY_RE = re.compile(r"(?:\bil\s+|\bl')(\d{1,2}|primo)\b(?!\s*(?:person|post[oi]\b|copert|[:.]\d|e\s+mezz))")


def _next_day_of_month(today: datetime.date, day: int) -> datetime.date | None:
    """Prossimo giorno `day` del mese, da oggi in poi (il 31 salta i mesi che non lo hanno)."""
    year, month = today.year, today.month
    for _ in range(13):
        try:
            candidate = datetime.date(year, month, day)
            if candidate >= today:
                return candidate
        except ValueError:
            pass
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return None


def _date_in_message(normalized: str, today: datetime.date, bare_day: bool = False) -> datetime.date | None:
    """Data detta nella frase: "21 novembre", "21/11", "sabato 21" e, con `bare_day`, "il 21"."""
    match = _SPOKEN_DATE_RE.search(normalized)
    if match:
        day = 1 if match.group(1) == "primo" else int(match.group(1))
        return _dated_day(today, day, _ITALIAN_MONTHS[match.group(2)])
    match = _SLASH_DATE_RE.search(normalized)
    if match:
        year = int(match.group(3)) if match.group(3) else None
        return _dated_day(today, int(match.group(1)), int(match.group(2)), year + 2000 if year and year < 100 else year)
    match = _WEEKDAY_AND_DAY_RE.search(normalized) or (_BARE_DAY_RE.search(normalized) if bare_day else None)
    if match:
        return _next_day_of_month(today, 1 if match.group(1) == "primo" else int(match.group(1)))
    return None


def _weekday_date_conflict(message: str, bare_day: bool = False) -> tuple[datetime.date, datetime.date] | None:
    """I due giorni possibili, in ordine, se giorno della settimana e data non coincidono
    ("venerdì 21 novembre" quando il 21 è sabato: venerdì 20 e sabato 21). None se coincidono
    o se manca uno dei due. Se il cliente nomina anche il giorno giusto non c'è conflitto."""
    normalized = _normalize_for_match(message)
    today = datetime.datetime.now(tz=ZoneInfo("Europe/Rome")).date()
    dated = _date_in_message(normalized, today, bare_day=bare_day)
    if dated is None:
        return None
    named = [
        _ITALIAN_WEEKDAYS[m.group(1)]
        for m in re.finditer(rf"{_NOT_A_DAY_LOOKBEHIND}\b({_WEEKDAY_NAMES_RE})\b", normalized)
    ]
    if not named or dated.weekday() in named:
        return None
    # Il giorno nominato più vicino alla data, mai nel passato
    closest = dated + datetime.timedelta(days=(named[0] - dated.weekday() + 3) % 7 - 3)
    if closest < today:
        closest += datetime.timedelta(days=7)
    return tuple(sorted((dated, closest)))


def _spoken_day_of_month(day: datetime.date, with_month: bool) -> str:
    number = "il primo" if day.day == 1 else f"l'{day.day}" if day.day in (8, 11) else f"il {day.day}"
    return f"{number} {list(_ITALIAN_MONTHS)[day.month - 1]}" if with_month else number


def _date_conflict_question(first: datetime.date, second: datetime.date) -> str:
    """"Venerdì è il 20, sabato è il 21: quale dei due?\""""
    with_month = first.month != second.month
    return (
        f"{_WEEKDAY_IT[first.weekday()].capitalize()} è {_spoken_day_of_month(first, with_month)}, "
        f"{_WEEKDAY_IT[second.weekday()]} è {_spoken_day_of_month(second, with_month)}: quale dei due?"
    )


def _resolve_date_conflict(message: str, candidates: list[str]) -> datetime.date | None:
    """Quale dei due giorni ha scelto il cliente: per giorno della settimana, numero o
    "il primo"/"il secondo". None se la risposta non ne indica esattamente uno."""
    days = [datetime.date.fromisoformat(c) for c in candidates]
    normalized = _TIME_IN_SENTENCE_RE.sub(" ", _normalize_for_match(message))
    chosen = set()
    for day in days:
        names = [name for name, weekday in _ITALIAN_WEEKDAYS.items() if weekday == day.weekday()]
        if re.search(rf"\b(?:{'|'.join(names)}|{day.day})\b", normalized):
            chosen.add(day)
    if not chosen and re.search(r"\bprimo\b", normalized):
        chosen.add(next((d for d in days if d.day == 1), days[0]))
    if not chosen and re.search(r"\bsecondo\b", normalized):
        chosen.add(days[1])
    return chosen.pop() if len(chosen) == 1 else None
_SPOKEN_DATE_RE = re.compile(r"\b(\d{1,2}|primo)\s+(?:di\s+)?(" + "|".join(_ITALIAN_MONTHS) + r")\b")
_SLASH_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_SPOKEN_TODAY_RE = re.compile(r"\b(?:oggi|stasera|sta\s+sera|questa\s+sera|stanotte)\b")


def _spoken_pickup_day(message: str, restaurant_id: str = "") -> datetime.date | None:
    """Giorno di ritiro detto esplicitamente: domani, dopodomani, una data, un giorno
    della settimana, stasera/oggi. None se non c'è. Il giorno può essere chiuso:
    lo decide chi chiama. Il giorno della settimana di oggi vale oggi finché restano
    orari di ritiro, altrimenti la settimana prossima."""
    normalized = _normalize_for_match(message)
    today = datetime.datetime.now(tz=ZoneInfo("Europe/Rome")).date()
    if re.search(r"\bdopodomani\b", normalized):
        return today + datetime.timedelta(days=2)
    if re.search(r"\bdomani\b", normalized):
        return today + datetime.timedelta(days=1)
    dated = _date_in_message(normalized, today)
    if dated:
        return dated
    for name, weekday in _ITALIAN_WEEKDAYS.items():
        if re.search(rf"{_NOT_A_DAY_LOOKBEHIND}\b{name}\b", normalized):
            days_ahead = (weekday - today.weekday()) % 7
            if days_ahead == 0 and earliest_pickup_minutes_today(restaurant_id) is None:
                days_ahead = 7
            return today + datetime.timedelta(days=days_ahead)
    return today if _SPOKEN_TODAY_RE.search(normalized) else None


def _is_open_day(day: datetime.date, restaurant_id: str = "") -> bool:
    if not load_restaurant(restaurant_id=restaurant_id).get("opening_hours"):
        return True
    return bool(get_opening_range_text(restaurant_id, day))


def _first_open_day_after(day: datetime.date, restaurant_id: str = "") -> datetime.date | None:
    for days_ahead in range(1, 8):
        candidate = day + datetime.timedelta(days=days_ahead)
        if _is_open_day(candidate, restaurant_id):
            return candidate
    return None


def _closed_day_question(closed_day: datetime.date, proposal: datetime.date) -> str:
    """"Domani siamo chiusi, il primo giorno disponibile è martedì, va bene?\""""
    closed = pickup_day_phrase(closed_day).split(",")[0]
    return f"{closed[0].upper()}{closed[1:]} siamo chiusi, il primo giorno disponibile è {pickup_day_phrase(proposal)}, va bene?"


def _time_in_sentence(message: str) -> str | None:
    """Orario detto dentro una frase ("per domani alle 8"): deve seguire "alle"/"per le"."""
    match = _TIME_IN_SENTENCE_RE.search(_normalize_for_match(message))
    return _extract_local_pickup_time(match.group(0)) if match else None


def _is_new_time_at_recap(message: str, current_time: str | None) -> bool:
    """La frase detta al riepilogo dà un orario diverso da quello riepilogato. Un numero
    senza "alle"/"per le" vale solo con i minuti o da solo ("8.45"): "due persone" no."""
    spoken = _time_in_sentence(message)
    if not spoken and re.search(r"\d", message):
        spoken = _extract_local_pickup_time(message)
    if not spoken or spoken == "prima_possibile":
        return False

    def minutes(value: str | None) -> int | None:
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", value or "")
        return int(match.group(1)) % 12 * 60 + int(match.group(2)) if match else None

    # "alle 8" al riepilogo delle 20:00 è lo stesso orario
    return minutes(spoken) != minutes(current_time)


_OFF_MENU_PIZZA_NAMES = ("Personalizzata", "Pizza personalizzata")
_SPOKEN_COUNT_RE = re.compile(r"\b(?:una|un|uno|due|tre|quattro|cinque|sei|sette|otto|nove|dieci|\d+)\b")
PICKUP_DAY_QUESTION = "Per quale giorno le serve?"


def _similar_menu_flavours(words: list[str], menu_items: list[dict], limit: int = 3) -> list[str]:
    """I gusti del menu più simili alle parole dette: confronto col nome e con gli
    ingredienti. A pari somiglianza prima il gusto più semplice (meno ingredienti);
    senza parole da confrontare, i primi del menu."""
    wanted = [word for phrase in words for word in _normalize_for_match(phrase).split()]

    def score(entry: dict) -> float:
        tokens = _normalize_for_match(" ".join([entry["name"], *(entry.get("ingredients") or [])])).split()
        return sum(max((SequenceMatcher(None, word, token).ratio() for token in tokens), default=0.0) for word in wanted)

    ranked = (
        sorted(menu_items, key=lambda entry: (-round(score(entry), 3), len(entry.get("ingredients") or [])))
        if wanted
        else list(menu_items)
    )
    return [entry["name"] for entry in ranked[:limit]]


def _off_menu_question(items: list[dict], menu_items: list[dict], unavailable_names: set[str] | None = None) -> str:
    """ "Salamino non è nel menu, abbiamo Diavola, Napoli e Romana. Quale vuole?"
    "Oggi non c'è" solo per un prodotto del menu non disponibile (`unavailable_names`,
    nomi normalizzati), mai per un nome che nel menu non esiste."""
    names = [_format_pizza_list(item.get("add_ingredients") or []) or "quella pizza" for item in items]
    spoken = _format_pizza_list(names)
    on_menu = bool(unavailable_names) and all(_normalize_for_match(name) in unavailable_names for name in names)
    if on_menu:
        verb = "oggi non c'è" if len(items) == 1 else "oggi non ci sono"
    else:
        verb = "non è nel menu" if len(items) == 1 else "non sono nel menu"
    alternatives = _format_pizza_list(_similar_menu_flavours(items[0].get("add_ingredients") or [], menu_items))
    return f"{spoken[0].upper()}{spoken[1:]} {verb}, abbiamo {alternatives}. Quale vuole?"


_NAME_FILLER_WORDS = {"e", "ed", "di", "del", "della", "con", "al", "alla", "la", "il", "lo", "le", "i", "gli"}


def _name_words(text: str) -> list[str]:
    return [re.sub(r"h?[aeiou]$", "", w) for w in re.findall(r"[a-z0-9]+", _normalize_for_match(text))
            if w not in _NAME_FILLER_WORDS]


def _only_menu_name_containing(said: str, menu_items: list[dict]) -> str | None:
    """Il solo prodotto del menu il cui nome contiene tutte le parole dette ("Rosa" →
    "Pepe Rosa"); None se nessuno o più di uno."""
    words = _name_words(said)
    if not words:
        return None
    matches = [entry["name"] for entry in menu_items if set(words) <= set(_name_words(entry["name"]))]
    return matches[0] if len(matches) == 1 else None


def _looks_like_product_name(said: str, restaurant_name: str = "", customer_name: str | None = None) -> bool:
    """Un nome detto come prodotto che non è un saluto, il nome del locale o il nome del
    cliente: "capricciosa" sì, "salve" o "Giacomo" no."""
    def plain(text: str) -> list[str]:
        return [w for w in re.findall(r"[a-z0-9]+", _normalize_for_match(text or "")) if w not in _NAME_FILLER_WORDS]

    words = plain(said)
    if not words or is_only_greeting(said, restaurant_name) or words == plain(restaurant_name):
        return False
    return not (customer_name and words == plain(customer_name))


def _off_menu_proposal_question(name: str) -> str:
    return f"Intende {name}?"


def _close_menu_name(said: str, menu_items: list[dict]) -> str | None:
    """Il solo prodotto del menu di cui `said` è chiaramente una parte o una storpiatura:
    ogni parola detta è una parola del nome o quasi ("Rosa" → "Pepe Rosa", "pistaxi" →
    "Zucca e Pistacchi"). None se nessuno o più di uno: "capricciosa" non è vicino a nulla."""
    def words(text: str) -> list[str]:
        return [w for w in re.findall(r"[a-z0-9]+", _normalize_for_match(text or ""))
                if w not in _NAME_FILLER_WORDS and not _SPOKEN_COUNT_RE.fullmatch(w)]

    def close(word: str, name_word: str) -> bool:
        if word == name_word:
            return True
        return min(len(word), len(name_word)) >= 4 and SequenceMatcher(None, word, name_word).ratio() >= 0.75

    said_words = words(said)
    if not said_words:
        return None
    matches = [
        entry["name"] for entry in menu_items
        if all(any(close(w, n) for n in words(entry["name"])) for w in said_words)
    ]
    return matches[0] if len(matches) == 1 else None


def _off_menu_last_list(items: list[dict], menu_items: list[dict]) -> str:
    """Terza volta sullo stesso gusto senza un nome vicino: niente "Intende", si dice che
    non c'è e si propongono i gusti del menu."""
    name = _format_pizza_list(items[0].get("add_ingredients") or []) or "quella pizza"
    alternatives = _format_pizza_list(_similar_menu_flavours(items[0].get("add_ingredients") or [], menu_items))
    return f"Mi dispiace, {name} non c'è. Posso proporle {alternatives}: quale preferisce?"


# ── Controlli deterministici per i locali senza pizza personalizzata (PaP) ──
# Si applicano dopo l'estrazione dell'LLM e prima di ogni riepilogo: l'LLM propone,
# il codice decide unità, quantità, gusto e modifiche.

# G1: un peso vale solo se il cliente in questa chiamata ha detto chili, etti o grammi
_PAP_WEIGHT_SAID_RE = re.compile(r"\b(?:chil[oi]|kg|ett[oi]|hg|grammi|grammo)\b")
# G4: la quantità di una riga cambia solo se il cliente dice un numero o "anche/in più/altri"
_PAP_QUANTITY_SAID_RE = re.compile(
    r"\b(?:\d+|un|uno|una|due|tre|quattro|cinque|sei|sette|otto|nove|dieci|mezz[oaie]|solo|sol[oa]mente)\b"
)
_SUBSTITUTION_VERB_WORDS = {
    "mettimi", "mettici", "metti", "metta", "mette", "fammi", "fai", "faccia", "voglio", "vorrei",
    "prendo", "dammi", "dia", "meglio", "mi", "ci",
}
_SUBSTITUTION_NOISE_WORDS = {
    "per", "favore", "grazie", "pure", "allora", "piuttosto", "quello", "quella", "gusto", "no", "pero",
    "aspetta", "anzi",
}
_SUBSTITUTION_ARTICLES = {"il", "la", "lo", "l", "un", "una", "uno", "del", "della", "dello", "dell", "di", "dei",
                          "degli", "delle"}
_SUBSTITUTION_MARKERS = (("invece",), ("al", "posto"), ("anziche",))


def _substitution_words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", _normalize_for_match(text)) if w not in _SUBSTITUTION_NOISE_WORDS]


def _flavour_in(words: list[str], entries: list[dict]) -> str | None:
    """Il solo nome in `entries` di cui `words` (senza verbi e articoli ai lati) è una
    parte o una storpiatura."""
    while words and (words[0] in _SUBSTITUTION_VERB_WORDS or words[0] in _SUBSTITUTION_ARTICLES):
        words = words[1:]
    return _close_menu_name(" ".join(words), entries) if words else None


def _split_substitution(old_part: list[str], new_part: list[str], cart_names: list[dict],
                        menu_items: list[dict]) -> tuple[str, str] | None:
    old_name, new_name = _flavour_in(old_part, cart_names), _flavour_in(new_part, menu_items)
    return (old_name, new_name) if old_name and new_name and old_name != new_name else None


def _parse_flavour_substitution(message: str, cart: list[dict], menu_items: list[dict],
                                at_recap: bool) -> tuple[int, str] | None:
    """(indice della riga da cambiare, gusto nuovo del menu) per "invece di X mettimi Y",
    "al posto di X Y", "mettimi Y invece di X", "cambia X con Y" e, al riepilogo con un
    solo gusto nel carrello, "mettimi Y". None se la frase non è una sostituzione o i
    nomi non si risolvono in un solo gusto."""
    words = _substitution_words(message)
    cart_names = [{"name": line.get("pizza_name", "")} for line in cart]
    found: tuple[str, str] | None = None
    for marker in _SUBSTITUTION_MARKERS:
        for at in range(len(words) - len(marker) + 1):
            if tuple(words[at:at + len(marker)]) != marker:
                continue
            before, after = words[:at], words[at + len(marker):]
            if before and any(w in _SUBSTITUTION_VERB_WORDS for w in before):
                # "Mettimi il pepe rosa invece della zucca"
                found = found or _split_substitution(after, before, cart_names, menu_items)
            for cut in range(1, len(after)):
                # "Invece della zucca (mettimi) il pepe rosa"
                found = found or _split_substitution(after[:cut], after[cut:], cart_names, menu_items)
            if not found and before:
                found = _split_substitution(after, before, cart_names, menu_items)
    if not found:
        for at, word in enumerate(words):
            if re.fullmatch(r"cambi(?:a|ami|amo|are|ate)", word) and "con" in words[at + 1:]:
                cut = words.index("con", at + 1)
                found = _split_substitution(words[at + 1:cut], words[cut + 1:], cart_names, menu_items)
                break
    if found:
        old_name, new_name = found
        return next(i for i, line in enumerate(cart) if line.get("pizza_name") == old_name), new_name
    flavours = {line.get("pizza_name") for line in cart}
    if at_recap and len(flavours) == 1 and words and words[0] in _SUBSTITUTION_VERB_WORDS - {"mi", "ci"}:
        new_name = _flavour_in(words, menu_items)
        if new_name and new_name not in flavours:
            return 0, new_name
    return None


def _substitute_flavour(line: dict, new_name: str, row) -> dict:
    """G3: la riga cambia gusto con la STESSA quantità, porzione e temperatura."""
    replaced = {
        **line, "pizza_name": new_name, "add_ingredients": [], "remove_ingredients": [],
        "pizza_type": getattr(row, "pizza_type", None) or line.get("pizza_type", "Normale"),
        "sale_unit": getattr(row, "sale_unit", None) or line.get("sale_unit", "piece"),
    }
    if replaced["sale_unit"] != line.get("sale_unit"):
        # A pezzo ↔ al taglio: il numero resta, porzione e caldo/freddo si chiedono
        replaced["quantity"] = max(1, int(round(float(line.get("quantity") or 1))))
        replaced["size"] = "normale"
        replaced["order_unit"] = "tranci" if replaced["sale_unit"] == "kg" else ""
        replaced["temperature"] = ""
    for key in [k for k in replaced if k.startswith("_") and k.endswith("_unresolved")]:
        replaced.pop(key)
    return replaced


def _pap_guard_cart(items: list[dict], *, before: list[dict], message: str, weight_said: bool,
                    menu_names: set[str] | None = None) -> list[str]:
    """G1, G2, G4: corregge le righe che non possono andare al riepilogo così come sono e
    restituisce i motivi (per il log). Un campo tolto viene poi chiesto da determine_state."""
    problems: list[str] = []
    said = _normalize_for_match(message)
    quantity_said = bool(_PAP_QUANTITY_SAID_RE.search(said) or _EXPLICIT_ADD_RE.search(said))
    kept: list[dict] = []
    for line in items:
        name = line.get("pizza_name")
        if menu_names is not None and name not in menu_names:
            problems.append(f"{name!r} non è un gusto del menu: riga tolta")
            continue
        if line.get("add_ingredients") or line.get("remove_ingredients"):
            problems.append(
                f"{name!r} con modifiche add={line.get('add_ingredients')} remove={line.get('remove_ingredients')}: tolte"
            )
            line["add_ingredients"], line["remove_ingredients"] = [], []
        if line.get("sale_unit") == "kg":
            unchanged = any(
                o.get("pizza_name") == name and kg_order_unit(o) == kg_order_unit(line)
                and float(o.get("quantity") or 0) == float(line.get("quantity") or 0)
                for o in before
            )
            if kg_order_unit(line) == "kg" and not weight_said and not unchanged:
                quantity = float(line.get("quantity") or 0)
                spoken = _extract_kg_quantity(message)
                if spoken and spoken[0] == "tranci":
                    quantity = spoken[1]
                elif quantity != int(quantity) or quantity < 1:
                    quantity = 0
                problems.append(f"{name!r} a peso senza chili/etti/grammi detti: tranci, quantità {quantity:g}")
                line["order_unit"] = "tranci"
                line["quantity"] = float(quantity)
                if line.get("size") not in ("piena", "mezza"):
                    line["size"] = "normale"
            if kg_order_unit(line) == "tranci" and not float(line.get("quantity") or 0):
                # "Intrancio di pepe rosa": il numero detto vicino al gusto vale
                clause = _kg_item_clauses([line], message).get(0) or (message if len(items) == 1 else "")
                spoken = _extract_kg_quantity(clause) if clause else None
                if spoken and spoken[0] == "tranci":
                    line["quantity"] = float(spoken[1])
            if kg_order_unit(line) == "tranci":
                quantity = float(line.get("quantity") or 0)
                if quantity and quantity != int(quantity):
                    problems.append(f"{name!r}: {quantity:g} tranci non è un numero intero, si chiede")
                    line["quantity"] = 0
        elif line.get("size") in ("piena", "mezza"):
            problems.append(f"{name!r} a pezzo con porzione {line.get('size')!r}: tolta")
            line["size"] = "normale"
        if not quantity_said:
            old = next(
                (o for o in before if o.get("pizza_name") == name
                 and o.get("order_unit", "") == line.get("order_unit", "")
                 and float(o.get("quantity") or 0) != float(line.get("quantity") or 0)
                 and float(o.get("quantity") or 0) > 0),
                None,
            )
            if old is not None and sum(1 for o in before if o.get("pizza_name") == name) == 1:
                problems.append(
                    f"{name!r}: quantità {line.get('quantity')} senza un numero detto, resta {old.get('quantity')}"
                )
                line["quantity"] = old["quantity"]
        kept.append(line)
    items[:] = kept
    for problem in problems:
        print(f"[Guard] riga sospetta: {problem}")
    return problems


FLAVOUR_CHANGE_QUESTION = "Gli ingredienti dei gusti non si possono cambiare. Vuole cambiare gusto?"
FLAVOUR_CHANGE_WHICH = "Quale gusto vuole al posto di {name}?"


# Risposta a "vuole ordinare per domani?" che non aggiunge altro: niente LLM
_NEXT_DAY_ACCEPT_WORDS = {
    "no", "si", "ok", "okay", "va", "bene", "certo", "volentieri", "d'accordo", "perfetto", "esatto",
    "grazie", "mille", "per", "dai", "pure", "allora", "domani", "dopodomani", *_ITALIAN_WEEKDAYS,
}


def _is_bare_next_day_answer(message: str) -> bool:
    return all(word in _NEXT_DAY_ACCEPT_WORDS for word in re.findall(r"[\w']+", _normalize_for_match(message)))


def _spoken_pickup_time(pickup_time: str | None) -> str:
    """'20:00' → '20', '20:30' → '20:30'."""
    match = re.fullmatch(r"(\d{1,2}):00", pickup_time or "")
    return str(int(match.group(1))) if match else str(pickup_time)


def _today_phrase(pickup_time: str | None) -> str:
    """Ritiro in giornata: 'stasera' dalle 17 in poi, altrimenti 'oggi'."""
    match = re.match(r"^(\d{1,2}):", pickup_time or "")
    return "stasera" if match and int(match.group(1)) >= 17 else "oggi"


INTENT_QUESTION = "Vuole ordinare da asporto o prenotare un tavolo?"
CORRECTION_QUESTION = "Va bene, cosa devo correggere?"
CORRECTION_FIELDS_QUESTION = "Mi dica pure cosa cambiare: il giorno, l'ora, le persone o il nome."
CALLBACK_REVIEW_REASON = "Cliente non capito dopo 3 correzioni"
CALLBACK_GOODBYE = "La faccio richiamare dal locale appena possibile. A presto!"
CALLBACK_HANDOFF_MESSAGE = "La passo subito al locale, resti in linea."
CALLBACK_NOT_POSSIBLE = "Mi dispiace, al momento non riesco a prendere la richiesta. La preghiamo di richiamare tra qualche minuto."
_BARE_NO_RE = re.compile(r"^(?:ma\s+)?no(?:\s+no)*(?:\s+(?:aspetta|aspetti|scusa|scusi|guarda|guardi|sbagliato))*$")
# "lo sport" e "la sporto": "l'asporto" come lo scrive l'STT
_ORDER_ANSWER_RE = re.compile(
    r"\b(?:ordin\w*|asporto|ritiro|ritirare|da\s+portare\s+via|(?:lo|la|l|da|d)\s+sport[oa]?)\b"
)
_CORRECTION_FILLER = {
    "no", "il", "la", "le", "l", "i", "lo", "e", "sbagliato", "sbagliata", "sbagliate", "cambiare", "cambia",
    "correggere", "devi", "deve", "ha", "capito", "male", "solo", "numero", "di",
}
_CORRECTION_FIELDS = {
    "giorno": "date", "data": "date", "ora": "time", "orario": "time", "persone": "party", "nome": "name",
    "pizza": "items", "pizze": "items", "ordine": "items", "prodotti": "items", "prodotto": "items",
}


def _is_bare_no(message: str) -> bool:
    """Un "no" senza altri dati, anche ripetuto: "No no no no."."""
    words = re.findall(r"[a-z]+", _normalize_for_match(message))
    return bool(words) and _BARE_NO_RE.match(" ".join(words)) is not None


def _correction_field(message: str) -> str | None:
    """Il dato da correggere se il cliente nomina solo quello ("il giorno", "no, l'ora")."""
    words = [w for w in re.findall(r"[a-z]+", _normalize_for_match(message)) if w not in _CORRECTION_FILLER]
    return _CORRECTION_FIELDS.get(words[0]) if len(words) == 1 else None


def _closed_kitchen_intent_question(restaurant_id: str = "") -> tuple[datetime.date, str]:
    """Cucina chiusa e intento non ancora noto: (prossimo giorno aperto, domanda)."""
    next_date, weekday = get_next_open_day(restaurant_id=restaurant_id)
    today = datetime.datetime.now(tz=ZoneInfo("Europe/Rome")).date()
    when = "domani" if next_date == today + datetime.timedelta(days=1) else weekday
    return next_date, f"La cucina stasera è chiusa. Vuole ordinare per {when} o prenotare un tavolo?"


def _next_day_question(restaurant_id: str = "") -> str:
    """Domanda quando oggi non restano orari di ritiro (locali senza preordine automatico)."""
    next_date, weekday = get_next_open_day(restaurant_id=restaurant_id)
    today = datetime.datetime.now(tz=ZoneInfo("Europe/Rome")).date()
    when = "domani" if next_date == today + datetime.timedelta(days=1) else weekday
    return f"Per stasera la cucina è chiusa, vuole ordinare per {when}?"


def build_confirmation_recap(merged_order: dict, day_phrase: str | None = None) -> str:
    """Riepilogo finale unico e breve: quantità, porzione, gusto, caldo/freddo,
    giorno, ora e nome. Es. "Allora: due tranci interi di bufala, freddi, per
    domani giovedì alle 20, a nome Giacomo. Confermo?"

    Costruito dagli stessi item che vengono salvati, quindi coincide con l'ordine.
    """
    items = merged_order.get("items", [])
    items_text = "; ".join(format_single_item_for_customer(item) for item in items)
    at = f"alle {_spoken_pickup_time(merged_order.get('pickup_time'))}"
    when = f"per {(day_phrase or _today_phrase(merged_order.get('pickup_time'))).replace(',', '')} {at}"
    name = merged_order.get("customer_name")
    name_part = f", a nome {name}" if name and name != UNKNOWN_CUSTOMER_NAME else ""
    return f"Allora: {items_text}, {when}{name_part}. Confermo?"


_TODAY_ORDER_RE = re.compile(
    r"\b(?:per\s+oggi|oggi\s+stesso|in\s+giornata|entro\s+oggi|stasera|sta\s+sera|questa\s+sera"
    r"|oggi\s+pomeriggio|questo\s+pomeriggio|per\s+pranzo\s+oggi)\b",
    re.IGNORECASE,
)


def _is_today_order_request(message: str) -> bool:
    """Return True if the message explicitly asks for a same-day order."""
    return bool(_TODAY_ORDER_RE.search(message))


@router.post("/", response_model=ChatResponse)
def chat(request: ChatRequest, session: SessionDep):
    request_started_at = time.perf_counter()

    # Carica la sessione prima di qualsiasi controllo
    session_statement = select(ConversationSession).where(
        ConversationSession.session_id == request.session_id
    )
    conversation = session.exec(session_statement).first()
    if not conversation:
        conversation = ConversationSession(
            session_id=request.session_id,
            customer_name=None,
            pickup_time=None,
            items_json="[]",
            suggested_items_json="[]",
            state="collecting_items",
            completed=False,
        )
        session.add(conversation)
        session.commit()
        session.refresh(conversation)

    # Restaurant scope for this session (resolved at call time in voice_incoming)
    restaurant_id: str = conversation.restaurant_id or ""

    # Senza configurazione del locale (orari, flag) non si può confermare nulla:
    # meglio un messaggio di servizio che un ordine con dati sbagliati.
    if not ensure_restaurant_config(restaurant_id):
        print(f"[Chat] Configurazione assente per restaurant_id={restaurant_id!r} → servizio non disponibile")
        _log_chat_timing(request.session_id, "restaurant_unavailable", request_started_at)
        return ChatResponse(
            session_id=request.session_id,
            user_message=request.message,
            extracted_order={},
            merged_order={},
            valid=False,
            missing_items=[],
            response_message=SERVICE_UNAVAILABLE_MESSAGE,
            order_id=None,
            state="unavailable",
        )

    if not is_agent_active(restaurant_id=restaurant_id):
        print("[Chat] agent_active=False → rifiuto messaggio")
        _log_chat_timing(request.session_id, "agent_closed", request_started_at)
        return ChatResponse(
            session_id=request.session_id,
            user_message=request.message,
            extracted_order={},
            merged_order={},
            valid=False,
            missing_items=[],
            response_message=build_closed_message(restaurant_id=restaurant_id),
            order_id=None,
            state="closed",
        )

    message_lower = unicodedata.normalize("NFC", request.message.lower())

    if conversation.completed and conversation.state == "reservation_completed":
        res_data = json.loads(conversation.reservation_json or "{}")
        response_message = "La prenotazione è già stata confermata. A presto!"
        extracted_stub = {"intent": "reservation", "items": [], "customer_name": None, "pickup_time": None}
        session.add(ConversationLog(
            session_id=request.session_id,
            user_message=request.message,
            extracted_order_json=json.dumps(extracted_stub, ensure_ascii=False),
            merged_order_json=json.dumps(res_data, ensure_ascii=False),
            response_message=response_message,
            valid=True,
            missing_items_json="[]",
            state="reservation_completed",
        ))
        session.commit()
        _log_chat_timing(request.session_id, "reservation_completed_repeat", request_started_at)
        return ChatResponse(
            session_id=request.session_id,
            user_message=request.message,
            extracted_order=extracted_stub,
            merged_order=res_data,
            valid=True,
            missing_items=[],
            response_message=response_message,
            order_id=None,
            state="reservation_completed",
        )

    if conversation.completed:
        existing_items = json.loads(conversation.items_json)
        merged_order = {
            "customer_name": conversation.customer_name,
            "pickup_time": conversation.pickup_time,
            "items": existing_items,
        }
        existing_order = _get_order_for_session(session, conversation.session_id)
        response_message = build_assistant_response(
            merged_order=merged_order,
            state="completed",
            missing_messages=[],
            order_saved=True,
            intent="already_completed",
            new_valid_items=[],
            customer_phone=conversation.customer_phone,
        )
        extracted_stub = {
            "intent": "already_completed",
            "items": [],
            "customer_name": None,
            "pickup_time": None,
        }
        session.add(ConversationLog(
            session_id=request.session_id,
            user_message=request.message,
            extracted_order_json=json.dumps(extracted_stub, ensure_ascii=False),
            merged_order_json=json.dumps(merged_order, ensure_ascii=False),
            response_message=response_message,
            valid=True,
            missing_items_json="[]",
            state="completed",
        ))
        session.commit()
        _log_chat_timing(request.session_id, "completed_repeat", request_started_at)
        return ChatResponse(
            session_id=request.session_id,
            user_message=request.message,
            extracted_order=extracted_stub,
            merged_order=merged_order,
            valid=True,
            missing_items=[],
            response_message=response_message,
            order_id=existing_order.id if existing_order else None,
            state="completed",
        )

    # ── FLUSSO PRENOTAZIONE ───────────────────────────────────────────────────
    # Rilevamento nel primo turno (collecting_items + carrello vuoto)
    _res_pref_ack = {"pending": False}

    def _capture_reservation_preferences() -> None:
        """Preferenza di sala e richieste particolari dette in un turno di prenotazione."""
        room, note = _extract_reservation_preferences(request.message)
        if not room and not note:
            return
        res_data = json.loads(conversation.reservation_json or "{}")
        if room:
            res_data["preferred_room"] = room
        if note and note not in (res_data.get("notes") or []):
            res_data["notes"] = [*(res_data.get("notes") or []), note]
        conversation.reservation_json = json.dumps(res_data, ensure_ascii=False)
        _res_pref_ack["pending"] = bool(room)
        print(f"[Reservation] Preferenza registrata: sala={room!r} nota={bool(note)}")

    def _known_customer_notes() -> str:
        """Tavolo e note abituali del cliente già noto dal numero di telefono."""
        if not conversation.customer_phone:
            return ""
        try:
            return customer_reservation_notes(lookup_customer(conversation.customer_phone, restaurant_id=restaurant_id))
        except Exception as exc:
            print(f"[Reservation] Lookup cliente per le note non riuscito: {type(exc).__name__}")
            return ""

    def _prefs_only_reservation_json() -> str:
        """Ricomincia dal giorno tenendo preferenza di sala e note già dette."""
        res_data = json.loads(conversation.reservation_json or "{}")
        kept = {k: res_data[k] for k in ("preferred_room", "notes") if res_data.get(k)}
        return json.dumps(kept, ensure_ascii=False)

    def _save_large_group_request(res_data: dict, review_reason: str, phone: str | None = None) -> str | None:
        """Salva la richiesta di un gruppo oltre il limite come "da_confermare" e ne tiene
        l'id in res_data, così i turni successivi la aggiornano invece di duplicarla."""
        _notes = [_reservation_notes_text(res_data), _known_customer_notes()]
        reservation_id = save_reservation_to_base44(
            customer_name=conversation.customer_name or "Ospite",
            customer_phone=phone or conversation.customer_phone,
            date=res_data.get("date", ""),
            time=res_data.get("time", ""),
            party_size=int(res_data.get("party_size") or 0),
            session_id=request.session_id,
            notes=" · ".join(n for n in _notes if n),
            restaurant_id=restaurant_id,
            preferred_room=res_data.get("preferred_room"),
            status="da_confermare",
            review_reason=review_reason,
        )
        if reservation_id:
            res_data["pending_reservation_id"] = reservation_id
            res_data["pending_review_reason"] = review_reason
            conversation.reservation_json = json.dumps(res_data, ensure_ascii=False)
        return reservation_id

    def _ask_reservation_date_conflict(days: tuple) -> str:
        """Giorno e data non coincidono: si chiede quale dei due, senza scegliere."""
        res_data = json.loads(conversation.reservation_json or "{}")
        res_data["date_conflict"] = [day.isoformat() for day in days]
        conversation.reservation_json = json.dumps(res_data, ensure_ascii=False)
        conversation.state = "collecting_reservation_date"
        print(f"[Reservation] giorno e data non coincidono: chiedo tra {days[0]} e {days[1]}")
        return _date_conflict_question(*days)

    def _res_response(state: str, resp: str, valid: bool = False) -> ChatResponse:
        if _res_pref_ack["pending"]:
            resp = f"{RESERVATION_PREFERENCE_ACK} {resp}"
            _res_pref_ack["pending"] = False
        stub = {"intent": "reservation", "items": [], "customer_name": None, "pickup_time": None}
        res_data = json.loads(conversation.reservation_json or "{}")
        session.add(ConversationLog(
            session_id=request.session_id,
            user_message=request.message,
            extracted_order_json=json.dumps(stub, ensure_ascii=False),
            merged_order_json=json.dumps(res_data, ensure_ascii=False),
            response_message=resp,
            valid=valid,
            missing_items_json="[]",
            state=state,
        ))
        session.add(conversation)
        session.commit()
        return ChatResponse(
            session_id=request.session_id,
            user_message=request.message,
            extracted_order=stub,
            merged_order=res_data,
            valid=valid,
            missing_items=[],
            response_message=resp,
            order_id=None,
            state=state,
        )

    # Quando le prenotazioni sono disabilitate (modalità asporto puro):
    # - ignora qualsiasi intent di prenotazione e riporta al flusso ordine
    # - reindirizza eventuali stati di prenotazione rimasti aperti
    _reservations_on = is_reservations_enabled(restaurant_id=restaurant_id)
    # Locali con phone_orders_next_day_only (es. Pizza a Pezzi): ogni ordine telefonico
    # è un preordine per il prossimo giorno di apertura. Il giorno si fissa una volta
    # per sessione, così riepilogo e ordine salvato coincidono anche a cavallo della mezzanotte.
    _next_day_only = is_phone_orders_next_day_only(restaurant_id=restaurant_id)
    _preorder_date: "datetime.date | None" = None
    _day_phrase: "str | None" = None
    if _next_day_only:
        _today_rome = datetime.datetime.now(tz=ZoneInfo("Europe/Rome")).date()
        if conversation.pickup_date and datetime.date.fromisoformat(conversation.pickup_date) > _today_rome:
            _preorder_date = datetime.date.fromisoformat(conversation.pickup_date)
        else:
            _preorder_date, _ = get_next_open_day(restaurant_id=restaurant_id)
            conversation.pickup_date = _preorder_date.isoformat()
            session.add(conversation)
            session.commit()
        _day_phrase = pickup_day_phrase(_preorder_date)
        print(f"[Preorder] ritiro {_preorder_date} ({_day_phrase})")
    elif conversation.pickup_date:
        # Altri locali: il ritiro per un giorno successivo vale solo se il cliente
        # l'ha detto o l'ha accettato ("vuole ordinare per domani?" → sì).
        _agreed_date = datetime.date.fromisoformat(conversation.pickup_date)
        if _agreed_date > datetime.datetime.now(tz=ZoneInfo("Europe/Rome")).date():
            _preorder_date = _agreed_date
            _day_phrase = pickup_day_phrase(_preorder_date)
            print(f"[Pickup] ritiro concordato per {_preorder_date} ({_day_phrase})")
    # Oggi non restano orari di ritiro: per i locali senza preordine automatico si chiede
    # al cliente se vuole ordinare per il prossimo giorno di apertura, senza spostarlo da soli.
    _ask_next_day = (
        not _next_day_only
        and _preorder_date is None
        and earliest_pickup_minutes_today(restaurant_id) is None
    )

    def _check_pickup_time(pickup_time: str) -> tuple[str | None, str | None]:
        """(orario valido, None) oppure (None, messaggio d'errore per il cliente)."""
        is_valid, suggestion, closing_time = validate_pickup_time(
            pickup_time, restaurant_id=restaurant_id, pickup_date=_preorder_date,
        )
        if is_valid:
            return pickup_time, None
        # "alle 8" detto al mattino è quasi sempre le 20
        hour_match = re.match(r"^(\d{1,2}):(\d{2})$", pickup_time)
        if hour_match and int(hour_match.group(1)) < 12:
            evening = f"{int(hour_match.group(1)) + 12:02d}:{hour_match.group(2)}"
            if validate_pickup_time(evening, restaurant_id=restaurant_id, pickup_date=_preorder_date)[0]:
                return evening, None
        if _preorder_date is None:
            if not _next_day_only and earliest_pickup_minutes_today(restaurant_id) is None:
                return None, _next_day_question(restaurant_id)
            return None, _build_pickup_time_error(pickup_time, suggestion, closing_time)
        hours = get_opening_range_text(restaurant_id, _preorder_date)
        hours_part = f" siamo aperti {hours}" if hours else " siamo chiusi a quell'ora"
        return None, f"Mi dispiace, {_day_phrase}{hours_part}. A che ora passa?"

    # Ordine o prenotazione secondo questa frase; si calcola prima di ogni flusso
    _call_intent: str | None = None
    # Giorno cambiato in questo turno e domanda aperta per un giorno in cui si è chiusi
    _day_changed = False
    _closed_day_ask: str | None = None
    # Il cliente ha appena scelto tra i due giorni: "sabato" non va riletto come il prossimo
    _date_conflict_resolved = False
    # Orario già detto che non vale nel giorno appena scelto: si dice e se ne propone uno
    _refused_time: list[str] = []

    def _set_pickup_day(day: "datetime.date", said: bool = True) -> None:
        """Il cliente ha scelto il giorno di ritiro: vale per il resto della chiamata e
        recupera l'orario detto prima, se rientra negli orari di quel giorno. `said`:
        la risposta di questo turno nomina già il giorno, non va ripetuto."""
        nonlocal _preorder_date, _day_phrase, _ask_next_day
        is_today = day == datetime.datetime.now(tz=ZoneInfo("Europe/Rome")).date()
        _preorder_date = None if is_today else day
        _day_phrase = None if is_today else pickup_day_phrase(day)
        _ask_next_day = False
        conversation.pickup_date = None if is_today else day.isoformat()
        data = _slot_state(conversation)
        if said:
            data["pickup_day_said"] = True
        else:
            data.pop("pickup_day_said", None)
        kept_time = data.pop("next_day_pickup_time", None)
        data.pop("next_day_proposal", None)
        data.pop("asking_pickup_day", None)
        data.pop("date_conflict", None)
        _store_slot_state(conversation, data)
        refused = None
        if conversation.pickup_time and _check_pickup_time(conversation.pickup_time)[1]:
            refused = conversation.pickup_time
            conversation.pickup_time = None
        if kept_time and not conversation.pickup_time:
            conversation.pickup_time = _check_pickup_time(kept_time)[0]
            refused = None if conversation.pickup_time else kept_time
        _refused_time[:] = [refused] if refused else []

    def _refused_time_question() -> str | None:
        """L'orario detto non vale nel giorno scelto: lo si dice e si propone il primo
        orario libero di quel giorno, con sì o no (come la proposta dopo due mancati)."""
        if not _refused_time or conversation.pickup_time:
            return None
        refused = _refused_time.pop()
        slot = first_pickup_slot(restaurant_id, pickup_date=_preorder_date)
        data = _slot_state(conversation)
        data.setdefault("pickup_time", {})["proposed"] = slot
        _store_slot_state(conversation, data)
        hours = get_opening_range_text(restaurant_id, _preorder_date) if _preorder_date else None
        when = (_day_phrase or "stasera").replace(", ", " ")
        hours_part = f", siamo aperti {hours}" if hours else ""
        print(f"[Pickup] orario {refused} non valido per {when}: propongo {slot}")
        return (
            f"Alle {_spoken_pickup_time(refused)} {when} non è possibile{hours_part}. "
            f"Va bene alle {_spoken_pickup_time(slot)}? Mi dica sì o no."
        )

    def _propose_day(proposal: "datetime.date", question: str) -> str:
        """Il giorno chiesto non si può: si propone `proposal` e si attende sì/no."""
        nonlocal _closed_day_ask
        _closed_day_ask = question
        data = _slot_state(conversation)
        data["next_day_proposal"] = {"date": proposal.isoformat(), "question": question}
        data.pop("asking_pickup_day", None)
        data.pop("date_conflict", None)
        spoken_time = _time_in_sentence(request.message)
        if spoken_time and spoken_time != "prima_possibile":
            data["next_day_pickup_time"] = spoken_time
        _store_slot_state(conversation, data)
        print(f"[Pickup] giorno chiesto non disponibile: propongo {proposal}")
        return question

    def _ask_pickup_date_conflict(days: tuple) -> str:
        """Giorno e data non coincidono: si chiede quale dei due, senza scegliere."""
        nonlocal _closed_day_ask
        _closed_day_ask = _date_conflict_question(*days)
        data = _slot_state(conversation)
        data["date_conflict"] = [day.isoformat() for day in days]
        data.pop("next_day_proposal", None)
        data.pop("asking_pickup_day", None)
        spoken_time = _time_in_sentence(request.message)
        if spoken_time and spoken_time != "prima_possibile":
            data["next_day_pickup_time"] = spoken_time
        _store_slot_state(conversation, data)
        print(f"[Pickup] giorno e data non coincidono: chiedo tra {days[0]} e {days[1]}")
        return _closed_day_ask

    def _propose_day_after_closed(closed_day: "datetime.date", proposal: "datetime.date") -> str:
        return _propose_day(proposal, _closed_day_question(closed_day, proposal))

    def _propose_only_order_day() -> str:
        """Preordine fisso: un altro giorno non si ignora, si dice qual è l'unico possibile."""
        return _propose_day(_preorder_date, f"Prendiamo ordini solo per {_day_phrase.replace(', ', ' ')}, va bene?")

    def _apply_spoken_day() -> str | None:
        """Un giorno detto esplicitamente vale sempre, anche con la cucina aperta. Restituisce
        la frase che lo conferma, solo quando oggi non restano orari di ritiro; un giorno
        chiuso, o diverso da quello fisso dei preordini, apre la proposta (_closed_day_ask).
        Le prenotazioni hanno il loro flusso."""
        nonlocal _day_changed
        if conversation.completed or _date_conflict_resolved or _call_intent == "reservation":
            return None
        # Alla domanda sul nome "Domenica" è un nome: il giorno deve essere esplicito
        if conversation.state == "collecting_name" and not re.search(
            r"\b(?:per|domani|dopodomani)\b", _normalize_for_match(request.message),
        ):
            return None
        conflict = _weekday_date_conflict(request.message)
        if conflict:
            _ask_pickup_date_conflict(conflict)
            return None
        day = _spoken_pickup_day(request.message, restaurant_id)
        if day is None:
            return None
        if _next_day_only:
            # "Per stasera" ha già la sua risposta (al telefono solo per il giorno dopo)
            if day not in (_preorder_date, datetime.datetime.now(tz=ZoneInfo("Europe/Rome")).date()):
                _propose_only_order_day()
            return None
        if day == datetime.datetime.now(tz=ZoneInfo("Europe/Rome")).date():
            # Stasera: annulla un giorno detto prima; a cucina chiusa resta la domanda
            if _preorder_date is not None and earliest_pickup_minutes_today(restaurant_id) is not None:
                _set_pickup_day(day, said=False)
                _day_changed = True
            return None
        if day == _preorder_date:
            return None
        if not _is_open_day(day, restaurant_id):
            proposal = _first_open_day_after(day, restaurant_id)
            if proposal:
                _propose_day_after_closed(day, proposal)
            return None
        kitchen_closed = _ask_next_day
        _set_pickup_day(day, said=kitchen_closed)
        _day_changed = True
        print(f"[Pickup] giorno di ritiro detto dal cliente: {day}")
        return f"La cucina stasera è chiusa, segno per {_day_phrase.replace(', ', ' ')}." if kitchen_closed else None

    def _keep_time_for_next_day(pickup_time: str, error: str | None) -> None:
        """Orario rifiutato solo perché oggi non si ritira più: si tiene per il giorno scelto."""
        if error and error == _next_day_question(restaurant_id):
            data = _slot_state(conversation)
            data["next_day_pickup_time"] = pickup_time
            _store_slot_state(conversation, data)

    def _day_said_for(state: str) -> bool:
        """True se il giorno di ritiro è già stato detto. La domanda sull'orario lo dice
        la prima volta: da lì in poi vale come detto (resta solo il riepilogo finale)."""
        data = _slot_state(conversation)
        said = bool(data.get("pickup_day_said"))
        if not said and _day_phrase and state == "collecting_pickup_time":
            data["pickup_day_said"] = True
            _store_slot_state(conversation, data)
        return said

    def _choose_pickup_hour(local_time: str, message: str) -> tuple[str | None, str | None]:
        """Ore 1-11 senza "di mattina"/"di sera": vale l'ora che rientra negli orari del
        giorno di ritiro, altrimenti +12. Se vanno bene entrambe → (None, domanda)."""
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", local_time)
        if not match or not 1 <= int(match.group(1)) <= 11 or _spoken_day_part(message):
            return local_time, None
        hour, minute = int(match.group(1)), match.group(2)
        morning, evening = f"{hour:02d}:{minute}", f"{hour + 12:02d}:{minute}"
        if not get_opening_hours(restaurant_id=restaurant_id):
            return (evening if hour >= 6 else morning), None  # senza orari: sera, come il prompt
        morning_ok = validate_pickup_time(morning, restaurant_id=restaurant_id, pickup_date=_preorder_date)[0]
        evening_ok = validate_pickup_time(evening, restaurant_id=restaurant_id, pickup_date=_preorder_date)[0]
        if morning_ok and evening_ok:
            spoken = _spoken_pickup_time(f"{hour}:{minute}")
            return None, f"Le {spoken} di mattina o le {spoken} di sera?"
        return (morning if morning_ok else evening), None

    def _nearest_pickup_slot(requested: str) -> str | None:
        """G5: il primo orario valido più vicino a quello chiesto ("alle 9" con chiusura
        alle 21 → l'ultimo orario prima della chiusura, non l'apertura). Si provano l'ora
        detta e la stessa ora di sera o di mattina."""
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", requested or "")
        if not match:
            return None
        hour, minute = int(match.group(1)), int(match.group(2))
        best: tuple[int, str] | None = None
        for candidate_hour in {hour % 12, hour % 12 + 12}:
            candidate = f"{candidate_hour:02d}:{minute:02d}"
            valid, suggestion, _closing = validate_pickup_time(
                candidate, restaurant_id=restaurant_id, pickup_date=_preorder_date,
            )
            slot = candidate if valid else suggestion
            slot_match = re.fullmatch(r"(\d{1,2}):(\d{2})", slot or "")
            if not slot_match:
                continue
            distance = abs(
                (int(slot_match.group(1)) * 60 + int(slot_match.group(2))) - (candidate_hour * 60 + minute)
            )
            if best is None or distance < best[0]:
                best = (distance, slot)
        return best[1] if best else None

    def _pickup_time_miss(
        merged_order: dict, error: str | None, refused_proposal: bool = False, requested: str | None = None,
    ) -> tuple[str, str, int | None]:
        """Orario non capito (o rifiutato) alla domanda sull'orario: (stato, risposta, order_id).
        1° frase d'esempio (o l'errore sugli orari), 2° proposta del primo orario
        disponibile, 3° ordine salvato al primo orario con needs_review.
        "No" alla proposta: si chiede una volta l'orario; se ancora non va, l'ordine
        si salva senza orario di ritiro (mai all'orario rifiutato)."""
        data = _slot_state(conversation)
        attempts = data.setdefault("pickup_time", {})
        attempts["misses"] = int(attempts.get("misses") or 0) + 1
        if (
            requested and error and not refused_proposal and not attempts.get("refused")
            and not is_custom_pizza_enabled(restaurant_id)
        ):
            # G5 (PaP): orario capito ma fuori fascia → si propone subito il più vicino
            nearest = _nearest_pickup_slot(requested)
            if nearest:
                attempts["proposed"] = nearest
                _store_slot_state(conversation, data)
                print(f"[Pickup] orario {requested} fuori fascia: propongo il più vicino {nearest}")
                return (
                    "collecting_pickup_time",
                    f"{error.removesuffix(' A che ora passa?')} Va bene alle {_spoken_pickup_time(nearest)}? "
                    "Mi dica sì o no.",
                    None,
                )
        if refused_proposal:
            attempts["refused"] = attempts.pop("proposed")
            _store_slot_state(conversation, data)
            return "collecting_pickup_time", PICKUP_TIME_ASK_AFTER_NO_MESSAGE, None
        if attempts.get("refused"):
            data.setdefault("review", []).append(
                f"Orario non capito, il cliente ha rifiutato {attempts.pop('refused')}"
            )
            _store_slot_state(conversation, data)
            conversation.pickup_time = None
            merged_order["pickup_time"] = ""  # Order.pickup_time non è nullable: "" = da definire
            print("[PickupTime] orario non capito dopo il rifiuto della proposta: ordine senza orario, needs_review")
            order = _confirm_order(session, conversation, merged_order, restaurant_id)
            return "completed", PICKUP_TIME_UNDEFINED_MESSAGE, order.id
        if attempts["misses"] == 1:
            _store_slot_state(conversation, data)
            return "collecting_pickup_time", error or PICKUP_TIME_MISS_MESSAGE, None
        if attempts["misses"] == 2:
            attempts["proposed"] = first_pickup_slot(restaurant_id, pickup_date=_preorder_date)
            _store_slot_state(conversation, data)
            return (
                "collecting_pickup_time",
                f"Va bene alle {_spoken_pickup_time(attempts['proposed'])}? Mi dica sì o no.",
                None,
            )
        slot = attempts.get("proposed")
        if not slot or _check_pickup_time(slot)[1]:
            slot = first_pickup_slot(restaurant_id, pickup_date=_preorder_date)
        attempts.pop("proposed", None)  # i tentativi restano per il log, si azzerano con l'orario
        data.setdefault("review", []).append(
            f"Orario non capito: segnato il primo orario disponibile ({slot}), verificare col cliente"
        )
        _store_slot_state(conversation, data)
        conversation.pickup_time = slot
        merged_order["pickup_time"] = slot
        note = f"Segno alle {_spoken_pickup_time(slot)}, il locale la richiama se serve."
        next_state = determine_state(
            merged_order=merged_order,
            missing_messages=[],
            completed=conversation.completed,
            intended_quantity=conversation.intended_quantity,
        )
        print(f"[PickupTime] orario non capito dopo 3 tentativi: segnato {slot}, needs_review")
        if next_state == "awaiting_confirmation":
            order = _confirm_order(session, conversation, merged_order, restaurant_id)
            return "completed", f"{note} Grazie, a presto!", order.id
        conversation.state = next_state
        return next_state, f"{note} " + build_assistant_response(
            merged_order=merged_order,
            state=next_state,
            missing_messages=[],
            order_saved=False,
            intent="set_pickup_time",
            new_valid_items=[],
            customer_phone=conversation.customer_phone,
            day_phrase=_day_phrase,
            restaurant_id=restaurant_id,
            day_said=_day_said_for(next_state),
        ), None

    def _name_miss(merged_order: dict) -> tuple[str, str]:
        """Nome non capito alla domanda sul nome: (stato, risposta).
        1° richiesta diversa, 2° proposta di ciò che si è capito, 3° ordine da verificare."""
        data = _slot_state(conversation)
        attempts = data.setdefault("customer_name", {})
        attempts["misses"] = int(attempts.get("misses") or 0) + 1
        attempts.pop("proposed", None)
        if attempts["misses"] < 3:
            guess = _name_guess(request.message) if attempts["misses"] == 2 else None
            if guess:
                attempts["proposed"] = guess
            _store_slot_state(conversation, data)
            print(f"[Name] nome non capito, fallimenti={attempts['misses']}")
            if attempts["misses"] == 1:
                return "collecting_name", NAME_MISS_MESSAGE
            return "collecting_name", (
                f"Ho capito {guess}, giusto? Mi dica sì o no." if guess else NAME_MISS_NO_GUESS_MESSAGE
            )
        del data["customer_name"]
        data.setdefault("review", []).append("Nome non capito: chiedere il nome al cliente")
        _store_slot_state(conversation, data)
        conversation.customer_name = UNKNOWN_CUSTOMER_NAME
        merged_order["customer_name"] = UNKNOWN_CUSTOMER_NAME
        next_state = determine_state(
            merged_order=merged_order,
            missing_messages=[],
            completed=conversation.completed,
            intended_quantity=conversation.intended_quantity,
        )
        print("[Name] nome non capito dopo 3 tentativi: needs_review")
        conversation.state = next_state
        return next_state, "Segno l'ordine senza nome, il locale la richiama se serve. " + build_assistant_response(
            merged_order=merged_order,
            state=next_state,
            missing_messages=[],
            order_saved=False,
            intent="set_customer_name",
            new_valid_items=[],
            customer_phone=conversation.customer_phone,
            day_phrase=_day_phrase,
            restaurant_id=restaurant_id,
            day_said=_day_said_for(next_state),
        )

    def _current_merged_order() -> dict:
        merged = {
            "customer_name": conversation.customer_name,
            "pickup_time": conversation.pickup_time,
            "items": json.loads(conversation.items_json),
        }
        if _preorder_date:
            merged["pickup_date"] = _preorder_date.isoformat()
        return merged

    def _local_reply(state: str, response_message: str, merged_order: dict, intent: str,
                     order_id: int | None = None) -> ChatResponse:
        """Turno risolto senza LLM: salva sessione e log, risponde."""
        extracted_stub = {"intent": intent, "items": [], "customer_name": None, "pickup_time": None}
        valid = state == "completed"
        if not conversation.completed:
            conversation.state = state
        session.add(conversation)
        session.add(ConversationLog(
            session_id=request.session_id,
            user_message=request.message,
            extracted_order_json=json.dumps(extracted_stub, ensure_ascii=False),
            merged_order_json=json.dumps(merged_order, ensure_ascii=False),
            response_message=response_message,
            valid=valid,
            missing_items_json="[]",
            state=state,
        ))
        session.commit()
        _log_chat_timing(request.session_id, intent, request_started_at, state=state)
        return ChatResponse(
            session_id=request.session_id,
            user_message=request.message,
            extracted_order=extracted_stub,
            merged_order=merged_order,
            valid=valid,
            missing_items=[],
            response_message=response_message,
            order_id=order_id,
            state=state,
        )

    _reservation_states = {
        "collecting_reservation_date",
        "collecting_reservation_time",
        "collecting_reservation_party",
        "collecting_reservation_name",
        "awaiting_reservation_confirmation",
        "reservation_handoff",
        "collecting_reservation_callback_name",
        "collecting_reservation_callback_phone",
    }
    if not _reservations_on:
        if conversation.state in _reservation_states:
            conversation.state = "collecting_items"
            conversation.reservation_json = "{}"
            session.add(conversation)
            session.commit()
            print("[Reservation] reservations_enabled=False: stato prenotazione resettato")

    _restaurant_name = load_restaurant(restaurant_id=restaurant_id).get("name") or ""
    _res_question_states = {
        "collecting_reservation_date",
        "collecting_reservation_time",
        "collecting_reservation_party",
        "collecting_reservation_name",
        "awaiting_reservation_confirmation",
    }
    _order_question_states = {"collecting_items", "collecting_name", "collecting_pickup_time", "awaiting_confirmation"}
    _intent_states = {*_order_question_states, *_res_question_states, *_KG_SLOT_STATES, "confirming_next_day", "confirming_usual"}
    _in_reservation = conversation.state in _res_question_states
    _cart_now = json.loads(conversation.items_json)
    if conversation.state in _intent_states and not conversation.completed:
        _call_intent = classify_call_intent(
            request.message,
            lambda: get_proposable_menu(restaurant_id=restaurant_id),
            _restaurant_name,
            strict=_in_reservation,
        )

    if not _reservations_on:
        if _call_intent in ("reservation", "unknown"):
            print("[Reservation] reservations_enabled=False: intent prenotazione ignorato")
            _log_chat_timing(request.session_id, "reservation_disabled", request_started_at)
            return _res_response(
                "collecting_items",
                "Mi dispiace, al momento accettiamo solo ordini da asporto. Cosa desidera ordinare?",
            )

    _prefilled_party: int | None = None

    def _ask_call_intent(question: str, state: str, proposal: "datetime.date | None" = None) -> str:
        """Si chiede ordine o prenotazione; la frase si tiene per giorno e orario già detti."""
        data = _slot_state(conversation)
        # Già aperta: "salve" non cancella il "domani alle 8" detto prima
        data.setdefault("intent_question", {"message": request.message})
        data["intent_asked"] = True
        if proposal:
            data["next_day_proposal"] = {"date": proposal.isoformat(), "question": question}
        _store_slot_state(conversation, data)
        conversation.state = state
        return question

    _intent_slots = _slot_state(conversation)
    _intent_open = _intent_slots.get("intent_question")
    _intent_earlier = (_intent_open or {}).get("message", "")
    _said_order = bool(_intent_open) and _ORDER_ANSWER_RE.search(_normalize_for_match(request.message)) is not None

    def _keep_earlier_day_and_time() -> None:
        """Ordine: valgono giorno e orario detti nella frase prima della domanda. Quelli
        detti da ora in poi li sostituiscono, come in ogni altro turno."""
        data = _slot_state(conversation)
        earlier = (data.pop("intent_question", None) or {}).get("message", "")
        _store_slot_state(conversation, data)
        conflict = _weekday_date_conflict(earlier)
        earlier_day = None if conflict else _spoken_pickup_day(earlier, restaurant_id)
        if conflict:
            _ask_pickup_date_conflict(conflict)
        elif earlier_day and not _next_day_only and not _is_open_day(earlier_day, restaurant_id):
            proposal = _first_open_day_after(earlier_day, restaurant_id)
            if proposal:
                _propose_day_after_closed(earlier_day, proposal)
        elif earlier_day:
            _set_pickup_day(earlier_day, said=False)
        earlier_time = _time_in_sentence(earlier)
        if not earlier_time or earlier_time == "prima_possibile":
            return
        if _closed_day_ask:
            data = _slot_state(conversation)
            data["next_day_pickup_time"] = earlier_time
            _store_slot_state(conversation, data)
        else:
            conversation.pickup_time = _check_pickup_time(earlier_time)[0]

    def _names_a_product(message: str) -> bool:
        return mentions_product(
            strip_restaurant_name(message, _restaurant_name), get_proposable_menu(restaurant_id=restaurant_id),
        )

    if _reservations_on and _intent_open and not conversation.completed:
        if _call_intent == "unknown":
            _call_intent = "reservation"  # "prenotare" come risposta alla domanda
        elif _call_intent != "reservation" and conversation.state == "collecting_items":
            if not _next_day_only and (_call_intent == "order" or _names_a_product(request.message)):
                # "Una capricciosa lo sport": il prodotto lo estrae il percorso normale
                _keep_earlier_day_and_time()
            elif _said_order and not _call_intent:
                _keep_earlier_day_and_time()
                if _closed_day_ask:
                    conversation.state = "confirming_next_day"
                    return _res_response("confirming_next_day", _closed_day_ask)
                return _res_response("collecting_items", "Cosa desidera ordinare?")

    # "Vorrei prenotare" senza prodotti né tavolo: si chiede, senza presupporre
    if (
        _reservations_on
        and _call_intent == "unknown"
        and conversation.state == "collecting_items"
        and not _cart_now
    ):
        _log_chat_timing(request.session_id, "call_intent_question", request_started_at)
        if _ask_next_day:
            _proposal_day, _closed_question = _closed_kitchen_intent_question(restaurant_id)
            return _res_response(
                "confirming_next_day", _ask_call_intent(_closed_question, "confirming_next_day", _proposal_day),
            )
        return _res_response("collecting_items", _ask_call_intent(INTENT_QUESTION, "collecting_items"))

    # Solo saluti o il nome del locale: mai un ordine, e l'intento non è ancora noto
    if (
        conversation.state == "collecting_items"
        and not _cart_now
        and not conversation.completed
        and is_only_greeting(request.message, _restaurant_name)
    ):
        _log_chat_timing(request.session_id, "greeting_only", request_started_at)
        if _ask_next_day and _reservations_on:
            _proposal_day, _closed_question = _closed_kitchen_intent_question(restaurant_id)
            return _res_response(
                "confirming_next_day", _ask_call_intent(_closed_question, "confirming_next_day", _proposal_day),
            )
        if _ask_next_day:
            return _res_response("confirming_next_day", _next_day_question(restaurant_id))
        _order_already_chosen = _intent_slots.get("intent_asked") and not _intent_slots.get("intent_question")
        if _reservations_on and not _next_day_only and not _order_already_chosen:
            return _res_response("collecting_items", _ask_call_intent(INTENT_QUESTION, "collecting_items"))
        return _res_response("collecting_items", "Cosa desidera ordinare?")

    # Cambio di intento: da ordine a tavolo. Giorno, orario e nome restano, gli articoli
    # non confermati no. Con articoli nel carrello serve la parola "tavolo".
    _switch_to_reservation = (
        _reservations_on
        and _call_intent == "reservation"
        and conversation.state not in _res_question_states
        and (not _cart_now or mentions_table(request.message))
    )
    _reservation_seed: dict = {}
    if _switch_to_reservation:
        if conversation.pickup_date and not _next_day_only:
            _reservation_seed["date"] = conversation.pickup_date
        if conversation.pickup_time:
            _reservation_seed["time"] = conversation.pickup_time
        if _cart_now:
            print(f"[Intent] Da ordine a prenotazione: scartati {len(_cart_now)} articoli non confermati")
        conversation.items_json = "[]"
        conversation.intended_quantity = None
        conversation.pickup_time = None
        if not _next_day_only:
            conversation.pickup_date = None
        _intent_slots = _slot_state(conversation)
        for _key in ("intent_question", "next_day_proposal", "asking_pickup_day", "date_conflict", "next_day_pickup_time", "correcting"):
            _intent_slots.pop(_key, None)
        _store_slot_state(conversation, _intent_slots)

    # Cambio di intento: da prenotazione a ordine. Giorno, orario e nome restano.
    def _switch_reservation_to_order() -> None:
        res_data = json.loads(conversation.reservation_json or "{}")
        conversation.reservation_json = "{}"
        conversation.state = "collecting_items"
        data = _slot_state(conversation)
        data.pop("correcting", None)
        _store_slot_state(conversation, data)
        if res_data.get("date"):
            _set_pickup_day(datetime.date.fromisoformat(res_data["date"]), said=False)
        if res_data.get("time"):
            conversation.pickup_time = _check_pickup_time(res_data["time"])[0]
        print("[Intent] Da prenotazione a ordine: giorno, orario e nome restano")

    if (
        _call_intent == "order"
        and conversation.state in _res_question_states
        and not json.loads(conversation.reservation_json or "{}").get("pending_reservation_id")
    ):
        _switch_reservation_to_order()
        _in_reservation = False

    # "No" a una domanda non è una risposta non capita: si chiede cosa correggere, e mai
    # due volte la stessa frase. Al terzo "no" di fila si saluta.
    def _callback_fallback() -> "tuple[str, str]":
        """Cliente non capito: come per i gruppi, si salva la richiesta e si prova il
        trasferimento al locale; senza numero di trasferimento si saluta subito."""
        _, handoff_phone = get_large_party_settings(restaurant_id)
        if not handoff_phone:
            return "completed", finish_callback_request(conversation, request.session_id, restaurant_id)
        save_callback_request(conversation, request.session_id, restaurant_id)
        res_data = json.loads(conversation.reservation_json or "{}")
        res_data["handoff_phone"] = handoff_phone
        conversation.reservation_json = json.dumps(res_data, ensure_ascii=False)
        conversation.state = "reservation_handoff"
        print("[Callback] Cliente non capito: trasferimento al locale")
        return "reservation_handoff", CALLBACK_HANDOFF_MESSAGE

    def _ask_correction() -> "tuple[str, str]":
        data = _slot_state(conversation)
        asked = int(data.get("correcting") or 0)
        data["correcting"] = asked + 1
        _store_slot_state(conversation, data)
        if asked == 0:
            return conversation.state, CORRECTION_QUESTION
        if asked == 1:
            return conversation.state, CORRECTION_FIELDS_QUESTION
        return _callback_fallback()

    _correction_slots = _slot_state(conversation)
    _no_has_its_own_meaning = (
        bool(conversation.pending_customer_name)
        or bool(_correction_slots.get("customer_name", {}).get("proposed"))
        or bool(_correction_slots.get("pickup_time", {}).get("proposed"))
        or bool(_correction_slots.get("off_menu_proposal"))  # "No" a "Intende Pepe Rosa?"
        or bool(_correction_slots.get("flavour_change"))  # "No" a "Vuole cambiare gusto?"
    )
    if (
        _is_bare_no(request.message)
        and not conversation.completed
        and not _no_has_its_own_meaning
        and conversation.state in {*_res_question_states, *_order_question_states}
    ):
        _no_state, _no_reply = _ask_correction()
        conversation.state = _no_state
        _log_chat_timing(request.session_id, "correction_question", request_started_at)
        return _res_response(_no_state, _no_reply)

    # Risposta a "cosa devo correggere?" durante un ordine
    if _correction_slots.get("correcting") and conversation.state in _order_question_states:
        _field = _correction_field(request.message)
        _correction_slots.pop("correcting", None)
        _store_slot_state(conversation, _correction_slots)
        if _field == "name":
            conversation.customer_name = None
            conversation.state = "collecting_name"
            return _res_response("collecting_name", "A che nome?")
        if _field == "time":
            conversation.pickup_time = None
            conversation.state = "collecting_pickup_time"
            return _res_response("collecting_pickup_time", "A che ora passa?")
        if _field == "date":
            _correction_slots["asking_pickup_day"] = True
            _store_slot_state(conversation, _correction_slots)
            conversation.state = "confirming_next_day"
            return _res_response("confirming_next_day", PICKUP_DAY_QUESTION)
        conversation.state = "collecting_items"
        if _field == "items":
            return _res_response("collecting_items", "Mi dica pure cosa cambiare nell'ordine.")
        # Il dato corretto è nella frase: la legge il percorso normale

    # Trigger: prenotazione da inizio chiamata o cambio di intento
    if _switch_to_reservation:
        conversation.state = "collecting_reservation_date"
        conversation.reservation_json = "{}"
        _capture_reservation_preferences()
        print(f"[Reservation] Intent rilevato, entro nel flusso prenotazione")
        _log_chat_timing(request.session_id, "reservation_intent", request_started_at)
        # Giorno, ora e persone già detti nella stessa frase: si chiede solo il resto
        _details_text = f"{_intent_earlier} {request.message}".strip()
        _details = {**_reservation_seed, **_extract_reservation_details(_details_text)}
        _res_data = json.loads(conversation.reservation_json or "{}")
        if _details.get("party_size"):
            # Le persone si tengono anche se mancano giorno o ora: non si richiedono dopo
            _res_data["party_size"] = _details["party_size"]
            conversation.reservation_json = json.dumps(_res_data, ensure_ascii=False)
        _date_conflict = _weekday_date_conflict(_details_text, bare_day=True)
        if _date_conflict:
            return _res_response("collecting_reservation_date", _ask_reservation_date_conflict(_date_conflict))
        _day_error = _reservation_day_error(_details["date"], restaurant_id) if _details.get("date") else None
        if not _details.get("date") or _day_error:
            return _res_response(
                "collecting_reservation_date",
                _day_error or "Certo! Per quale giorno vuole prenotare?",
            )
        _res_data["date"] = _details["date"]
        _date_it = _format_reservation_date_it(_details["date"])
        if _details.get("time"):
            _res_time = resolve_pickup_time(_details["time"])
            _time_ok, _time_error = validate_reservation_time(_details["date"], _res_time, restaurant_id=restaurant_id)
            if _time_ok:
                _res_data["time"] = _res_time
        conversation.reservation_json = json.dumps(_res_data, ensure_ascii=False)
        if "time" not in _res_data:
            conversation.state = "collecting_reservation_time"
            _ask = _time_error if _details.get("time") and _time_error else f"Certo, {_date_it}. A che ora?"
            return _res_response("collecting_reservation_time", _ask)
        conversation.state = "collecting_reservation_party"
        if not _details.get("party_size"):
            return _res_response(
                "collecting_reservation_party",
                f"Certo, {_date_it} alle {_res_data['time']}. Per quante persone?",
            )
        # Tutto detto: il numero di persone passa dal controllo disponibilità qui sotto
        _prefilled_party = _details["party_size"]

    if conversation.state in _reservation_states:
        _capture_reservation_preferences()

    # Risposta a "cosa devo correggere?", oppure "no, per domani", durante una prenotazione
    if conversation.state in _res_question_states and (
        _slot_state(conversation).get("correcting") or _normalize_for_match(request.message).startswith("no ")
        or _normalize_for_match(request.message).startswith("no,")
    ) and not (
        conversation.state == "awaiting_reservation_confirmation"
        and _reservation_confirmation_intent(request.message) == "cancel"
        and not _extract_reservation_details(request.message)
        and not _correction_field(request.message)
        and not _slot_state(conversation).get("correcting")
    ):
        _was_correcting = bool(_slot_state(conversation).get("correcting"))
        _clear_correcting = _slot_state(conversation)
        _clear_correcting.pop("correcting", None)
        _store_slot_state(conversation, _clear_correcting)
        _field = _correction_field(request.message)
        _res_now = json.loads(conversation.reservation_json or "{}")
        if _ORDER_ANSWER_RE.search(_normalize_for_match(request.message)) and not _res_now.get("pending_reservation_id"):
            _switch_reservation_to_order()
            return _res_response("collecting_items", "Cosa desidera ordinare?")
        if _field:
            _field_steps = {
                "date": ("date", "collecting_reservation_date", "Per quale giorno?"),
                "time": ("time", "collecting_reservation_time", "A che ora?"),
                "party": ("party_size", "collecting_reservation_party", "Per quante persone?"),
            }
            if _field == "name":
                conversation.customer_name = None
                conversation.state = "collecting_reservation_name"
                return _res_response("collecting_reservation_name", "A nome di chi?")
            if _field in _field_steps:
                _key, _step_state, _step_question = _field_steps[_field]
                _res_now.pop(_key, None)
                conversation.reservation_json = json.dumps(_res_now, ensure_ascii=False)
                conversation.state = _step_state
                return _res_response(_step_state, _step_question)
        _conflict_now = _weekday_date_conflict(request.message, bare_day=True)
        if _conflict_now:
            return _res_response("collecting_reservation_date", _ask_reservation_date_conflict(_conflict_now))
        _new_details = _extract_reservation_details(request.message)
        if _new_details.get("date"):
            _day_error = _reservation_day_error(_new_details["date"], restaurant_id)
            if _day_error:
                conversation.state = "collecting_reservation_date"
                return _res_response("collecting_reservation_date", _day_error)
            _res_now["date"] = _new_details["date"]
        if _new_details.get("time"):
            _new_time = resolve_pickup_time(_new_details["time"])
            _time_ok, _time_error = validate_reservation_time(
                _res_now.get("date", ""), _new_time, restaurant_id=restaurant_id,
            ) if _res_now.get("date") else (True, None)
            if not _time_ok:
                conversation.reservation_json = json.dumps(_res_now, ensure_ascii=False)
                conversation.state = "collecting_reservation_time"
                return _res_response("collecting_reservation_time", _time_error or "A che ora?")
            _res_now["time"] = _new_time
        if _new_details.get("party_size"):
            _res_now["party_size"] = _new_details["party_size"]
        if _new_details:
            conversation.reservation_json = json.dumps(_res_now, ensure_ascii=False)
            if not _res_now.get("date"):
                conversation.state = "collecting_reservation_date"
                return _res_response("collecting_reservation_date", "Per quale giorno?")
            if not _res_now.get("time"):
                conversation.state = "collecting_reservation_time"
                return _res_response("collecting_reservation_time", "A che ora?")
            if not _res_now.get("party_size"):
                conversation.state = "collecting_reservation_party"
                return _res_response("collecting_reservation_party", "Per quante persone?")
            # Tutto noto: disponibilità e riepilogo passano dal controllo qui sotto
            conversation.state = "collecting_reservation_party"
            _prefilled_party = int(_res_now["party_size"])
        elif _was_correcting:
            return _res_response(conversation.state, CORRECTION_FIELDS_QUESTION)

    if conversation.state in ("collecting_reservation_date", "collecting_reservation_time"):
        # "Sabato, siamo in 4": le persone dette prima della domanda non si richiedono
        _said_party = _extract_party_in_sentence(request.message)
        if _said_party:
            _res_data = json.loads(conversation.reservation_json or "{}")
            _res_data["party_size"] = _said_party
            conversation.reservation_json = json.dumps(_res_data, ensure_ascii=False)

    if conversation.state == "collecting_reservation_date":
        _date_conflict = _weekday_date_conflict(request.message, bare_day=True)
        if _date_conflict:
            _log_chat_timing(request.session_id, "reservation_date_conflict", request_started_at)
            return _res_response("collecting_reservation_date", _ask_reservation_date_conflict(_date_conflict))
        _res_data = json.loads(conversation.reservation_json or "{}")
        _open_conflict = _res_data.get("date_conflict")
        if _open_conflict:
            _chosen_day = _resolve_date_conflict(request.message, _open_conflict)
            res_date = _chosen_day.isoformat() if _chosen_day else _extract_reservation_date(request.message)
            if not res_date:
                _log_chat_timing(request.session_id, "reservation_date_conflict", request_started_at)
                return _res_response(
                    "collecting_reservation_date",
                    _date_conflict_question(*map(datetime.date.fromisoformat, _open_conflict)),
                )
            del _res_data["date_conflict"]
            conversation.reservation_json = json.dumps(_res_data, ensure_ascii=False)
        else:
            res_date = _extract_reservation_date(request.message)
        _res_day_error = _reservation_day_error(res_date, restaurant_id) if res_date else None
        if _res_day_error:
            _log_chat_timing(request.session_id, "reservation_date_unavailable", request_started_at)
            return _res_response("collecting_reservation_date", _res_day_error)
        if res_date:
            res_data = json.loads(conversation.reservation_json or "{}")
            res_data["date"] = res_date
            conversation.reservation_json = json.dumps(res_data, ensure_ascii=False)
            conversation.state = "collecting_reservation_time"
            print(f"[Reservation] Data: {res_date}")
            _log_chat_timing(request.session_id, "reservation_date", request_started_at)
            return _res_response("collecting_reservation_time", "A che ora?")
        _log_chat_timing(request.session_id, "reservation_date_retry", request_started_at)
        return _res_response(
            "collecting_reservation_date",
            _reservation_date_retry_message(restaurant_id),
        )

    if conversation.state == "collecting_reservation_time":
        local_time = _extract_local_pickup_time(request.message)
        if local_time:
            res_time = resolve_pickup_time(local_time)
            res_data = json.loads(conversation.reservation_json or "{}")
            is_valid_time, time_error = validate_reservation_time(
                res_data.get("date", ""),
                res_time,
                restaurant_id=restaurant_id,
            )
            if not is_valid_time:
                _res_day_error = _reservation_day_error(res_data.get("date", ""), restaurant_id)
                if _res_day_error:
                    # Oggi non restano orari: si torna a chiedere il giorno
                    conversation.state = "collecting_reservation_date"
                    _log_chat_timing(request.session_id, "reservation_date_unavailable", request_started_at)
                    return _res_response("collecting_reservation_date", _res_day_error)
                conversation.state = "collecting_reservation_time"
                _log_chat_timing(request.session_id, "reservation_time_invalid", request_started_at)
                return _res_response("collecting_reservation_time", time_error or "Non posso prenotare per quell'orario. A che ora?")
            res_data["time"] = res_time
            conversation.reservation_json = json.dumps(res_data, ensure_ascii=False)
            conversation.state = "collecting_reservation_party"
            print(f"[Reservation] Orario: {res_time}")
            _log_chat_timing(request.session_id, "reservation_time", request_started_at)
            if not res_data.get("party_size"):
                return _res_response("collecting_reservation_party", "Per quante persone?")
            # Persone già dette: si passa subito al controllo qui sotto
            _prefilled_party = int(res_data["party_size"])
        else:
            _log_chat_timing(request.session_id, "reservation_time_retry", request_started_at)
            return _res_response(
                "collecting_reservation_time",
                "Non ho capito l'orario. Può indicarmi a che ora? Per esempio alle 20, o alle 8 di sera.",
            )

    if conversation.state == "collecting_reservation_party":
        party = _prefilled_party or _extract_party_size(request.message)
        if party and party >= 1:
            res_data = json.loads(conversation.reservation_json or "{}")
            res_data["party_size"] = party
            conversation.reservation_json = json.dumps(res_data, ensure_ascii=False)

            # Gruppi oltre max_party_size_auto: si passa la chiamata al locale
            # (<Dial> in voice) oppure si raccolgono i dati per richiamare.
            _party_limit, _handoff_phone = get_large_party_settings(restaurant_id)
            if _party_limit and party > _party_limit:
                res_data["large_group"] = True
                # La richiesta si salva subito: una riattaccata non deve farla perdere
                _save_large_group_request(
                    res_data,
                    f"Gruppo di {party} persone (limite automatico {_party_limit}): "
                    + (
                        "trasferimento al locale in corso, se non gestita richiamare il cliente"
                        if _handoff_phone else "richiamare il cliente"
                    )
                    + (f" al {conversation.customer_phone}" if conversation.customer_phone else ""),
                )
                if _handoff_phone:
                    res_data["handoff_phone"] = _handoff_phone
                    conversation.reservation_json = json.dumps(res_data, ensure_ascii=False)
                    conversation.state = "reservation_handoff"
                    print(f"[Reservation] Gruppo di {party} (> {_party_limit}): trasferimento al locale")
                    _log_chat_timing(request.session_id, "reservation_handoff", request_started_at)
                    return _res_response(
                        "reservation_handoff",
                        "Per i gruppi numerosi la passo subito al locale, resti in linea.",
                    )
                conversation.reservation_json = json.dumps(res_data, ensure_ascii=False)
                _cb_state, _cb_msg = large_group_callback_prompt(conversation, "Per i gruppi numerosi la richiamiamo noi.")
                _log_chat_timing(request.session_id, "reservation_large_group_callback", request_started_at)
                return _res_response(_cb_state, _cb_msg)

            # Controllo disponibilità e assegnazione tavolo
            try:
                available, next_slot, table_info = check_reservation_availability(
                    res_data.get("date", ""),
                    res_data.get("time", ""),
                    party,
                    restaurant_id=restaurant_id,
                )
            except ReservationAvailabilityError as exc:
                print(f"[Reservation] Availability non verificabile: {exc}")
                conversation.state = "collecting_reservation_party"
                _log_chat_timing(request.session_id, "reservation_availability_error", request_started_at)
                return _res_response(
                    "collecting_reservation_party",
                    "Mi dispiace, al momento non riesco a controllare la disponibilità dei tavoli. Vuole che riprovi tra un attimo?",
                )
            if table_info:
                _apply_reservation_table_info(res_data, table_info)
                conversation.reservation_json = json.dumps(res_data, ensure_ascii=False)
                print(f"[Reservation] Tavolo assegnato: {table_info.get('table_name')!r}")
            if not available:
                if next_slot:
                    res_data["time"] = next_slot
                    _apply_reservation_table_info(res_data, table_info)
                    conversation.reservation_json = json.dumps(res_data, ensure_ascii=False)
                    if not conversation.customer_name:
                        conversation.state = "collecting_reservation_name"
                        msg = (
                            f"Mi dispiace, quello slot è esaurito. "
                            f"Ho disponibilità alle {next_slot}. A nome di chi?"
                        )
                    else:
                        conversation.state = "awaiting_reservation_confirmation"
                        date_it = _format_reservation_date_it(res_data.get("date", ""))
                        summary = _format_reservation_summary(
                            conversation.customer_name, date_it, next_slot,
                            party, res_data.get("table_name"),
                            preferred_room=res_data.get("preferred_room"),
                        )
                        msg = f"Mi dispiace, quello slot è esaurito. Ho disponibilità alle {next_slot}. Allora: {summary}. Confermo?"
                    print(f"[Reservation] Slot esaurito, propongo {next_slot}")
                else:
                    conversation.state = "collecting_reservation_date"
                    conversation.reservation_json = _prefs_only_reservation_json()
                    msg = "Mi dispiace, per quella sera siamo al completo. Per quale altro giorno?"
                _log_chat_timing(request.session_id, "reservation_no_availability", request_started_at)
                return _res_response(conversation.state, msg)

            if not conversation.customer_name:
                conversation.state = "collecting_reservation_name"
                print(f"[Reservation] Party: {party}, chiedo nome")
                _log_chat_timing(request.session_id, "reservation_party", request_started_at)
                return _res_response("collecting_reservation_name", "A nome di chi?")
            else:
                conversation.state = "awaiting_reservation_confirmation"
                _log_chat_timing(request.session_id, "reservation_party_known_name", request_started_at)
        else:
            _log_chat_timing(request.session_id, "reservation_party_retry", request_started_at)
            return _res_response(
                "collecting_reservation_party",
                "Non ho capito. Per quante persone è la prenotazione?",
            )

        if conversation.state == "awaiting_reservation_confirmation":
            res_data = json.loads(conversation.reservation_json or "{}")
            date_it = _format_reservation_date_it(res_data.get("date", ""))
            summary = _format_reservation_summary(
                conversation.customer_name or "",
                date_it,
                res_data.get("time", ""),
                int(res_data.get("party_size") or 1),
                res_data.get("table_name"),
                preferred_room=res_data.get("preferred_room"),
            )
            msg = f"Allora: {summary}. Confermo?"
            return _res_response("awaiting_reservation_confirmation", msg)

    if conversation.state == "reservation_handoff" and json.loads(conversation.reservation_json or "{}").get("callback_request"):
        # Richiesta già salvata e nessun trasferimento in corso: non c'è altro da chiedere
        _callback_reply = finish_callback_request(conversation, request.session_id, restaurant_id)
        return _res_response("completed", _callback_reply)

    if conversation.state == "reservation_handoff":
        # Nessun trasferimento in corso (chat di test, o chiamata tornata all'agente)
        _cb_state, _cb_msg = large_group_callback_prompt(conversation, "Al momento il locale non risponde.")
        _log_chat_timing(request.session_id, "reservation_handoff_fallback", request_started_at)
        return _res_response(_cb_state, _cb_msg)

    if conversation.state == "collecting_reservation_callback_name":
        local_name = _extract_local_customer_name(request.message)
        if local_name:
            conversation.customer_name = local_name
            _cb_state, _cb_msg = large_group_callback_prompt(conversation, "Grazie.")
            return _res_response(_cb_state, _cb_msg.replace(" Prendo i dati e la richiamiamo noi per confermare.", ""))
        return _res_response("collecting_reservation_callback_name", "A nome di chi devo segnare la richiesta?")

    if conversation.state == "collecting_reservation_callback_phone":
        callback_phone = _normalize_callback_phone(request.message, conversation.customer_phone)
        if not callback_phone:
            return _res_response(
                "collecting_reservation_callback_phone",
                "Mi scusi, a che numero possiamo richiamarla?",
            )
        res_data = json.loads(conversation.reservation_json or "{}")
        _party = int(res_data.get("party_size") or 0)
        _party_limit, _ = get_large_party_settings(restaurant_id)
        _review_reason = (
            f"Gruppo di {_party} persone (limite automatico {_party_limit}): "
            + ("trasferimento al locale non riuscito, " if res_data.get("handoff_phone") else "")
            + f"richiamare il cliente al {callback_phone}"
        )
        if res_data.get("pending_reservation_id"):
            # Richiesta già salvata prima del trasferimento: si completano i dati
            _notes = [_reservation_notes_text(res_data), _known_customer_notes()]
            reservation_id = res_data["pending_reservation_id"] if update_reservation_on_base44(
                res_data["pending_reservation_id"],
                {
                    "customer_name": conversation.customer_name or "Ospite",
                    "customer_phone": callback_phone,
                    "notes": " · ".join(n for n in _notes if n),
                    "preferred_room": res_data.get("preferred_room"),
                    "review_reason": _review_reason,
                },
            ) else None
        else:
            reservation_id = _save_large_group_request(res_data, _review_reason, phone=callback_phone)
        if not reservation_id:
            return _res_response(
                "collecting_reservation_callback_phone",
                "Mi dispiace, al momento non riesco a registrare la richiesta. Vuole che riprovi tra un attimo?",
            )
        conversation.state = "reservation_completed"
        conversation.completed = True
        date_it = _format_reservation_date_it(res_data.get("date", ""))
        _log_chat_timing(request.session_id, "reservation_callback_saved", request_started_at)
        return _res_response(
            "reservation_completed",
            f"Grazie{', ' + conversation.customer_name if conversation.customer_name else ''}. "
            f"Ho registrato la richiesta per {_party} persone, {date_it} alle {res_data.get('time', '')}: "
            "la richiameremo il prima possibile per confermare. A presto!",
            valid=True,
        )

    if conversation.state == "collecting_reservation_name":
        local_name = _extract_local_customer_name(request.message)
        if local_name:
            conversation.customer_name = local_name
            conversation.state = "awaiting_reservation_confirmation"
            res_data = json.loads(conversation.reservation_json or "{}")
            date_it = _format_reservation_date_it(res_data.get("date", ""))
            summary = _format_reservation_summary(
                local_name,
                date_it,
                res_data.get("time", ""),
                int(res_data.get("party_size") or 1),
                res_data.get("table_name"),
                preferred_room=res_data.get("preferred_room"),
            )
            msg = f"Allora: {summary}. Confermo?"
            print(f"[Reservation] Nome: {mask_name(local_name)}")
            _log_chat_timing(request.session_id, "reservation_name", request_started_at)
            return _res_response("awaiting_reservation_confirmation", msg)
        _log_chat_timing(request.session_id, "reservation_name_retry", request_started_at)
        return _res_response("collecting_reservation_name", "A nome di chi devo mettere la prenotazione?")

    if conversation.state == "awaiting_reservation_confirmation":
        reservation_intent = _reservation_confirmation_intent(request.message)

        if reservation_intent == "confirm":
            res_data = json.loads(conversation.reservation_json or "{}")
            _res_date = res_data.get("date", "")
            _res_time = res_data.get("time", "")
            _res_party = int(res_data.get("party_size") or 1)

            is_valid_time, time_error = validate_reservation_time(_res_date, _res_time, restaurant_id=restaurant_id)
            if not is_valid_time:
                conversation.state = "collecting_reservation_time"
                _log_chat_timing(request.session_id, "reservation_confirm_time_invalid", request_started_at)
                return _res_response(
                    "collecting_reservation_time",
                    time_error or "Non posso prenotare per quell'orario. A che ora?",
                )

            # Ricontrolla disponibilità al momento della conferma: il tavolo
            # assegnato nel turno precedente potrebbe essere stato preso nel frattempo.
            try:
                available, next_slot, table_info = check_reservation_availability(
                    _res_date,
                    _res_time,
                    _res_party,
                    restaurant_id=restaurant_id,
                )
            except ReservationAvailabilityError as exc:
                print(f"[Reservation] Availability non verificabile in conferma: {exc}")
                conversation.state = "awaiting_reservation_confirmation"
                _log_chat_timing(request.session_id, "reservation_confirm_availability_error", request_started_at)
                return _res_response(
                    "awaiting_reservation_confirmation",
                    "Mi dispiace, al momento non riesco a ricontrollare la disponibilità dei tavoli. Vuole che riprovi tra un attimo?",
                )
            if not available:
                if next_slot:
                    res_data["time"] = next_slot
                    _apply_reservation_table_info(res_data, table_info)
                    conversation.reservation_json = json.dumps(res_data, ensure_ascii=False)
                    date_it = _format_reservation_date_it(_res_date)
                    summary = _format_reservation_summary(
                        conversation.customer_name or "Ospite", date_it, next_slot,
                        _res_party, res_data.get("table_name"),
                        preferred_room=res_data.get("preferred_room"),
                    )
                    msg = (
                        f"Mi dispiace, nel frattempo quello slot è stato preso. "
                        f"Ho disponibilità alle {next_slot}. Allora: {summary}. Confermo?"
                    )
                    _log_chat_timing(request.session_id, "reservation_recheck_moved", request_started_at)
                    return _res_response("awaiting_reservation_confirmation", msg)

                conversation.state = "collecting_reservation_date"
                conversation.reservation_json = _prefs_only_reservation_json()
                _log_chat_timing(request.session_id, "reservation_recheck_unavailable", request_started_at)
                return _res_response(
                    "collecting_reservation_date",
                    "Mi dispiace, nel frattempo per quella sera siamo al completo. Per quale altro giorno vuole prenotare?",
                )

            _apply_reservation_table_info(res_data, table_info)
            conversation.reservation_json = json.dumps(res_data, ensure_ascii=False)
            _res_table_id = res_data.get("table_id")
            _res_table_name = res_data.get("table_name")
            _res_combined = res_data.get("combined_tables") or []
            _res_extended = bool(res_data.get("extended", False))
            # Salva su Base44
            _notes = [_reservation_notes_text(res_data), _known_customer_notes()]
            reservation_id = save_reservation_to_base44(
                customer_name=conversation.customer_name or "Ospite",
                customer_phone=conversation.customer_phone,
                date=_res_date,
                time=_res_time,
                party_size=_res_party,
                session_id=request.session_id,
                notes=" · ".join(n for n in _notes if n),
                table_id=_res_table_id,
                table_name=_res_table_name,
                combined_tables=_res_combined,
                extended=_res_extended,
                restaurant_id=restaurant_id,
                preferred_room=res_data.get("preferred_room"),
            )
            if not reservation_id:
                conversation.state = "awaiting_reservation_confirmation"
                _log_chat_timing(request.session_id, "reservation_save_failed", request_started_at)
                return _res_response(
                    "awaiting_reservation_confirmation",
                    "Mi dispiace, al momento non riesco a registrare la prenotazione. Vuole che riprovi tra un attimo?",
                )
            res_data["reservation_id"] = reservation_id  # riferimento per il CallLog
            conversation.reservation_json = json.dumps(res_data, ensure_ascii=False)
            print(
                f"[Reservation] Confermata session={request.session_id} "
                f"id={reservation_id} date={res_data.get('date')} time={res_data.get('time')} "
                f"party={res_data.get('party_size')} table={_res_table_name!r} "
                f"name={mask_name(conversation.customer_name or '')}"
            )
            _enqueue_reservation_sms_side_effect(
                session=session,
                customer_name=conversation.customer_name or "Ospite",
                customer_phone=conversation.customer_phone,
                date=res_data.get("date", ""),
                reservation_time=res_data.get("time", ""),
                party_size=int(res_data.get("party_size") or 1),
                table_name=_res_table_name,
                restaurant_name=load_restaurant(restaurant_id=restaurant_id).get("name"),
                restaurant_phone=load_restaurant(restaurant_id=restaurant_id).get("phone"),
            )

            conversation.state = "reservation_completed"
            conversation.completed = True
            date_it = _format_reservation_date_it(res_data.get("date", ""))
            name_part = f" {conversation.customer_name}" if conversation.customer_name else ""
            if _is_mobile_phone(conversation.customer_phone):
                resp_msg = f"Perfetto{name_part}! Prenotazione confermata per {date_it} alle {res_data.get('time')}. Le mando un SMS di conferma. A presto!"
            else:
                resp_msg = f"Perfetto{name_part}! Prenotazione confermata per {date_it} alle {res_data.get('time')}. A presto!"
            _log_chat_timing(request.session_id, "reservation_confirmed", request_started_at)
            return _res_response("reservation_completed", resp_msg, valid=True)

        elif reservation_intent == "cancel":
            conversation.state = "collecting_reservation_date"
            conversation.reservation_json = "{}"
            _log_chat_timing(request.session_id, "reservation_cancelled", request_started_at)
            return _res_response(
                "collecting_reservation_date",
                "Prenotazione annullata. Per quale altro giorno vuole prenotare?",
            )
        else:
            res_data = json.loads(conversation.reservation_json or "{}")
            date_it = _format_reservation_date_it(res_data.get("date", ""))
            _party = int(res_data.get("party_size") or 1)
            _table_part = f", {res_data['table_name']}" if res_data.get("table_name") else ""
            msg = (
                f"{conversation.customer_name or ''}, confermo per "
                f"{date_it} alle {res_data.get('time', '')}, "
                f"{_party} {'persona' if _party == 1 else 'persone'}{_table_part}?"
            )
            _log_chat_timing(request.session_id, "reservation_confirm_retry", request_started_at)
            return _res_response("awaiting_reservation_confirmation", msg)

    if conversation.state == "reservation_completed":
        _log_chat_timing(request.session_id, "reservation_completed_repeat", request_started_at)
        return _res_response("reservation_completed", "La prenotazione è già stata confermata. A presto!", valid=True)

    # Frase che conferma il giorno al cliente: precede la risposta di questo turno
    _day_note: str | None = None

    # Risposta a "Per stasera la cucina è chiusa, vuole ordinare per domani?".
    if conversation.state == "confirming_next_day":
        _yes = re.search(r"\b(s[iì]|ok|va bene|certo|volentieri|d'accordo|perfetto|esatto)\b", message_lower)
        _no = re.search(r"\b(no|niente|lasci|lascia|non importa)\b", message_lower)
        _nd_slots = _slot_state(conversation)
        _nd_proposal = _nd_slots.get("next_day_proposal") or {}
        # Dopo un "no" si è chiesto il giorno: non c'è nessuna proposta a cui dire sì
        _nd_conflict = _nd_slots.get("date_conflict")
        _nd_asking_day = bool(_nd_slots.get("asking_pickup_day") or _nd_conflict)
        _nd_intent_open = bool(_nd_slots.get("intent_question"))
        if not _nd_intent_open and not _yes and not _no and not _nd_asking_day and _names_a_product(request.message):
            # "Una margherita" al posto del sì: vale come sì, e il prodotto lo estrae l'LLM
            _yes = True
        if _nd_slots.get("intent_question"):
            # "Vuole ordinare per domani o prenotare un tavolo?": il sì da solo non sceglie,
            # un prodotto sì. Il giorno detto prima vale più di quello proposto.
            _yes = _ORDER_ANSWER_RE.search(_normalize_for_match(request.message)) or (
                not _next_day_only and _names_a_product(request.message)
            )
            if _yes:
                _nd_earlier = _nd_slots.pop("intent_question", {}).get("message", "")
                if not _next_day_only:
                    _nd_earlier_time = _time_in_sentence(_nd_earlier)
                    if _nd_earlier_time and _nd_earlier_time != "prima_possibile":
                        _nd_slots["next_day_pickup_time"] = _nd_earlier_time
                    _nd_earlier_day = _spoken_pickup_day(_nd_earlier, restaurant_id)
                    if (
                        _nd_earlier_day
                        and _nd_earlier_day > datetime.datetime.now(tz=ZoneInfo("Europe/Rome")).date()
                        and _is_open_day(_nd_earlier_day, restaurant_id)
                        and not _weekday_date_conflict(_nd_earlier)
                    ):
                        _nd_proposal = {**_nd_proposal, "date": _nd_earlier_day.isoformat()}
                        _nd_slots["next_day_proposal"] = _nd_proposal
                _store_slot_state(conversation, _nd_slots)
        _nd_question = (
            _date_conflict_question(*map(datetime.date.fromisoformat, _nd_conflict)) if _nd_conflict
            else PICKUP_DAY_QUESTION if _nd_asking_day
            else _nd_proposal.get("question") or _next_day_question(restaurant_id)
        )
        _nd_asked_day = (
            None if _nd_asking_day
            else datetime.date.fromisoformat(_nd_proposal["date"]) if _nd_proposal
            else get_next_open_day(restaurant_id=restaurant_id)[0]
        )
        _nd_new_conflict = _weekday_date_conflict(request.message)
        if _nd_new_conflict:
            return _local_reply(
                "confirming_next_day",
                _ask_pickup_date_conflict(_nd_new_conflict),
                _current_merged_order(),
                "date_conflict",
            )
        _nd_spoken_day = _resolve_date_conflict(request.message, _nd_conflict) if _nd_conflict else None
        _date_conflict_resolved = _nd_spoken_day is not None
        _nd_spoken_day = _nd_spoken_day or _spoken_pickup_day(request.message, restaurant_id)
        if _next_day_only and _nd_spoken_day and _nd_spoken_day != _preorder_date:
            # Preordine fisso: qualsiasi altro giorno riporta all'unico possibile
            return _local_reply(
                "confirming_next_day", _propose_only_order_day(), _current_merged_order(), "only_order_day",
            )
        _nd_today_refused = (
            _nd_spoken_day == datetime.datetime.now(tz=ZoneInfo("Europe/Rome")).date()
            and earliest_pickup_minutes_today(restaurant_id) is None
        )
        if _nd_today_refused:
            _nd_spoken_day = None  # "stasera" a cucina chiusa non è una scelta possibile
        if _nd_today_refused and _nd_intent_open and not _yes and not _no:
            # "Per stasera alle 8" alla domanda ordine/tavolo: si dice che stasera non si può
            # e si ripete la scelta; l'orario resta per il giorno che il cliente sceglierà
            _nd_tonight_time = _time_in_sentence(request.message)
            if _nd_tonight_time and _nd_tonight_time != "prima_possibile":
                _nd_slots["next_day_pickup_time"] = _nd_tonight_time
                _store_slot_state(conversation, _nd_slots)
            _nd_tonight = "Stasera" if datetime.datetime.now(tz=ZoneInfo("Europe/Rome")).hour >= 17 else "Oggi"
            _nd_choice = _closed_kitchen_intent_question(restaurant_id)[1].split(". ", 1)[1]
            return _local_reply(
                "confirming_next_day",
                f"{_nd_tonight} purtroppo la cucina è chiusa. {_nd_choice}",
                _current_merged_order(),
                "tonight_refused",
            )
        # "No, venerdì" sceglie un altro giorno; "no, domani no" rifiuta quello proposto
        _nd_accepted = bool((_yes and not _nd_asking_day) or _nd_spoken_day) and (
            not _no or bool(_nd_spoken_day and _nd_spoken_day != _nd_asked_day)
        )
        if _nd_today_refused and not _next_day_only and not _nd_slots.get("intent_question"):
            # Ha chiesto stasera: lo si dice e si chiede conferma del giorno proposto, senza
            # spostarlo. L'orario della frase resta per quel giorno; il resto lo estrae l'LLM.
            _nd_proposal_day = _nd_asked_day or get_next_open_day(restaurant_id=restaurant_id)[0]
            _nd_tonight = "Stasera" if datetime.datetime.now(tz=ZoneInfo("Europe/Rome")).hour >= 17 else "Oggi"
            _propose_day(
                _nd_proposal_day,
                f"{_nd_tonight} purtroppo la cucina è chiusa. Va bene per {pickup_day_phrase(_nd_proposal_day)}?",
            )
            _nd_accepted = False
            conversation.state = "collecting_items"
        if _nd_accepted and _nd_spoken_day and not _is_open_day(_nd_spoken_day, restaurant_id):
            _nd_next_open = _first_open_day_after(_nd_spoken_day, restaurant_id)
            if _nd_next_open:
                return _local_reply(
                    "confirming_next_day",
                    _propose_day_after_closed(_nd_spoken_day, _nd_next_open),
                    _current_merged_order(),
                    "closed_day_proposal",
                )
            _nd_accepted = False
        if _nd_accepted and not _is_bare_next_day_answer(request.message):
            # "sì, due margherite alle 8": anche il resto della frase va estratto
            _set_pickup_day(_nd_spoken_day or _nd_asked_day)
            conversation.state = "collecting_items"
            _day_note = f"Perfetto, il ritiro è per {_day_phrase or _today_phrase(conversation.pickup_time)}."
            print(f"[Pickup] cliente accetta il ritiro per {_preorder_date} e aggiunge altro")

    if conversation.state == "confirming_next_day":
        _nd_items = json.loads(conversation.items_json)
        _nd_merged = {
            "customer_name": conversation.customer_name,
            "pickup_time": conversation.pickup_time,
            "items": _nd_items,
        }
        if _nd_accepted:
            _set_pickup_day(_nd_spoken_day or _nd_asked_day)
            _nd_merged["pickup_date"] = conversation.pickup_date
            _nd_merged["pickup_time"] = conversation.pickup_time
            _nd_state = determine_state(
                merged_order=_nd_merged,
                missing_messages=[],
                completed=conversation.completed,
                intended_quantity=conversation.intended_quantity,
            )
            _nd_slots = _slot_state(conversation)
            _nd_pending = _nd_slots.pop("off_menu_pending", None)
            _store_slot_state(conversation, _nd_slots)
            if _nd_pending:
                # Il giorno è concordato: ora la pizza che non c'è
                _nd_next = _nd_pending
                _nd_state = "collecting_items"
            elif not _nd_items:
                _nd_next = "Cosa desidera ordinare?"
            elif _nd_state == "collecting_pickup_time":
                _nd_next = "A che ora passa?"
            else:
                _nd_next = build_assistant_response(
                    merged_order=_nd_merged,
                    state=_nd_state,
                    missing_messages=[],
                    order_saved=False,
                    intent="confirm_next_day",
                    new_valid_items=[],
                    customer_phone=conversation.customer_phone,
                    day_phrase=_day_phrase,
                    restaurant_id=restaurant_id,
                    day_said=True,
                )
            _nd_time_question = None if _nd_pending else _refused_time_question()
            if _nd_time_question:
                _nd_state, _nd_next = "collecting_pickup_time", _nd_time_question
            _nd_resp = f"Perfetto, il ritiro è per {_day_phrase or _today_phrase(conversation.pickup_time)}. {_nd_next}"
            print(f"[Pickup] cliente accetta il ritiro per {_preorder_date}")
        elif _no and (not _nd_asking_day or _nd_conflict):
            # "No" al giorno proposto: si chiede quale serve e si riapplicano le regole del locale
            _nd_slots = _slot_state(conversation)
            _nd_slots.pop("next_day_proposal", None)
            _nd_slots.pop("date_conflict", None)
            _nd_slots["asking_pickup_day"] = True
            _store_slot_state(conversation, _nd_slots)
            _nd_state = "confirming_next_day"
            _nd_resp = PICKUP_DAY_QUESTION
            print("[Pickup] giorno proposto rifiutato: chiedo per quale giorno")
        elif _is_bare_no(request.message):
            # Un "no" a "Per quale giorno le serve?" non chiude senza lasciare traccia
            _fallback_state, _fallback_reply = _callback_fallback()
            return _res_response(_fallback_state, _fallback_reply)
        elif _no:
            _nd_state = "completed"
            _nd_resp = "Va bene, nessun problema. La aspettiamo un'altra volta, buona serata!"
            print("[Pickup] nessun giorno va bene al cliente: chiusura")
        else:
            _nd_state = "confirming_next_day"
            _nd_resp = f"Mi scusi, non ho capito. {_nd_question}"
        conversation.state = _nd_state
        session.add(conversation)
        session.add(ConversationLog(
            session_id=request.session_id,
            user_message=request.message,
            extracted_order_json=json.dumps({"intent": "confirm_next_day", "items": []}, ensure_ascii=False),
            merged_order_json=json.dumps(_nd_merged, ensure_ascii=False),
            response_message=_nd_resp,
            valid=False,
            missing_items_json="[]",
            state=_nd_state,
        ))
        session.commit()
        _log_chat_timing(request.session_id, "confirm_next_day", request_started_at, state=_nd_state)
        return ChatResponse(
            session_id=request.session_id,
            user_message=request.message,
            extracted_order={"intent": "confirm_next_day", "items": []},
            merged_order=_nd_merged,
            valid=False,
            missing_items=[],
            response_message=_nd_resp,
            order_id=None,
            state=_nd_state,
        )

    # Preordini: al telefono solo ordini per il prossimo giorno di apertura.
    # Una richiesta per stasera/oggi riceve la spiegazione, senza LLM e in qualsiasi stato.
    # Con dei prodotti nella prima frase questi restano nel carrello: li estrae l'LLM e dopo
    # il sì si prosegue con la domanda che manca.
    _today_with_products = False
    if _next_day_only and _is_today_order_request(request.message):
        _today_word = "Stasera" if datetime.datetime.now(tz=ZoneInfo("Europe/Rome")).hour >= 17 else "Oggi"
        _today_guard_msg = (
            f"{_today_word} purtroppo al telefono prendiamo solo ordini per il giorno dopo, "
            f"ma può passare direttamente in negozio. Va bene per {_day_phrase}?"
        )
        if conversation.state == "collecting_items" and _names_a_product(request.message):
            _propose_day(_preorder_date, _today_guard_msg)
            _today_with_products = True
    if _next_day_only and _is_today_order_request(request.message) and not _today_with_products:
        _cur_merged = {
            "customer_name": conversation.customer_name,
            "pickup_time": conversation.pickup_time,
            "items": json.loads(conversation.items_json),
        }
        session.add(ConversationLog(
            session_id=request.session_id,
            user_message=request.message,
            extracted_order_json=json.dumps({"intent": "today_order_rejected", "items": []}, ensure_ascii=False),
            merged_order_json=json.dumps(_cur_merged, ensure_ascii=False),
            response_message=_today_guard_msg,
            valid=False,
            missing_items_json="[]",
            state=conversation.state,
        ))
        session.commit()
        _log_chat_timing(request.session_id, "today_order_rejected", request_started_at)
        return ChatResponse(
            session_id=request.session_id,
            user_message=request.message,
            extracted_order={"intent": "today_order_rejected", "items": []},
            merged_order=_cur_merged,
            valid=False,
            missing_items=[],
            response_message=_today_guard_msg,
            order_id=None,
            state=conversation.state,
        )

    if conversation.state in ("awaiting_confirmation", "collecting_name", "collecting_pickup_time", *_KG_SLOT_STATES):
        _day_note = _apply_spoken_day()
        if _closed_day_ask:
            return _local_reply("confirming_next_day", _closed_day_ask, _current_merged_order(), "closed_day_proposal")

    # Correzione del giorno al riepilogo ("no, per domani"): si aggiorna e si riepiloga,
    # senza confermare né annullare
    if conversation.state == "awaiting_confirmation" and _day_changed:
        _new_day_time = _time_in_sentence(request.message)
        if _new_day_time and _new_day_time != "prima_possibile":
            conversation.pickup_time = _check_pickup_time(_new_day_time)[0] or conversation.pickup_time
        _new_day_merged = _current_merged_order()
        _new_day_state = determine_state(
            merged_order=_new_day_merged,
            missing_messages=[],
            completed=conversation.completed,
            intended_quantity=conversation.intended_quantity,
        )
        _new_day_resp = build_assistant_response(
            merged_order=_new_day_merged,
            state=_new_day_state,
            missing_messages=[],
            order_saved=False,
            intent="change_pickup_day",
            new_valid_items=[],
            customer_phone=conversation.customer_phone,
            day_phrase=_day_phrase,
            restaurant_id=restaurant_id,
            day_said=_day_said_for(_new_day_state),
        )
        _new_day_time_question = _refused_time_question()
        if _new_day_time_question:
            _new_day_state, _new_day_resp = "collecting_pickup_time", _new_day_time_question
        if _day_note:
            _new_day_resp = f"{_day_note} {_new_day_resp}"
        return _local_reply(_new_day_state, _new_day_resp, _new_day_merged, "change_pickup_day")

    # ── Controlli deterministici PaP (custom_pizza_enabled = false) ──────────
    # G1: si ricorda se in questa chiamata il cliente ha detto chili, etti o grammi.
    # G3: "invece di X mettimi Y" e simili: la riga X diventa Y con la stessa quantità,
    # porzione e temperatura, senza passare dall'LLM.
    _pap_guards = not is_custom_pizza_enabled(restaurant_id)
    if _pap_guards and _PAP_WEIGHT_SAID_RE.search(_normalize_for_match(request.message)):
        _weight_slots = _slot_state(conversation)
        if not _weight_slots.get("weight_said"):
            _weight_slots["weight_said"] = True
            _store_slot_state(conversation, _weight_slots)
    _sub_cart = json.loads(conversation.items_json) if _pap_guards else []
    if _sub_cart and not conversation.completed and conversation.state not in _reservation_states:
        _sub_menu = get_proposable_menu(restaurant_id=restaurant_id)
        _sub_slots = _slot_state(conversation)
        _change_target = _sub_slots.get("flavour_change")
        _sub = _parse_flavour_substitution(
            request.message, _sub_cart, _sub_menu,
            at_recap=conversation.state == "awaiting_confirmation" or bool(_change_target),
        )
        _sub_reply: str | None = None
        if _sub is None and _change_target:
            # Risposta a "Vuole cambiare gusto?"
            _named = _flavour_in(_substitution_words(request.message), _sub_menu)
            _target_index = next(
                (i for i, line in enumerate(_sub_cart) if line.get("pizza_name") == _change_target), None,
            )
            _change_answer = _yes_no_answer(request.message)
            if _named and _target_index is not None and _named != _change_target:
                _sub = (_target_index, _named)
            elif _change_answer == "yes" and not _named:
                _sub_reply = FLAVOUR_CHANGE_WHICH.format(name=_change_target.lower())
            else:
                _sub_slots.pop("flavour_change", None)
                _store_slot_state(conversation, _sub_slots)
                if _change_answer == "no":
                    _sub = (-1, "")  # nessun cambio: si torna alla domanda o al riepilogo
        if _sub_reply:
            return _local_reply(conversation.state, _sub_reply, _current_merged_order(), "flavour_change_which")
        if _sub is not None:
            _index, _new_flavour = _sub
            if _index >= 0:
                _row = session.exec(_menu_select(restaurant_id, MenuItem.name == _new_flavour)).first()
                _old_flavour = _sub_cart[_index].get("pizza_name")
                _sub_cart[_index] = _substitute_flavour(_sub_cart[_index], _new_flavour, _row)
                print(f"[Guard] sostituzione di gusto: {_old_flavour!r} → {_new_flavour!r}, stessa quantità")
                conversation.items_json = json.dumps(_sub_cart, ensure_ascii=False)
            _sub_slots = _slot_state(conversation)
            _sub_slots.pop("flavour_change", None)
            _store_slot_state(conversation, _sub_slots)
            _sub_merged = _current_merged_order()
            _sub_state = determine_state(
                merged_order=_sub_merged,
                missing_messages=[],
                completed=conversation.completed,
                intended_quantity=conversation.intended_quantity,
            )
            _sub_resp = build_assistant_response(
                merged_order=_sub_merged,
                state=_sub_state,
                missing_messages=[],
                order_saved=False,
                intent="recap_correction" if _index >= 0 else "flavour_change_no",
                new_valid_items=[],
                customer_phone=conversation.customer_phone,
                day_phrase=_day_phrase,
                restaurant_id=restaurant_id,
                day_said=_day_said_for(_sub_state),
            )
            return _local_reply(_sub_state, _sub_resp, _sub_merged, "flavour_substitution")

    # Correzione al riepilogo: il carrello si aggiorna e si riepiloga di nuovo, mai lo
    # stesso riepilogo. Intero/mezzo, caldo/freddo, quantità e orario si leggono qui; un
    # altro gusto o una correzione non chiara passano dall'LLM. Prima del sì/no, perché
    # "Sì, però caldi" non è una conferma.
    _recap_correction_before: tuple[str, str | None] | None = None
    if conversation.state == "awaiting_confirmation" and not conversation.completed:
        _recap_items = json.loads(conversation.items_json)
        _other_product = _names_product_outside_cart(
            request.message, _recap_items, get_proposable_menu(restaurant_id=restaurant_id),
        )
        _recap_changed = not _other_product and _apply_recap_item_changes(_recap_items, request.message)
        _recap_new_time = _recap_time_error = None
        _said_recap_time = _time_in_sentence(request.message) if _next_day_only else None
        if _said_recap_time and _said_recap_time != "prima_possibile":
            _checked_recap_time, _recap_time_error = _check_pickup_time(_said_recap_time)
            if _checked_recap_time == conversation.pickup_time:
                _recap_time_error = None
            else:
                _recap_new_time = _checked_recap_time
        if not _other_product and (_recap_changed or _recap_new_time or _recap_time_error):
            conversation.items_json = json.dumps(_recap_items, ensure_ascii=False)
            if _recap_new_time:
                conversation.pickup_time = _recap_new_time
            _recap_merged = _current_merged_order()
            print(f"[Confirm] correzione al riepilogo: voci={_recap_changed} orario={_recap_new_time!r}")
            if _recap_time_error:
                return _local_reply("collecting_pickup_time", _recap_time_error, _recap_merged, "recap_correction")
            _recap_state = determine_state(
                merged_order=_recap_merged,
                missing_messages=[],
                completed=conversation.completed,
                intended_quantity=conversation.intended_quantity,
            )
            _recap_resp = build_assistant_response(
                merged_order=_recap_merged,
                state=_recap_state,
                missing_messages=[],
                order_saved=False,
                intent="recap_correction",
                new_valid_items=[],
                customer_phone=conversation.customer_phone,
                day_phrase=_day_phrase,
                restaurant_id=restaurant_id,
                day_said=_day_said_for(_recap_state),
            )
            return _local_reply(_recap_state, _recap_resp, _recap_merged, "recap_correction")
        if _other_product or _RECAP_CHANGE_WORDS_RE.search(_normalize_for_match(request.message)):
            _recap_correction_before = (conversation.items_json, conversation.pickup_time)
            conversation.state = "collecting_items"

    # "No, per le 9" al riepilogo: è una correzione, non l'annullamento dell'ordine
    if conversation.state == "awaiting_confirmation" and re.match(
        r"no\b[\s,]+(?!grazie\b|lascia\b|lasci\b|niente\b|annull|cancell)", _normalize_for_match(request.message),
    ):
        _recap_correction_before = (conversation.items_json, conversation.pickup_time)
        conversation.state = "collecting_items"

    # Un orario diverso al riepilogo ("Per 8.45."): lo legge la domanda sull'orario, che lo
    # valida e riepiloga. Qui l'LLM non passa, e senza questo l'orario andrebbe perso.
    if (
        conversation.state == "awaiting_confirmation"
        and not _next_day_only
        and _is_new_time_at_recap(request.message, conversation.pickup_time)
    ):
        conversation.state = "collecting_pickup_time"

    # ── FAST PATH: awaiting_confirmation ──────────────────────────────────────
    # Quando lo stato è awaiting_confirmation NON chiamiamo l'LLM.
    # La risposta del cliente è gestita con puro Python: sì/no/chiedi di nuovo.
    if conversation.state == "awaiting_confirmation":
        _confirm_words = {"si", "sì", "ok", "va bene", "confermo", "certo", "esatto", "perfetto", "yes"}
        _cancel_words  = {"no", "annulla", "cancella", "stop"}
        _is_confirm = _contains_any_word(message_lower, _confirm_words)
        _is_cancel  = _contains_any_word(message_lower, _cancel_words)

        existing_items = json.loads(conversation.items_json)
        merged_order = {
            "customer_name": conversation.customer_name,
            "pickup_time":   conversation.pickup_time,
            "items":         existing_items,
        }
        if _preorder_date:
            merged_order["pickup_date"] = _preorder_date.isoformat()

        order = None
        # Preordini: una domanda al riepilogo riceve la risposta, poi si richiede la conferma
        _recap_answer = None
        if _next_day_only:
            if _PRICE_QUESTION_RE.search(_normalize_for_match(request.message)):
                _recap_answer = f"{_recap_price_answer(session, existing_items, restaurant_id)} {RECAP_CONFIRM_QUESTION}"
            elif not _is_confirm and not _is_cancel:
                _recap_time = _time_in_sentence(request.message)
                if (
                    _recap_time and _recap_time != "prima_possibile"
                    and _check_pickup_time(_recap_time)[0] == conversation.pickup_time
                ):
                    _recap_answer = (
                        f"Sì, alle {_spoken_pickup_time(conversation.pickup_time)}. {RECAP_CONFIRM_QUESTION}"
                    )
        if _recap_answer:
            _is_confirm = _is_cancel = False
            _resp = _recap_answer
            _extracted_stub = {"intent": "recap_question", "items": [], "customer_name": None, "pickup_time": None}
        elif _is_confirm and not conversation.completed:
            order = _confirm_order(session, conversation, merged_order, restaurant_id)

            name_part = f", {merged_order['customer_name']}" if merged_order.get("customer_name") else ""
            _resp = (
                f"Perfetto{name_part}! Le arriva un SMS di conferma."
                if _is_mobile_phone(conversation.customer_phone)
                else f"Perfetto{name_part}, a presto!"
            )
            _extracted_stub = {"intent": "confirm_order", "items": [], "customer_name": None, "pickup_time": None}

        elif _is_cancel:
            conversation.items_json = "[]"
            conversation.customer_name = None
            conversation.pickup_time = None
            if not _next_day_only:
                conversation.pickup_date = None
            conversation.state = "collecting_items"
            conversation.completed = False
            session.add(conversation)
            session.commit()
            merged_order["items"] = []
            _resp = "Va bene, ordine annullato. Mi dica pure se vuole ricominciare."
            _extracted_stub = {"intent": "cancel_order", "items": [], "customer_name": None, "pickup_time": None}

        else:
            # Messaggio ambiguo — ripeti il riepilogo e richiedi conferma
            _resp = build_confirmation_recap(merged_order, _day_phrase)
            _extracted_stub = {"intent": "unknown", "items": [], "customer_name": None, "pickup_time": None}

        session.add(ConversationLog(
            session_id=request.session_id,
            user_message=request.message,
            extracted_order_json=json.dumps(_extracted_stub, ensure_ascii=False),
            merged_order_json=json.dumps(merged_order, ensure_ascii=False),
            response_message=_resp,
            valid=_is_confirm,
            missing_items_json="[]",
            state=conversation.state,
        ))
        session.commit()
        print(f"[Confirm] fast-path: confirm={_is_confirm} cancel={_is_cancel} → {_resp!r}")
        _log_chat_timing(
            request.session_id,
            "awaiting_confirmation",
            request_started_at,
            confirm=_is_confirm,
            cancel=_is_cancel,
        )
        return ChatResponse(
            session_id=request.session_id,
            user_message=request.message,
            extracted_order=_extracted_stub,
            merged_order=merged_order,
            valid=_is_confirm,
            missing_items=[],
            response_message=_resp,
            order_id=order.id if order is not None else None,
            state=conversation.state,
        )
    # ── FINE FAST PATH ────────────────────────────────────────────────────────

    # ── NORMAL PATH ──────────────────────────────────────────────────────────────

    # Gestione identità cliente riconosciuto da telefono
    _pending = conversation.pending_customer_name
    if _pending and not conversation.customer_name:
        _positive = any(m in message_lower for m in ["sì", "si", "esatto", "giusto", "corretto", "sono io"])
        _negative = any(m in message_lower for m in ["no", "sbagliato", "non sono"])
        if _positive:
            conversation.customer_name = _pending
            conversation.pending_customer_name = None
        elif _negative:
            conversation.pending_customer_name = None

    # ── FAST PATH: stati stretti senza LLM ───────────────────────────────────
    if conversation.state == "collecting_name":
        _name_proposed = _slot_state(conversation).get("customer_name", {}).get("proposed")
        _name_answer = _yes_no_answer(request.message) if _name_proposed else None
        if _name_answer == "no":
            _merged_now = _current_merged_order()
            _miss_state, _miss_resp = _name_miss(_merged_now)
            return _local_reply(_miss_state, _miss_resp, _merged_now, "name_not_understood")
        local_name = _name_proposed if _name_answer == "yes" else _extract_local_customer_name(request.message)
        if local_name and _day_changed and _spoken_pickup_day(local_name, restaurant_id):
            local_name = None  # "per domani": è il giorno, non il nome
        if local_name:
            existing_items = json.loads(conversation.items_json)
            conversation.customer_name = local_name
            _clear_slot_attempts(conversation, "customer_name")
            merged_order = {
                "customer_name": conversation.customer_name,
                "pickup_time": conversation.pickup_time,
                "items": existing_items,
            }
            if _preorder_date:
                merged_order["pickup_date"] = _preorder_date.isoformat()
            state = determine_state(
                merged_order=merged_order,
                missing_messages=[],
                completed=conversation.completed,
                intended_quantity=conversation.intended_quantity,
            )
            conversation.state = state
            response_message = build_assistant_response(
                merged_order=merged_order,
                state=state,
                missing_messages=[],
                order_saved=False,
                intent="set_customer_name",
                new_valid_items=[],
                customer_phone=conversation.customer_phone,
                day_phrase=_day_phrase,
                day_said=_day_said_for(state),
            )
            if _day_note:
                response_message = f"{_day_note} {response_message}"
            extracted_stub = {
                "intent": "set_customer_name",
                "items": [],
                "customer_name": local_name,
                "pickup_time": None,
            }
            valid = bool(existing_items and conversation.customer_name and conversation.pickup_time)
            session.add(conversation)
            session.add(ConversationLog(
                session_id=request.session_id,
                user_message=request.message,
                extracted_order_json=json.dumps(extracted_stub, ensure_ascii=False),
                merged_order_json=json.dumps(merged_order, ensure_ascii=False),
                response_message=response_message,
                valid=valid,
                missing_items_json="[]",
                state=state,
            ))
            session.commit()
            _log_chat_timing(request.session_id, "local_name", request_started_at, state=state)
            return ChatResponse(
                session_id=request.session_id,
                user_message=request.message,
                extracted_order=extracted_stub,
                merged_order=merged_order,
                valid=valid,
                missing_items=[],
                response_message=response_message,
                order_id=None,
                state=state,
            )

    if conversation.state == "collecting_pickup_time":
        _pt_data = _slot_state(conversation)
        _pt_slot = _pt_data.get("pickup_time", {})
        local_pickup_time = _extract_local_pickup_time(request.message, evening_only=False)
        _day_part = _spoken_day_part(request.message)
        if not local_pickup_time and _pt_slot.get("ambiguous") and _day_part:
            # Risposta a "Le 8 di mattina o le 8 di sera?"
            local_pickup_time = _pt_slot["ambiguous"][1 if _day_part == "sera" else 0]
        elif local_pickup_time and local_pickup_time != "prima_possibile":
            local_pickup_time, _ambiguity_question = _choose_pickup_hour(local_pickup_time, request.message)
            if _ambiguity_question:
                _merged_now = _current_merged_order()
                if _pt_slot.pop("ambiguous", None):
                    # Già chiesto mattina o sera: mai la stessa domanda due volte
                    _store_slot_state(conversation, _pt_data)
                    _miss_state, _miss_resp, _miss_order_id = _pickup_time_miss(_merged_now, None)
                    _log_pickup_time_turn("nessuno", conversation)
                    return _local_reply(_miss_state, _miss_resp, _merged_now, "pickup_time_not_understood", _miss_order_id)
                _ambiguous_hour = _extract_local_pickup_time(request.message, evening_only=False)
                _h, _m = (int(x) for x in _ambiguous_hour.split(":"))
                _pt_data.setdefault("pickup_time", {})["ambiguous"] = [f"{_h:02d}:{_m:02d}", f"{_h + 12:02d}:{_m:02d}"]
                _store_slot_state(conversation, _pt_data)
                _log_pickup_time_turn("regole", conversation)
                return _local_reply("collecting_pickup_time", _ambiguity_question, _merged_now, "pickup_time_ambiguous")
        _pt_proposed = _pt_slot.get("proposed")
        _pt_answer = _yes_no_answer(request.message) if _pt_proposed and not local_pickup_time else None
        if _pt_answer == "yes":
            local_pickup_time = _pt_proposed
        elif _pt_answer == "no":
            _merged_now = _current_merged_order()
            _miss_state, _miss_resp, _miss_order_id = _pickup_time_miss(_merged_now, None, refused_proposal=True)
            _log_pickup_time_turn("nessuno", conversation)
            return _local_reply(_miss_state, _miss_resp, _merged_now, "pickup_time_not_understood", _miss_order_id)
        if local_pickup_time:
            pickup_time = resolve_pickup_time(local_pickup_time, restaurant_id=restaurant_id, keep_hour=True)
            checked_time, pickup_time_error = _check_pickup_time(pickup_time)
            is_valid_pickup = pickup_time_error is None
            _keep_time_for_next_day(pickup_time, pickup_time_error)
            if checked_time:
                conversation.pickup_time = checked_time

            existing_items = json.loads(conversation.items_json)
            merged_order = {
                "customer_name": conversation.customer_name,
                "pickup_time": conversation.pickup_time,
                "items": existing_items,
            }
            if _preorder_date:
                merged_order["pickup_date"] = _preorder_date.isoformat()
            missing_messages = [pickup_time_error] if pickup_time_error else []
            state = (
                "confirming_next_day"
                if pickup_time_error and pickup_time_error == _next_day_question(restaurant_id)
                else "collecting_pickup_time"
                if pickup_time_error
                else determine_state(
                    merged_order=merged_order,
                    missing_messages=[],
                    completed=conversation.completed,
                    intended_quantity=conversation.intended_quantity,
                )
            )
            conversation.state = state
            response_message = build_assistant_response(
                merged_order=merged_order,
                state=state,
                missing_messages=missing_messages,
                order_saved=False,
                intent="set_pickup_time",
                new_valid_items=[],
                customer_phone=conversation.customer_phone,
                pickup_time_error=pickup_time_error,
                day_phrase=_day_phrase,
                day_said=_day_said_for(state),
            )
            order_id = None
            if state == "collecting_pickup_time":
                # Orario riconosciuto ma fuori orario: conta come risposta non valida
                state, response_message, order_id = _pickup_time_miss(
                    merged_order, pickup_time_error, requested=pickup_time,
                )
                if not conversation.completed:
                    conversation.state = state
            if _day_note:
                response_message = f"{_day_note} {response_message}"
            _log_pickup_time_turn("regole", conversation)
            if conversation.pickup_time:
                _clear_slot_attempts(conversation, "pickup_time")
            extracted_stub = {
                "intent": "set_pickup_time",
                "items": [],
                "customer_name": None,
                "pickup_time": pickup_time,
            }
            valid = bool(
                (not pickup_time_error or state == "completed")
                and existing_items
                and conversation.customer_name
                and conversation.pickup_time
            )
            session.add(conversation)
            session.add(ConversationLog(
                session_id=request.session_id,
                user_message=request.message,
                extracted_order_json=json.dumps(extracted_stub, ensure_ascii=False),
                merged_order_json=json.dumps(merged_order, ensure_ascii=False),
                response_message=response_message,
                valid=valid,
                missing_items_json=json.dumps(missing_messages, ensure_ascii=False),
                state=state,
            ))
            session.commit()
            _log_chat_timing(
                request.session_id,
                "local_pickup_time",
                request_started_at,
                state=state,
                valid=is_valid_pickup,
            )
            return ChatResponse(
                session_id=request.session_id,
                user_message=request.message,
                extracted_order=extracted_stub,
                merged_order=merged_order,
                valid=valid,
                missing_items=[],
                response_message=response_message,
                order_id=order_id,
                state=state,
            )

    # Quantità / porzione / caldo-freddo: risposta breve riconosciuta localmente →
    # niente LLM. Applica tutti i campi detti nella frase ("due interi caldi") e
    # chiede solo quelli che mancano. Frasi più ricche passano dall'LLM.
    if conversation.state in _KG_SLOT_STATES and len(request.message.split()) <= 5:
        existing_items = json.loads(conversation.items_json)
        slot = _KG_SLOT_STATES[conversation.state]
        applied = _apply_kg_answers_from_message(
            existing_items, request.message, bare_number_ok=slot == "quantity",
        )
        if slot not in applied and _apply_kg_proposal_answer(existing_items, slot, request.message):
            applied.add(slot)
        if applied:
            conversation.items_json = json.dumps(existing_items, ensure_ascii=False)
            merged_order = {
                "customer_name": conversation.customer_name,
                "pickup_time": conversation.pickup_time,
                "items": existing_items,
            }
            if _preorder_date:
                merged_order["pickup_date"] = _preorder_date.isoformat()
            state = determine_state(
                merged_order=merged_order,
                missing_messages=[],
                completed=conversation.completed,
                intended_quantity=conversation.intended_quantity,
            )
            conversation.state = state
            response_message = build_assistant_response(
                merged_order=merged_order,
                state=state,
                missing_messages=[],
                order_saved=False,
                intent=f"set_kg_{slot}",
                new_valid_items=[],
                customer_phone=conversation.customer_phone,
                day_phrase=_day_phrase,
                day_said=_day_said_for(state),
            )
            if _day_note:
                response_message = f"{_day_note} {response_message}"
            extracted_stub = {"intent": f"set_kg_{slot}", "items": [], "customer_name": None, "pickup_time": None}
            session.add(conversation)
            session.add(ConversationLog(
                session_id=request.session_id,
                user_message=request.message,
                extracted_order_json=json.dumps(extracted_stub, ensure_ascii=False),
                merged_order_json=json.dumps(merged_order, ensure_ascii=False),
                response_message=response_message,
                valid=False,
                missing_items_json="[]",
                state=state,
            ))
            session.commit()
            print(f"[KgSlot] {sorted(applied)} da risposta locale → stato={state!r}")
            _log_chat_timing(request.session_id, "local_kg_slot", request_started_at, state=state)
            return ChatResponse(
                session_id=request.session_id,
                user_message=request.message,
                extracted_order=extracted_stub,
                merged_order=merged_order,
                valid=False,
                missing_items=[],
                response_message=response_message,
                order_id=None,
                state=state,
            )

    # ── CONFIRMING_USUAL: skip LLM dove possibile ────────────────────────────

    # Una pizza fuori menu non è riproponibile come "le solite"
    fav_pizzas_session = [
        name for name in json.loads(conversation.favorite_pizzas_json or "[]")
        if name not in _OFF_MENU_PIZZA_NAMES
    ]

    # Gestore: il cliente risponde alla domanda "Le preparo le solite?"
    if conversation.state == "confirming_usual":
        fav_list = fav_pizzas_session[:3]
        _yes_usual = any(w in message_lower for w in [
            "sì", "si", "ok", "va bene", "certo", "esatto", "dai", "perfetto", "confermo",
            "sì grazie", "si grazie", "sì certo", "si certo",
        ])
        _no_usual = any(w in message_lower for w in [
            "no", "no grazie", "diverso", "diversi", "altri", "altre", "cambio", "diversa",
        ])

        if _yes_usual and fav_list:
            # Aggiungi le pizze preferite alla sessione
            existing_items = json.loads(conversation.items_json)
            added = []
            for pizza_name in fav_list:
                menu_item = session.exec(
                    _menu_select(
                        restaurant_id,
                        MenuItem.name == pizza_name,
                        MenuItem.available == True,  # noqa: E712
                    )
                ).first()
                if menu_item and not any(ei["pizza_name"] == pizza_name for ei in existing_items + added):
                    added.append({
                        "pizza_name": menu_item.name,
                        "pizza_type": menu_item.pizza_type,
                        "quantity": 1,
                        "dough_type": "classica",
                        "add_ingredients": [],
                        "remove_ingredients": [],
                    })
            merged_items_usual = existing_items + added
            conversation.items_json = json.dumps(merged_items_usual, ensure_ascii=False)
            conversation.intended_quantity = None
            merged_order_usual = {
                "customer_name": conversation.customer_name,
                "pickup_time": conversation.pickup_time,
                "items": merged_items_usual,
            }
            if _preorder_date:
                merged_order_usual["pickup_date"] = _preorder_date.isoformat()
            state_usual = determine_state(
                merged_order=merged_order_usual,
                missing_messages=[],
                completed=conversation.completed,
            )
            conversation.state = state_usual
            session.add(conversation)
            session.commit()
            _resp_usual = build_assistant_response(
                merged_order=merged_order_usual,
                state=state_usual,
                missing_messages=[],
                order_saved=False,
                intent="add_items",
                new_valid_items=added,
                customer_phone=conversation.customer_phone,
                day_phrase=_day_phrase,
                day_said=_day_said_for(state_usual),
            )
            session.add(ConversationLog(
                session_id=request.session_id,
                user_message=request.message,
                extracted_order_json=json.dumps({"intent": "confirm_usual", "items": added}, ensure_ascii=False),
                merged_order_json=json.dumps(merged_order_usual, ensure_ascii=False),
                response_message=_resp_usual,
                valid=bool(added),
                missing_items_json="[]",
                state=state_usual,
            ))
            session.commit()
            print(f"[Usual] Confermate {len(added)} pizze abituali → stato={state_usual!r}")
            _log_chat_timing(request.session_id, "confirming_usual_yes", request_started_at, added=len(added))
            return ChatResponse(
                session_id=request.session_id,
                user_message=request.message,
                extracted_order={"intent": "confirm_usual", "items": added},
                merged_order=merged_order_usual,
                valid=bool(added),
                missing_items=[],
                response_message=_resp_usual,
                order_id=None,
                state=state_usual,
            )

        elif _no_usual:
            conversation.state = "collecting_items"
            session.add(conversation)
            session.commit()
            _resp_usual = "Mi dica pure cosa desidera!"
            _merged_now = {
                "customer_name": conversation.customer_name,
                "pickup_time": conversation.pickup_time,
                "items": json.loads(conversation.items_json),
            }
            session.add(ConversationLog(
                session_id=request.session_id,
                user_message=request.message,
                extracted_order_json=json.dumps({"intent": "decline_usual", "items": [], "customer_name": None, "pickup_time": None}, ensure_ascii=False),
                merged_order_json=json.dumps(_merged_now, ensure_ascii=False),
                response_message=_resp_usual,
                valid=False,
                missing_items_json="[]",
                state="collecting_items",
            ))
            session.commit()
            print("[Usual] Rifiutate pizze abituali → collecting_items")
            _log_chat_timing(request.session_id, "confirming_usual_no", request_started_at)
            return ChatResponse(
                session_id=request.session_id,
                user_message=request.message,
                extracted_order={"intent": "decline_usual", "items": [], "customer_name": None, "pickup_time": None},
                merged_order=_merged_now,
                valid=False,
                missing_items=[],
                response_message=_resp_usual,
                order_id=None,
                state="collecting_items",
            )

        else:
            # Risposta ambigua: ripeti la domanda senza chiamare l'LLM
            pizza_list_str = _format_pizza_list(fav_list)
            _resp_usual = f"Le preparo le solite? {pizza_list_str}"
            _merged_now = {
                "customer_name": conversation.customer_name,
                "pickup_time": conversation.pickup_time,
                "items": json.loads(conversation.items_json),
            }
            session.add(ConversationLog(
                session_id=request.session_id,
                user_message=request.message,
                extracted_order_json=json.dumps({"intent": "unknown", "items": [], "customer_name": None, "pickup_time": None}, ensure_ascii=False),
                merged_order_json=json.dumps(_merged_now, ensure_ascii=False),
                response_message=_resp_usual,
                valid=False,
                missing_items_json="[]",
                state="confirming_usual",
            ))
            session.commit()
            _log_chat_timing(request.session_id, "confirming_usual_ambiguous", request_started_at)
            return ChatResponse(
                session_id=request.session_id,
                user_message=request.message,
                extracted_order={"intent": "unknown", "items": [], "customer_name": None, "pickup_time": None},
                merged_order=_merged_now,
                valid=False,
                missing_items=[],
                response_message=_resp_usual,
                order_id=None,
                state="confirming_usual",
            )

    # Trigger: transizione a confirming_usual (prima della chiamata LLM)
    if (
        fav_pizzas_session
        and not _today_with_products  # "2 tranci di zucca per stasera": i prodotti sono già detti
        and conversation.state == "collecting_items"
        and not json.loads(conversation.items_json)  # carrello vuoto
        and (
            extract_intended_quantity(request.message) is not None
            or any(w in message_lower for w in ["solite", "stesse", "stessa cosa", "al solito"])
        )
    ):
        _declared_qty = extract_intended_quantity(request.message)
        conversation.state = "confirming_usual"
        if _declared_qty:
            conversation.intended_quantity = _declared_qty
        _usual_day_note = _apply_spoken_day()
        if _closed_day_ask:
            return _local_reply("confirming_next_day", _closed_day_ask, _current_merged_order(), "closed_day_proposal")
        _usual_time = _time_in_sentence(request.message)
        if _usual_time and _usual_time != "prima_possibile":
            conversation.pickup_time, _usual_time_error = _check_pickup_time(_usual_time)
            _keep_time_for_next_day(_usual_time, _usual_time_error)
        session.add(conversation)
        session.commit()
        _usual_limit = _declared_qty if _declared_qty else 2
        fav_list = fav_pizzas_session[:_usual_limit]
        pizza_list_str = _format_pizza_list(fav_list)
        _resp_usual = f"Le preparo le solite? {pizza_list_str}"
        if _usual_day_note:
            _resp_usual = f"{_usual_day_note} {_resp_usual}"
        print(f"[Usual] declared_qty={_declared_qty} → propongo {len(fav_list)} pizze")
        _merged_now = {
            "customer_name": conversation.customer_name,
            "pickup_time": conversation.pickup_time,
            "items": [],
        }
        session.add(ConversationLog(
            session_id=request.session_id,
            user_message=request.message,
            extracted_order_json=json.dumps({"intent": "order_intent", "items": [], "customer_name": None, "pickup_time": None}, ensure_ascii=False),
            merged_order_json=json.dumps(_merged_now, ensure_ascii=False),
            response_message=_resp_usual,
            valid=False,
            missing_items_json="[]",
            state="confirming_usual",
        ))
        session.commit()
        print(f"[Usual] Trigger: limit={_usual_limit} → {len(fav_list)} pizze in lista → confirming_usual")
        _log_chat_timing(request.session_id, "confirming_usual_trigger", request_started_at, proposed=len(fav_list))
        return ChatResponse(
            session_id=request.session_id,
            user_message=request.message,
            extracted_order={"intent": "order_intent", "items": [], "customer_name": None, "pickup_time": None},
            merged_order=_merged_now,
            valid=False,
            missing_items=[],
            response_message=_resp_usual,
            order_id=None,
            state="confirming_usual",
        )

    # Risposta sì/no a "Intende Pepe Rosa?" (pizza fuori menu): niente LLM
    _proposal_slots = _slot_state(conversation)
    _proposal = _proposal_slots.get("off_menu_proposal")
    _proposal_answer = (
        _yes_no_answer(request.message) if _proposal and conversation.state == "collecting_items" else None
    )
    if _proposal_answer == "yes":
        _proposal_slots.pop("off_menu_proposal", None)
        _proposal_slots.pop("off_menu_asked", None)
        _proposal_slots.pop("off_menu_said", None)
        _proposal_slots.pop("off_menu_quantity", None)
        _store_slot_state(conversation, _proposal_slots)
        _row = session.exec(_menu_select(restaurant_id, MenuItem.name == _proposal["name"])).first()
        _chosen = {
            **_proposal["item"], "pizza_name": _proposal["name"], "add_ingredients": [],
            "pizza_type": _row.pizza_type if _row else _proposal["item"].get("pizza_type", "Normale"),
            "sale_unit": getattr(_row, "sale_unit", "piece") if _row else "piece",
        }
        if _chosen["sale_unit"] == "kg":
            if _chosen.get("order_unit") not in ("kg", "tranci"):
                # La quantità dell'LLM per un nome sconosciuto non è stata detta: si chiede
                _said_quantity = _extract_kg_quantity(_proposal.get("message", ""))
                _chosen["order_unit"], _chosen["quantity"] = _said_quantity or ("", 0)
            _normalize_kg_order_unit(_chosen, _proposal.get("message", ""))
            if _chosen.get("temperature") not in ("calda", "fredda"):
                _chosen["temperature"] = ""
        _merged_now = _current_merged_order()
        _merged_now["items"] = [*_merged_now["items"], _chosen]
        conversation.items_json = json.dumps(_merged_now["items"], ensure_ascii=False)
        _proposal_state = determine_state(
            merged_order=_merged_now,
            missing_messages=[],
            completed=conversation.completed,
            intended_quantity=conversation.intended_quantity,
        )
        _proposal_resp = build_assistant_response(
            merged_order=_merged_now,
            state=_proposal_state,
            missing_messages=[],
            order_saved=False,
            intent="add_items",
            new_valid_items=[_chosen],
            customer_phone=conversation.customer_phone,
            day_phrase=_day_phrase,
            restaurant_id=restaurant_id,
            day_said=_day_said_for(_proposal_state),
        )
        print(f"[Cart] Proposta accettata: {_proposal['name']!r}")
        return _local_reply(_proposal_state, _proposal_resp, _merged_now, "off_menu_proposal_yes")
    if _proposal_answer == "no":
        _proposal_slots.pop("off_menu_proposal", None)
        _merged_now = _current_merged_order()
        if _proposal.get("kind") == "loop":
            _store_slot_state(conversation, _proposal_slots)
            _handover_state, _handover_resp = _callback_fallback()
            return _local_reply(_handover_state, _handover_resp, _merged_now, "off_menu_handover")
        # "No" a "Intende Pepe Rosa?": si propongono i gusti più simili
        _proposal_menu = get_proposable_menu(restaurant_id=restaurant_id)
        _proposal_resp = _off_menu_question([_proposal["item"]], _proposal_menu)
        _proposal_slots["off_menu_asked"] = 1
        _store_slot_state(conversation, _proposal_slots)
        return _local_reply("collecting_items", _proposal_resp, _merged_now, "off_menu_proposal_no")

    # Da qui in poi serve davvero l'LLM: carichiamo menu/dough solo dopo tutti
    # i fast path locali, così i turni semplici non pagano latenza di rete/cache.
    # get_proposable_menu() filtra available=True + no ingredienti finiti.
    menu_items_for_llm = get_proposable_menu(restaurant_id=restaurant_id)
    first_names = [item["name"] for item in menu_items_for_llm[:3]]
    print(f"[Chat] menu_items_for_llm: {len(menu_items_for_llm)} voci proposabili. Prime 3: {first_names}")

    if not menu_items_for_llm:
        print("[Chat] Fallback al DB locale")
        db_menu_items = session.exec(_menu_select(restaurant_id)).all()
        menu_items_for_llm = [
            {
                "name": item.name,
                "category": item.category,
                "dough_type": _PIZZA_TYPE_TO_DOUGH.get(item.pizza_type, "classica"),
                "pizza_type": item.pizza_type,
                "price": item.price,
                "available": item.available,
                "ingredients": [],
                "sale_unit": item.sale_unit,
            }
            for item in db_menu_items
        ]

    dough_items = load_doughs()

    # 1. Estrai item SOLO dal messaggio corrente.
    # Il contesto di sessione (items, nome) va nel messaggio utente — non nel system prompt —
    # così il system prompt rimane statico e il prefix caching OpenAI rimane attivo.
    existing_items_ctx = json.loads(conversation.items_json)
    extracted = extract_order_from_text(
        strip_restaurant_name(request.message, _restaurant_name) or request.message,
        menu_items_for_llm,
        dough_items,
        state=conversation.state,
        existing_items=existing_items_ctx,
        customer_name=conversation.customer_name,
        restaurant_id=restaurant_id,
    )

    # LLM timeout fallback: rispondi "Ok!" e lascia il turno successivo riprocessare
    if extracted.get("_llm_fallback"):
        print("[Chat] LLM fallback attivo → risposta Ok! senza modifiche al carrello")
        _current_merged = {
            "customer_name": conversation.customer_name,
            "pickup_time": conversation.pickup_time,
            "items": json.loads(conversation.items_json),
        }
        session.add(ConversationLog(
            session_id=request.session_id,
            user_message=request.message,
            extracted_order_json=json.dumps({"intent": "llm_timeout", "items": []}, ensure_ascii=False),
            merged_order_json=json.dumps(_current_merged, ensure_ascii=False),
            response_message="Ok!",
            valid=False,
            missing_items_json="[]",
            state=conversation.state,
        ))
        session.commit()
        _log_chat_timing(request.session_id, "llm_fallback", request_started_at, state=conversation.state)
        return ChatResponse(
            session_id=request.session_id,
            user_message=request.message,
            extracted_order={"intent": "llm_timeout", "items": []},
            merged_order=_current_merged,
            valid=False,
            missing_items=[],
            response_message="Ok!",
            order_id=None,
            state=conversation.state,
        )

    existing_items = json.loads(conversation.items_json)
    _cart_before_turn = json.loads(conversation.items_json)  # il merge modifica le righe sul posto
    new_items = extracted.get("items", [])
    intent = extracted.get("intent", "unknown")
    # Il nome del locale non è mai un prodotto, anche se somiglia a una voce del menu
    _dropped_additions = False
    _kept_by_name = [
        item for item in new_items
        if not is_only_greeting(str(item.get("pizza_name") or ""), _restaurant_name)
    ]
    if len(_kept_by_name) != len(new_items):
        new_items, _dropped_additions = _kept_by_name, True
    for item in new_items:
        item.setdefault("add_ingredients", [])
        item.setdefault("remove_ingredients", [])
        # Lookup sale_unit from DB so it's available throughout the conversation
        if item.get("pizza_name") and item["pizza_name"] != "Personalizzata":
            _mi = session.exec(
                _menu_select(restaurant_id, MenuItem.name == item["pizza_name"])
            ).first()
            item["sale_unit"] = getattr(_mi, "sale_unit", "piece") if _mi else "piece"
            # Pizza fuori menu: Margherita base con il nome detto come aggiunta. Un nome
            # simile a una voce del menu resta così: il backend chiede "Forse intendevi...".
            if (
                not _mi
                and intent in ("add_items", "modify_items", "replace_items")
                and item["pizza_name"] != "__last__"
                and not item["pizza_name"].upper().endswith("(SG)")
                and not get_typo_correction_suggestions(session, item["pizza_name"], limit=1, restaurant_id=restaurant_id)
            ):
                print("[Cart] Pizza fuori menu: Margherita base con il nome come aggiunta")
                item["add_ingredients"] = [item["pizza_name"].lower(), *item["add_ingredients"]]
                item["pizza_name"] = "Personalizzata"
        else:
            item["sale_unit"] = "piece"

    # Margherita + aggiunte solo se le parole sono ingredienti del locale o alias. Saluti,
    # nome del locale e nomi di persona non diventano mai aggiunte.
    # Locale senza pizza personalizzata: un nome che sembra un prodotto ma non è nel menu
    # né tra gli ingredienti ("una capricciosa") non si scarta in silenzio. Va alla domanda
    # sulla pizza fuori menu, che dice cosa non c'è, propone i gusti più vicini e non va in loop.
    _custom_pizza_on = is_custom_pizza_enabled(restaurant_id)
    _kept_items = []
    for item in new_items:
        _said = item.get("add_ingredients") or []
        if item.get("pizza_name") in _OFF_MENU_PIZZA_NAMES:
            _good = [a for a in _said if is_ingredient(str(a), menu_items_for_llm, _restaurant_name)]
        else:
            _good = [a for a in _said if not is_only_greeting(str(a), _restaurant_name)]
        if len(_good) != len(_said):
            _dropped_additions = True
            print(f"[Cart] Scartate {len(_said) - len(_good)} parole che non sono ingredienti")
            _unknown = [
                a for a in _said
                if a not in _good and _looks_like_product_name(str(a), _restaurant_name, conversation.customer_name)
            ]
            if (
                not _custom_pizza_on
                and item.get("pizza_name") in _OFF_MENU_PIZZA_NAMES
                and _unknown
                and intent in ("add_items", "modify_items", "replace_items")
            ):
                print(f"[Cart] {len(_unknown)} nomi non riconosciuti: domanda sui gusti del menu")
                item["add_ingredients"] = [a for a in _said if a in _good or a in _unknown]
                _kept_items.append(item)
                continue
            item["add_ingredients"] = _good
            if not _good and item.get("pizza_name") in _OFF_MENU_PIZZA_NAMES:
                continue
        _kept_items.append(item)
    new_items = _kept_items
    if _dropped_additions and not new_items and intent in ("add_items", "modify_items", "replace_items"):
        intent = "unknown"
    # "Una Mario" è già un ordine anche se l'LLM non ha trovato nessuna pizza
    if not new_items and intent == "unknown" and re.match(
        rf"\s*{_OFF_MENU_COUNT}\s+[a-z]", _normalize_for_match(request.message),
    ):
        _dropped_additions = True

    # custom_pizza_enabled = false: la pizza fuori menu non si prende, si propongono i
    # gusti più simili. La quantità detta vale per il gusto scelto ("2 salamino" → "diavola").
    _off_menu_reply: str | None = None
    _off_menu_handover = False
    _off_menu_slots = _slot_state(conversation)
    if intent in ("add_items", "modify_items", "replace_items") and not _custom_pizza_on:
        _refused = [item for item in new_items if item["pizza_name"] in _OFF_MENU_PIZZA_NAMES]
        if _refused:
            new_items = [item for item in new_items if item["pizza_name"] not in _OFF_MENU_PIZZA_NAMES]
            # Mai la stessa domanda all'infinito: 1ª volta la domanda con i gusti simili (o
            # "Intende X?" se il nome è dentro un solo prodotto), ripetuta una volta; alla
            # seconda risposta non risolta "Intende X?" con sì/no; poi si passa al locale.
            _asked = int(_off_menu_slots.get("off_menu_asked") or 0)
            _pending_proposal = _off_menu_slots.pop("off_menu_proposal", None)
            _said = " ".join(_refused[0].get("add_ingredients") or [])
            # Un gusto diverso dal precedente è una richiesta nuova: il conteggio riparte
            _said_key = " ".join(sorted(_name_words(" ".join(
                a for refused in _refused for a in (refused.get("add_ingredients") or [])
            ))))
            _previous_key = _off_menu_slots.get("off_menu_said")
            if _previous_key is not None and _previous_key != _said_key:
                print("[Cart] Pizza fuori menu: gusto diverso dal precedente, il conteggio riparte")
                _asked, _pending_proposal = 0, None
            _off_menu_slots["off_menu_said"] = _said_key
            _contained = (
                _only_menu_name_containing(_said, menu_items_for_llm)
                if len(_refused) == 1 and _asked == 0 and not _pending_proposal else None
            )
            # Dal terzo turno sulla stessa richiesta: "Intende X?" solo per un nome vicino
            _close = (
                (_close_menu_name(_said, menu_items_for_llm) or _close_menu_name(request.message, menu_items_for_llm))
                if _asked >= 2 and not _contained else None
            )
            if (_pending_proposal and _pending_proposal.get("kind") == "loop") or _asked >= 3:
                _off_menu_handover = True
                _off_menu_reply = CALLBACK_HANDOFF_MESSAGE
                print("[Cart] Pizza fuori menu: ancora non risolta dopo la proposta → passo al locale")
            elif _contained or _close:
                _proposed = _contained or _close
                _off_menu_slots["off_menu_proposal"] = {
                    "name": _proposed, "kind": "contained" if _contained else "loop",
                    "item": _refused[0], "message": request.message,
                }
                _off_menu_reply = _off_menu_proposal_question(_proposed)
                print(f"[Cart] Pizza fuori menu: propongo {_proposed!r} con sì/no")
            elif _asked >= 2:
                _off_menu_reply = _off_menu_last_list(_refused, menu_items_for_llm)
                print("[Cart] Pizza fuori menu: nessun nome vicino, propongo i gusti senza \"Intende\"")
            else:
                _unavailable = {
                    _normalize_for_match(row.name)
                    for row in session.exec(_menu_select(restaurant_id, MenuItem.available == False)).all()  # noqa: E712
                }
                _off_menu_reply = _off_menu_question(_refused, menu_items_for_llm, _unavailable)
            _off_menu_slots["off_menu_asked"] = _asked + 1
            if len(_refused) == 1 and int(_refused[0].get("quantity") or 1) > 1:
                _off_menu_slots["off_menu_quantity"] = int(_refused[0]["quantity"])
            _store_slot_state(conversation, _off_menu_slots)
            print(f"[Cart] Pizza fuori menu non accettata dal locale: {len(_refused)} voci")
    if new_items and not _off_menu_reply and _off_menu_slots.get("off_menu_quantity"):
        _said_quantity = _off_menu_slots.pop("off_menu_quantity")
        _store_slot_state(conversation, _off_menu_slots)
        if (
            len(new_items) == 1
            and new_items[0].get("sale_unit") != "kg"
            and not _SPOKEN_COUNT_RE.search(_normalize_for_match(request.message))
        ):
            new_items[0]["quantity"] = _said_quantity
    if not _off_menu_reply and not new_items and _off_menu_slots.get("off_menu_pending"):
        # "Sì, alle 8" alla domanda sul giorno: ora la pizza che non c'è
        _off_menu_reply = _off_menu_slots.pop("off_menu_pending")
        _store_slot_state(conversation, _off_menu_slots)
    if (
        not _off_menu_reply
        and (new_items or intent != "unknown")  # un turno a vuoto non risolve nulla
        and (_off_menu_slots.get("off_menu_asked") or _off_menu_slots.get("off_menu_proposal"))
    ):
        # Risolta o lasciata dal cliente: il conteggio riparte
        _off_menu_slots.pop("off_menu_asked", None)
        _off_menu_slots.pop("off_menu_proposal", None)
        _off_menu_slots.pop("off_menu_said", None)
        _store_slot_state(conversation, _off_menu_slots)

    # Voci al taglio nuove: si tengono solo quantità, porzione e caldo/freddo detti
    # dal cliente. Nessun default: i campi mancanti vengono chiesti (determine_state).
    _prior_state = conversation.state
    for item in new_items:
        if item.get("sale_unit") == "kg":
            _normalize_kg_order_unit(item, request.message)
            if item.get("temperature") not in ("calda", "fredda"):
                item["temperature"] = ""
    _apply_kg_clause_values(new_items, request.message)
    # G2 (PaP): nessuna modifica di ingredienti. Su un gusto già nel carrello la frase non
    # cambia la riga (né la quantità): si chiede "Vuole cambiare gusto?".
    _flavour_change_ask: str | None = None
    if _pap_guards and new_items:
        _cart_names = {line.get("pizza_name") for line in json.loads(conversation.items_json)}
        _unmodified = []
        for item in new_items:
            if item.get("add_ingredients") or item.get("remove_ingredients"):
                print(
                    f"[Guard] riga sospetta: {item.get('pizza_name')!r} con modifiche "
                    f"add={item.get('add_ingredients')} remove={item.get('remove_ingredients')}: tolte"
                )
                item["add_ingredients"], item["remove_ingredients"] = [], []
                _flavour_change_ask = item["pizza_name"]
                if item.get("pizza_name") in _cart_names:
                    continue  # la riga del carrello resta com'è
            _unmodified.append(item)
        new_items = _unmodified

    # Prodotto riconosciuto solo dall'LLM ("una capriciosa"): l'ordine è deciso anche così
    if (
        new_items
        and not _next_day_only
        and intent in ("add_items", "modify_items", "replace_items")
        and _slot_state(conversation).get("intent_question")
    ):
        _keep_earlier_day_and_time()

    # Giorno già detto dal cliente: va fissato prima di validare l'orario
    if intent not in ("cancel_order", "clear_cart"):
        _day_note = _apply_spoken_day() or _day_note

    # 2. Aggiorna orario di ritiro (con validazione orari)
    pickup_time_error = None
    _extracted_time = extracted.get("pickup_time")
    if _preorder_date and not _next_day_only and _extracted_time == "prima_possibile":
        # "Prima possibile" vale per oggi: per un altro giorno è la sua apertura.
        # L'LLM lo restituisce anche per un giorno detto senza orario ("per domani").
        _extracted_time = (
            first_pickup_slot(restaurant_id, pickup_date=_preorder_date)
            if re.search(r"prima|subito|appena", message_lower)
            else None
        )
    if _extracted_time and _closed_day_ask:
        # Il giorno è ancora da concordare: l'orario si valuta quando il cliente risponde
        if _extracted_time != "prima_possibile":
            _keep_time_for_next_day(_extracted_time, _next_day_question(restaurant_id))
    elif _extracted_time:
        pt = resolve_pickup_time(_extracted_time, restaurant_id=restaurant_id)
        checked_time, pickup_time_error = _check_pickup_time(pt)
        _keep_time_for_next_day(pt, pickup_time_error)
        if checked_time:
            conversation.pickup_time = checked_time

    # 3. Aggiorna nome cliente
    if extracted.get("customer_name"):
        conversation.customer_name = extracted["customer_name"]

    # 4. Merge semplice: sessione = fonte di verità
    removed_names: list[str] = []
    not_found_names: list[str] = []

    if intent == "cancel_order":
        merged_items = []
        conversation.customer_name = None
        conversation.pickup_time = None
        conversation.intended_quantity = None
        if not _next_day_only:
            conversation.pickup_date = None
    elif intent == "clear_cart":
        merged_items = []
        conversation.intended_quantity = None
        print("[Cart] Carrello svuotato")
    elif intent == "replace_items" and new_items:
        merged_items = new_items
    elif intent == "remove_items" and new_items:
        merged_items = list(existing_items)
        for ni in new_items:
            target_name = ni["pizza_name"]
            # Risolvi "__last__" → ultima pizza nel carrello
            if target_name == "__last__":
                if merged_items:
                    target_name = merged_items[-1]["pizza_name"]
                else:
                    not_found_names.append("l'ultima pizza")
                    continue
            # Cerca e rimuovi (case-insensitive, prima occorrenza)
            before_len = len(merged_items)
            merged_items = [ei for ei in merged_items if ei["pizza_name"].lower() != target_name.lower()]
            if len(merged_items) < before_len:
                print(f"[Cart] Rimosso: {target_name}")
                removed_names.append(target_name)
            else:
                print(f"[Cart] Non trovato nel carrello: {target_name}")
                not_found_names.append(target_name)
        # Aggiorna intended_quantity in base agli item rimasti
        if conversation.intended_quantity is not None:
            new_qty = sum(int(ei.get("quantity") or 1) for ei in merged_items)
            conversation.intended_quantity = new_qty if new_qty > 0 else None
    elif new_items:
        # Stessa pizza + stesso impasto → somma quantità e aggiorna ingredienti; altrimenti aggiungi
        merged_items = list(existing_items)
        _adds_more = bool(_EXPLICIT_ADD_RE.search(_normalize_for_match(request.message)))
        for ni in new_items:
            ni_add = ni.get("add_ingredients") or []
            ni_rem = ni.get("remove_ingredients") or []
            # Prodotto già nel carrello ripetuto con la stessa quantità (es. alla domanda
            # intero/mezzo): non si aggiunge nulla, valgono solo i dettagli nuovi.
            # Si aggiunge solo con "altri", "aggiungi", "anche".
            _repeated = None if _adds_more else _repeated_cart_item(merged_items, ni)
            if _repeated is not None:
                print(f"[Cart] {ni['pizza_name']!r} ripetuto con la stessa quantità: nessuna aggiunta")
                _fill_repeated_item(_repeated, ni)
                continue
            # Locale senza pizza personalizzata: lo stesso prodotto detto con un'altra
            # quantità ("3 tranci di zucca" con 2 nel carrello) la sostituisce. Si somma
            # solo con "anche", "ancora", "in più", "altri".
            _requantified = (
                None if _adds_more or _custom_pizza_on
                else _repeated_cart_item(
                    [ei for ei in merged_items if any(ei is old for old in existing_items)], ni, same_quantity=False,
                )
            )
            if _requantified is not None:
                print(f"[Cart] {ni['pizza_name']!r}: quantità {_requantified.get('quantity')} → {ni['quantity']}")
                _requantified["quantity"] = ni["quantity"]
                if _requantified.get("sale_unit") == "kg" and not _requantified.get("order_unit"):
                    _requantified["order_unit"] = ni.get("order_unit", "")
                _fill_repeated_item(_requantified, ni)
                continue
            for ei in merged_items:
                if (ei["pizza_name"] == ni["pizza_name"]
                        and ei["pizza_type"] == ni["pizza_type"]
                        and ei.get("size", "normale") == ni.get("size", "normale")
                        and ei.get("order_unit", "") == ni.get("order_unit", "")
                        and ei.get("temperature", "") == ni.get("temperature", "")):
                    ei["quantity"] += ni["quantity"]
                    # Aggiorna ingredienti solo se il nuovo item ne dichiara
                    if ni_add:
                        ei["add_ingredients"] = ni_add
                    if ni_rem:
                        ei["remove_ingredients"] = ni_rem
                    break
            else:
                merged_items.append(ni)
    else:
        merged_items = existing_items

    # Cambio di gusto al riepilogo ("invece di zucca mettimi pepe rosa"): il gusto nuovo
    # prende quantità, porzione e caldo/freddo di quello che sostituisce, se non li ha
    if _recap_correction_before is not None:
        _before_items = json.loads(_recap_correction_before[0])
        _gone = [o for o in _before_items if all(o["pizza_name"] != m["pizza_name"] for m in merged_items)]
        _arrived = [m for m in merged_items if all(m["pizza_name"] != o["pizza_name"] for o in _before_items)]
        if len(_gone) == 1 and len(_arrived) == 1 and _gone[0].get("sale_unit") == _arrived[0].get("sale_unit") == "kg":
            _old, _new = _gone[0], _arrived[0]
            if _kg_slot_missing(_new, "quantity"):
                _new["order_unit"], _new["quantity"] = _old.get("order_unit", ""), _old.get("quantity", 0)
            if _kg_slot_missing(_new, "portion") and _old.get("size") in ("piena", "mezza"):
                _new["size"] = _old["size"]
            if _kg_slot_missing(_new, "temperature") and _old.get("temperature") in ("calda", "fredda"):
                _new["temperature"] = _old["temperature"]
            print(f"[Confirm] cambio gusto al riepilogo: {_old['pizza_name']!r} → {_new['pizza_name']!r}")

    # Risposte a porzione / caldo-freddo per le voci già nel carrello. Con voci nuove
    # nello stesso messaggio valgono solo i valori estratti per ciascuna voce
    # ("2 etti di porchetta calda" non rende calda anche la bufala di prima).
    _kg_unresolved_now = False
    if not new_items:
        # "Salmone e limone fredda": il prodotto nominato prende il valore detto, anche
        # se nel carrello ne aveva già un altro
        _kg_named = _apply_kg_clause_values(merged_items, request.message, temperature_only=True)
        _kg_applied = _apply_kg_answers_from_message(
            merged_items, request.message, bare_number_ok=_prior_state == "collecting_kg_quantity",
        ) | _kg_named
        if (
            _prior_state in _KG_SLOT_STATES
            and intent != "ask_kg_price"  # una domanda sul prezzo non è una risposta mancata
            and not pickup_time_error  # orario rifiutato: la domanda si ripete dopo l'errore
            and _KG_SLOT_STATES[_prior_state] not in _kg_applied
        ):
            _kg_unresolved_now = _register_kg_miss(merged_items, _KG_SLOT_STATES[_prior_state])

    # G1, G2, G4 (PaP): prima di ogni riepilogo il carrello si controlla col codice
    if _pap_guards:
        _pap_guard_cart(
            merged_items,
            before=json.loads(_recap_correction_before[0]) if _recap_correction_before else _cart_before_turn,
            message=request.message,
            weight_said=bool(_slot_state(conversation).get("weight_said")),
        )

    merged_order = {
        "customer_name": conversation.customer_name,
        "pickup_time": conversation.pickup_time,
        "items": merged_items,
    }
    if _preorder_date:
        merged_order["pickup_date"] = _preorder_date.isoformat()
    conversation.items_json = json.dumps(merged_items, ensure_ascii=False)

    # 5. Validazione item contro DB (nessun fuzzy matching)
    valid_items = []
    invalid_items = []
    missing_messages = []

    # Precarica nomi items nascosti per ingredienti finiti (una sola chiamata per turno)
    _sold_out_names = get_sold_out_item_names(restaurant_id=restaurant_id)

    for item in merged_items:
        # Pizza libera: sempre valida, usa prezzo Margherita come base
        if item["pizza_name"] == "Personalizzata":
            valid_items.append(item)
            continue

        menu_item = session.exec(
            _menu_select(
                restaurant_id,
                MenuItem.name == item["pizza_name"],
                MenuItem.pizza_type == item["pizza_type"],
            )
        ).first()
        # Fallback: il dough_type è una preferenza del cliente, non una voce separata
        # nel DB (che ha solo pizza_type="Normale"). Se non trovato con pizza_type
        # specifico, cerca per solo nome — se esiste la pizza è valida.
        if not menu_item:
            menu_item = session.exec(
                _menu_select(restaurant_id, MenuItem.name == item["pizza_name"])
            ).first()
            if menu_item:
                item["pizza_type"] = menu_item.pizza_type
        if menu_item and menu_item.available:
            # Verifica ingredienti finiti (sold_out_ingredients dal ristorante)
            if item["pizza_name"].lower() in _sold_out_names:
                msg = _build_sold_out_item_message(session, item["pizza_name"], _sold_out_names)
                missing_messages.append(msg)
                invalid_items.append(item)
            elif menu_item.sale_unit == "kg":
                # Voci al taglio: quantità mancante chiesta come slot (determine_state)
                item["sale_unit"] = "kg"
                valid_items.append(item)
            else:
                dough_code = item.get("dough_type", "classica")
                if is_dough_available(dough_code):
                    valid_items.append(item)
                else:
                    invalid_items.append(item)
                    missing_messages.append(f"L'impasto '{dough_code}' non è disponibile.")
        else:
            msg, _ = build_missing_item_message(session, item, restaurant_id)
            missing_messages.append(msg)
            invalid_items.append(item)

    missing_items = [f'{item["pizza_name"]} ({item["pizza_type"]})' for item in invalid_items]

    # 6. La conferma avviene solo nel fast path awaiting_confirmation, cioè dopo
    # che il cliente ha sentito il riepilogo: un "sì" qui porta al riepilogo.

    valid = (
        len(invalid_items) == 0
        and len(valid_items) > 0
        and merged_order.get("customer_name") is not None
        and merged_order.get("pickup_time") is not None
    )

    # Aggiorna quantità dichiarata (es. "vorrei due pizze") — sempre, anche quando
    # l'LLM ha già estratto item specifici (altrimenti intended_quantity resta None
    # se l'LLM estrae un item generico dalla frase di dichiarazione).
    declared = extract_intended_quantity(request.message)
    if declared:
        conversation.intended_quantity = declared

    _items_count = sum(int(i.get("quantity") or 1) for i in merged_items)
    print(f"[Session] intended_quantity={conversation.intended_quantity} items_count={_items_count}")

    order_id = None
    order_saved = False

    state = determine_state(
        merged_order=merged_order,
        missing_messages=missing_messages,
        completed=conversation.completed,
        intended_quantity=conversation.intended_quantity,
    )
    # Orario rifiutato: non riporta a collecting_items. Una domanda su porzione o
    # caldo/freddo resta aperta; altrimenti si richiede l'orario.
    if pickup_time_error:
        if not missing_messages and state not in _KG_SLOT_STATES and state != "completed":
            state = "collecting_pickup_time"
        missing_messages.append(pickup_time_error)
    # Override esplicito: se intended_quantity è dichiarato e non raggiunto,
    # forza collecting_items indipendentemente da ciò che determine_state ha calcolato.
    if conversation.intended_quantity and state not in ("collecting_items", "completed"):
        _collected = sum(int(item.get("quantity") or 1) for item in merged_order.get("items", []))
        if _collected < conversation.intended_quantity:
            print(f"[State] intended_quantity={conversation.intended_quantity} _collected={_collected} → forza collecting_items")
            state = "collecting_items"
    _next_day_ask = (_ask_next_day or bool(_closed_day_ask)) and state != "completed" and intent != "cancel_order"
    if _next_day_ask:
        state = "confirming_next_day"
        if _off_menu_reply:
            # Prima si concorda il giorno, poi la pizza che non c'è
            _pending_slots = _slot_state(conversation)
            _pending_slots["off_menu_pending"] = _off_menu_reply
            _store_slot_state(conversation, _pending_slots)
    elif _off_menu_reply and state != "completed":
        state = "collecting_items"
    conversation.state = state

    response_message = build_assistant_response(
        merged_order=merged_order,
        state=conversation.state,
        missing_messages=missing_messages,
        order_saved=order_saved,
        intent=intent,
        new_valid_items=valid_items,
        customer_phone=conversation.customer_phone,
        pickup_time_error=pickup_time_error,
        removed_names=removed_names,
        not_found_names=not_found_names,
        intended_quantity=conversation.intended_quantity,
        day_phrase=_day_phrase,
        restaurant_id=restaurant_id,
        day_said=_day_said_for(conversation.state),
    )
    _intent_not_known = (
        _reservations_on
        and intent == "unknown"
        and not merged_items
        and not conversation.pickup_time
        and not missing_messages
        and not _slot_state(conversation).get("intent_asked")
    )
    if _next_day_ask and _intent_not_known and not _closed_day_ask:
        _proposal_day, _closed_question = _closed_kitchen_intent_question(restaurant_id)
        response_message = _ask_call_intent(_closed_question, "confirming_next_day", _proposal_day)
    elif _next_day_ask:
        response_message = _closed_day_ask or _next_day_question(restaurant_id)
    elif _off_menu_reply and state != "completed":
        response_message = _off_menu_reply
    elif _dropped_additions and not merged_items and not missing_messages:
        response_message = "Cosa desidera ordinare?"
    elif _intent_not_known and conversation.state == "collecting_items":
        response_message = _ask_call_intent(INTENT_QUESTION, "collecting_items")
    elif (_day_note or _day_changed) and not merged_items and not missing_messages:
        response_message = "Cosa desidera ordinare?"
    if _off_menu_handover and state != "completed" and not _next_day_ask:
        conversation.state, response_message = _callback_fallback()
    if _flavour_change_ask and not conversation.completed and not _off_menu_reply:
        # G2: "Vuole cambiare gusto?" al posto di una modifica di ingredienti
        _fc_slots = _slot_state(conversation)
        _fc_slots["flavour_change"] = _flavour_change_ask
        _store_slot_state(conversation, _fc_slots)
        response_message = FLAVOUR_CHANGE_QUESTION

    # Domande obbligatorie rimaste senza risposta: mai la stessa domanda due volte di fila
    if _kg_unresolved_now:
        response_message = f"Va bene, lo segno da verificare. {response_message}"
    if conversation.customer_name:
        _clear_slot_attempts(conversation, "customer_name")
    elif _prior_state == "collecting_name" and conversation.state == "collecting_name" and not _day_changed:
        conversation.state, response_message = _name_miss(merged_order)
    if _prior_state == "collecting_pickup_time":
        if (
            conversation.state == "collecting_pickup_time"
            and not conversation.pickup_time
            and intent != "ask_kg_price"
            and not _day_changed  # ha detto il giorno: non è una risposta mancata
        ):
            conversation.state, response_message, order_id = _pickup_time_miss(merged_order, pickup_time_error)
            valid = valid or conversation.state == "completed"
        _log_pickup_time_turn(
            "llm" if extracted.get("pickup_time") and not pickup_time_error else "nessuno", conversation,
        )
    if conversation.pickup_time:
        _clear_slot_attempts(conversation, "pickup_time")
    if not _next_day_ask and conversation.state != "completed":
        _llm_time_question = _refused_time_question()
        if _llm_time_question:
            conversation.state, response_message = "collecting_pickup_time", _llm_time_question
    if _day_note:
        response_message = f"{_day_note} {response_message}"
    # Correzione al riepilogo che non ha cambiato nulla: mai lo stesso riepilogo, si
    # chiede cosa cambiare
    if (
        _recap_correction_before is not None
        and (conversation.items_json, conversation.pickup_time) == _recap_correction_before
        and conversation.state == "awaiting_confirmation"
        and not missing_messages
        and not _flavour_change_ask  # G2: c'è già la domanda sul gusto
    ):
        response_message = _recap_change_question(merged_items)

    session.add(conversation)
    session.commit()

    log_entry = ConversationLog(
        session_id=request.session_id,
        user_message=request.message,
        extracted_order_json=json.dumps(extracted, ensure_ascii=False),
        merged_order_json=json.dumps(merged_order, ensure_ascii=False),
        response_message=response_message,
        valid=valid,
        missing_items_json=json.dumps(missing_items, ensure_ascii=False),
        state=conversation.state,
    )
    session.add(log_entry)
    session.commit()

    _log_chat_timing(
        request.session_id,
        "llm_path",
        request_started_at,
        state=conversation.state,
        intent=intent,
        valid=valid,
    )
    return ChatResponse(
        session_id=request.session_id,
        user_message=request.message,
        extracted_order=extracted,
        merged_order=merged_order,
        valid=valid,
        missing_items=missing_items,
        response_message=response_message,
        order_id=order_id,
        state=conversation.state,
    )
