"""Riepilogo giornaliero degli ordini per il titolare (thread avviato da main.py all'avvio).

Per ogni locale con Restaurant.daily_summary_enabled, all'ora daily_summary_time
("HH:MM", ora di Roma; vuoto o non valido → 11:00 con warning) e solo nei giorni di
apertura, invia a owner_notify_phone (ripiego OWNER_PHONE) un SMS con gli ordini
con ritiro oggi, esclusi annullati e ritirati.

La data dell'ultimo invio è salvata su Base44 (Restaurant.last_daily_summary_date,
"YYYY-MM-DD"): il DB locale non sopravvive ai deploy. Un locale con la data di oggi
viene saltato, così un riavvio non manda un secondo riepilogo; se il servizio riparte
dopo l'orario e il riepilogo di oggi non è partito, parte subito.
"""
import datetime
import os
import re
import threading
import time
from collections import defaultdict
from zoneinfo import ZoneInfo

from app.privacy import mask_phone
from app.services import base44_client
from app.services.conversation_service import (
    _MONTH_IT,
    _WEEKDAY_IT,
    _WEEKDAY_NAMES,
    _build_pizza_lines,
    _parse_opening_range,
    _send_twilio_message,
    owner_phone_for_restaurant,
    restaurant_flag,
)

ROME = ZoneInfo("Europe/Rome")
DEFAULT_SUMMARY_TIME = (11, 0)
SMS_MAX_CHARS = 1500
# Il thread rilegge i Restaurant almeno ogni 15 minuti: orari e attivazioni
# cambiati su Base44 valgono senza riavvio, e un invio fallito si ritenta.
MAX_SLEEP_SECONDS = 15 * 60
MAX_SEND_ATTEMPTS_PER_DAY = 3
# Base44 Order.status: "completato" è l'ordine ritirato.
EXCLUDED_STATUSES = {
    "annullato", "annullata", "cancellato", "cancellata",
    "ritirato", "ritirata", "completato", "completata", "consegnato", "consegnata",
}

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_PORTION_LABEL = {"piena": ("intero", "interi"), "mezza": ("mezzo", "mezzi")}
_TEMPERATURE_LABEL = {"calda": ("caldo", "caldi"), "fredda": ("freddo", "freddi")}
_PORTION_ORDER = {"piena": 0, "mezza": 1, None: 2}
_TEMPERATURE_ORDER = {"calda": 0, "fredda": 1, None: 2}

# Invii riusciti in questo processo, per (restaurant_id, data): se la scrittura di
# last_daily_summary_date su Base44 fallisce il riepilogo non riparte a ogni controllo.
_sent_in_process: set[tuple[str, str]] = set()
_attempts: dict[tuple[str, str], int] = defaultdict(int)


# ── Configurazione del locale ─────────────────────────────────────────────────

def is_daily_summary_enabled(restaurant: dict) -> bool:
    """Restaurant.daily_summary_enabled: null o assente → False."""
    return restaurant_flag(restaurant, "daily_summary_enabled", default=False)


def summary_time(restaurant: dict) -> tuple[int, int]:
    """(ora, minuti) da Restaurant.daily_summary_time; vuoto o non valido → 11:00 con warning."""
    raw = restaurant.get("daily_summary_time")
    match = _TIME_RE.match(str(raw or "").strip())
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour < 24 and minute < 60:
            return hour, minute
    print(
        f"[DailySummary] WARNING daily_summary_time {'vuoto' if not raw else 'non valido'} ({raw!r}) "
        f"per restaurant_id={restaurant.get('id')!r}: uso {DEFAULT_SUMMARY_TIME[0]:02d}:{DEFAULT_SUMMARY_TIME[1]:02d}"
    )
    return DEFAULT_SUMMARY_TIME


def is_open_on(restaurant: dict, day: datetime.date) -> bool:
    """True se il locale apre quel giorno secondo opening_hours. Senza orari: aperto
    (come get_next_open_day)."""
    opening_hours = restaurant.get("opening_hours")
    if not opening_hours or not isinstance(opening_hours, dict):
        return True
    return _parse_opening_range(opening_hours.get(_WEEKDAY_NAMES[day.weekday()], "")) is not None


# ── Ordini ────────────────────────────────────────────────────────────────────

def fetch_orders_for_pickup(restaurant_id: str, day: str) -> list[dict]:
    """Order del locale con ritiro `day`: pickup_date = day, oppure senza pickup_date
    e order_date = day (ordini per lo stesso giorno). Esclusi annullati, ritirati e
    record di esempio. Solleva se Base44 non risponde."""
    by_pickup = base44_client.query_entities("Order", {"restaurant_id": restaurant_id, "pickup_date": day})
    same_day = base44_client.query_entities("Order", {"restaurant_id": restaurant_id, "order_date": day})
    orders: dict[str, dict] = {}
    for order in by_pickup + [o for o in same_day if not o.get("pickup_date")]:
        orders[order.get("id") or str(id(order))] = order
    return [
        o for o in orders.values()
        if str(o.get("status") or "").strip().lower() not in EXCLUDED_STATUSES and not o.get("is_sample")
    ]


def _pickup_sort_key(order: dict) -> tuple[int, str]:
    match = _TIME_RE.match(str(order.get("pickup_time") or "").strip())
    minutes = int(match.group(1)) * 60 + int(match.group(2)) if match else 24 * 60
    return minutes, str(order.get("order_number") or "")


def _needs_review(order: dict) -> bool:
    return bool(order.get("needs_review"))


def _slice_unit(item: dict) -> str:
    """"kg", "tranci", "" (al taglio senza quantità) o "pezzi" (pizze intere)."""
    if item.get("sale_unit") != "kg":
        return "pezzi"
    unit = item.get("order_unit")
    if unit in ("kg", "tranci"):
        return unit
    return "kg" if float(item.get("quantity") or 0) > 0 else ""


def _portion(item: dict) -> str | None:
    portion = item.get("portion") or item.get("size")
    return portion if portion in _PORTION_LABEL else None


def _temperature(item: dict) -> str | None:
    temperature = item.get("temperature")
    return temperature if temperature in _TEMPERATURE_LABEL else None


def _format_kg(kg: float) -> str:
    return f"{round(kg, 3):g}".replace(".", ",") + " kg"


def _kg_part(by_temperature: dict[str | None, float]) -> str:
    total = sum(by_temperature.values())
    keys = sorted(by_temperature, key=_TEMPERATURE_ORDER.get)
    if len(keys) == 1:
        label = keys[0] or "caldo/freddo da definire"
        return f"{_format_kg(total)} {label}"
    detail = ", ".join(f"{key or 'da definire'} {_format_kg(by_temperature[key])}" for key in keys)
    return f"{_format_kg(total)} ({detail})"


def _slices_part(count: int, portion: str | None, temperature: str | None) -> str:
    plural = count != 1
    portion_str = _PORTION_LABEL[portion][plural] if portion else "porzione da definire"
    temperature_str = _TEMPERATURE_LABEL[temperature][plural] if temperature else "caldo/freddo da definire"
    return f"{count} {'tranci' if plural else 'trancio'} {portion_str} {temperature_str}"


def flavour_totals(orders: list[dict]) -> list[str]:
    """Righe "Bufala: 3,5 kg (calda 2 kg, fredda 1,5 kg) · 6 tranci interi freddi",
    una per prodotto in ordine alfabetico; unità e temperature non si sommano tra loro."""
    kg: dict[str, dict[str | None, float]] = defaultdict(lambda: defaultdict(float))
    slices: dict[str, dict[tuple[str | None, str | None], int]] = defaultdict(lambda: defaultdict(int))
    pieces: dict[str, int] = defaultdict(int)
    undefined: dict[str, int] = defaultdict(int)
    for order in orders:
        for item in order.get("items") or []:
            name = item.get("pizza_name") or "?"
            unit = _slice_unit(item)
            if unit == "kg":
                kg[name][_temperature(item)] += float(item.get("quantity") or 0)
            elif unit == "tranci":
                count = int(item.get("slices") or round(float(item.get("quantity") or 0)))
                slices[name][(_portion(item), _temperature(item))] += count
            elif unit == "pezzi":
                pieces[name] += int(item.get("quantity") or 1)
            else:
                undefined[name] += 1

    lines = []
    for name in sorted(set(kg) | set(slices) | set(pieces) | set(undefined), key=str.lower):
        parts = []
        if name in kg:
            parts.append(_kg_part(kg[name]))
        for (portion, temperature) in sorted(
            slices.get(name, {}), key=lambda k: (_PORTION_ORDER[k[0]], _TEMPERATURE_ORDER[k[1]]),
        ):
            parts.append(_slices_part(slices[name][(portion, temperature)], portion, temperature))
        if name in pieces:
            parts.append(f"{pieces[name]} {'pezzo' if pieces[name] == 1 else 'pezzi'}")
        if name in undefined:
            parts.append(f"{undefined[name]} con quantità da definire")
        lines.append(f"{name}: " + " · ".join(parts))
    return lines


# ── Testo ─────────────────────────────────────────────────────────────────────

def _order_head(order: dict) -> str:
    pickup = order.get("pickup_time") or "orario da definire"
    return f"{pickup} {order.get('customer_name') or '?'} {order.get('customer_phone') or ''}".rstrip()


def _item_lines(order: dict) -> list[str]:
    # Su Base44 la porzione dei tranci può stare solo in "portion".
    items = [{**item, "size": item.get("size") or item.get("portion")} for item in order.get("items") or []]
    return _build_pizza_lines(items)


def build_summary_blocks(day: datetime.date, orders: list[dict]) -> list[tuple[str, str]]:
    """Il riepilogo come elenco di (separatore, blocco): un blocco non si spezza
    mai tra due SMS, e un ordine è sempre un solo blocco."""
    count = len(orders)
    header = (
        f"Ordini di {_WEEKDAY_IT[day.weekday()]} {day.day} {_MONTH_IT[day.month - 1]} "
        f"— {count} {'ordine' if count == 1 else 'ordini'}"
    )
    blocks: list[tuple[str, str]] = [("", header)]
    if not orders:
        blocks.append(("\n", "Nessun ordine per oggi"))
        return blocks

    ordered = sorted(orders, key=_pickup_sort_key)

    to_verify = [o for o in ordered if _needs_review(o)]
    for i, order in enumerate(to_verify):
        line = f"{_order_head(order)} — {order.get('review_reason') or 'motivo non indicato'}"
        blocks.append(("\n\n", f"DA VERIFICARE\n{line}") if i == 0 else ("\n", line))

    for i, line in enumerate(flavour_totals(ordered)):
        blocks.append(("\n\n", f"TOTALI PER GUSTO\n{line}") if i == 0 else ("\n", line))

    for i, order in enumerate(ordered):
        head = _order_head(order) + (" (DA VERIFICARE)" if _needs_review(order) else "")
        text = "\n".join([head, *_item_lines(order)])
        blocks.append(("\n\n", f"RITIRI\n{text}") if i == 0 else ("\n\n", text))
    return blocks


def split_messages(blocks: list[tuple[str, str]], limit: int = SMS_MAX_CHARS) -> list[str]:
    """Unisce i blocchi in SMS da al massimo `limit` caratteri, numerati "(1/3)" se
    sono più di uno. Un blocco più lungo del limite (ordine enorme) si divide per righe."""
    numbering_room = len("(99/99)\n")
    room = limit - numbering_room

    units: list[tuple[str, str]] = []
    for sep, block in blocks:
        if len(block) <= room:
            units.append((sep, block))
            continue
        for j, line in enumerate(block.split("\n")):
            units.append((sep if j == 0 else "\n", line[:room]))

    messages: list[str] = []
    current = ""
    for sep, block in units:
        if current and len(current) + len(sep) + len(block) > room:
            messages.append(current)
            current = ""
        current = f"{current}{sep}{block}" if current else block
    if current:
        messages.append(current)

    if len(messages) == 1:
        return messages
    return [f"({i}/{len(messages)})\n{message}" for i, message in enumerate(messages, start=1)]


def build_summary_messages(day: datetime.date, orders: list[dict]) -> list[str]:
    return split_messages(build_summary_blocks(day, orders))


def preview_summary(restaurant: dict, day: datetime.date) -> dict:
    """Riepilogo di `day` per il locale, senza inviarlo né scrivere su Base44."""
    rid = restaurant.get("id", "")
    orders = fetch_orders_for_pickup(rid, day.isoformat())
    messages = build_summary_messages(day, orders)
    hour, minute = summary_time(restaurant)
    for i, message in enumerate(messages, start=1):
        print(f"[DailySummary] Anteprima {rid!r} {day} parte {i}/{len(messages)}:\n{message}")
    return {
        "restaurant_id": rid,
        "date": day.isoformat(),
        "enabled": is_daily_summary_enabled(restaurant),
        "time": f"{hour:02d}:{minute:02d}",
        "open": is_open_on(restaurant, day),
        "orders": len(orders),
        "messages": messages,
    }


# ── Invio ─────────────────────────────────────────────────────────────────────

def _send_sms(to: str, body: str) -> str:
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    sms_from = (os.getenv("TWILIO_NUMBER") or "").removeprefix("whatsapp:")
    if not account_sid or not auth_token or not sms_from:
        return "skip:configurazione_mancante"
    return _send_twilio_message(to, body, sms_from, account_sid, auth_token)


def send_summary(restaurant: dict, day: datetime.date) -> bool:
    """Invia il riepilogo di oggi e salva last_daily_summary_date. True se gli SMS sono partiti."""
    rid = restaurant.get("id", "")
    name = restaurant.get("name") or rid
    phone = owner_phone_for_restaurant(restaurant, rid)
    if not phone:
        print(f"[DailySummary] ERRORE: nessun numero del titolare per {name!r}, riepilogo non inviato")
        return False

    orders = fetch_orders_for_pickup(rid, day.isoformat())
    messages = build_summary_messages(day, orders)
    statuses = [_send_sms(phone, message) for message in messages]
    print(
        f"[DailySummary] {name!r} {day}: {len(orders)} ordini, {len(messages)} SMS a {mask_phone(phone)}: {statuses}"
    )
    if not all(status.startswith("inviato") for status in statuses):
        print(f"[DailySummary] ERRORE: riepilogo non inviato del tutto per {name!r}")
        return False

    _sent_in_process.add((rid, day.isoformat()))
    if base44_client.update_restaurant({"last_daily_summary_date": day.isoformat()}, restaurant_id=rid) is None:
        print(f"[DailySummary] ERRORE: last_daily_summary_date non salvata su Base44 per {name!r}")
    return True


def _is_due(restaurant: dict, now: datetime.datetime) -> bool:
    rid = restaurant.get("id", "")
    today = now.date()
    if not is_daily_summary_enabled(restaurant):
        return False
    if restaurant.get("last_daily_summary_date") == today.isoformat() or (rid, today.isoformat()) in _sent_in_process:
        return False
    if not is_open_on(restaurant, today):
        return False
    hour, minute = summary_time(restaurant)
    return now >= now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def run_due_summaries(now: datetime.datetime | None = None) -> list[dict] | None:
    """Invia i riepiloghi dovuti (anche quelli in ritardo, es. dopo un riavvio) e
    ritorna i Restaurant letti. None se l'elenco non è leggibile da Base44."""
    now = now or datetime.datetime.now(tz=ROME)
    restaurants = base44_client.get_all_restaurants_or_none()
    if restaurants is None:
        print("[DailySummary] Elenco ristoranti Base44 non disponibile")
        return None
    for restaurant in restaurants:
        if not _is_due(restaurant, now):
            continue
        key = (restaurant.get("id", ""), now.date().isoformat())
        if _attempts[key] >= MAX_SEND_ATTEMPTS_PER_DAY:
            continue
        _attempts[key] += 1
        try:
            send_summary(restaurant, now.date())
        except Exception as e:
            print(f"[DailySummary] Errore inatteso per {restaurant.get('name')!r}: {type(e).__name__}: {e}")
        if _attempts[key] >= MAX_SEND_ATTEMPTS_PER_DAY and key not in _sent_in_process:
            print(f"[DailySummary] ERRORE: riepilogo di {key[1]} non inviato dopo {MAX_SEND_ATTEMPTS_PER_DAY} tentativi")
    return restaurants


def _seconds_until_next_check(now: datetime.datetime, restaurants: list[dict] | None) -> float:
    """Fino al prossimo orario di riepilogo di un locale attivo, al massimo 15 minuti."""
    sleep = float(MAX_SLEEP_SECONDS)
    for restaurant in restaurants or []:
        if not is_daily_summary_enabled(restaurant):
            continue
        hour, minute = summary_time(restaurant)
        next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if next_run <= now:
            next_run += datetime.timedelta(days=1)
        sleep = min(sleep, (next_run - now).total_seconds())
    return max(sleep, 1.0)


def _daily_summary_worker() -> None:
    while True:
        now = datetime.datetime.now(tz=ROME)
        try:
            restaurants = run_due_summaries(now)
        except Exception as e:
            print(f"[DailySummary] Errore inatteso: {type(e).__name__}: {e}")
            restaurants = None
        time.sleep(_seconds_until_next_check(datetime.datetime.now(tz=ROME), restaurants))


def start_daily_summary_thread() -> None:
    thread = threading.Thread(target=_daily_summary_worker, name="daily-summary", daemon=True)
    thread.start()
    print("[DailySummary] Thread avviato")
