"""Bug della chiamata CA1426060937daa2fdca7a2c6610ddba56 (1 ottobre, 23:02 a Roma).

1. Con reservations_enabled = null l'agente rispondeva "solo asporto": i flag del
   Restaurant a null valgono il loro default, mai False.
2. Il CallLog restava senza trascrizione ed era "abbandonata" anche dopo una
   conversazione, se a chiudere era il chiamante.

Base44 è simulato: nessuna scrittura in produzione.
"""
import asyncio
import datetime
import json
import types
import unittest
from unittest.mock import patch

from sqlmodel import SQLModel, Session, create_engine
from sqlmodel.pool import StaticPool

import app.routes.chat as chat_module
import app.routes.voice as voice_module
import tests.test_reservation_covers_flow as covers
from app.models import ConversationLog
from app.services import base44_client
from app.services import conversation_service as service


class RestaurantFlagTests(unittest.TestCase):
    def test_null_flags_take_their_default(self):
        flag = service.restaurant_flag
        self.assertTrue(flag({"reservations_enabled": None}, "reservations_enabled", default=True))
        self.assertTrue(flag({"agent_active": None}, "agent_active", default=True))
        self.assertTrue(flag({"table_assignment_enabled": ""}, "table_assignment_enabled", default=True))
        self.assertFalse(flag({"phone_orders_next_day_only": None}, "phone_orders_next_day_only", default=False))
        self.assertFalse(flag({"reservations_enabled": False}, "reservations_enabled", default=True))
        self.assertFalse(flag({"reservations_enabled": "false"}, "reservations_enabled", default=True))

    def test_is_helpers_read_null_as_default(self):
        restaurant = {"reservations_enabled": None, "agent_active": None, "phone_orders_next_day_only": None,
                      "table_assignment_enabled": None}
        with patch.object(service, "load_restaurant", side_effect=lambda restaurant_id="": dict(restaurant)):
            self.assertTrue(service.is_reservations_enabled("rest-cds"))
            self.assertTrue(service.is_agent_active("rest-cds"))
            self.assertFalse(service.is_phone_orders_next_day_only("rest-cds"))
            self.assertTrue(service.is_table_assignment_enabled("rest-cds"))


class _NullReservationsAt2302(covers._ReservationHarness):
    """Corte del Sole con reservations_enabled = null, giovedì 1 ottobre alle 23:02."""

    now = datetime.datetime(2026, 10, 1, 23, 2, tzinfo=covers.hours.ROME)
    restaurant = {**covers.CDS_RESERVATIONS, "reservations_enabled": None}

    def setUp(self):
        super().setUp()
        # Riconoscimento reale dell'intento di prenotazione, come nella chiamata
        p = patch.object(chat_module, "detect_reservation_intent", service.detect_reservation_intent)
        p.start()
        self.addCleanup(p.stop)


class NullReservationsTests(_NullReservationsAt2302):
    def test_table_for_tomorrow_at_2302_with_null_reservations(self):
        r = self.say("Buonasera, vorrei un tavolo per domani")
        self.assertEqual(r.state, "collecting_reservation_time")
        self.assertEqual(r.response_message, "Certo, domani, venerdì 2 ottobre. A che ora?")
        self.assertNotIn("asporto", r.response_message)
        r = self.book("alle 20", "4 persone", "Giacomo")
        self.assertEqual(
            r.response_message,
            "Allora: Giacomo, domani, venerdì 2 ottobre alle 20:00, 4 persone. Confermo?",
        )
        r = self.say("sì")
        self.assertEqual(r.state, "reservation_completed")
        saved = self.reservations[0]
        self.assertEqual((saved["date"], saved["time"], saved["party_size"]), ("2026-10-02", "20:00", 4))

    def test_whole_request_in_one_sentence_asks_only_the_name(self):
        r = self.say("vorrei prenotare un tavolo per domani alle 20 per 4 persone")
        self.assertEqual((r.state, r.response_message), ("collecting_reservation_name", "A nome di chi?"))
        reservation = json.loads(self.conversation().reservation_json)
        self.assertEqual((reservation["date"], reservation["time"], reservation["party_size"]), ("2026-10-02", "20:00", 4))

    def test_day_and_people_without_time_asks_the_time(self):
        r = self.say("un tavolo per sabato, siamo in 6")
        self.assertEqual(r.state, "collecting_reservation_time")
        self.assertEqual(r.response_message, "Certo, sabato 3 ottobre. A che ora?")

    def test_tonight_at_2302_asks_another_day(self):
        r = self.say("vorrei un tavolo per stasera")
        self.assertEqual(r.state, "collecting_reservation_date")
        self.assertEqual(r.response_message, "Per stasera non è più possibile prenotare. Per quale altro giorno?")

    def test_reservations_explicitly_off_still_means_takeaway_only(self):
        self.restaurant = {**self.restaurant, "reservations_enabled": False}
        r = self.say("vorrei un tavolo per domani")
        self.assertIn("solo ordini da asporto", r.response_message)


class ReservationDetailsTests(unittest.TestCase):
    def test_only_explicit_details_are_read(self):
        with patch.object(chat_module, "datetime", covers.hours._frozen_datetime_module(
            datetime.datetime(2026, 10, 1, 23, 2, tzinfo=covers.hours.ROME),
        )):
            details = chat_module._extract_reservation_details("un tavolo per domani alle 20 per 4 persone")
            self.assertEqual(details, {"date": "2026-10-02", "time": "20:00", "party_size": 4})
            # "un tavolo" non è un numero di persone, "per domani" non è un'ora
            self.assertEqual(chat_module._extract_reservation_details("vorrei un tavolo per domani"), {"date": "2026-10-02"})
            self.assertEqual(chat_module._extract_reservation_details("vorrei prenotare un tavolo"), {})


def _run(coro):
    return asyncio.run(coro)


class CallLogTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        self.updates = []
        self.record = {"id": "cl-1", "session_id": "s-1", "started_at": "2026-10-01T21:02:00+00:00"}
        for p in (
            patch.object(voice_module, "_db_engine", self.engine),
            patch.object(base44_client, "find_call_log_by_sid", side_effect=lambda sid, **kw: dict(self.record)),
            patch.object(base44_client, "update_call_log", side_effect=lambda cid, data, **kw: self.updates.append(data)),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _log(self, user, agent):
        with Session(self.engine) as db:
            db.add(ConversationLog(
                session_id="s-1", user_message=user, extracted_order_json="{}", merged_order_json="{}",
                response_message=agent, valid=False, missing_items_json="[]", state="collecting_items",
            ))
            db.commit()

    def test_caller_hangs_up_after_talking_keeps_transcript(self):
        self._log("Buonasera, vorrei un tavolo per domani", "Certo, domani, venerdì 2 ottobre. A che ora?")
        self._log("alle 20", "Per quante persone?")
        _run(voice_module._call_log_close_from_status("CA1", "completed", "48"))
        patch_data = self.updates[0]
        self.assertEqual(patch_data["outcome"], "nessun_ordine")
        self.assertEqual(patch_data["duration_seconds"], 48)
        self.assertIn("Utente: Buonasera, vorrei un tavolo per domani", patch_data["transcript"])
        self.assertIn("Agente: Per quante persone?", patch_data["transcript"])
        self.assertIn("dopo 2 turni", patch_data["summary"])

    def test_caller_hangs_up_without_speaking_stays_abandoned(self):
        _run(voice_module._call_log_close_from_status("CA1", "completed", "5"))
        patch_data = self.updates[0]
        self.assertNotIn("outcome", patch_data)  # resta "abbandonata"
        self.assertNotIn("transcript", patch_data)

    def test_closed_call_log_is_not_touched(self):
        self.record["ended_at"] = "2026-10-01T21:05:00+00:00"
        _run(voice_module._call_log_close_from_status("CA1", "completed", "60"))
        self.assertEqual(self.updates, [])

    def test_call_log_is_created_with_the_session_id(self):
        created = []
        with patch.object(base44_client, "create_call_log", side_effect=lambda data, **kw: created.append(data) or {"id": "cl-1"}):
            _run(voice_module._call_log_create("s-1", "rest-cds", "+393331234567", "CA1"))
        self.assertEqual((created[0]["session_id"], created[0]["call_sid"]), ("s-1", "CA1"))

    def test_confirmed_reservation_closes_the_call_as_prenotazione(self):
        result = types.SimpleNamespace(
            state="reservation_completed", order_id=None,
            merged_order={"date": "2026-10-02", "time": "20:00", "party_size": 4},
        )
        self.assertEqual(
            voice_module._call_log_outcome_for_result(result),
            ("prenotazione", "Prenotazione per 4 persone, 2026-10-02 alle 20:00"),
        )
        pending = types.SimpleNamespace(
            state="reservation_completed", order_id=None,
            merged_order={"date": "2026-10-03", "time": "20:00", "party_size": 12, "large_group": True},
        )
        self.assertIn("da confermare", voice_module._call_log_outcome_for_result(pending)[1])

    def test_calls_still_in_progress_are_not_closed(self):
        result = types.SimpleNamespace(state="collecting_reservation_time", order_id=None, merged_order={})
        self.assertIsNone(voice_module._call_log_outcome_for_result(result))


if __name__ == "__main__":
    unittest.main()
