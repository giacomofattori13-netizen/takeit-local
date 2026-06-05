import datetime
import json
import os
import threading
import time
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.db import create_db_and_tables, engine
from app.privacy import mask_phone
from app.routes.menu import router as menu_router
from app.routes.orders import router as orders_router
from app.routes.chat import recover_order_side_effects
from app.routes.chat import router as chat_router
from app.routes.sessions import router as sessions_router
from app.routes.tts import router as tts_router
from app.routes.logs import router as logs_router
from app.routes.owner_command import router as owner_command_router
from app.routes.sms import router as sms_router
from app.routes.voice import close_tts_stream_client, router as voice_router, prewarm_audio_cache
from app.services.menu_sync import sync_menu_to_db
from app.services.conversation_service import (
    fetch_and_save_doughs,
    fetch_and_save_restaurant,
    prewarm_system_prompt,
    reset_menu_cache,
    reset_restaurant_cache,
    MENU_JSON_PATH,
)
from app.startup_migrations import apply_startup_column_migrations, ensure_order_idempotency_index

app = FastAPI(title="TakeIt Local Core")


# ── Daily reset ───────────────────────────────────────────────────────────────

def _is_daily_mode(restaurant: dict) -> bool:
    """Returns True if the restaurant uses daily menu (reservations_enabled=False).
    Only these restaurants are eligible for the daily reset.
    """
    value = restaurant.get("reservations_enabled", True)
    if isinstance(value, str):
        value = value.lower() not in ("false", "0", "no")
    return not bool(value)


def _perform_daily_reset() -> None:
    """Reset sold_out_ingredients and MenuItem.available ONLY for restaurants
    in 'daily menu / al taglio' mode (reservations_enabled=False).
    Restaurants with reservations_enabled=True (e.g. Corte del Sole) are never touched.
    """
    from app.services.base44_client import get_all_restaurants, update_restaurant, get_menu_items, update_menu_item
    from app.models import MenuItem as DBMenuItem
    from sqlmodel import Session, select

    print("[DailyReset] Inizio reset giornaliero")

    restaurants = get_all_restaurants()
    if not restaurants:
        print("[DailyReset] Nessun ristorante trovato su Base44, skip")
        return

    eligible = [r for r in restaurants if _is_daily_mode(r)]
    skipped  = len(restaurants) - len(eligible)
    print(
        f"[DailyReset] {len(restaurants)} ristoranti totali → "
        f"{len(eligible)} idonei al reset, {skipped} saltati (reservations_enabled=True)"
    )

    if not eligible:
        print("[DailyReset] Nessun ristorante idoneo, nulla da fare")
        return

    for restaurant in eligible:
        rid = restaurant.get("id", "")
        name = restaurant.get("name") or rid
        print(f"[DailyReset] Reset ristorante: {name!r} (id={rid!r})")

        # 1. Svuota sold_out_ingredients
        sold_out = restaurant.get("sold_out_ingredients") or []
        if sold_out:
            update_restaurant({"sold_out_ingredients": []}, restaurant_id=rid)
            print(f"[DailyReset]   sold_out resettati: {sold_out}")
        else:
            print("[DailyReset]   Nessun ingrediente finito")

        # 2. Riabilita MenuItem su Base44 (solo quelli di questo ristorante)
        b44_items = get_menu_items(restaurant_id=rid)
        disabled = [i for i in b44_items if not i.get("available", True)]
        for item in disabled:
            update_menu_item(str(item["id"]), {"available": True})
        if disabled:
            print(f"[DailyReset]   {len(disabled)} MenuItem riabilitati su Base44")

        # 3. Aggiorna DB locale (righe con questo restaurant_id)
        with Session(engine) as db:
            db_items = db.exec(
                select(DBMenuItem).where(DBMenuItem.restaurant_id == rid)
            ).all()
            changed = 0
            for di in db_items:
                if not di.available:
                    di.available = True
                    db.add(di)
                    changed += 1
            db.commit()
            if changed:
                print(f"[DailyReset]   {changed} voci DB riabilitate")

        # 4. Reset cache per questo ristorante
        reset_restaurant_cache(restaurant_id=rid)
        fetch_and_save_restaurant(restaurant_id=rid)
        reset_menu_cache(restaurant_id=rid)
        print(f"[DailyReset]   Cache invalidata per restaurant_id={rid!r}")

    print("[DailyReset] Reset completato")


def _daily_reset_worker() -> None:
    rome = ZoneInfo("Europe/Rome")
    reset_str = os.getenv("DAILY_RESET_HOUR", "11:00")
    try:
        h, m = map(int, reset_str.split(":"))
    except Exception:
        h, m = 11, 0

    while True:
        now = datetime.datetime.now(tz=rome)
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
            _perform_daily_reset()
        except Exception as e:
            print(f"[DailyReset] Errore inatteso: {type(e).__name__}: {e}")


def _start_daily_reset_thread() -> None:
    thread = threading.Thread(target=_daily_reset_worker, name="daily-reset", daemon=True)
    thread.start()
    print("[DailyReset] Thread avviato")


# ── Startup / shutdown ────────────────────────────────────────────────────────

@app.on_event("startup")
def on_startup():
    create_db_and_tables()
    applied_migrations = apply_startup_column_migrations(engine)
    if applied_migrations:
        print(f"[Startup] Migrazioni DB applicate: {applied_migrations}")

    if ensure_order_idempotency_index(engine):
        print("[Startup] Indice idempotenza ordini creato")

    recover_order_side_effects()

    synced = sync_menu_to_db()
    if synced:
        print(f"[Startup] DB sincronizzato: {synced} voci da menu_data.json")
    else:
        print("[Startup] ATTENZIONE: menu vuoto, DB non sincronizzato")

    doughs = fetch_and_save_doughs()
    print(f"[Startup] Impasti disponibili: {[d['name'] for d in doughs]}")

    prewarm_system_prompt()

    restaurant = fetch_and_save_restaurant()
    print(f"[Startup] Restaurant: {list(restaurant.keys()) if restaurant else 'non caricato'}")

    prewarm_audio_cache()

    _twilio_vars = ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM", "TWILIO_NUMBER"]
    _present = [v for v in _twilio_vars if os.getenv(v)]
    _missing = [v for v in _twilio_vars if not os.getenv(v)]
    print(f"[Startup] Twilio — presenti: {_present}, mancanti: {_missing}")
    _wa_from_raw = os.environ.get("TWILIO_WHATSAPP_FROM")
    _wa_from_clean = _wa_from_raw.removeprefix("whatsapp:") if _wa_from_raw else None
    _twilio_number = os.environ.get("TWILIO_NUMBER")
    print(
        f"[Startup] TWILIO_WHATSAPP_FROM raw={mask_phone(_wa_from_raw)} "
        f"→ clean={mask_phone(_wa_from_clean)}"
    )
    print(f"[Startup] TWILIO_NUMBER={mask_phone(_twilio_number)}")

    _start_daily_reset_thread()
    print(
        f"[Startup] OWNER_PHONE={'✓' if os.getenv('OWNER_PHONE') else '✗ NON CONFIGURATO'}"
    )


@app.on_event("shutdown")
async def on_shutdown():
    await close_tts_stream_client()


# ── Static / routes ───────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.api_route("/ping", methods=["GET", "HEAD"])
def ping():
    return {"status": "ok"}


@app.get("/")
def root():
    return {"status": "ok", "message": "TakeIt local core is running"}


@app.get("/ui")
def serve_ui():
    return FileResponse("app/static/index.html")


@app.get("/dashboard")
def serve_dashboard():
    return FileResponse("app/static/dashboard.html")

@app.get("/tech-dashboard")
def serve_tech_dashboard():
    return FileResponse("app/static/tech_dashboard.html")


app.include_router(menu_router)
app.include_router(orders_router)
app.include_router(chat_router)
app.include_router(sessions_router)
app.include_router(tts_router)
app.include_router(logs_router)
app.include_router(owner_command_router)
app.include_router(sms_router)
app.include_router(voice_router)
