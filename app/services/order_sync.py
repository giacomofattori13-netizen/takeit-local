"""Creazione degli Order su Base44 con numero progressivo per ristorante e per giorno.

Il numero è definitivo quando save_order_to_base44 ritorna: l'Order esiste già su
Base44 con quel numero e il controllo dei doppioni è stato fatto. Il chiamante lo
usa solo dopo, quindi un numero non ancora verificato non arriva mai al cliente.

Unicità:
- un lock per ristorante serializza "leggi il massimo di oggi → crea l'Order" nello
  stesso processo, e il lock si rilascia solo quando il record esiste su Base44;
- dopo la creazione si rileggono gli Order con lo stesso numero: se ce n'è più di
  uno (es. più istanze del backend) tiene il numero il più vecchio e gli altri ne
  prendono uno nuovo.
"""
import datetime
import threading
from zoneinfo import ZoneInfo

from app.privacy import mask_name, mask_phone
from app.services import base44_client
from app.services.conversation_service import _PIZZA_TYPE_TO_DOUGH

ROME_TZ = ZoneInfo("Europe/Rome")
MAX_RENUMBER_ATTEMPTS = 5
# Il salvataggio avviene durante il turno di conferma (timeout voce 25 s): ogni
# chiamata a Base44 deve restare breve, altrimenti l'ordine passa al retry.
BASE44_TIMEOUT_SECONDS = 4.0

_restaurant_locks: dict[str, threading.Lock] = {}
_restaurant_locks_guard = threading.Lock()


class Base44OrderError(RuntimeError):
    """L'Order non è stato salvato (o numerato) su Base44."""


def rome_today() -> str:
    return datetime.datetime.now(ROME_TZ).date().isoformat()


def _restaurant_lock(restaurant_id: str) -> threading.Lock:
    with _restaurant_locks_guard:
        return _restaurant_locks.setdefault(restaurant_id, threading.Lock())


def _creation_key(order: dict) -> tuple[str, str]:
    # I record appena creati hanno il suffisso Z, quelli riletti no.
    return ((order.get("created_date") or "").rstrip("Z"), order.get("id") or "")


def _next_number(orders: list[dict]) -> int:
    return max((int(o.get("order_number") or 0) for o in orders), default=0) + 1


def build_order_items(items: list[dict]) -> list[dict]:
    """Item nel formato di Order.items su Base44.

    Per le voci al kg temperatura e porzione compaiono solo se il cliente le ha
    dette: nessun valore predefinito (un campo assente finisce in needs_review).
    """
    base44_items = []
    for item in items:
        is_kg = item.get("sale_unit") == "kg"
        size = item.get("size") or "normale"
        b44_item = {
            "pizza_name": item["pizza_name"],
            "quantity": item["quantity"],
            "sale_unit": item.get("sale_unit", "piece"),
            "dough_type": (
                item.get("dough_type")
                or _PIZZA_TYPE_TO_DOUGH.get(item.get("pizza_type", ""), "classica")
            ),
            "add_ingredients": item.get("add_ingredients", []),
            "remove_ingredients": item.get("remove_ingredients", []),
            "base_price": item.get("base_price", 0.0),
            "extras_price": item.get("extras_price", 0.0),
            "total_price": item.get("total_price", 0.0),
        }
        if not is_kg:
            b44_item["size"] = size
        elif size in ("piena", "mezza"):
            b44_item["size"] = size
            b44_item["portion"] = size
        if is_kg and item.get("temperature") in ("calda", "fredda"):
            b44_item["temperature"] = item["temperature"]
        base44_items.append(b44_item)
    return base44_items


def _list_orders(restaurant_id: str, order_date: str, **extra) -> list[dict]:
    return base44_client.query_entities(
        "Order",
        {"restaurant_id": restaurant_id, "order_date": order_date, **extra},
        timeout=BASE44_TIMEOUT_SECONDS,
    )


def _ensure_unique_number(record: dict, restaurant_id: str, order_date: str) -> dict:
    """Rinumera `record` finché è l'unico (o il più vecchio) con il suo numero."""
    for _ in range(MAX_RENUMBER_ATTEMPTS):
        number = int(record.get("order_number") or 0)
        same = _list_orders(restaurant_id, order_date, order_number=number)
        if not any(o.get("id") == record["id"] for o in same):
            same.append(record)  # il record appena creato può non essere ancora visibile
        oldest = min(same, key=_creation_key)
        if number > 0 and oldest.get("id") == record["id"]:
            return {"id": record["id"], "order_number": number}

        new_number = _next_number(_list_orders(restaurant_id, order_date) + [record])
        print(
            f"[OrderSync] Numero #{number} già usato oggi da id={oldest.get('id')!r}: "
            f"id={record['id']!r} → #{new_number}"
        )
        base44_client.update_entity(
            "Order", record["id"], {"order_number": new_number}, timeout=BASE44_TIMEOUT_SECONDS,
        )
        record = {**record, "order_number": new_number}
    raise Base44OrderError(
        f"numero d'ordine non univoco dopo {MAX_RENUMBER_ATTEMPTS} tentativi (id={record['id']!r})"
    )


def save_order_to_base44(
    *,
    session_id: str | None,
    restaurant_id: str,
    customer_name: str,
    customer_phone: str | None,
    pickup_time: str,
    ai_confidence: float,
    items: list[dict],
    pickup_date: str | None = None,
    order_date: str | None = None,
    review_reasons: list[str] | None = None,
) -> dict:
    """Crea l'Order su Base44 e ritorna {"id", "order_number"} con numero definitivo.

    Idempotente per session_id: un retry dopo una risposta persa non crea un secondo
    Order. Solleva Base44OrderError (o l'errore HTTP) se l'ordine non è salvato.
    """
    if not base44_client.base44_token():
        raise Base44OrderError("BASE44_TOKEN non configurato")
    if not restaurant_id:
        raise Base44OrderError("restaurant_id mancante")
    order_date = order_date or rome_today()

    with _restaurant_lock(restaurant_id):
        existing = (
            base44_client.query_entities(
                "Order", {"session_id": session_id}, timeout=BASE44_TIMEOUT_SECONDS,
            )
            if session_id else []
        )
        if existing:
            record = existing[0]
            restaurant_id = record.get("restaurant_id") or restaurant_id
            order_date = record.get("order_date") or order_date
            print(f"[OrderSync] Order già presente per session={session_id!r}: id={record.get('id')!r}")
        else:
            reasons = list(review_reasons or [])
            if ai_confidence < 0.8:
                reasons.insert(0, "Bassa confidenza AI")
            base44_items = build_order_items(items)
            payload = {
                "order_number": _next_number(_list_orders(restaurant_id, order_date)),
                "order_date": order_date,
                "restaurant_id": restaurant_id,
                "customer_name": customer_name,
                "customer_phone": customer_phone,
                "status": "nuovo",
                "source": "telefono",
                "pickup_time": pickup_time,
                "total_amount": round(sum(i.get("total_price", 0.0) for i in items), 2),
                "ai_confidence": ai_confidence,
                "needs_review": bool(reasons),
                "review_reason": "; ".join(reasons) if reasons else None,
                "items": base44_items,
            }
            if session_id:
                payload["session_id"] = session_id
            if pickup_date:
                payload["pickup_date"] = pickup_date
            print(
                f"[OrderSync] Creo Order #{payload['order_number']} restaurant={restaurant_id!r} "
                f"date={order_date} customer={mask_name(customer_name)} "
                f"phone={mask_phone(customer_phone)} items={len(base44_items)} "
                f"total={payload['total_amount']} pickup_date={pickup_date!r}"
            )
            record = base44_client.create_entity("Order", payload, timeout=BASE44_TIMEOUT_SECONDS)
            if not record.get("id"):
                raise Base44OrderError("risposta Base44 senza id")

        result = _ensure_unique_number(record, restaurant_id, order_date)

    print(f"[OrderSync] Order id={result['id']!r} numero definitivo #{result['order_number']}")
    return result
