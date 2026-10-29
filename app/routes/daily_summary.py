"""Anteprima del riepilogo giornaliero: genera il testo per un locale e una data
senza inviarlo né scrivere su Base44, per verificarlo prima di attivarlo."""
import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from app.security import require_admin_api_key
from app.services import base44_client
from app.services.daily_summary import ROME, preview_summary

router = APIRouter(
    prefix="/daily-summary",
    tags=["daily-summary"],
    dependencies=[Depends(require_admin_api_key)],
)


@router.get("/preview")
def daily_summary_preview(
    restaurant_id: str = Query(...),
    date: str | None = Query(default=None, description="YYYY-MM-DD, default oggi (Roma)"),
):
    try:
        day = datetime.date.fromisoformat(date) if date else datetime.datetime.now(tz=ROME).date()
    except ValueError:
        raise HTTPException(status_code=400, detail="date deve essere YYYY-MM-DD")
    restaurant = base44_client.get_restaurant_by_id(restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail=f"Restaurant {restaurant_id} non trovato su Base44")
    try:
        return preview_summary(restaurant, day)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Order Base44 non leggibili: {type(e).__name__}")
