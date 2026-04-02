from sqlmodel import Session, delete

from app.db import engine
from app.models import MenuItem
from app.services.conversation_service import load_menu_from_base44, reset_menu_cache


def sync_menu_to_db() -> int:
    """
    Svuota la tabella MenuItem e la risincronizza da menu_data.json.
    Invalida prima la cache in-memory. Ritorna il numero di voci inserite.
    """
    reset_menu_cache()
    menu = load_menu_from_base44()
    if not menu:
        print("[MenuSync] Menu vuoto, DB non aggiornato")
        return 0

    with Session(engine) as session:
        session.exec(delete(MenuItem))
        for item in menu:
            session.add(MenuItem(
                name=item["name"],
                category=item.get("category", ""),
                pizza_type=item["pizza_type"],
                price=item.get("price", 0.0),
                available=item.get("available", True),
            ))
        session.commit()

    print(f"[MenuSync] DB sincronizzato: {len(menu)} voci")
    return len(menu)
