"""SMS webhook for owner commands via Twilio.

Processes commands from the owner's phone number (OWNER_PHONE env var).
Actions: sold_out / back (ingredient), item_off / item_on (whole dish).
"""
import asyncio
import json
import os
import re

import httpx
from fastapi import APIRouter, Form, Request, Response

router = APIRouter(prefix="/sms", tags=["sms"])


def _normalize_phone(phone: str | None) -> str:
    p = re.sub(r"[\s\-\(\)]", "", phone or "").strip()
    if p and not p.startswith("+"):
        p = f"+39{p}"
    return p.lower()


# ── Claude interpretation ─────────────────────────────────────────────────────

_INTERPRET_SYSTEM = (
    "Sei l'assistente di una pizzeria. Il titolare ti invia comandi via SMS.\n\n"
    "Restituisci SOLO JSON (nessun testo extra):\n"
    '{"action":"sold_out"|"back"|"item_off"|"item_on"|"unknown",'
    '"ingredient":<string|null>,"item_name":<string|null>}\n\n'
    "Regole:\n"
    "- sold_out: ingrediente finito (es. 'finita la bufala', 'mozzarella finita')\n"
    "- back: ingrediente tornato (es. 'tornata la bufala', 'di nuovo la mozzarella')\n"
    "- item_off: piatto non disponibile (es. 'togli la margherita', 'margherita finita')\n"
    "- item_on: piatto disponibile di nuovo (es. 'rimetti la margherita')\n"
    "- unknown: non riconoscibile\n"
    "ingredient e item_name: normalizzati lower+trim, null se non applicabile"
)


def _interpret_command(command_text: str) -> dict:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("[SMS] ANTHROPIC_API_KEY mancante")
        return {"action": "unknown", "ingredient": None, "item_name": None}
    try:
        import anthropic
    except ImportError:
        print("[SMS] anthropic non installato")
        return {"action": "unknown", "ingredient": None, "item_name": None}

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=os.getenv("ANTHROPIC_MODEL_SMS", "claude-sonnet-4-20250514"),
            max_tokens=100,
            system=_INTERPRET_SYSTEM,
            messages=[{"role": "user", "content": command_text}],
        )
        raw = (msg.content[0].text if msg.content else "").strip()
        m = re.search(r"\{.*?\}", raw, re.DOTALL)
        if not m:
            print(f"[SMS] Claude non ha restituito JSON valido: {raw!r}")
            return {"action": "unknown", "ingredient": None, "item_name": None}
        parsed = json.loads(m.group())
        for k in ("ingredient", "item_name"):
            if parsed.get(k):
                parsed[k] = str(parsed[k]).lower().strip()
        return parsed
    except Exception as e:
        print(f"[SMS] Errore Claude: {type(e).__name__}: {e}")
        return {"action": "unknown", "ingredient": None, "item_name": None}


# ── Action handlers ───────────────────────────────────────────────────────────

def _get_restaurant_for_action(restaurant_id: str) -> dict | None:
    """Fetch the restaurant dict for the given id, using cache or Base44."""
    from app.services.base44_client import get_restaurant, get_restaurant_by_id
    if restaurant_id:
        return get_restaurant_by_id(restaurant_id) or get_restaurant()
    return get_restaurant()


def _apply_sold_out(ingredient: str, restaurant_id: str = "") -> str:
    from app.services.base44_client import update_restaurant
    from app.services.conversation_service import (
        fetch_and_save_restaurant,
        reset_menu_cache,
        load_menu_from_base44,
    )

    restaurant = _get_restaurant_for_action(restaurant_id)
    if not restaurant:
        return "Errore: impossibile leggere dati ristorante."

    rid = restaurant.get("id")
    current = [s.lower().strip() for s in (restaurant.get("sold_out_ingredients") or []) if s]
    if ingredient in current:
        return f"'{ingredient}' era già segnato come finito."

    current.append(ingredient)
    ok = update_restaurant({"sold_out_ingredients": current}, restaurant_id=rid)
    if not ok:
        return f"Errore aggiornamento Base44 per '{ingredient}'."

    menu = load_menu_from_base44(restaurant_id=restaurant_id)
    affected = sum(
        1 for item in menu
        if ingredient in {ing.lower().strip() for ing in item.get("ingredients", [])}
    )

    fetch_and_save_restaurant(restaurant_id=restaurant_id)
    reset_menu_cache(restaurant_id=restaurant_id)

    return f"'{ingredient}' segnato come finito: nascoste {affected} voci di menù."


def _apply_back(ingredient: str, restaurant_id: str = "") -> str:
    from app.services.base44_client import update_restaurant
    from app.services.conversation_service import fetch_and_save_restaurant, reset_menu_cache

    restaurant = _get_restaurant_for_action(restaurant_id)
    if not restaurant:
        return "Errore: impossibile leggere dati ristorante."

    rid = restaurant.get("id")
    current = [s.lower().strip() for s in (restaurant.get("sold_out_ingredients") or []) if s]
    if ingredient not in current:
        return f"'{ingredient}' non era segnato come finito."

    updated = [s for s in current if s != ingredient]
    ok = update_restaurant({"sold_out_ingredients": updated}, restaurant_id=rid)
    if not ok:
        return f"Errore rimozione '{ingredient}' dai finiti."

    fetch_and_save_restaurant(restaurant_id=restaurant_id)
    reset_menu_cache(restaurant_id=restaurant_id)

    return f"'{ingredient}' di nuovo disponibile."


def _apply_item_off(item_name: str, restaurant_id: str = "") -> str:
    return _toggle_item(item_name, available=False, restaurant_id=restaurant_id)


def _apply_item_on(item_name: str, restaurant_id: str = "") -> str:
    return _toggle_item(item_name, available=True, restaurant_id=restaurant_id)


def _toggle_item(item_name: str, *, available: bool, restaurant_id: str = "") -> str:
    """Set available flag on all matching items in Base44, DB, and menu_data.json."""
    from app.services.base44_client import get_menu_items, update_menu_item
    from app.services.conversation_service import MENU_JSON_PATH, reset_menu_cache
    from app.db import engine
    from app.models import MenuItem as DBMenuItem
    from sqlmodel import Session, select

    b44_items = get_menu_items(restaurant_id=restaurant_id or None)
    matches = [i for i in b44_items if i.get("name", "").lower().strip() == item_name]

    if not matches:
        return f"Nessun piatto trovato con nome '{item_name}'."

    for item in matches:
        update_menu_item(str(item["id"]), {"available": available})

    # Update local DB
    with Session(engine) as db:
        db_items = db.exec(select(DBMenuItem)).all()
        changed = 0
        for di in db_items:
            if di.name.lower().strip() == item_name:
                di.available = available
                db.add(di)
                changed += 1
        db.commit()
        print(f"[SMS] DB aggiornato: {changed} voci '{item_name}' → available={available}")

    # Update menu_data.json
    try:
        import json as _json
        with open(MENU_JSON_PATH, encoding="utf-8") as f:
            menu_json = _json.load(f)
        for item in menu_json:
            if item.get("name", "").lower().strip() == item_name:
                item["available"] = available
        with open(MENU_JSON_PATH, "w", encoding="utf-8") as f:
            _json.dump(menu_json, f, ensure_ascii=False, indent=2)
        print(f"[SMS] menu_data.json aggiornato per '{item_name}'")
    except Exception as e:
        print(f"[SMS] Errore menu_data.json: {type(e).__name__}: {e}")

    reset_menu_cache(restaurant_id=restaurant_id)

    verb = "rimosso dal" if not available else "rimesso nel"
    return f"'{item_name}' {verb} menù ({len(matches)} varianti)."


# ── Twilio SMS reply ──────────────────────────────────────────────────────────

def _send_reply(to: str, body: str) -> None:
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_raw = os.getenv("TWILIO_NUMBER")
    if not all([account_sid, auth_token, from_raw]):
        print("[SMS] Credenziali Twilio mancanti, skip reply")
        return
    from_number = from_raw.removeprefix("whatsapp:")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    try:
        resp = httpx.post(
            url,
            auth=(account_sid, auth_token),
            data={"From": from_number, "To": to, "Body": body},
            timeout=10,
        )
        resp.raise_for_status()
        print(f"[SMS] Reply inviata: {body[:60]!r}")
    except Exception as e:
        print(f"[SMS] Errore invio reply: {type(e).__name__}: {e}")


# ── Webhook endpoint ──────────────────────────────────────────────────────────

@router.post("/incoming")
async def sms_incoming(
    request: Request,
    From: str = Form(default=""),
    To: str = Form(default=""),
    Body: str = Form(default=""),
):
    """Twilio SMS webhook. Processes owner commands."""
    owner_phone = os.getenv("OWNER_PHONE", "").strip()
    if not owner_phone:
        print("[SMS] OWNER_PHONE non configurato, ignore")
        return Response(content="<?xml version='1.0'?><Response/>", media_type="application/xml")

    if _normalize_phone(From) != _normalize_phone(owner_phone):
        print(f"[SMS] Mittente non autorizzato: {From!r}")
        return Response(content="<?xml version='1.0'?><Response/>", media_type="application/xml")

    command_text = Body.strip()
    if not command_text:
        return Response(content="<?xml version='1.0'?><Response/>", media_type="application/xml")

    # Risolvi il ristorante dal numero chiamato (To)
    from app.services.conversation_service import resolve_restaurant_from_phone as _resolve
    _restaurant, restaurant_id, _match_method = await asyncio.to_thread(_resolve, To)
    print(f"[SMS] To={To!r} → restaurant_id={restaurant_id!r} match={_match_method!r}")

    print(f"[SMS] Comando titolare: {command_text!r}")

    from app.services.base44_client import create_owner_command, update_owner_command

    # a. Crea OwnerCommand su Base44
    cmd = create_owner_command({
        "command_text": command_text,
        "source": "sms",
        "status": "pending",
        **({"restaurant_id": restaurant_id} if restaurant_id else {}),
    })
    cmd_id = cmd.get("id") if cmd else None

    # b. Interpreta con Claude
    action = _interpret_command(command_text)
    print(f"[SMS] Azione: {action}")

    # c. Applica
    act = action.get("action", "unknown")
    ingredient = action.get("ingredient")
    item_name = action.get("item_name")
    status = "executed"
    try:
        if act == "sold_out" and ingredient:
            result_message = _apply_sold_out(ingredient, restaurant_id=restaurant_id)
        elif act == "back" and ingredient:
            result_message = _apply_back(ingredient, restaurant_id=restaurant_id)
        elif act == "item_off" and item_name:
            result_message = _apply_item_off(item_name, restaurant_id=restaurant_id)
        elif act == "item_on" and item_name:
            result_message = _apply_item_on(item_name, restaurant_id=restaurant_id)
        else:
            result_message = (
                "Comando non riconosciuto. Esempi: "
                "'finita la bufala', 'tornata la bufala', "
                "'togli la margherita', 'rimetti la margherita'."
            )
            status = "failed"
    except Exception as e:
        print(f"[SMS] Errore applicazione: {type(e).__name__}: {e}")
        result_message = f"Errore: {type(e).__name__}: {e}"
        status = "failed"

    # d. Aggiorna OwnerCommand su Base44
    if cmd_id:
        update_owner_command(cmd_id, {
            "interpreted_action": json.dumps(action, ensure_ascii=False),
            "status": status,
            "result": result_message,
        })

    # e. Rispondi al titolare via SMS
    _send_reply(From, result_message)

    return Response(content="<?xml version='1.0'?><Response/>", media_type="application/xml")
