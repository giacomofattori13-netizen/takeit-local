import json
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

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

    message = client.messages.create(
        model=model,
        max_tokens=256,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    try:
        action = json.loads(message.content[0].text.strip())
    except Exception:
        raise HTTPException(status_code=500, detail="Claude non ha restituito JSON valido")

    print(f"[OwnerCommand] Comando: '{request.command}' → {action}")

    if action["action"] == "remove_ingredient":
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

    elif action["action"] == "disable_dough_type":
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

    elif action["action"] == "disable_pizza":
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
