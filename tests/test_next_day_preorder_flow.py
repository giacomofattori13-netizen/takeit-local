"""Flusso Pizza a Pezzi: preordine per il prossimo giorno aperto, quantità a peso
o a tranci, porzione dei tranci, caldo/freddo, nome e ora.

Il tempo è congelato a domenica 4 ottobre 2026, 15:00 (Roma): il lunedì è chiuso,
quindi il ritiro va a martedì 6 ottobre.
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
from app.models import ConversationSession, MenuItem, OrderSideEffect
from app.schemas import ChatRequest
from app.services import conversation_service as service

ROME = ZoneInfo("Europe/Rome")
FROZEN_NOW = datetime.datetime(2026, 10, 4, 15, 0, tzinfo=ROME)  # domenica
TUESDAY = datetime.date(2026, 10, 6)
PAP_ID = "rest-pap"
CDS_ID = "rest-cds"
PAP_HOURS = {
    "monday": "closed",
    **{day: "17:00-21:00" for day in ("tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")},
}
RESTAURANTS = {
    PAP_ID: {
        "id": PAP_ID, "name": "Pizza a Pezzi", "opening_hours": PAP_HOURS,
        "reservations_enabled": False, "phone_orders_next_day_only": True,
        "price_per_kg_cold": 18.5, "price_per_kg_hot": 19.9,
    },
    CDS_ID: {
        "id": CDS_ID, "name": "Corte del Sole",
        "opening_hours": {day: "00:00-23:59" for day in PAP_HOURS},
        "reservations_enabled": False,
    },
}
NOTHING = {"intent": "unknown", "customer_name": None, "pickup_time": None, "items": []}


def _bufala(**extra):
    """Bufala al taglio come la estrae l'LLM; di default solo il gusto."""
    return {
        "pizza_name": "Bufala", "pizza_type": "Normale", "dough_type": "classica",
        "quantity": 0.0, "order_unit": "", "size": "normale", "temperature": "",
        "add_ingredients": [], "remove_ingredients": [], **extra,
    }


def _bufala_kg(kg=0.5, **extra):
    return _bufala(order_unit="kg", quantity=kg, **extra)


def _bufala_slices(count, **extra):
    return _bufala(order_unit="tranci", quantity=count, **extra)


class _FrozenDateTime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW.astimezone(tz) if tz else FROZEN_NOW.replace(tzinfo=None)


class _FrozenDate(datetime.date):
    @classmethod
    def today(cls):
        return FROZEN_NOW.date()


_FROZEN_DATETIME_MODULE = types.SimpleNamespace(
    **{name: getattr(datetime, name) for name in dir(datetime) if not name.startswith("__")}
)
_FROZEN_DATETIME_MODULE.datetime = _FrozenDateTime
_FROZEN_DATETIME_MODULE.date = _FrozenDate


class _FlowHarness(unittest.TestCase):
    """Chat reale con LLM, Base44 e orologio simulati."""

    restaurant_id = PAP_ID

    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.addCleanup(self.session.close)
        self.session.add(MenuItem(
            name="Bufala", category="rosse", pizza_type="Normale", price=18.5,
            sale_unit="kg", restaurant_id=self.restaurant_id,
        ))
        self.session.add(ConversationSession(
            session_id="s-1", customer_phone="+393331234567", items_json="[]",
            state="collecting_items", completed=False, restaurant_id=self.restaurant_id,
        ))
        self.session.commit()

        self.next_extraction = NOTHING
        self.llm_calls = 0
        self.saved = []

        def fake_extract(*args, **kwargs):
            self.llm_calls += 1
            result, self.next_extraction = self.next_extraction, NOTHING
            return result

        def fake_save(**kwargs):
            self.saved.append(kwargs)
            return {"id": "b44-1", "order_number": 1}

        load = lambda restaurant_id="": dict(RESTAURANTS.get(restaurant_id, {}))  # noqa: E731
        for p in (
            patch.object(service, "datetime", _FROZEN_DATETIME_MODULE),
            patch.object(chat_module, "datetime", _FROZEN_DATETIME_MODULE),
            patch.object(service, "load_restaurant", side_effect=load),
            patch.object(chat_module, "load_restaurant", side_effect=load),
            patch.object(chat_module, "ensure_restaurant_config", return_value=True),
            patch.object(chat_module, "is_agent_active", return_value=True),
            patch.object(chat_module, "detect_reservation_intent", return_value=False),
            patch.object(chat_module, "get_proposable_menu", return_value=[{"name": "Bufala", "sale_unit": "kg"}]),
            patch.object(chat_module, "get_sold_out_item_names", return_value=set()),
            patch.object(chat_module, "load_doughs", return_value=[]),
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

    def order_up_to_name(self):
        """Mezzo chilo di bufala calda già detto; restano nome e ora."""
        self.say("mezzo chilo di bufala", {**NOTHING, "intent": "add_items", "items": [_bufala_kg()]})
        return self.say("calda")


class PreorderFlowTests(_FlowHarness):
    # ── Flusso completo e riepilogo ─────────────────────────────────────────

    def test_call_in_slices_asks_only_what_is_missing_and_recap_matches_saved_order(self):
        r = self.say("della bufala", {**NOTHING, "intent": "add_items", "items": [_bufala()]})
        self.assertEqual(r.state, "collecting_kg_quantity")
        self.assertEqual(r.response_message, "Quanti tranci, interi o mezzi? Caldi o freddi?")

        r = self.say("due interi")
        self.assertEqual((r.state, r.response_message), ("collecting_kg_temperature", "Caldi o freddi?"))

        r = self.say("freddi")
        self.assertEqual((r.state, r.response_message), ("collecting_name", "A che nome?"))

        r = self.say("Giacomo")
        self.assertEqual(r.state, "collecting_pickup_time")
        self.assertEqual(r.response_message, "Domani siamo chiusi, il ritiro è per martedì. A che ora passa?")

        r = self.say("alle 8")
        self.assertEqual(r.state, "awaiting_confirmation")
        self.assertEqual(
            r.response_message,
            "Allora: due tranci interi di bufala, freddi, per martedì alle 20, a nome Giacomo. Confermo?",
        )
        self.assertEqual(self.saved, [])
        self.assertEqual(self.llm_calls, 1)  # le risposte brevi non passano dall'LLM

        r = self.say("sì")
        self.assertEqual(r.state, "completed")
        self.assertEqual(r.response_message, "Perfetto, Giacomo! Le arriverà la conferma su WhatsApp.")
        saved = self.saved[0]
        self.assertEqual(saved["pickup_date"], TUESDAY.isoformat())
        self.assertEqual(saved["pickup_time"], "20:00")
        self.assertIsNone(saved.get("review_reasons"))
        item = saved["items"][0]
        self.assertEqual(
            (item["order_unit"], item["quantity"], item["size"], item["temperature"]),
            ("tranci", 2, "piena", "fredda"),
        )
        # A tranci il peso non è noto: nessun totale, prezzo a peso al ritiro
        self.assertIsNone(item["total_price"])

    def test_weight_order_skips_the_portion_question(self):
        r = self.say("mezzo chilo di bufala", {**NOTHING, "intent": "add_items", "items": [_bufala_kg()]})
        self.assertEqual((r.state, r.response_message), ("collecting_kg_temperature", "Calda o fredda?"))
        self.say("calda")
        self.say("Giacomo")
        r = self.say("alle 8")
        self.assertEqual(
            r.response_message,
            "Allora: mezzo chilo di bufala, calda, per martedì alle 20, a nome Giacomo. Confermo?",
        )
        self.say("sì")
        item = self.saved[0]["items"][0]
        self.assertEqual((item["order_unit"], item["quantity"], item["temperature"]), ("kg", 0.5, "calda"))
        self.assertNotIn(item.get("size"), ("piena", "mezza"))
        self.assertEqual(item["total_price"], 9.95)

    def test_weight_said_as_answer_to_the_quantity_question(self):
        self.say("della bufala", {**NOTHING, "intent": "add_items", "items": [_bufala()]})
        r = self.say("tre etti caldi")
        self.assertEqual(r.state, "collecting_name")
        item = json.loads(self.conversation().items_json)[0]
        self.assertEqual((item["order_unit"], item["quantity"], item["temperature"]), ("kg", 0.3, "calda"))

    def test_slices_without_portion_ask_portion_and_temperature_together(self):
        r = self.say("due tranci di bufala", {**NOTHING, "intent": "add_items", "items": [_bufala_slices(2)]})
        self.assertEqual((r.state, r.response_message), ("collecting_kg_portion", "Interi o mezzi? Caldi o freddi?"))

        r = self.say("caldi")  # risponde solo a una parte
        self.assertEqual((r.state, r.response_message), ("collecting_kg_portion", "Interi o mezzi?"))

        r = self.say("mezzi")
        self.assertEqual(r.state, "collecting_name")

    def test_single_slice_question_is_singular(self):
        r = self.say("un trancio di bufala", {**NOTHING, "intent": "add_items", "items": [_bufala_slices(1)]})
        self.assertEqual(r.response_message, "Intero o mezzo? Caldo o freddo?")

    def test_questions_are_short_without_measures_or_product_name(self):
        r = self.say("della bufala", {**NOTHING, "intent": "add_items", "items": [_bufala()]})
        for banned in ("15×20", "7,5×10", "portar via", "mangiare subito", "Bufala", "bufala"):
            self.assertNotIn(banned, r.response_message)

    def test_product_is_named_only_with_more_than_one_slice_item(self):
        porchetta = {**_bufala_slices(2, size="piena", temperature="calda"), "pizza_name": "Porchetta"}
        self.session.add(MenuItem(
            name="Porchetta", category="rosse", pizza_type="Normale", price=18.5,
            sale_unit="kg", restaurant_id=self.restaurant_id,
        ))
        self.session.commit()
        r = self.say(
            "due tranci interi caldi di porchetta e della bufala",
            {**NOTHING, "intent": "add_items", "items": [porchetta, _bufala()]},
        )
        self.assertEqual(r.response_message, "Per la bufala, quanti tranci, interi o mezzi? Caldi o freddi?")

    def test_information_given_together_is_not_asked_again(self):
        r = self.say(
            "due tranci interi di bufala freddi, sono Giacomo, per le 19",
            {**NOTHING, "intent": "add_items", "customer_name": "Giacomo", "pickup_time": "19:00",
             "items": [_bufala_slices(2, size="piena", temperature="fredda")]},
        )
        self.assertEqual(r.state, "awaiting_confirmation")
        self.assertEqual(
            r.response_message,
            "Allora: due tranci interi di bufala, freddi, per martedì alle 19, a nome Giacomo. Confermo?",
        )

    def test_quantity_portion_and_temperature_in_one_answer(self):
        self.say("della bufala", {**NOTHING, "intent": "add_items", "items": [_bufala()]})
        r = self.say("tre mezzi caldi")
        self.assertEqual(r.state, "collecting_name")
        self.assertEqual(self.llm_calls, 1)  # risposta breve gestita senza LLM
        item = json.loads(self.conversation().items_json)[0]
        self.assertEqual((item["quantity"], item["size"], item["temperature"]), (3, "mezza", "calda"))

    def test_price_question_gives_kg_prices_and_repeats_the_open_question(self):
        self.say("della bufala", {**NOTHING, "intent": "add_items", "items": [_bufala()]})
        r = self.say("quanto costa al chilo?", {**NOTHING, "intent": "ask_kg_price"})
        self.assertEqual(
            r.response_message,
            "Al taglio costa 18,50 euro al chilo fredda e 19,90 euro calda. "
            "Il totale lo facciamo alla bilancia. Quanti tranci, interi o mezzi? Caldi o freddi?",
        )
        self.assertEqual(r.state, "collecting_kg_quantity")

    # ── Domanda saltata: una per campo ──────────────────────────────────────

    def test_skipped_flavours_are_asked_and_nothing_is_confirmed(self):
        r = self.say("sono Giacomo, per le 8", {**NOTHING, "customer_name": "Giacomo", "pickup_time": "20:00"})
        self.assertEqual(r.state, "collecting_items")
        r = self.say("sì")
        self.assertNotEqual(r.state, "completed")
        self.assertEqual(self.saved, [])

    def test_skipped_portion_is_asked_again_and_other_answers_are_kept(self):
        self.say("due tranci di bufala", {**NOTHING, "intent": "add_items", "items": [_bufala_slices(2)]})
        r = self.say("mi chiamo Giacomo", {**NOTHING, "customer_name": "Giacomo"})
        self.assertEqual(r.state, "collecting_kg_portion")
        self.assertEqual(r.response_message, "Scusi, interi o mezzi? Caldi o freddi?")
        self.assertEqual(self.conversation().customer_name, "Giacomo")
        r = self.say("mezzi freddi")
        # il nome è già noto: si passa direttamente all'orario
        self.assertEqual(r.state, "collecting_pickup_time")

    def test_skipped_temperature_is_asked_again_never_defaulted(self):
        self.say("mezzo chilo di bufala", {**NOTHING, "intent": "add_items", "items": [_bufala_kg()]})
        r = self.say("va bene")
        self.assertEqual((r.state, r.response_message), ("collecting_kg_temperature", "Scusi, calda o fredda?"))
        item = self.conversation().items_json
        self.assertNotIn('"temperature": "fredda"', item)
        self.assertNotIn('"temperature": "calda"', item)

    def test_skipped_name_is_asked_again_and_pickup_time_is_kept(self):
        self.order_up_to_name()
        r = self.say("alle 8", {**NOTHING, "pickup_time": "20:00"})
        self.assertEqual(r.state, "collecting_name")
        self.assertEqual(r.response_message, "A che nome?")
        r = self.say("Giacomo")
        self.assertEqual(r.state, "awaiting_confirmation")

    def test_skipped_pickup_time_is_asked_again_with_the_day(self):
        self.order_up_to_name()
        self.say("Giacomo")
        r = self.say("va bene")
        self.assertEqual(r.state, "collecting_pickup_time")
        self.assertIn("il ritiro è per martedì. A che ora passa?", r.response_message)
        self.assertEqual(self.saved, [])

    # ── Risposta non capita due volte: niente default, needs_review ─────────

    def test_unrecognised_portion_twice_saves_without_default_and_needs_review(self):
        self.say("due tranci di bufala", {**NOTHING, "intent": "add_items", "items": [_bufala_slices(2)]})
        r = self.say("boh")
        self.assertEqual(r.state, "collecting_kg_portion")
        r = self.say("non saprei")
        # si passa oltre, senza valore
        self.assertEqual((r.state, r.response_message), ("collecting_kg_temperature", "Caldi o freddi?"))
        self.say("freddi")
        self.say("Giacomo")
        r = self.say("alle 8")
        self.assertIn("due tranci di bufala, porzione da definire, freddi", r.response_message)
        self.say("sì")

        saved = self.saved[0]
        self.assertNotIn(saved["items"][0].get("size"), ("piena", "mezza"))
        self.assertEqual(saved["items"][0]["temperature"], "fredda")
        self.assertEqual(len(saved["review_reasons"]), 1)
        self.assertIn("Porzione (intero/mezzo) non capito dopo 2 tentativi per Bufala", saved["review_reasons"][0])

    def test_unrecognised_quantity_twice_saves_without_default_and_needs_review(self):
        self.say("della bufala", {**NOTHING, "intent": "add_items", "items": [_bufala()]})
        self.say("boh")
        r = self.say("non saprei")
        self.assertEqual((r.state, r.response_message), ("collecting_kg_temperature", "Caldi o freddi?"))
        self.say("freddi")
        self.say("Giacomo")
        r = self.say("alle 8")
        self.assertIn("bufala, quantità da definire, fredda", r.response_message)
        self.say("sì")
        saved = self.saved[0]
        self.assertEqual(saved["items"][0]["quantity"], 0)
        self.assertIn("Quantità (peso o numero di tranci) non capito", saved["review_reasons"][0])

    # ── Richiesta per stasera ────────────────────────────────────────────────

    def test_request_for_tonight_is_redirected_to_the_shop(self):
        r = self.say("vorrei mezzo chilo di bufala per stasera")
        self.assertEqual(self.llm_calls, 0)
        self.assertIn("al telefono prendiamo solo ordini per il giorno dopo", r.response_message)
        self.assertIn("per stasera può passare direttamente in negozio", r.response_message)
        self.assertIn("il ritiro è per martedì", r.response_message)
        self.assertEqual(self.conversation().items_json, "[]")

    def test_request_for_tonight_at_pickup_time_question_is_not_taken_as_tomorrow(self):
        self.order_up_to_name()
        self.say("Giacomo")
        r = self.say("stasera alle 8")
        self.assertIn("per stasera può passare direttamente in negozio", r.response_message)
        self.assertIsNone(self.conversation().pickup_time)

    # ── Ora di ritiro fuori orario ───────────────────────────────────────────

    def test_pickup_time_after_closing_is_refused_with_that_days_hours(self):
        self.order_up_to_name()
        self.say("Giacomo")
        r = self.say("alle 22")
        self.assertEqual(r.state, "collecting_pickup_time")
        self.assertEqual(
            r.response_message,
            "Mi dispiace, martedì siamo aperti dalle 17:00 alle 21:00. A che ora passa?",
        )
        self.assertIsNone(self.conversation().pickup_time)

    def test_pickup_time_before_opening_is_refused(self):
        self.order_up_to_name()
        self.say("Giacomo")
        r = self.say("alle 16", {**NOTHING, "pickup_time": "16:00"})
        self.assertEqual(r.state, "collecting_pickup_time")
        self.assertIn("dalle 17:00 alle 21:00", r.response_message)

    def test_refused_pickup_time_during_portion_question_keeps_the_question_open(self):
        self.say("due tranci di bufala", {**NOTHING, "intent": "add_items", "items": [_bufala_slices(2)]})
        r = self.say("alle 15", {**NOTHING, "intent": "set_pickup_time", "pickup_time": "15:00"})
        self.assertEqual(r.state, "collecting_kg_portion")
        self.assertEqual(self.conversation().state, "collecting_kg_portion")
        self.assertEqual(
            r.response_message,
            "Mi dispiace, martedì siamo aperti dalle 17:00 alle 21:00. Interi o mezzi? Caldi o freddi?",
        )
        self.assertIsNone(self.conversation().pickup_time)

        # La risposta alla porzione vale ancora, e l'orario viene chiesto dopo
        r = self.say("interi e caldi")
        self.assertEqual(r.state, "collecting_name")
        r = self.say("Giacomo")
        self.assertEqual(r.state, "collecting_pickup_time")

    def test_refused_pickup_time_outside_kg_questions_asks_the_time_again(self):
        self.say(
            "mezzo chilo di bufala calda",
            {**NOTHING, "intent": "add_items", "items": [_bufala_kg(temperature="calda")]},
        )
        r = self.say("alle 15", {**NOTHING, "intent": "set_pickup_time", "pickup_time": "15:00"})
        self.assertEqual(r.state, "collecting_pickup_time")
        self.assertIn("dalle 17:00 alle 21:00", r.response_message)

    def test_validate_pickup_time_uses_the_pickup_day_hours(self):
        with patch.object(service, "load_restaurant", side_effect=lambda restaurant_id="": RESTAURANTS[PAP_ID]):
            check = lambda t, d=TUESDAY: service.validate_pickup_time(t, PAP_ID, pickup_date=d)[0]  # noqa: E731
            self.assertTrue(check("17:00"))
            self.assertTrue(check("20:45"))
            self.assertFalse(check("20:50"))
            self.assertFalse(check("16:30"))
            self.assertFalse(check("19:00", datetime.date(2026, 10, 5)))  # lunedì chiuso


class SlicePhrasesTests(unittest.TestCase):
    def test_portion_synonyms(self):
        for word in ("intera", "intero", "inter", "tutto", "pieno", "piena", "interi"):
            self.assertEqual(chat_module._extract_kg_size(word), "piena", word)
        for word in ("mezza", "mezzo", "metà", "mezzi"):
            self.assertEqual(chat_module._extract_kg_size(word), "mezza", word)
        for phrase in ("mezzo chilo", "un chilo e mezzo", "alle otto e mezza"):
            self.assertIsNone(chat_module._extract_kg_size(phrase), phrase)

    def test_temperature_synonyms(self):
        for word in ("calda", "caldo", "caldi", "calde"):
            self.assertEqual(chat_module._extract_temperature(word), "calda", word)
        for word in ("fredda", "freddo", "freddi", "fredde"):
            self.assertEqual(chat_module._extract_temperature(word), "fredda", word)

    def test_quantity_answers(self):
        q = chat_module._extract_kg_quantity
        self.assertEqual(q("tre etti"), ("kg", 0.3))
        self.assertEqual(q("mezzo chilo"), ("kg", 0.5))
        self.assertEqual(q("un chilo e mezzo"), ("kg", 1.5))
        self.assertEqual(q("300 grammi"), ("kg", 0.3))
        self.assertEqual(q("due tranci"), ("tranci", 2))
        self.assertEqual(q("tre interi", bare_number_ok=True), ("tranci", 3))
        self.assertIsNone(q("tre interi"))

    def test_confirmation_lines_for_kitchen_and_customer(self):
        lines = service._build_pizza_lines([
            {"pizza_name": "Bufala", "sale_unit": "kg", "order_unit": "tranci", "quantity": 2,
             "size": "piena", "temperature": "fredda"},
            {"pizza_name": "Porchetta", "sale_unit": "kg", "order_unit": "kg", "quantity": 0.5,
             "temperature": "calda"},
        ])
        self.assertEqual(lines, ["- 2 tranci interi Bufala (freddi)", "- 500g Porchetta (calda)"])
        self.assertEqual(service.format_total_line(None), "Prezzo a peso al ritiro")
        self.assertEqual(service.format_total_line(9.95), "Totale: \u20ac9.95")


class CorteDelSoleUnaffectedTests(_FlowHarness):
    """Senza phone_orders_next_day_only nessuna regola del giorno dopo."""

    restaurant_id = CDS_ID

    def test_no_preorder_rule(self):
        r = self.say("mezzo chilo di bufala per stasera", {**NOTHING, "intent": "add_items", "items": [_bufala_kg()]})
        self.assertEqual(self.llm_calls, 1)
        self.assertEqual(r.state, "collecting_kg_temperature")
        self.assertIsNone(self.conversation().pickup_date)
        self.say("calda")
        r = self.say("Giacomo")
        self.assertEqual(r.response_message, "Per che ora?")


if __name__ == "__main__":
    unittest.main()
