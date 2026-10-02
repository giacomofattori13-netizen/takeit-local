"""Reset giornaliero dei locali (thread avviato da main.py all'avvio).

Per ogni locale:
- sold_out_ingredients viene svuotato sempre, anche per i locali con prenotazioni;
- i MenuItem disattivati vengono riattivati solo se daily_reset_enabled;
- le cache di menu e ristorante vengono invalidate per tutti i locali.

La data dell'ultimo reset riuscito è salvata su Base44 (Restaurant.last_daily_reset_date,
"YYYY-MM-DD", ora di Roma): il DB locale non sopravvive ai deploy. Un locale già
resettato oggi viene saltato, così il recupero all'avvio non cancella i "finiti"
segnati dal titolare dopo il reset.
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
    restaurant_flag,
)

ROME = ZoneInfo("Europe/Rome")
DAILY_RESET_ATTEMPTS_DEFAULT = 3
DAILY_RESET_RETRY_DELAY_DEFAULT_SECONDS = 450.0  # 3 tentativi in 15 minuti


def is_daily_reset_enabled(restaurant: dict) -> bool:
    """True se i MenuItem disattivati del locale vanno riattivati ogni giorno.

    Dipende solo da Restaurant.daily_reset_enabled: null o assente → False.
    """
    return restaurant_flag(restaurant, "daily_reset_enabled", default=False)


def _reset_restaurant(restaurant: dict, today: str) -> bool:
    """Resetta un locale. False se una scrittura su Base44 fallisce: in quel caso
    la data del reset non viene salvata, il DB locale non viene toccato e il
    chiamante non invalida le cache."""
    rid = restaurant.get("id", "")
    name = restaurant.get("name") or rid
    reenable = is_daily_reset_enabled(restaurant)
    print(f"[DailyReset] Reset ristorante: {name!r} (id={rid!r}, riattivazione menu={reenable})")

    if reenable and not _reenable_menu_items_on_base44(rid, name):
        return False

    # Svuota sold_out_ingredients (tutti i locali) e segna il reset di oggi
    sold_out = restaurant.get("sold_out_ingredients") or []
    patch = {"sold_out_ingredients": [], "last_daily_reset_date": today}
    if base44_client.update_restaurant(patch, restaurant_id=rid) is None:
        print(f"[DailyReset]   ERRORE: sold_out/data reset non salvati su Base44 per {name!r}")
        return False
    if sold_out:
        print(f"[DailyReset]   sold_out resettati: {sold_out}")
    else:
        print("[DailyReset]   Nessun ingrediente finito")

    if reenable:
        _reenable_local_menu_items(rid)
    return True


def _reenable_menu_items_on_base44(rid: str, name: str) -> bool:
    """Riabilita su Base44 i MenuItem disattivati del locale. False se una scrittura fallisce."""
    b44_items = base44_client.get_menu_items(restaurant_id=rid)
    disabled = [i for i in b44_items if not i.get("available", True)]
    failed = [
        item for item in disabled
        if base44_client.update_menu_item(str(item["id"]), {"available": True}) is None
    ]
    if failed:
        print(
            f"[DailyReset]   ERRORE: {len(failed)}/{len(disabled)} MenuItem non riabilitati su Base44 "
            f"per {name!r}: {[i.get('name') for i in failed]}"
        )
        return False
    if disabled:
        print(f"[DailyReset]   {len(disabled)} MenuItem riabilitati su Base44")
    return True


def _reenable_local_menu_items(rid: str) -> None:
    """Riabilita nel DB locale le righe con questo restaurant_id."""
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


def _today() -> str:
    return datetime.datetime.now(tz=ROME).date().isoformat()


def perform_daily_reset(today: str | None = None) -> list[str] | None:
    """Resetta i locali non ancora resettati oggi e ritorna gli id di quelli
    falliti ([] = tutto ok). None se l'elenco dei ristoranti non è leggibile da Base44."""
    today = today or _today()
    print(f"[DailyReset] Inizio reset giornaliero ({today})")

    restaurants = base44_client.get_all_restaurants_or_none()
    if restaurants is None:
        print("[DailyReset] Elenco ristoranti Base44 non disponibile")
        return None
    if not restaurants:
        print("[DailyReset] Nessun ristorante su Base44, nulla da fare")
        return []

    due = [r for r in restaurants if r.get("last_daily_reset_date") != today]
    reenable_count = sum(1 for r in due if is_daily_reset_enabled(r))
    print(
        f"[DailyReset] {len(restaurants)} ristoranti, {len(restaurants) - len(due)} già resettati oggi; "
        f"da resettare {len(due)} (menu riattivato per {reenable_count})"
    )

    failed = [r.get("id", "") for r in due if not _reset_restaurant(r, today)]

    # Cache di menu e ristorante invalidate per tutti i locali, tranne quelli
    # in cui le scritture su Base44 sono fallite
    for restaurant in restaurants:
        rid = restaurant.get("id", "")
        if rid not in failed:
            _invalidate_caches(rid)

    if failed:
        print(f"[DailyReset] Reset incompleto: scritture Base44 fallite per {failed}")
    else:
        print("[DailyReset] Reset completato")
    return failed


def _positive_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, default))
    except ValueError:
        return default
    return value if value > 0 else default


def run_daily_reset_with_retries() -> bool:
    """Esegue il reset riprovando se Base44 non risponde o una scrittura fallisce
    (default 3 tentativi, uno ogni 7,5 minuti). True se un tentativo va a buon fine."""
    attempts = int(_positive_env("DAILY_RESET_ATTEMPTS", DAILY_RESET_ATTEMPTS_DEFAULT))
    delay = _positive_env("DAILY_RESET_RETRY_DELAY_SECONDS", DAILY_RESET_RETRY_DELAY_DEFAULT_SECONDS)
    for attempt in range(1, attempts + 1):
        try:
            failed = perform_daily_reset()
        except Exception as e:
            print(f"[DailyReset] Errore inatteso: {type(e).__name__}: {e}")
            failed = None
        if failed == []:
            return True
        if attempt < attempts:
            print(f"[DailyReset] Tentativo {attempt}/{attempts} non riuscito, nuovo tentativo tra {delay / 60:.1f} min")
            time.sleep(delay)
    print(f"[DailyReset] ERRORE: reset non riuscito dopo {attempts} tentativi")
    return False


def _reset_time() -> tuple[int, int]:
    reset_str = os.getenv("DAILY_RESET_HOUR", "11:00")
    try:
        h, m = map(int, reset_str.split(":"))
        return h, m
    except Exception:
        return 11, 0


def _catch_up_missed_reset(now: datetime.datetime) -> bool:
    """All'avvio: se l'orario di reset di oggi è già passato, resetta i locali
    che oggi non sono ancora stati resettati (es. riavvio o deploy dopo le 11:00).
    Ritorna True se il recupero è stato avviato."""
    h, m = _reset_time()
    if now < now.replace(hour=h, minute=m, second=0, microsecond=0):
        return False
    print(f"[DailyReset] Avvio dopo le {h:02d}:{m:02d}: recupero dei locali non ancora resettati oggi")
    run_daily_reset_with_retries()
    return True


def _daily_reset_worker() -> None:
    h, m = _reset_time()
    _catch_up_missed_reset(datetime.datetime.now(tz=ROME))

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
        run_daily_reset_with_retries()


def start_daily_reset_thread() -> None:
    thread = threading.Thread(target=_daily_reset_worker, name="daily-reset", daemon=True)
    thread.start()
    print("[DailyReset] Thread avviato")
