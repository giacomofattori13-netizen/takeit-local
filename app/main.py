from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.db import create_db_and_tables
from app.routes.menu import router as menu_router
from app.routes.orders import router as orders_router
from app.routes.chat import router as chat_router
from app.routes.sessions import router as sessions_router
from app.routes.tts import router as tts_router
from app.routes.logs import router as logs_router
from app.services.conversation_service import load_menu_from_base44

app = FastAPI(title="TakeIt Local Core")


@app.on_event("startup")
def on_startup():
    create_db_and_tables()

    menu = load_menu_from_base44()
    if menu:
        first_names = [item["name"] for item in menu[:3]]
        print(f"[Startup] Menu caricato: {len(menu)} voci. Prime 3: {first_names}")
    else:
        print("[Startup] ATTENZIONE: menu vuoto dopo load_menu_from_base44()")


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