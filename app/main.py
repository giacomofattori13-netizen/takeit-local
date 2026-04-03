import os

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from sqlalchemy import text

from app.db import create_db_and_tables, engine
from app.routes.menu import router as menu_router
from app.routes.orders import router as orders_router
from app.routes.chat import router as chat_router
from app.routes.sessions import router as sessions_router
from app.routes.tts import router as tts_router
from app.routes.logs import router as logs_router
from app.routes.owner_command import router as owner_command_router
from app.services.menu_sync import sync_menu_to_db
from app.services.conversation_service import fetch_and_save_doughs, load_restaurant

app = FastAPI(title="TakeIt Local Core")


@app.on_event("startup")
def on_startup():
    for migration_sql in [
        "ALTER TABLE conversationsession ADD COLUMN customer_phone VARCHAR",
        "ALTER TABLE conversationsession ADD COLUMN intended_quantity INTEGER",
    ]:
        try:
            with engine.connect() as conn:
                conn.execute(text(migration_sql))
                conn.commit()
        except Exception:
            pass  # colonna già esistente

    create_db_and_tables()
    synced = sync_menu_to_db()
    if synced:
        print(f"[Startup] DB sincronizzato: {synced} voci da menu_data.json")
    else:
        print("[Startup] ATTENZIONE: menu vuoto, DB non sincronizzato")

    doughs = fetch_and_save_doughs()
    print(f"[Startup] Impasti disponibili: {[d['name'] for d in doughs]}")

    restaurant = load_restaurant()
    print(f"[Startup] Restaurant: {list(restaurant.keys()) if restaurant else 'non caricato'}")

    _twilio_vars = ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM"]
    _present = [v for v in _twilio_vars if os.getenv(v)]
    _missing = [v for v in _twilio_vars if not os.getenv(v)]
    print(f"[Startup] Twilio — presenti: {_present}, mancanti: {_missing}")
    print(f"[Startup] Twilio environ check — TWILIO_ACCOUNT_SID is not None: {os.environ.get('TWILIO_ACCOUNT_SID') is not None}, TWILIO_AUTH_TOKEN is not None: {os.environ.get('TWILIO_AUTH_TOKEN') is not None}, TWILIO_WHATSAPP_FROM is not None: {os.environ.get('TWILIO_WHATSAPP_FROM') is not None}")


app.mount("/static", StaticFiles(directory="app/static"), name="static")


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