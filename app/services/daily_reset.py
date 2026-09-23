"""Reset giornaliero dei locali (thread avviato da main.py all'avvio).

Per ogni locale:
- sold_out_ingredients viene svuotato sempre, anche per i locali con prenotazioni;
- i MenuItem disattivati vengono riattivati solo se daily_reset_enabled;
- le cache di menu e ristorante vengono invalidate per tutti i locali.
"""
import datetime
import os
import threading
import time
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from app.db import engine
from app.models import MenuItem as DBMenuItem
from app.services import base44_client
from app.services.conversation_service import (
    fetch_and_save_restaurant,
    reset_menu_cache,
    reset_restaurant_cache,
)

ROME = ZoneInfo("Europe/Rome")


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.lower() not in ("false", "0", "no")
    return bool(value)


def is_daily_reset_enabled(restaurant: dict) -> bool:
    """True se i MenuItem disattivati del locale vanno riattivati ogni giorno.

    Usa Restaurant.daily_reset_enabled; se il campo manca (transitorio) ripiega
    su `not reservations_enabled` (modalità menu del giorno / al taglio).
    """
    value = restaurant.get("daily_reset_enabled")
    if value is None:
        return not _truthy(restaurant.get("reservations_enabled", True))
    return _truthy(value)


def _reset_restaurant(restaurant: dict) -> None:
    rid = restaurant.get("id", "")
    name = restaurant.get("name") or rid
    reenable = is_daily_reset_enabled(restaurant)
    print(f"[DailyReset] Reset ristorante: {name!r} (id={rid!r}, riattivazione menu={reenable})")

    # 1. Svuota sold_out_ingredients (tutti i locali)
    sold_out = restaurant.get("sold_out_ingredients") or []
    if sold_out:
        base44_client.update_restaurant({"sold_out_ingredients": []}, restaurant_id=rid)
        print(f"[DailyReset]   sold_out resettati: {sold_out}")
    else:
        print("[DailyReset]   Nessun ingrediente finito")

    if not reenable:
        return

    # 2. Riabilita MenuItem su Base44 (solo quelli di questo ristorante)
    b44_items = base44_client.get_menu_items(restaurant_id=rid)
    disabled = [i for i in b44_items if not i.get("available", True)]
    for item in disabled:
        base44_client.update_menu_item(str(item["id"]), {"available": True})
    if disabled:
        print(f"[DailyReset]   {len(disabled)} MenuItem riabilitati su Base44")

    # 3. Aggiorna DB locale (righe con questo restaurant_id)
    with Session(engine) as db:
        db_items = db.exec(select(DBMenuItem).where(DBMenuItem.restaurant_id == rid)).all()
        changed = 0
        for di in db_items:
            if not di.available:
                di.available = True
                db.add(di)
                changed += 1
        db.commit()
        if changed:
            print(f"[DailyReset]   {changed} voci DB riabilitate")


def _invalidate_caches(restaurant_id: str) -> None:
    reset_restaurant_cache(restaurant_id=restaurant_id)
    fetch_and_save_restaurant(restaurant_id=restaurant_id)
    reset_menu_cache(restaurant_id=restaurant_id)
    print(f"[DailyReset]   Cache invalidata per restaurant_id={restaurant_id!r}")


def perform_daily_reset() -> None:
    print("[DailyReset] Inizio reset giornaliero")

    restaurants = base44_client.get_all_restaurants()
    if not restaurants:
        print("[DailyReset] Nessun ristorante trovato su Base44, skip")
        return

    reenable_count = sum(1 for r in restaurants if is_daily_reset_enabled(r))
    print(
        f"[DailyReset] {len(restaurants)} ristoranti: sold_out svuotati per tutti, "
        f"menu riattivato per {reenable_count}"
    )

    for restaurant in restaurants:
        _reset_restaurant(restaurant)

    # Cache di menu e ristorante invalidate per tutti i locali
    for restaurant in restaurants:
        _invalidate_caches(restaurant.get("id", ""))

    print("[DailyReset] Reset completato")


def _reset_time() -> tuple[int, int]:
    reset_str = os.getenv("DAILY_RESET_HOUR", "11:00")
    try:
        h, m = map(int, reset_str.split(":"))
        return h, m
    except Exception:
        return 11, 0


def _daily_reset_worker() -> None:
    h, m = _reset_time()

    while True:
        now = datetime.datetime.now(tz=ROME)
        next_reset = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now >= next_reset:
            next_reset += datetime.timedelta(days=1)
        sleep_seconds = (next_reset - now).total_seconds()
        print(
            f"[DailyReset] Prossimo reset: {next_reset.strftime('%Y-%m-%d %H:%M')} "
            f"(tra {sleep_seconds / 3600:.1f}h)"
        )
        time.sleep(max(sleep_seconds, 1))
        try:
            perform_daily_reset()
        except Exception as e:
            print(f"[DailyReset] Errore inatteso: {type(e).__name__}: {e}")


def start_daily_reset_thread() -> None:
    thread = threading.Thread(target=_daily_reset_worker, name="daily-reset", daemon=True)
    thread.start()
    print("[DailyReset] Thread avviato")
