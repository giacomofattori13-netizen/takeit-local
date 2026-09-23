from sqlmodel import Session, delete, func, select

from app.db import engine
from app.models import MenuItem
from app.services.conversation_service import load_menu_from_base44, read_menu_file_raw, reset_menu_cache

# Un menu che scende sotto questa frazione delle righe già presenti è sospetto
# (risposta Base44 parziale): le righe attuali vengono mantenute.
MIN_MENU_RATIO = 0.5


def _restaurant_ids_to_sync() -> list[str]:
    """Id dei locali da sincronizzare: tutti quelli su Base44; se Base44 non
    risponde, quelli presenti nel file locale (così l'avvio offline non azzera i prezzi)."""
    from app.services.base44_client import get_all_restaurants_or_none

    restaurants = get_all_restaurants_or_none()
    if restaurants is not None:
        ids = [str(r["id"]) for r in restaurants if r.get("id")]
        source = "Base44"
    else:
        ids = sorted({str(i["restaurant_id"]) for i in read_menu_file_raw() if i.get("restaurant_id")})
        source = "file locale"
    print(f"[MenuSync] Locali da sincronizzare ({source}): {ids}")
    return ids


def _existing_row_count(restaurant_id: str) -> int:
    with Session(engine) as session:
        return session.exec(
            select(func.count()).select_from(MenuItem).where(MenuItem.restaurant_id == restaurant_id)
        ).one()


def sync_menu_to_db() -> int:
    """
    Risincronizza la tabella MenuItem con il menu di ogni ristorante, salvando
    restaurant_id su ogni riga (i prezzi del locale X non vengono mai usati per Y).

    Per ogni locale le righe vengono sostituite solo se il suo menu non è vuoto e
    ha almeno il 50% delle righe già nel DB per quel locale: un errore o una
    risposta parziale di Base44 non cancella i prezzi già presenti.
    Le righe legacy senza restaurant_id vengono rimosse appena almeno un locale
    è sincronizzato. Invalida prima la cache in-memory. Ritorna le voci inserite.
    """
    reset_menu_cache()
    menus: dict[str, list[dict]] = {}
    for rid in _restaurant_ids_to_sync():
        menu = load_menu_from_base44(restaurant_id=rid)
        if not menu:
            print(f"[MenuSync] Menu vuoto per restaurant_id={rid!r}, righe esistenti invariate")
            continue
        existing = _existing_row_count(rid)
        if len(menu) < existing * MIN_MENU_RATIO:
            print(
                f"[MenuSync] WARNING: menu sospetto per restaurant_id={rid!r}: {len(menu)} voci "
                f"contro {existing} nel DB (< {MIN_MENU_RATIO:.0%}), righe esistenti invariate"
            )
            continue
        menus[rid] = menu

    if not menus:
        print("[MenuSync] Nessun menu disponibile, DB non aggiornato")
        return 0

    with Session(engine) as session:
        session.exec(delete(MenuItem).where(MenuItem.restaurant_id.is_(None)))
        for rid, menu in menus.items():
            session.exec(delete(MenuItem).where(MenuItem.restaurant_id == rid))
            for item in menu:
                session.add(MenuItem(
                    name=item["name"],
                    category=item.get("category", ""),
                    pizza_type=item["pizza_type"],
                    price=item.get("price", 0.0),
                    available=item.get("available", True),
                    sale_unit=item.get("sale_unit", "piece"),
                    restaurant_id=rid,
                ))
        session.commit()

    total = sum(len(menu) for menu in menus.values())
    counts = {rid: len(menu) for rid, menu in menus.items()}
    print(f"[MenuSync] DB sincronizzato: {total} voci {counts}")
    return total
