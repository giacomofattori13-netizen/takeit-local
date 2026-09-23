import json
import os
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError, field_validator

from app.services import base44_client
from app.services.conversation_service import MENU_JSON_PATH, read_menu_file_raw
from app.services.menu_sync import sync_menu_to_db
from app.security import require_admin_api_key

router = APIRouter(
    prefix="/owner-command",
    tags=["owner"],
    dependencies=[Depends(require_admin_api_key)],
)


class OwnerCommandRequest(BaseModel):
    command: str
    restaurant_id: str | None = None


class OwnerAction(BaseModel):
    action: Literal[
        "remove_ingredient",
        "disable_dough_type",
        "disable_pizza",
        "unknown",
    ]
    ingredient: str | None = None
    dough_type: str | None = None
    pizza_name: str | None = None
    reason: str | None = None

    @field_validator("action", mode="before")
    @classmethod
    def normalize_action(cls, value):
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator(
        "ingredient",
        "dough_type",
        "pizza_name",
        "reason",
        mode="before",
    )
    @classmethod
    def strip_optional_text(cls, value):
        if isinstance(value, str):
            return value.strip()
        return value


def _unknown_action(reason: str) -> dict:
    return {"action": "unknown", "reason": reason}


def _json_candidate(raw_text: str | None) -> str:
    text = (raw_text or "").strip()
    if not text:
        return ""

    fenced = re.search(
        r"```(?:json)?\s*(.*?)\s*```",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        return fenced.group(1).strip()

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]

    return text


def _parse_owner_action(raw_text: str | None) -> dict:
    candidate = _json_candidate(raw_text)
    if not candidate:
        return _unknown_action("Claude non ha restituito contenuto")

    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return _unknown_action("Claude non ha restituito JSON valido")

    if not isinstance(payload, dict):
        return _unknown_action("Claude ha restituito JSON non oggetto")

    try:
        action = OwnerAction.model_validate(payload)
    except ValidationError:
        return _unknown_action("Claude ha restituito azione non valida")

    parsed = action.model_dump(exclude_none=True)
    action_name = parsed["action"]
    if action_name == "remove_ingredient" and not parsed.get("ingredient"):
        return _unknown_action("Ingrediente mancante")
    if action_name == "disable_dough_type" and not parsed.get("dough_type"):
        return _unknown_action("Tipo impasto mancante")
    if action_name == "disable_pizza" and not parsed.get("pizza_name"):
        return _unknown_action("Nome pizza mancante")
    if action_name == "unknown" and not parsed.get("reason"):
        parsed["reason"] = "Comando non riconosciuto"

    return parsed


def _extract_message_text(message) -> str | None:
    content = getattr(message, "content", None)
    if not content:
        return None
    text = getattr(content[0], "text", None)
    return text if isinstance(text, str) else None


_SYSTEM_PROMPT = """Sei l'assistente di gestione di una pizzeria. Ricevi comandi in linguaggio naturale dal titolare e devi interpretarli restituendo un'azione strutturata in JSON.

Azioni possibili:
- {"action": "remove_ingredient", "ingredient": "<nome ingrediente esatto dal menu>"}
  → quando un ingrediente è finito e va rimosso da tutte le pizze che lo contengono
- {"action": "disable_dough_type", "dough_type": "<classica|integrale|senza_glutine>"}
  → quando un tipo di impasto è finito e tutte le pizze con quell'impasto vanno disabilitate
- {"action": "disable_pizza", "pizza_name": "<nome esatto dal menu>"}
  → quando una pizza specifica è da disabilitare
- {"action": "unknown", "reason": "<spiegazione>"}
  → se il comando non è interpretabile

Rispondi SOLO con JSON valido, nessun testo extra."""


def _resolve_restaurant_id(requested: str | None) -> str:
    """Locale su cui agire: quello richiesto, altrimenti l'unico presente in
    menu_data.json (il menu che il comando modificava prima)."""
    if requested and requested.strip():
        return requested.strip()
    file_ids = {str(i["restaurant_id"]) for i in read_menu_file_raw() if i.get("restaurant_id")}
    if len(file_ids) == 1:
        return file_ids.pop()
    raise HTTPException(status_code=400, detail="restaurant_id obbligatorio")


def _plan_changes(action: dict, items: list[dict]) -> tuple[list[tuple[dict, dict]], str, str]:
    """Ritorna ([(MenuItem Base44, patch)], messaggio se nessuna voce, messaggio di esito)."""
    name = action.get("action")
    if name == "remove_ingredient":
        ingredient = action.get("ingredient", "")
        changes = [
            (item, {"ingredients": [i for i in item.get("ingredients") or [] if i != ingredient]})
            for item in items
            if ingredient in (item.get("ingredients") or [])
        ]
        return changes, f"Ingrediente '{ingredient}' non trovato nel menu", f"Rimosso '{ingredient}' da {len(changes)} pizze"
    if name == "disable_dough_type":
        dough = action.get("dough_type", "")
        changes = [
            (item, {"available": False})
            for item in items
            if item.get("dough_type") == dough and item.get("available", True)
        ]
        return (
            changes,
            f"Nessuna pizza con impasto '{dough}' trovata o già disabilitata",
            f"Disabilitate {len(changes)} pizze con impasto '{dough}'",
        )
    pizza = action.get("pizza_name", "")
    changes = [
        (item, {"available": False})
        for item in items
        if (item.get("name") or "").lower() == pizza.lower() and item.get("available", True)
    ]
    return changes, f"Pizza '{pizza}' non trovata o già disabilitata", f"Disabilitata pizza '{pizza}' ({len(changes)} varianti)"


def _mirror_to_menu_file(restaurant_id: str, changes: list[tuple[dict, dict]]) -> None:
    """Riporta le modifiche su menu_data.json (ripiego offline), solo per le voci del locale."""
    try:
        with open(MENU_JSON_PATH, encoding="utf-8") as f:
            menu = json.load(f)
    except Exception as e:
        print(f"[OwnerCommand] menu_data.json non aggiornato: {type(e).__name__}: {e}")
        return
    by_name = {(item.get("name") or "").lower(): patch for item, patch in changes}
    for entry in menu:
        patch = by_name.get((entry.get("name") or "").lower())
        if patch and entry.get("restaurant_id") == restaurant_id:
            entry.update(patch)
    _write_menu(menu)


@router.post("/")
def owner_command(request: OwnerCommandRequest):
    restaurant_id = _resolve_restaurant_id(request.restaurant_id)
    menu = base44_client.get_menu_items(restaurant_id=restaurant_id)
    if not menu:
        raise HTTPException(status_code=503, detail=f"Menu Base44 non disponibile per restaurant_id={restaurant_id}")

    all_ingredients = sorted({ing for item in menu for ing in item.get("ingredients") or []})
    all_dough_types = sorted({item.get("dough_type") or "classica" for item in menu})
    all_pizza_names = sorted({item["name"] for item in menu if item.get("name")})

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY non configurata")

    try:
        import anthropic
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail="Pacchetto anthropic non installato",
        ) from exc

    client = anthropic.Anthropic(api_key=api_key)
    model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

    user_prompt = (
        f"Ingredienti disponibili: {json.dumps(all_ingredients, ensure_ascii=False)}\n"
        f"Tipi di impasto: {json.dumps(all_dough_types, ensure_ascii=False)}\n"
        f"Pizze nel menu: {json.dumps(all_pizza_names, ensure_ascii=False)}\n\n"
        f'Comando del titolare: "{request.command}"'
    )

    try:
        message = client.messages.create(
            model=model,
            max_tokens=256,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Errore Claude owner command: {type(exc).__name__}",
        ) from exc

    action = _parse_owner_action(_extract_message_text(message))

    print(f"[OwnerCommand] Comando: '{request.command}' → {action} (restaurant_id={restaurant_id!r})")

    if action.get("action") not in ("remove_ingredient", "disable_dough_type", "disable_pizza"):
        return {"ok": False, "action": action, "details": action.get("reason", "Comando non riconosciuto")}

    changes, not_found, details = _plan_changes(action, menu)
    if not changes:
        return {"ok": False, "action": action, "details": not_found}

    # Base44 è la fonte del menu dell'agente: le modifiche vanno scritte lì.
    failed = [item for item, patch in changes if base44_client.update_menu_item(str(item["id"]), patch) is None]
    if not failed:
        _mirror_to_menu_file(restaurant_id, changes)
    synced = sync_menu_to_db()
    if failed:
        names = [item.get("name") for item in failed]
        details = f"Errore Base44: {len(failed)}/{len(changes)} voci non aggiornate ({names})"
        print(f"[OwnerCommand] {details}")
        return {"ok": False, "action": action, "details": details, "synced_items": synced}

    print(f"[OwnerCommand] {details}. DB: {synced} voci")
    return {"ok": True, "action": action, "details": details, "synced_items": synced}


def _write_menu(menu: list) -> None:
    with open(MENU_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(menu, f, ensure_ascii=False, indent=2)
