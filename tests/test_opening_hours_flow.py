"""Corte del Sole (19:00-22:00 tutti i giorni, preparazione minima 30 minuti).

L'agente risponde a qualsiasi ora: gli orari limitano solo l'ora di ritiro e l'ora
della prenotazione. Quando oggi non restano orari di ritiro l'agente chiede se il
cliente vuole ordinare per domani, senza spostare l'ordine da solo.
Giovedì 1 ottobre 2026, ora di Roma.
"""
import datetime
import json
import types
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sqlmodel import SQLModel, Session, create_engine, select
from sqlmodel.pool import StaticPool

import app.routes.chat as chat_module
from app.models import ConversationSession, MenuItem
from app.schemas import ChatRequest
from app.services import conversation_service as service

ROME = ZoneInfo("Europe/Rome")
THURSDAY = datetime.date(2026, 10, 1)
FRIDAY = datetime.date(2026, 10, 2)
CDS_ID = "rest-cds"
CDS = {
    "id": CDS_ID,
    "name": "Corte Del Sole",
    "opening_hours": {day: "19:00-22:00" for day in (
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")},
    "min_prep_minutes": 30,
}
NOTHING = {"intent": "unknown", "customer_name": None, "pickup_time": None, "items": []}
MARGHERITA = {
    "pizza_name": "Margherita", "pizza_type": "Normale", "dough_type": "classica",
    "quantity": 1, "size": "normale", "temperature": "", "order_unit": "",
    "add_ingredients": [], "remove_ingredients": [],
}


def _frozen_datetime_module(now: datetime.datetime):
    class FrozenDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    class FrozenDate(datetime.date):
        @classmethod
        def today(cls):
            return now.date()

    module = types.SimpleNamespace(
        **{name: getattr(datetime, name) for name in dir(datetime) if not name.startswith("__")}
    )
    module.datetime = FrozenDateTime
    module.date = FrozenDate
    return module


class _CorteHarness(unittest.TestCase):
    """Chat reale con LLM, Base44 e orologio simulati."""

    now = datetime.datetime(2026, 10, 1, 16, 0, tzinfo=ROME)
    restaurant = CDS

    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.addCleanup(self.session.close)
        self.session.add(MenuItem(
            name="Margherita", category="rosse", pizza_type="Normale", price=7.0,
            restaurant_id=CDS_ID,
        ))
        self.session.add(ConversationSession(
            session_id="s-1", customer_phone="+393331234567", items_json="[]",
            state="collecting_items", completed=False, restaurant_id=CDS_ID,
        ))
        self.session.commit()

        self.next_extraction = NOTHING
        self.saved = []

        def fake_extract(*args, **kwargs):
            result, self.next_extraction = self.next_extraction, NOTHING
            return result

        def fake_save(**kwargs):
            self.saved.append(kwargs)
            return {"id": "b44-1", "order_number": 1}

        frozen = _frozen_datetime_module(self.now)
        load = lambda restaurant_id="": dict(self.restaurant)  # noqa: E731
        for p in (
            patch.object(service, "datetime", frozen),
            patch.object(chat_module, "datetime", frozen),
            patch.object(service, "load_restaurant", side_effect=load),
            patch.object(chat_module, "load_restaurant", side_effect=load),
            patch.object(chat_module, "ensure_restaurant_config", return_value=True),
            patch.object(chat_module, "is_agent_active", return_value=True),
            patch.object(chat_module, "detect_reservation_intent", side_effect=lambda m: "prenot" in m.lower()),
            patch.object(chat_module, "get_proposable_menu", return_value=[{"name": "Margherita"}]),
            patch.object(chat_module, "get_sold_out_item_names", return_value=set()),
            patch.object(chat_module, "load_doughs", return_value=[]),
            patch.object(chat_module, "is_dough_available", return_value=True),
            patch.object(chat_module, "extract_order_from_text", side_effect=fake_extract),
            patch.object(chat_module, "save_order_to_base44", side_effect=fake_save),
            patch.object(chat_module, "_schedule_order_side_effect_job", return_value=None),
            patch.object(chat_module, "_db_engine", self.engine),
        ):
            p.start()
            self.addCleanup(p.stop)

    def say(self, message, extraction=None):
        if extraction is not None:
            self.next_extraction = extraction
        return chat_module.chat(ChatRequest(session_id="s-1", message=message), self.session)

    def conversation(self):
        self.session.expire_all()
        return self.session.exec(select(ConversationSession)).one()

    def order(self, message="una margherita", **extra):
        return self.say(message, {**NOTHING, "intent": "add_items", "items": [dict(MARGHERITA)], **extra})


class CallBeforeOpeningTests(_CorteHarness):
    """Chiamata alle 16:00: si ordina e si prenota per stasera."""

    def test_takeaway_for_tonight_and_recap_says_the_day(self):
        r = self.order("una margherita per le 8, sono Giacomo", customer_name="Giacomo", pickup_time="20:00")
        self.assertEqual(r.state, "awaiting_confirmation")
        self.assertEqual(r.response_message, "Allora: una margherita, per stasera alle 20, a nome Giacomo. Confermo?")
        self.say("sì")
        self.assertIsNone(self.saved[0].get("pickup_date"))  # ritiro in giornata

    def test_as_soon_as_possible_means_opening_time(self):
        self.order(customer_name="Giacomo")
        r = self.say("prima possibile", {**NOTHING, "intent": "set_pickup_time", "pickup_time": "prima_possibile"})
        self.assertEqual(r.state, "awaiting_confirmation")
        self.assertIn("per stasera alle 19", r.response_message)

    def test_time_before_opening_is_refused_with_first_pickup(self):
        self.order(customer_name="Giacomo")
        r = self.say("alle 18", {**NOTHING, "intent": "set_pickup_time", "pickup_time": "18:00"})
        self.assertEqual(r.state, "collecting_pickup_time")
        self.assertIn("Il prossimo orario disponibile è le 19:00", r.response_message)

    def test_reservation_for_tonight(self):
        r = self.say("vorrei prenotare un tavolo")
        self.assertEqual(r.state, "collecting_reservation_date")
        r = self.say("stasera")
        self.assertEqual((r.state, r.response_message), ("collecting_reservation_time", "A che ora?"))
        r = self.say("alle 20")
        self.assertEqual(r.state, "collecting_reservation_party")


class PrepTimeTests(_CorteHarness):
    """Alle 19:40 il primo ritiro possibile è 20:15 (40 + 30 minuti, arrotondato)."""

    now = datetime.datetime(2026, 10, 1, 19, 40, tzinfo=ROME)

    def test_pickup_closer_than_prep_time_is_refused(self):
        self.order(customer_name="Giacomo")
        r = self.say("alle 19:45", {**NOTHING, "intent": "set_pickup_time", "pickup_time": "19:45"})
        self.assertEqual(r.state, "collecting_pickup_time")
        self.assertIn("Il prossimo orario disponibile è le 20:15", r.response_message)

    def test_min_prep_minutes_is_per_restaurant_with_default(self):
        self.assertEqual(service.get_min_prep_minutes(CDS_ID), 30)
        self.assertEqual(service.earliest_pickup_minutes_today(CDS_ID), 20 * 60 + 15)
        with patch.object(service, "load_restaurant", side_effect=lambda restaurant_id="": {
            **CDS, "min_prep_minutes": None,
        }):
            self.assertEqual(service.get_min_prep_minutes(CDS_ID), service.DEFAULT_MIN_PREP_MINUTES)
            self.assertEqual(service.earliest_pickup_minutes_today(CDS_ID), 20 * 60)


class CallAfterLastPickupTests(_CorteHarness):
    """Chiamata alle 23:00: per l'asporto l'agente chiede se ordinare per domani."""

    now = datetime.datetime(2026, 10, 1, 23, 0, tzinfo=ROME)

    def test_agent_asks_for_tomorrow_and_keeps_the_order(self):
        r = self.order()
        self.assertEqual(r.state, "confirming_next_day")
        self.assertEqual(r.response_message, "Per stasera la cucina è chiusa, vuole ordinare per domani?")
        self.assertIsNone(self.conversation().pickup_date)  # nessuno spostamento automatico

        r = self.say("sì")
        self.assertEqual(r.state, "collecting_name")
        self.assertEqual(r.response_message, "Perfetto, il ritiro è per domani, venerdì. A che nome?")
        self.assertEqual(self.conversation().pickup_date, FRIDAY.isoformat())

        r = self.say("Giacomo")
        self.assertEqual(r.response_message, "Il ritiro è per domani, venerdì. A che ora passa?")
        r = self.say("alle 8")
        self.assertEqual(r.response_message, "Allora: una margherita, per domani venerdì alle 20, a nome Giacomo. Confermo?")
        self.say("sì")
        self.assertEqual(self.saved[0]["pickup_date"], FRIDAY.isoformat())
        self.assertEqual(self.saved[0]["pickup_time"], "20:00")

    def test_customer_says_no_and_the_call_closes(self):
        self.order()
        r = self.say("no grazie")
        self.assertEqual(r.state, "completed")
        self.assertEqual(r.response_message, "Va bene, nessun problema. La aspettiamo un'altra volta, buona serata!")
        self.assertEqual(self.saved, [])

    def test_unclear_answer_asks_again(self):
        self.order()
        r = self.say("boh")
        self.assertEqual(r.state, "confirming_next_day")
        self.assertIn("vuole ordinare per domani?", r.response_message)

    def test_reservation_for_tonight_asks_another_day(self):
        self.say("vorrei prenotare un tavolo")
        r = self.say("stasera")
        self.assertEqual(r.state, "collecting_reservation_date")
        self.assertEqual(r.response_message, "Per stasera non è più possibile prenotare. Per quale altro giorno?")
        r = self.say("sabato")
        self.assertEqual((r.state, r.response_message), ("collecting_reservation_time", "A che ora?"))

    def test_reservation_any_future_day(self):
        self.say("vorrei prenotare un tavolo")
        r = self.say("il 21 maggio")
        self.assertEqual(r.state, "collecting_reservation_time")
        self.assertEqual(json.loads(self.conversation().reservation_json)["date"], "2027-05-21")
        r = self.say("alle 20")
        self.assertEqual(r.state, "collecting_reservation_party")


class CallAfterMidnightTests(_CorteHarness):
    """Alle 00:30 di venerdì (ora di Roma) è già il nuovo giorno: si ordina per stasera."""

    now = datetime.datetime(2026, 10, 2, 0, 30, tzinfo=ROME)

    def test_order_at_half_past_midnight_is_for_tonight(self):
        r = self.order("una margherita per le 8, sono Giacomo", customer_name="Giacomo", pickup_time="08:00")
        self.assertEqual(r.state, "awaiting_confirmation")
        self.assertEqual(r.response_message, "Allora: una margherita, per stasera alle 20, a nome Giacomo. Confermo?")

    def test_reservation_dates_use_rome_time(self):
        self.say("vorrei prenotare un tavolo")
        self.say("domani")
        self.assertEqual(json.loads(self.conversation().reservation_json)["date"], "2026-10-03")


class ReservationDateTextTests(_CorteHarness):
    def test_recap_date_always_names_the_day(self):
        fmt = chat_module._format_reservation_date_it
        self.assertEqual(fmt(THURSDAY.isoformat()), "stasera, giovedì 1 ottobre")
        self.assertEqual(fmt(FRIDAY.isoformat()), "domani, venerdì 2 ottobre")
        self.assertEqual(fmt("2027-05-21"), "venerdì 21 maggio 2027")


if __name__ == "__main__":
    unittest.main()
