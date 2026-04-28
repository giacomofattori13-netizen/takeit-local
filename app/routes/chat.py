import json
import random
import time
import unicodedata
import uuid
import re
from concurrent.futures import ThreadPoolExecutor

from typing import Annotated

from difflib import SequenceMatcher

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
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
    build_closed_message,
    extract_order_from_text,
    load_menu_from_base44,
    load_doughs,
    save_order_to_base44,
    send_whatsapp_confirmation,
    get_dough_surcharge,
    is_dough_available,
    is_agent_active,
    INGREDIENT_EXTRA_PRICE,
    SIZE_MINI_DISCOUNT,
    SIZE_DOPPIO_SURCHARGE,
    _PIZZA_TYPE_TO_DOUGH,
    get_agent_greeting,
    validate_pickup_time,
    resolve_pickup_time,
    lookup_customer,
    upsert_customer,
)

router = APIRouter(prefix="/chat", tags=["chat"])

SessionDep = Annotated[Session, Depends(get_session)]
_CUSTOMER_LOOKUP_EXECUTOR = ThreadPoolExecutor(max_workers=4)


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
    size = item.get("size", "normale")

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
        "Personalizzata": "pizze personalizzate",
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
    size = item.get("size", "normale")

    if quantity == 1:
        if pizza_name in ("Personalizzata", "Pizza personalizzata"):
            line = "una pizza personalizzata"
        else:
            line = f"una {pizza_name.lower()}"
    else:
        if pizza_name in ("Personalizzata", "Pizza personalizzata"):
            line = f"{quantity} pizze personalizzate"
        else:
            line = f"{quantity} {pluralize_pizza_name(pizza_name, quantity)}"

    if size == "mini":
        line += " (mini)"
    elif size == "doppio":
        line += " (doppio impasto)"

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
    removed_names: list[str] | None = None,
    not_found_names: list[str] | None = None,
) -> str:
    customer_name = merged_order.get("customer_name")
    pickup_time = merged_order.get("pickup_time")
    items_text = format_items_for_customer(merged_order["items"])

    # Pickup time closed-hours error — ritorna solo il messaggio di errore,
    # senza accodare missing_messages (che già contiene pickup_time_error).
    if pickup_time_error:
        return pickup_time_error

    # Missing/invalid items
    if missing_messages:
        return " ".join(missing_messages)

    # Cancellation / cart reset
    if intent == "cancel_order":
        return "Va bene, ordine annullato. Dimmi pure se vuoi ricominciare."

    if intent == "clear_cart":
        return "Ho cancellato tutto, dimmi pure cosa vuole!"

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
        name_part = f" {customer_name}" if customer_name else ""
        if _is_mobile_phone(customer_phone):
            return f"Perfetto{name_part}! Ti arriverà una conferma su WhatsApp."
        else:
            return f"Perfetto{name_part}, a presto!"

    # Awaiting confirmation — nessun riepilogo, solo chiedi conferma
    if state == "awaiting_confirmation":
        name_part = f" {customer_name}" if customer_name else ""
        return f"Perfetto{name_part}, confermo alle {pickup_time}?"

    # Collecting pickup time (ha già il nome, manca solo l’ora)
    if state == "collecting_pickup_time":
        return "Per che ora?"

    # Collecting name
    if state == "collecting_name":
        return "Che nome metto?"

    # Collecting items — risposte brevissime, niente domande
    if intent in ("add_items", "modify_items", "replace_items"):
        if not new_valid_items:
            # Il cliente ha dichiarato una quantità senza specificare le pizze
            return "Certo, dimmi pure!"
        return random.choice(["Ok!", "Aggiunto!", "Perfetto!", "Certo!"])

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

    order = Order(
        conversation_session_id=conversation.session_id,
        customer_name=merged_order["customer_name"],
        pickup_time=merged_order["pickup_time"],
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
        session.add(OrderItem(
            order_id=order.id,
            pizza_name=item["pizza_name"],
            pizza_type=item["pizza_type"],
            quantity=item["quantity"],
            add_ingredients_json=json.dumps(item.get("add_ingredients", []), ensure_ascii=False),
            remove_ingredients_json=json.dumps(item.get("remove_ingredients", []), ensure_ascii=False),
            dough_type=item.get("dough_type", "classica"),
            size=item.get("size", "normale"),
        ))
    session.commit()

    _mark_conversation_completed(session, conversation)
    return order, True


@router.post("/start", response_model=ChatStartResponse)
def start_chat(body: ChatStartRequest, session: SessionDep):
    print(f"[ChatStart] Body ricevuto: {body}")
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
        print(f"[Customer] Avvio lookup in parallelo per {phone!r}")
        lookup_future = _CUSTOMER_LOOKUP_EXECUTOR.submit(lookup_customer, phone)

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
    if lookup_future is not None:
        customer = lookup_future.result()
        if customer:
            found_name = customer.get("full_name", "").strip()
            if found_name:
                # Saluta direttamente per nome — il numero è conferma sufficiente
                first_name = found_name.split()[0]
                greeting = f"Ciao {first_name}! Come posso aiutarti?"
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

    print(f"[Agent] Saluto: {greeting!r}")

    return ChatStartResponse(
        session_id=conversation.session_id,
        state=conversation.state,
        completed=conversation.completed,
        response_message=greeting,
    )

@router.post("/", response_model=ChatResponse)
def chat(request: ChatRequest, session: SessionDep):
    if not is_agent_active():
        print("[Chat] agent_active=False → rifiuto messaggio")
        return ChatResponse(
            session_id=request.session_id,
            user_message=request.message,
            extracted_order={},
            merged_order={},
            valid=False,
            missing_items=[],
            response_message=build_closed_message(),
            order_id=None,
            state="closed",
        )

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

    message_lower = unicodedata.normalize("NFC", request.message.lower())

    # Carica la sessione prima della chiamata LLM per poter passare lo stato corrente al prompt
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

    # ── FAST PATH: awaiting_confirmation ──────────────────────────────────────
    # Quando lo stato è awaiting_confirmation NON chiamiamo l'LLM.
    # La risposta del cliente è gestita con puro Python: sì/no/chiedi di nuovo.
    if conversation.state == "awaiting_confirmation":
        _confirm_words = {"si", "sì", "ok", "va bene", "confermo", "certo", "esatto", "perfetto", "yes"}
        _cancel_words  = {"no", "annulla", "cancella", "stop"}
        _is_confirm = any(w in message_lower for w in _confirm_words)
        _is_cancel  = any(w in message_lower for w in _cancel_words)

        existing_items = json.loads(conversation.items_json)
        merged_order = {
            "customer_name": conversation.customer_name,
            "pickup_time":   conversation.pickup_time,
            "items":         existing_items,
        }

        order = None
        if _is_confirm and not conversation.completed:
            order, order_created = _persist_order_once(session, conversation, merged_order)

            # Pricing e salvataggio Base44
            _margherita_item = session.exec(
                select(MenuItem).where(MenuItem.name == "Margherita")
            ).first()
            _base_personalizzata = round(_margherita_item.price, 2) if _margherita_item else 0.0

            enriched_items = []
            for _item in merged_order["items"]:
                if _item["pizza_name"] == "Personalizzata":
                    _base = _base_personalizzata
                else:
                    _menu_item = session.exec(
                        select(MenuItem).where(
                            MenuItem.name == _item["pizza_name"],
                            MenuItem.pizza_type == _item["pizza_type"],
                        )
                    ).first()
                    if not _menu_item:
                        _menu_item = session.exec(
                            select(MenuItem).where(MenuItem.name == _item["pizza_name"])
                        ).first()
                    _base = round(_menu_item.price, 2) if _menu_item else 0.0
                _is_sg = "(SG)" in _item.get("pizza_name", "")
                _dough_code = _item.get("dough_type", "classica")
                _surcharge = 0.0 if _is_sg else get_dough_surcharge(_dough_code)
                _size = _item.get("size", "normale")
                if _size == "mini":
                    _base = round(max(0.0, _base - SIZE_MINI_DISCOUNT), 2)
                elif _size == "doppio":
                    _surcharge = round(_surcharge + SIZE_DOPPIO_SURCHARGE, 2)
                _extras = round(_surcharge + len(_item.get("add_ingredients", [])) * INGREDIENT_EXTRA_PRICE, 2)
                _total = round((_base + _extras) * _item["quantity"], 2)
                enriched_items.append({**_item, "base_price": _base, "extras_price": _extras, "total_price": _total})

            if order_created:
                save_order_to_base44(
                    customer_name=merged_order["customer_name"],
                    customer_phone=conversation.customer_phone,
                    pickup_time=merged_order["pickup_time"],
                    order_number=order.id,
                    ai_confidence=0.95,
                    items=enriched_items,
                )
                print(f"[SMS] Tentativo invio (awaiting_confirmation): phone={conversation.customer_phone!r} name={merged_order['customer_name']!r}")
                send_whatsapp_confirmation(
                    customer_name=merged_order["customer_name"],
                    customer_phone=conversation.customer_phone,
                    pickup_time=merged_order["pickup_time"],
                    items=enriched_items,
                    total_amount=round(sum(i.get("total_price", 0.0) for i in enriched_items), 2),
                )
                pizza_names = list(dict.fromkeys(i["pizza_name"] for i in merged_order["items"]))
                order_total = round(sum(i.get("total_price", 0.0) for i in enriched_items), 2)
                upsert_customer(
                    full_name=merged_order["customer_name"],
                    phone=conversation.customer_phone,
                    pizzas=pizza_names,
                    total_amount=order_total,
                )

            name_part = f" {merged_order['customer_name']}" if merged_order.get("customer_name") else ""
            _resp = (
                f"Perfetto{name_part}! Ti arriverà una conferma su WhatsApp."
                if _is_mobile_phone(conversation.customer_phone)
                else f"Perfetto{name_part}, a presto!"
            )
            _extracted_stub = {"intent": "confirm_order", "items": [], "customer_name": None, "pickup_time": None}

        elif _is_cancel:
            conversation.items_json = "[]"
            conversation.customer_name = None
            conversation.pickup_time = None
            conversation.state = "collecting_items"
            conversation.completed = False
            session.add(conversation)
            session.commit()
            merged_order["items"] = []
            _resp = "Va bene, ordine annullato. Dimmi pure se vuoi ricominciare."
            _extracted_stub = {"intent": "cancel_order", "items": [], "customer_name": None, "pickup_time": None}

        else:
            # Messaggio ambiguo — richiedi conferma di nuovo
            name_part = f" {merged_order['customer_name']}" if merged_order.get("customer_name") else ""
            _resp = f"Perfetto{name_part}, confermo per le {merged_order.get('pickup_time')}?"
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

    # ── CONFIRMING_USUAL: skip LLM dove possibile ────────────────────────────

    fav_pizzas_session = json.loads(conversation.favorite_pizzas_json or "[]")

    # Gestore: il cliente risponde alla domanda "Ti faccio le solite?"
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
                    select(MenuItem).where(
                        MenuItem.name == pizza_name,
                        MenuItem.available == True,
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
            _resp_usual = "Dimmi pure cosa vuoi!"
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
            _resp_usual = f"Ti faccio le solite? {pizza_list_str}"
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
        session.add(conversation)
        session.commit()
        _usual_limit = _declared_qty if _declared_qty else 2
        fav_list = fav_pizzas_session[:_usual_limit]
        pizza_list_str = _format_pizza_list(fav_list)
        _resp_usual = f"Ti faccio le solite? {pizza_list_str}"
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

    # 1. Estrai item SOLO dal messaggio corrente.
    # Il contesto di sessione (items, nome) va nel messaggio utente — non nel system prompt —
    # così il system prompt rimane statico e il prefix caching OpenAI rimane attivo.
    existing_items_ctx = json.loads(conversation.items_json)
    extracted = extract_order_from_text(
        request.message,
        menu_items_for_llm,
        dough_items,
        state=conversation.state,
        existing_items=existing_items_ctx,
        customer_name=conversation.customer_name,
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
    new_items = extracted.get("items", [])
    for item in new_items:
        item.setdefault("add_ingredients", [])
        item.setdefault("remove_ingredients", [])
    intent = extracted.get("intent", "unknown")

    # 2. Aggiorna orario di ritiro (con validazione orari)
    pickup_time_error = None
    if extracted.get("pickup_time"):
        pt = resolve_pickup_time(extracted["pickup_time"])
        is_valid, suggestion, closing_time = validate_pickup_time(pt)
        if not is_valid:
            if closing_time:
                pickup_time_error = (
                    f"Mi dispiace, chiudiamo alle {closing_time}. L'ultimo orario disponibile è le {suggestion}."
                    if suggestion else f"Mi dispiace, alle {pt} siamo già chiusi."
                )
            else:
                pickup_time_error = (
                    f"Mi dispiace, alle {pt} siamo chiusi. Il prossimo orario disponibile è le {suggestion}."
                    if suggestion else f"Mi dispiace, alle {pt} siamo chiusi."
                )
        else:
            conversation.pickup_time = pt

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
        for ni in new_items:
            ni_add = ni.get("add_ingredients") or []
            ni_rem = ni.get("remove_ingredients") or []
            for ei in merged_items:
                if (ei["pizza_name"] == ni["pizza_name"]
                        and ei["pizza_type"] == ni["pizza_type"]
                        and ei.get("size", "normale") == ni.get("size", "normale")):
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

    merged_order = {
        "customer_name": conversation.customer_name,
        "pickup_time": conversation.pickup_time,
        "items": merged_items,
    }
    conversation.items_json = json.dumps(merged_items, ensure_ascii=False)

    # 5. Validazione item contro DB (nessun fuzzy matching)
    valid_items = []
    invalid_items = []
    missing_messages = []
    if pickup_time_error:
        missing_messages.append(pickup_time_error)

    for item in merged_items:
        # Pizza libera: sempre valida, usa prezzo Margherita come base
        if item["pizza_name"] == "Personalizzata":
            valid_items.append(item)
            continue

        menu_item = session.exec(
            select(MenuItem).where(
                MenuItem.name == item["pizza_name"],
                MenuItem.pizza_type == item["pizza_type"],
            )
        ).first()
        # Fallback: il dough_type è una preferenza del cliente, non una voce separata
        # nel DB (che ha solo pizza_type="Normale"). Se non trovato con pizza_type
        # specifico, cerca per solo nome — se esiste la pizza è valida.
        if not menu_item:
            menu_item = session.exec(
                select(MenuItem).where(MenuItem.name == item["pizza_name"])
            ).first()
            if menu_item:
                item["pizza_type"] = menu_item.pizza_type
        if menu_item and menu_item.available:
            dough_code = item.get("dough_type", "classica")
            if is_dough_available(dough_code):
                valid_items.append(item)
            else:
                invalid_items.append(item)
                missing_messages.append(f"L'impasto '{dough_code}' non è disponibile.")
        else:
            msg, _ = build_missing_item_message(session, item)
            missing_messages.append(msg)
            invalid_items.append(item)

    missing_items = [f'{item["pizza_name"]} ({item["pizza_type"]})' for item in invalid_items]

    # 6. Rilevamento conferma semplice
    _confirm_words = {"sì", "si", "va bene", "ok", "perfetto", "confermo"}
    is_confirmation = (
        any(m in message_lower for m in _confirm_words)
        and not new_items
        and not extracted.get("customer_name")
        and not extracted.get("pickup_time")
    )
    if is_confirmation and not missing_messages and valid_items \
            and merged_order.get("customer_name") and merged_order.get("pickup_time"):
        intent = "confirm_order"

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
    # Override esplicito: se intended_quantity è dichiarato e non raggiunto,
    # forza collecting_items indipendentemente da ciò che determine_state ha calcolato.
    if conversation.intended_quantity and state not in ("collecting_items", "completed"):
        _collected = sum(int(item.get("quantity") or 1) for item in merged_order.get("items", []))
        if _collected < conversation.intended_quantity:
            print(f"[State] intended_quantity={conversation.intended_quantity} _collected={_collected} → forza collecting_items")
            state = "collecting_items"
    conversation.state = state

    if valid and not conversation.completed and intent == "confirm_order":
        order, order_created = _persist_order_once(session, conversation, merged_order)

        order_id = order.id
        order_saved = True

        # Prezzo base per pizze libere ("Personalizzata") = prezzo Margherita
        _margherita = session.exec(
            select(MenuItem).where(MenuItem.name == "Margherita")
        ).first()
        _base_personalizzata = round(_margherita.price, 2) if _margherita else 0.0

        enriched_items = []
        for item in merged_order["items"]:
            if item["pizza_name"] == "Personalizzata":
                menu_item = None
                base_price = _base_personalizzata
            else:
                menu_item = session.exec(
                    select(MenuItem).where(
                        MenuItem.name == item["pizza_name"],
                        MenuItem.pizza_type == item["pizza_type"],
                    )
                ).first()
                if not menu_item:
                    menu_item = session.exec(
                        select(MenuItem).where(MenuItem.name == item["pizza_name"])
                    ).first()
                base_price = round(menu_item.price, 2) if menu_item else 0.0
            is_sg_pizza = "(SG)" in item.get("pizza_name", "")
            dough_code = item.get("dough_type", "classica")
            dough_surcharge = 0.0 if is_sg_pizza else get_dough_surcharge(dough_code)
            size = item.get("size", "normale")
            if size == "mini":
                base_price = round(max(0.0, base_price - SIZE_MINI_DISCOUNT), 2)
            elif size == "doppio":
                dough_surcharge = round(dough_surcharge + SIZE_DOPPIO_SURCHARGE, 2)
            extras_price = round(dough_surcharge + len(item.get("add_ingredients", [])) * INGREDIENT_EXTRA_PRICE, 2)
            total_price = round((base_price + extras_price) * item["quantity"], 2)
            enriched_items.append({**item, "base_price": base_price, "extras_price": extras_price, "total_price": total_price})

        if order_created:
            save_order_to_base44(
                customer_name=merged_order["customer_name"],
                customer_phone=conversation.customer_phone,
                pickup_time=merged_order["pickup_time"],
                order_number=order.id,
                ai_confidence=0.9,
                items=enriched_items,
            )
            print(f"[SMS] Tentativo invio (confirm_order): phone={conversation.customer_phone!r} name={merged_order['customer_name']!r}")
            send_whatsapp_confirmation(
                customer_name=merged_order["customer_name"],
                customer_phone=conversation.customer_phone,
                pickup_time=merged_order["pickup_time"],
                items=enriched_items,
                total_amount=round(sum(i.get("total_price", 0.0) for i in enriched_items), 2),
            )

            pizza_names = list(dict.fromkeys(item["pizza_name"] for item in merged_order["items"]))
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
        new_valid_items=valid_items,
        customer_phone=conversation.customer_phone,
        pickup_time_error=pickup_time_error,
        removed_names=removed_names,
        not_found_names=not_found_names,
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
