import json
import random
import unicodedata
import uuid
import re

from typing import Annotated

from difflib import SequenceMatcher

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from app.db import get_session
from app.models import (
    MenuItem,
    Order,
    OrderItem,
    ConversationSession,
    ConversationLog,
)
from app.schemas import ChatRequest, ChatResponse, ChatStartResponse
from app.services.conversation_service import (
    extract_order_from_text,
    load_menu_from_base44,
    load_doughs,
    save_order_to_base44,
    get_dough_surcharge,
    is_dough_available,
    INGREDIENT_EXTRA_PRICE,
    _PIZZA_TYPE_TO_DOUGH,
    get_agent_greeting,
    validate_pickup_time,
    lookup_customer,
    upsert_customer,
)

router = APIRouter(prefix="/chat", tags=["chat"])

SessionDep = Annotated[Session, Depends(get_session)]


def build_missing_item_message(session: Session, item: dict) -> tuple[str, list[str]]:
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
            if (
                existing_item["pizza_name"] == new_item["pizza_name"]
                and existing_item["pizza_type"] == new_item["pizza_type"]
                and existing_item.get("add_ingredients", []) == new_item.get("add_ingredients", [])
                and existing_item.get("remove_ingredients", []) == new_item.get("remove_ingredients", [])
            ):
                existing_item["quantity"] += new_item["quantity"]
                found = True
                break

        if not found:
            merged.append(new_item)

    return merged

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
            updated_items.append(
                {
                    "pizza_name": existing_item["pizza_name"],
                    "pizza_type": existing_item["pizza_type"],
                    "quantity": remaining_quantity,
                }
            )

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

def format_single_item(item: dict) -> str:
    quantity = item["quantity"]
    pizza_name = item["pizza_name"]
    pizza_type = item["pizza_type"]
    add_ingredients = item.get("add_ingredients", [])
    remove_ingredients = item.get("remove_ingredients", [])

    if quantity == 1:
        line = f"una {pizza_name.lower()}"
    else:
        line = f"{quantity} {pluralize_pizza_name(pizza_name, quantity)}"

    if pizza_type == "Senza glutine":
        line += " senza glutine"

    if add_ingredients:
        line += " con " + ", ".join(add_ingredients)

    if remove_ingredients:
        line += " senza " + ", ".join(remove_ingredients)

    return line

def format_single_item_for_customer(item: dict) -> str:
    quantity = item["quantity"]
    pizza_name = item["pizza_name"]
    pizza_type = item["pizza_type"]
    add_ingredients = item.get("add_ingredients", [])
    remove_ingredients = item.get("remove_ingredients", [])

    is_plain_margherita = (
        pizza_name == "Margherita"
        and not add_ingredients
        and not remove_ingredients
    )

    is_margherita_with_variants = (
        pizza_name == "Margherita"
        and (add_ingredients or remove_ingredients)
    )

    is_bianca_style = (
        pizza_name == "Margherita"
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
            if quantity == 1:
                if len(add_ingredients) == 1:
                    line = f"una {add_ingredients[0]}"
                elif len(add_ingredients) > 1:
                    line = f"una pizza con {', '.join(add_ingredients)}"
                else:
                    line = "una margherita"
            else:
                if len(add_ingredients) == 1:
                    line = f"{quantity} {add_ingredients[0]}"
                elif len(add_ingredients) > 1:
                    line = f"{quantity} pizze con {', '.join(add_ingredients)}"
                else:
                    line = f"{quantity} margherite"

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
        "Pizza personalizzata": "pizze personalizzate",
    }

    if name in irregulars:
        return irregulars[name]

    return name.lower()

def format_single_item(item: dict) -> str:
    quantity = item["quantity"]
    pizza_name = item["pizza_name"]
    pizza_type = item["pizza_type"]
    add_ingredients = item.get("add_ingredients", [])
    remove_ingredients = item.get("remove_ingredients", [])

    if quantity == 1:
        if pizza_name == "Pizza personalizzata":
            line = "una pizza personalizzata"
        else:
            line = f"una {pizza_name.lower()}"
    else:
        if pizza_name == "Pizza personalizzata":
            line = f"{quantity} pizze personalizzate"
        else:
            line = f"{quantity} {pluralize_pizza_name(pizza_name, quantity)}"

    if pizza_type == "Senza glutine":
        line += " senza glutine"

    if add_ingredients:
        line += " con " + ", ".join(add_ingredients)

    if remove_ingredients:
        line += " senza " + ", ".join(remove_ingredients)

    return line

def format_items(items: list[dict]) -> str:
    if not items:
        return ""

    return ", ".join(format_single_item(item) for item in items)

def split_valid_and_invalid_items(
    session: Session,
    items: list[dict],
) -> tuple[list[dict], list[dict], list[str]]:
    valid_items = []
    invalid_items = []
    missing_messages = []

    for item in items:
        statement = select(MenuItem).where(
            MenuItem.name == item["pizza_name"],
            MenuItem.pizza_type == item["pizza_type"],
        )
        menu_item = session.exec(statement).first()

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

_NUMBER_WORDS = {
    "una": 1, "un": 1, "uno": 1,
    "due": 2, "tre": 3, "quattro": 4, "cinque": 5, "sei": 6, "sette": 7,
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
) -> str:
    customer_name = merged_order.get("customer_name")
    pickup_time = merged_order.get("pickup_time")
    items_text = format_items_for_customer(merged_order["items"])

    # Pickup time closed-hours error
    if pickup_time_error:
        if missing_messages:
            return pickup_time_error + " " + " ".join(missing_messages)
        return pickup_time_error

    # Missing/invalid items
    if missing_messages:
        return " ".join(missing_messages)

    # Cancellation
    if intent == "cancel_order":
        return "Va bene, ordine annullato. Dimmi pure se vuoi ricominciare."

    # Order completed
    if state == "completed" and order_saved:
        if _is_mobile_phone(customer_phone):
            return "Perfetto! Ti arriverà una conferma su WhatsApp. A presto!"
        else:
            return "Perfetto! Vuoi un riepilogo dell’ordine o ti fidi?"

    # Full summary before confirmation
    if state == "awaiting_confirmation":
        name_part = f" {customer_name}" if customer_name else ""
        return (
            f"Perfetto{name_part}! Riepilogo: {items_text}. "
            f"Ritiro alle {pickup_time}. Confermo?"
        )

    # Collecting pickup time (ha già il nome, manca solo l’ora)
    if state == "collecting_pickup_time":
        return "Per che ora?"

    # Collecting name
    if state == "collecting_name":
        return "Che nome metto?"

    # Collecting items — risposte brevissime, niente domande
    if intent in ("add_items", "modify_items", "replace_items"):
        return random.choice(["Ok!", "Aggiunto!", "Perfetto!", "Certo!"])

    if intent == "remove_items":
        if not merged_order["items"]:
            return "Rimosso."
        return "Rimosso!"

    # Segnale "ho finito" senza items: chiedi nome e ora
    if not merged_order["items"]:
        return "Certo, dimmi pure!"

    # Natural ordering intent (e.g. "vorrei ordinare")
    return "Certo, dimmi pure!"

def has_invalid_items(session: Session, items: list[dict]) -> bool:
    for item in items:
        statement = select(MenuItem).where(
            MenuItem.name == item["pizza_name"],
            MenuItem.pizza_type == item["pizza_type"],
        )
        menu_item = session.exec(statement).first()

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
) -> list[str]:
    menu_names = get_menu_names(session)

    scored_matches = []
    requested_lower = requested_name.lower().strip()

    for name in menu_names:
        similarity = SequenceMatcher(None, requested_lower, name.lower()).ratio()

        # soglia alta = solo typo molto vicini
        if similarity >= 0.82:
            scored_matches.append((name, similarity))

    scored_matches.sort(key=lambda x: x[1], reverse=True)

    return [name for name, _ in scored_matches[:limit]]

def singularize_pizza_name(name: str) -> str:
    name = name.strip()
    if not name:
        return name

    lower = name.lower()

    irregulars = {
        "diavole": "diavola",
        "margherite": "margherita",
        "capricciose": "capricciosa",
    }

    if lower in irregulars:
        return irregulars[lower].capitalize()

    # fallback leggero (solo se sembra davvero una pizza)
    if lower.endswith("e") and len(lower) > 4:
        return (lower[:-1] + "a").capitalize()

    return lower.capitalize()

def infer_quantity_from_message(message: str) -> int:
    message_lower = message.lower()

    # rimuovi orari tipo 20:00
    message_clean = re.sub(r"\b\d{1,2}:\d{2}\b", "", message_lower)

    # numeri scritti
    number_words = {
        "una": 1,
        "un": 1,
        "uno": 1,
        "due": 2,
        "tre": 3,
        "quattro": 4,
        "cinque": 5,
    }

    for word, value in number_words.items():
        if re.search(rf"\b{word}\b", message_clean):
            return value

    # numeri numerici
    numbers = re.findall(r"\b\d+\b", message_clean)

    if numbers:
        return int(numbers[0])

    return 1

def extract_ingredient_changes(message: str) -> tuple[list[str], list[str]]:
    message_lower = message.lower()

    known_ingredients = [
        "mozzarella senza lattosio",
        "pomodoro",
        "mozzarella",
        "würstel",
        "wurstel",
        "patatine",
        "prosciutto",
        "funghi",
        "salame",
        "olive",
        "cipolla",
        "salsiccia",
        "bresaola",
        "rucola",
        "grana",
    ]

    add_ingredients = []
    remove_ingredients = []

    def normalize(ingredient: str) -> str:
        return "würstel" if ingredient == "wurstel" else ingredient

    # ordina per lunghezza decrescente per matchare prima le stringhe più specifiche
    known_ingredients = sorted(known_ingredients, key=len, reverse=True)

    add_match = re.search(r"\bcon\b\s+(.+?)(?=\bsenza\b|$)", message_lower)
    remove_match = re.search(r"\bsenza\b\s+(.+?)(?=\bcon\b|$)", message_lower)

    if add_match:
        add_text = add_match.group(1)
        for ingredient in known_ingredients:
            if ingredient in add_text:
                normalized = normalize(ingredient)
                if normalized not in add_ingredients:
                    add_ingredients.append(normalized)

    if remove_match:
        remove_text = remove_match.group(1)
        for ingredient in known_ingredients:
            if ingredient in remove_text:
                normalized = normalize(ingredient)
                if normalized not in remove_ingredients:
                    remove_ingredients.append(normalized)

    return add_ingredients, remove_ingredients

def find_menu_pizza_in_message(message: str, menu_items_for_llm: list[dict]) -> dict | None:
    message_lower = message.lower()

    # ordiniamo per nome più lungo, così "quattro formaggi" viene trovata
    # prima di parole più corte
    sorted_menu_items = sorted(
        menu_items_for_llm,
        key=lambda item: len(item["name"]),
        reverse=True,
    )

    for item in sorted_menu_items:
        menu_name_lower = item["name"].lower()
        if menu_name_lower in message_lower:
            _is_gf = "senza glutine" in message_lower or "gluten free" in message_lower
            return {
                "pizza_name": item["name"],
                "pizza_type": "Senza glutine" if _is_gf else item["pizza_type"],
                "dough_type": "senza_glutine" if _is_gf else item.get("dough_type", "classica"),
                "quantity": infer_quantity_from_message(message_lower),
            }

    return None

def segment_explicitly_requests_custom_pizza(segment: str) -> bool:
    segment_lower = segment.lower().strip()

    custom_markers = [
        "pizza con",
        "una pizza con",
        "bianca con",
        "rossa con",
        "una bianca",
        "una rossa",
    ]

    return any(marker in segment_lower for marker in custom_markers)

def build_custom_pizza_from_message(message: str, menu_items_for_llm: list[dict]) -> dict | None:
    message_lower = message.lower().strip()

    quantity = infer_quantity_from_message(message_lower)
    add_ingredients, remove_ingredients = extract_ingredient_changes(message_lower)

    if "bianca" in message_lower:
        normalized_remove = ["pomodoro"]
        for ingredient in remove_ingredients:
            if ingredient != "pomodoro" and ingredient not in normalized_remove:
                normalized_remove.append(ingredient)

        _is_gf = "senza glutine" in message_lower or "gluten free" in message_lower
        return {
            "pizza_name": "Margherita",
            "pizza_type": "Senza glutine" if _is_gf else "Normale",
            "dough_type": "senza_glutine" if _is_gf else "classica",
            "quantity": quantity,
            "add_ingredients": add_ingredients,
            "remove_ingredients": normalized_remove,
        }

    # custom solo se ci sono davvero ingredienti/modifiche
    if not add_ingredients and not remove_ingredients:
        return None

    _is_gf = "senza glutine" in message_lower or "gluten free" in message_lower
    return {
        "pizza_name": "Margherita",
        "pizza_type": "Senza glutine" if _is_gf else "Normale",
        "dough_type": "senza_glutine" if _is_gf else "classica",
        "quantity": quantity,
        "add_ingredients": add_ingredients,
        "remove_ingredients": remove_ingredients,
    }

def split_order_segments(message: str) -> list[str]:
    text = message.lower().strip()

    starters = [
        "ciao,",
        "ciao",
        "mi fai",
        "vorrei",
        "voglio",
        "fammi",
        "prendo",
        "aggiungi anche",
        "aggiungi",
    ]

    for starter in starters:
        if text.startswith(starter):
            text = text[len(starter):].strip()
            break

    # rimuovi nome e orario dalla parte che serve per splittare gli item
    text = re.sub(r"\ba nome\s+[a-zàèéìòù]+\b", "", text)
    text = re.sub(r"\bper le\s+\d{1,2}:\d{2}\b", "", text)
    text = re.sub(r"\balle\s+\d{1,2}:\d{2}\b", "", text)

    # prima prova split su pattern numerici
    matches = list(re.finditer(r"\b(?:\d+|una|un|uno|due|tre|quattro|cinque)\s+", text))
    if len(matches) > 1:
        parts = []
        for i, match in enumerate(matches):
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            part = text[start:end].strip(" ,.")
            if part:
                parts.append(part)
        return parts

    # fallback: split su " e " solo se sembra separare due pizze
    parts = re.split(
        r"\s+e\s+(?=(?:una|un|uno|due|tre|quattro|cinque|\d+)\s+(?:bianca|rossa|pizza|margherita|diavola|capricciosa|quattro formaggi|würstel|wurstel))",
        text,
    )

    cleaned_parts = [part.strip(" ,.") for part in parts if part.strip(" ,.")]
    return cleaned_parts if cleaned_parts else [text.strip(" ,.")]

def extract_items_from_segments(segments: list[str], menu_items_for_llm: list[dict]) -> list[dict]:
    collected_items = []

    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue

        # 1. parser normale se non sembra custom esplicita
        if not segment_explicitly_requests_custom_pizza(segment):
            segment_extracted = extract_order_from_text(segment, menu_items_for_llm)

            if segment_extracted.get("items"):
                normalized_items = []

                for item in segment_extracted["items"]:
                    # Se l'LLM inventa "Pizza personalizzata", la trasformiamo in:
                    # - pizza custom vera, se il segmento descrive ingredienti
                    # - pizza sconosciuta, se invece è un nome pizza non nel menu
                    if item.get("pizza_name") == "Pizza personalizzata":
                        custom_item = build_custom_pizza_from_message(segment, menu_items_for_llm)

                        if custom_item:
                            normalized_items.append(custom_item)
                            continue

                        fallback_unknown_items = fallback_extract_unknown_items(
                            segment,
                            menu_items_for_llm,
                        )
                        if fallback_unknown_items:
                            for fallback_item in fallback_unknown_items:
                                fallback_item.setdefault("add_ingredients", [])
                                fallback_item.setdefault("remove_ingredients", [])
                            normalized_items.extend(fallback_unknown_items)
                            continue

                    item.setdefault("add_ingredients", [])
                    item.setdefault("remove_ingredients", [])
                    normalized_items.append(item)

                if normalized_items:
                    collected_items.extend(normalized_items)
                    continue

        # 2. custom builder
        custom_item = build_custom_pizza_from_message(segment, menu_items_for_llm)
        if custom_item:
            collected_items.append(custom_item)
            continue

        # 3. unknown pizza item (es. "3 diavole")
        fallback_unknown_items = fallback_extract_unknown_items(
            segment,
            menu_items_for_llm,
        )

        if fallback_unknown_items:
            for item in fallback_unknown_items:
                item.setdefault("add_ingredients", [])
                item.setdefault("remove_ingredients", [])
            collected_items.extend(fallback_unknown_items)
            continue

        # 4. fallback menu semplice
        fallback_items = fallback_extract_menu_items_from_message(
            segment,
            menu_items_for_llm,
        )

        if fallback_items:
            for item in fallback_items:
                item.setdefault("add_ingredients", [])
                item.setdefault("remove_ingredients", [])
            collected_items.extend(fallback_items)

    return collected_items

def fallback_extract_unknown_items(message: str, menu_items_for_llm: list[dict]) -> list[dict]:
    message_lower = message.lower().strip()

    patterns = [
        r"(?:vorrei|prendo|aggiungi|fai|allora fai|fammi)\s+(?:anche\s+)?(?:una|un|uno|due|tre|quattro|cinque|\d+)\s+([a-zàèéìòù]+)",
        r"(?:una|un|uno|due|tre|quattro|cinque|\d+)\s+([a-zàèéìòù]+)",
    ]

    excluded_words = {
        "pizza",
        "pizze",
        "nome",
        "ritiro",
        "orario",
        "bianca",
        "rossa",
    }

    for pattern in patterns:
        match = re.search(pattern, message_lower)
        if match:
            candidate = singularize_pizza_name(match.group(1))

            if candidate.lower() not in excluded_words:
                quantity = infer_quantity_from_message(message_lower)

                _is_gf = "senza glutine" in message_lower or "gluten free" in message_lower
                return [
                    {
                        "pizza_name": candidate,
                        "pizza_type": "Senza glutine" if _is_gf else "Normale",
                        "dough_type": "senza_glutine" if _is_gf else "classica",
                        "quantity": quantity,
                        "add_ingredients": [],
                        "remove_ingredients": [],
                    }
                ]

    return []

def fallback_extract_menu_items_from_message(message: str, menu_items_for_llm: list[dict]) -> list[dict]:
    message_lower = message.lower()
    quantity = infer_quantity_from_message(message_lower)
    found_items = []

    sorted_menu_items = sorted(
        menu_items_for_llm,
        key=lambda item: len(item["name"]),
        reverse=True,
    )

    for menu_item in sorted_menu_items:
        if menu_item["name"].lower() in message_lower:
            _is_gf = "senza glutine" in message_lower or "gluten free" in message_lower
            found_items.append(
                {
                    "pizza_name": menu_item["name"],
                    "pizza_type": "Senza glutine" if _is_gf else menu_item["pizza_type"],
                    "dough_type": "senza_glutine" if _is_gf else menu_item.get("dough_type", "classica"),
                    "quantity": quantity,
                    "add_ingredients": [],
                    "remove_ingredients": [],
                }
            )
            break

    return found_items

def should_force_segment_parsing(message: str) -> bool:
    message_lower = message.lower()

    numeric_chunks = re.findall(r"\d+\s+", message_lower)

    return (
        len(numeric_chunks) >= 2
        or (
            " e " in message_lower
            and any(char.isdigit() for char in message_lower)
        )
    )

class ChatStartRequest(BaseModel):
    test_phone: str | None = None


def _italian_title(full_name: str) -> str:
    """Restituisce 'il signor' o 'la signora' in base al nome (euristica sul finale)."""
    first = full_name.strip().split()[0] if full_name.strip() else full_name
    if first.lower().endswith("a"):
        return f"la signora {full_name}"
    return f"il signor {full_name}"


@router.post("/start", response_model=ChatStartResponse)
def start_chat(body: ChatStartRequest, session: SessionDep):
    print(f"[ChatStart] Body ricevuto: {body}")
    new_session_id = str(uuid.uuid4())
    phone = body.test_phone or None

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
    print(f"[Customer] customer_phone in sessione: {conversation.customer_phone!r}")

    # Riconoscimento cliente dal numero di telefono
    greeting = get_agent_greeting()
    if conversation.customer_phone:
        print(f"[Customer] Avvio lookup per {conversation.customer_phone!r}")
        customer = lookup_customer(conversation.customer_phone)
        if customer:
            found_name = customer.get("full_name", "").strip()
            if found_name:
                # Personalizza il saluto e salva il nome in attesa di conferma
                base = re.split(r"[.!?]", greeting)[0].strip()
                greeting = f"{base}! È lei {_italian_title(found_name)}?"
                conversation.pending_customer_name = found_name
                session.add(conversation)
                session.commit()
    else:
        print("[Customer] phone non disponibile, lookup saltato")

    print(f"[Agent] Saluto: {greeting!r}")

    return ChatStartResponse(
        session_id=conversation.session_id,
        state=conversation.state,
        completed=conversation.completed,
        response_message=greeting,
    )

@router.post("/", response_model=ChatResponse)
def chat(request: ChatRequest, session: SessionDep):
    # Carica il menu da Base44 (con cache 10 min); fallback al DB locale se vuoto
    menu_items_for_llm = load_menu_from_base44()
    first_names = [item["name"] for item in menu_items_for_llm[:3]]
    print(f"[Chat] menu_items_for_llm: {len(menu_items_for_llm)} voci. Prime 3: {first_names}")

    if not menu_items_for_llm:
        print("[Chat] Fallback al DB locale")
        db_menu_items = session.exec(select(MenuItem)).all()
        menu_items_for_llm = [
            {
                "name": item.name,
                "category": item.category,
                "dough_type": _PIZZA_TYPE_TO_DOUGH.get(item.pizza_type, "classica"),
                "pizza_type": item.pizza_type,
                "price": item.price,
                "available": item.available,
                "ingredients": [],
            }
            for item in db_menu_items
        ]

    dough_items = load_doughs()

    message_lower = request.message.lower()

    extracted = extract_order_from_text(request.message, menu_items_for_llm, dough_items)

    normalized_items = []

    for item in extracted.get("items", []):
        if item.get("pizza_name") == "Pizza personalizzata":
            custom = build_custom_pizza_from_message(
                request.message,
                menu_items_for_llm,
            )

            if custom:
                normalized_items.append(custom)
                continue

            fallback_unknown_items = fallback_extract_unknown_items(
                request.message,
                menu_items_for_llm,
            )

            if fallback_unknown_items:
                for fallback_item in fallback_unknown_items:
                    fallback_item.setdefault("add_ingredients", [])
                    fallback_item.setdefault("remove_ingredients", [])
                normalized_items.extend(fallback_unknown_items)
                continue

        item.setdefault("add_ingredients", [])
        item.setdefault("remove_ingredients", [])
        normalized_items.append(item)

    if normalized_items:
        extracted["items"] = normalized_items

    if not extracted.get("items"):
        forced_custom = build_custom_pizza_from_message(
            request.message,
            menu_items_for_llm,
        )

        if forced_custom:
            extracted["items"] = [forced_custom]
            if extracted.get("intent") == "unknown":
                extracted["intent"] = "add_items"

    if not extracted.get("items"):
        fallback_unknown_items = fallback_extract_unknown_items(
            request.message,
            menu_items_for_llm,
        )

        if fallback_unknown_items:
            for item in fallback_unknown_items:
                item.setdefault("add_ingredients", [])
                item.setdefault("remove_ingredients", [])
            extracted["items"] = fallback_unknown_items
            if extracted.get("intent") == "unknown":
                extracted["intent"] = "add_items"

    segments = split_order_segments(request.message)

    if len(segments) > 1:
        segmented_items = extract_items_from_segments(segments, menu_items_for_llm)
        if segmented_items:
            extracted["items"] = segmented_items
            extracted["intent"] = "add_items"

    # fallback menu items dal testo, se ancora non ci sono item
    if not extracted.get("items"):
        fallback_menu_items = fallback_extract_menu_items_from_message(
            request.message,
            menu_items_for_llm,
        )
        if fallback_menu_items:
            extracted["items"] = fallback_menu_items
            if extracted.get("intent") == "unknown":
                extracted["intent"] = "add_items"

    for item in extracted.get("items", []):
        item.setdefault("add_ingredients", [])
        item.setdefault("remove_ingredients", [])

    if "senza glutine" in message_lower or "gluten free" in message_lower:
        for item in extracted.get("items", []):
            item["pizza_type"] = "Senza glutine"
            item["dough_type"] = "senza_glutine"

    if extracted.get("items") and extracted.get("intent") == "unknown":
        extracted["intent"] = "add_items"

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

    # Gestione conferma/rifiuto identità cliente riconosciuto
    _pending = conversation.pending_customer_name
    if _pending and not conversation.customer_name:
        _positive = any(m in message_lower for m in ["sì", "si", "esatto", "giusto", "corretto", "sono io", "sì sono"])
        _negative = any(m in message_lower for m in ["no", "sbagliato", "non sono"])
        if _positive:
            conversation.customer_name = _pending
            conversation.pending_customer_name = None
            print(f"[Customer] Identità confermata: {_pending}")
        elif _negative:
            conversation.pending_customer_name = None
            print("[Customer] Identità rifiutata, trattato come nuovo cliente")

    existing_items = json.loads(conversation.items_json)
    existing_suggestions = json.loads(conversation.suggested_items_json)

    chosen_suggestion = extract_choice_from_suggestions(
        request.message,
        existing_suggestions,
    )
    selected_from_suggestions = chosen_suggestion is not None

    if chosen_suggestion:
        chosen_quantity = infer_quantity_from_message(message_lower)
        _is_gf = "senza glutine" in message_lower or "gluten free" in message_lower
        extracted["items"] = [
            {
                "pizza_name": chosen_suggestion,
                "pizza_type": "Senza glutine" if _is_gf else "Normale",
                "dough_type": "senza_glutine" if _is_gf else "classica",
                "quantity": chosen_quantity,
                "add_ingredients": [],
                "remove_ingredients": [],
            }
        ]
        extracted["intent"] = "add_items"

    merged_order = {
        "customer_name": conversation.customer_name,
        "pickup_time": conversation.pickup_time,
        "items": existing_items,
    }

    intent = extracted.get("intent", "unknown")

    confirmation_markers = [
        "sì",
        "si",
        "va bene",
        "ok",
        "perfetto",
        "confermo",
    ]

    is_simple_confirmation = (
        any(marker in message_lower for marker in confirmation_markers)
        and not extracted.get("items")
        and not extracted.get("customer_name")
        and not extracted.get("pickup_time")
    )

    correction_markers = [
        "no aspetta",
        "anzi",
        "correggo",
        "ho sbagliato",
        "volevo dire",
        "una sola",
        "solo una",
        "non due ma una",
        "allora fai",
    ]

    add_markers = [
        "aggiungi",
        "aggiungi anche",
        "fammi anche",
        "metti anche",
    ]

    remove_markers = [
        "togli",
        "leva",
        "rimuovi",
    ]

    replace_markers = [
        "invece",
        "al posto di",
        "sostituisci",
    ]

    cancel_markers = [
        "annulla tutto",
        "cancella tutto",
        "annulla ordine",
    ]

    existing_items_invalid = has_invalid_items(session, existing_items)

    has_existing_order = len(existing_items) > 0
    has_new_items = len(extracted.get("items", [])) > 0

    has_add_marker = any(marker in message_lower for marker in add_markers)
    has_correction_marker = any(marker in message_lower for marker in correction_markers)
    has_remove_marker = any(marker in message_lower for marker in remove_markers)
    has_replace_marker = any(marker in message_lower for marker in replace_markers)
    has_cancel_marker = any(marker in message_lower for marker in cancel_markers)

    if has_add_marker:
        intent = "add_items"

    if has_correction_marker:
        intent = "modify_items"

    if has_remove_marker:
        intent = "remove_items"

    if has_replace_marker:
        intent = "replace_items"

    if has_cancel_marker:
        intent = "cancel_order"

    # Se esiste già un ordine e il cliente manda nuove pizze senza marker di replace/remove/cancel,
    # trattiamo il messaggio come aggiunta, non come sostituzione.
    if (
        has_existing_order
        and has_new_items
        and not selected_from_suggestions
        and not has_remove_marker
        and not has_replace_marker
        and not has_cancel_marker
        and not has_correction_marker
    ):
        intent = "add_items"

    if extracted.get("customer_name"):
        merged_order["customer_name"] = extracted["customer_name"]

    pickup_time_error = None
    if extracted.get("pickup_time"):
        pt = extracted["pickup_time"]
        is_valid, suggestion = validate_pickup_time(pt)
        if not is_valid:
            if suggestion:
                pickup_time_error = f"Mi dispiace, alle {pt} siamo chiusi. Il prossimo orario disponibile è le {suggestion}."
            else:
                pickup_time_error = f"Mi dispiace, alle {pt} siamo chiusi."
        else:
            merged_order["pickup_time"] = pt

    if selected_from_suggestions:
        valid_existing_items = keep_only_valid_existing_items(session, existing_items)
        merged_order["items"] = merge_items(
            valid_existing_items,
            extracted.get("items", []),
        )
    elif (
        existing_items_invalid
        and has_new_items
        and not has_add_marker
        and not has_remove_marker
        and not has_replace_marker
        and not has_cancel_marker
        and not has_correction_marker
    ):
        # L'utente sta sostituendo un item non valido: conserva i valid esistenti e aggiungi i nuovi
        valid_existing_items = keep_only_valid_existing_items(session, existing_items)
        merged_order["items"] = merge_items(valid_existing_items, extracted.get("items", []))
        intent = "add_items"
    else:
        merged_order["items"] = apply_intent_to_items(
            existing_items=existing_items,
            new_items=extracted.get("items", []),
            intent=intent,
        )

    if intent == "cancel_order":
        merged_order["customer_name"] = None
        merged_order["pickup_time"] = None
        conversation.intended_quantity = None

    # Aggiorna il numero di pizze dichiarato se il cliente lo ha specificato
    # (solo quando il messaggio non contiene pizze specifiche, per evitare
    # di sovrascrivere conteggi già correttamente estratti dagli items)
    if not extracted.get("items"):
        declared = extract_intended_quantity(request.message)
        if declared:
            conversation.intended_quantity = declared
            print(f"[Chat] intended_quantity aggiornato a {declared}")

    # Segnale di fine ordine (es. 'è tutto', 'ho finito')
    customer_done = is_done_signal(request.message)

    conversation.customer_name = merged_order["customer_name"]
    conversation.pickup_time = merged_order["pickup_time"]
    conversation.items_json = json.dumps(merged_order["items"], ensure_ascii=False)

    valid_items = []
    invalid_items = []
    missing_messages = []
    new_suggestions = []

    db_menu_count = session.exec(select(MenuItem)).all()
    print(f"[Chat] DB SQLite MenuItem: {len(db_menu_count)} righe totali")
    print(f"[Chat] Items da validare: {[(i['pizza_name'], i['pizza_type']) for i in merged_order['items']]}")

    for item in merged_order["items"]:
        statement = select(MenuItem).where(
            MenuItem.name == item["pizza_name"],
            MenuItem.pizza_type == item["pizza_type"],
        )
        menu_item = session.exec(statement).first()
        print(f"[Chat] Validazione '{item['pizza_name']}' ({item['pizza_type']}): {'OK' if menu_item and menu_item.available else 'NON TROVATO nel DB'}")

        is_custom = bool(item.get("add_ingredients") or item.get("remove_ingredients"))

        if not menu_item or not menu_item.available:
            if is_custom:
                # VALIDAZIONE BASE: cerca la pizza base senza varianti
                base_item = session.exec(
                    select(MenuItem).where(
                        MenuItem.name == item["pizza_name"],
                        MenuItem.pizza_type == item["pizza_type"],
                    )
                ).first()
                if base_item and base_item.available:
                    valid_items.append(item)
                    continue

            # Fuzzy matching sul nome
            fuzzy_item, similarity = fuzzy_find_pizza(
                item["pizza_name"], item["pizza_type"], session
            )
            print(f"[Fuzzy] '{item['pizza_name']}' → '{fuzzy_item.name if fuzzy_item else None}' sim={similarity:.2f}")

            if fuzzy_item and similarity >= 0.80:
                # Auto-accetta: rinomina e riconvalida
                print(f"[Fuzzy] Auto-accettato '{item['pizza_name']}' → '{fuzzy_item.name}'")
                item["pizza_name"] = fuzzy_item.name
                item["pizza_type"] = fuzzy_item.pizza_type
                dough_code = item.get("dough_type", "classica")
                if is_dough_available(dough_code):
                    valid_items.append(item)
                else:
                    invalid_items.append(item)
                    missing_messages.append(f"L'impasto '{dough_code}' non è disponibile.")
                continue

            if fuzzy_item and similarity >= 0.60:
                # Chiedi conferma
                missing_messages.append(f"Vuoi dire la {fuzzy_item.name}?")
                new_suggestions.append(fuzzy_item.name)
                invalid_items.append(item)
                continue

            # Sotto 60%: pizza non esiste
            invalid_items.append(item)
            message, suggestions = build_missing_item_message(session, item)
            missing_messages.append(message)
            new_suggestions.extend(suggestions)
        else:
            # Controlla disponibilità impasto
            dough_code = item.get("dough_type", "classica")
            if not is_dough_available(dough_code):
                invalid_items.append(item)
                missing_messages.append(f"L'impasto '{dough_code}' non è al momento disponibile.")
            else:
                valid_items.append(item)

    if is_simple_confirmation and not existing_suggestions:
        if (
            merged_order.get("customer_name") is not None
            and merged_order.get("pickup_time") is not None
            and len(valid_items) > 0
            and len(invalid_items) == 0
        ):
            intent = "confirm_order"

    if is_simple_confirmation and intent == "unknown":
        intent = "confirmation"

    conversation.suggested_items_json = json.dumps(new_suggestions, ensure_ascii=False)

    missing_items = [
        f'{item["pizza_name"]} ({item["pizza_type"]})'
        for item in invalid_items
    ]

    new_valid_items = []
    existing_keys = {
        (
            item["pizza_name"],
            item["pizza_type"],
            tuple(item.get("add_ingredients", [])),
            tuple(item.get("remove_ingredients", [])),
        )
        for item in existing_items
    }

    for item in valid_items:
        key = (
            item["pizza_name"],
            item["pizza_type"],
            tuple(item.get("add_ingredients", [])),
            tuple(item.get("remove_ingredients", [])),
        )
        if key not in existing_keys or intent in {
            "add_items",
            "modify_items",
            "replace_items",
            "remove_items",
        }:
            new_valid_items.append(item)

    valid = (
        len(invalid_items) == 0
        and len(valid_items) > 0
        and merged_order.get("customer_name") is not None
        and merged_order.get("pickup_time") is not None
    )

    order_id = None
    order_saved = False

    state = determine_state(
        merged_order=merged_order,
        missing_messages=missing_messages,
        completed=conversation.completed,
        intended_quantity=conversation.intended_quantity,
        done_signal=customer_done,
    )
    conversation.state = state

    if valid and not conversation.completed and intent == "confirm_order":
        order = Order(
            customer_name=merged_order["customer_name"],
            pickup_time=merged_order["pickup_time"],
            status="new",
        )
        session.add(order)
        session.commit()
        session.refresh(order)

        for item in merged_order["items"]:
            order_item = OrderItem(
                order_id=order.id,
                pizza_name=item["pizza_name"],
                pizza_type=item["pizza_type"],
                quantity=item["quantity"],
                add_ingredients_json=json.dumps(
                    item.get("add_ingredients", []),
                    ensure_ascii=False,
                ),
                remove_ingredients_json=json.dumps(
                    item.get("remove_ingredients", []),
                    ensure_ascii=False,
                ),
            )
            session.add(order_item)

        session.commit()

        conversation.completed = True
        conversation.state = "completed"
        conversation.suggested_items_json = "[]"
        session.add(conversation)
        session.commit()

        order_id = order.id
        order_saved = True

        # Arricchisci ogni item con i prezzi e calcola total_amount
        enriched_items = []
        for item in merged_order["items"]:
            menu_item = session.exec(
                select(MenuItem).where(
                    MenuItem.name == item["pizza_name"],
                    MenuItem.pizza_type == item["pizza_type"],
                )
            ).first()
            base_price = round(menu_item.price, 2) if menu_item else 0.0
            is_sg_pizza = "(SG)" in item.get("pizza_name", "")
            dough_code = item.get("dough_type", "classica")
            dough_surcharge = 0.0 if is_sg_pizza else get_dough_surcharge(dough_code)
            add_count = len(item.get("add_ingredients", []))
            extras_price = round(dough_surcharge + add_count * INGREDIENT_EXTRA_PRICE, 2)
            total_price = round((base_price + extras_price) * item["quantity"], 2)
            print(f"[Pricing] Pizza: {item['pizza_name']}, base_price: {base_price}, dough: {dough_code}, dough_surcharge: {dough_surcharge}, aggiunte: {add_count}, extras_price: {extras_price}, total_price: {total_price}")
            enriched_items.append({
                **item,
                "base_price": base_price,
                "extras_price": extras_price,
                "total_price": total_price,
            })

        # Confidenza AI: 0.75 se nel turno precedente erano presenti suggerimenti
        # (item invalidi già corretti), 0.9 se l'ordine è arrivato diretto
        ai_confidence = 0.75 if existing_suggestions else 0.9

        save_order_to_base44(
            customer_name=merged_order["customer_name"],
            customer_phone=conversation.customer_phone,
            pickup_time=merged_order["pickup_time"],
            order_number=order.id,
            ai_confidence=ai_confidence,
            items=enriched_items,
        )

        # Crea o aggiorna il cliente su Base44
        pizza_names = list(dict.fromkeys(
            item["pizza_name"] for item in merged_order["items"]
        ))
        order_total = round(sum(i.get("total_price", 0.0) for i in enriched_items), 2)
        upsert_customer(
            full_name=merged_order["customer_name"],
            phone=conversation.customer_phone,
            pizzas=pizza_names,
            total_amount=order_total,
        )

    response_message = build_assistant_response(
        merged_order=merged_order,
        state=conversation.state,
        missing_messages=missing_messages,
        order_saved=order_saved,
        intent=intent,
        new_valid_items=new_valid_items,
        customer_phone=conversation.customer_phone,
        pickup_time_error=pickup_time_error,
    )

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