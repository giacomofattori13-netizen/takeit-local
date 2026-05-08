import json
import os
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError, field_validator

from app.services.conversation_service import MENU_JSON_PATH
from app.services.menu_sync import sync_menu_to_db
from app.security import require_admin_api_key

router = APIRouter(
    prefix="/owner-command",
    tags=["owner"],
    dependencies=[Depends(require_admin_api_key)],
)


class OwnerCommandRequest(BaseModel):
    command: str


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


@router.post("/")
def owner_command(request: OwnerCommandRequest):
    try:
        with open(MENU_JSON_PATH, encoding="utf-8") as f:
            menu = json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore lettura menu: {e}")

    all_ingredients = sorted({ing for item in menu for ing in item.get("ingredients", [])})
    all_dough_types = sorted({item.get("dough_type", "classica") for item in menu})
    all_pizza_names = sorted({item["name"] for item in menu})

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

    print(f"[OwnerCommand] Comando: '{request.command}' → {action}")

    if action.get("action") == "remove_ingredient":
        ingredient = action.get("ingredient", "")
        count = sum(
            1 for item in menu
            if ingredient in item.get("ingredients", [])
        )
        for item in menu:
            if ingredient in item.get("ingredients", []):
                item["ingredients"].remove(ingredient)

        if count == 0:
            return {"ok": False, "action": action, "details": f"Ingrediente '{ingredient}' non trovato nel menu"}

        _write_menu(menu)
        synced = sync_menu_to_db()
        details = f"Rimosso '{ingredient}' da {count} pizze"
        print(f"[OwnerCommand] {details}. DB: {synced} voci")
        return {"ok": True, "action": action, "details": details, "synced_items": synced}

    elif action.get("action") == "disable_dough_type":
        dough = action.get("dough_type", "")
        count = 0
        for item in menu:
            if item.get("dough_type") == dough and item.get("available", True):
                item["available"] = False
                count += 1

        if count == 0:
            return {"ok": False, "action": action, "details": f"Nessuna pizza con impasto '{dough}' trovata o già disabilitata"}

        _write_menu(menu)
        synced = sync_menu_to_db()
        details = f"Disabilitate {count} pizze con impasto '{dough}'"
        print(f"[OwnerCommand] {details}. DB: {synced} voci")
        return {"ok": True, "action": action, "details": details, "synced_items": synced}

    elif action.get("action") == "disable_pizza":
        name = action.get("pizza_name", "")
        count = 0
        for item in menu:
            if item["name"].lower() == name.lower() and item.get("available", True):
                item["available"] = False
                count += 1

        if count == 0:
            return {"ok": False, "action": action, "details": f"Pizza '{name}' non trovata o già disabilitata"}

        _write_menu(menu)
        synced = sync_menu_to_db()
        details = f"Disabilitata pizza '{name}' ({count} varianti)"
        print(f"[OwnerCommand] {details}. DB: {synced} voci")
        return {"ok": True, "action": action, "details": details, "synced_items": synced}

    else:
        return {"ok": False, "action": action, "details": action.get("reason", "Comando non riconosciuto")}


def _write_menu(menu: list) -> None:
    with open(MENU_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(menu, f, ensure_ascii=False, indent=2)
