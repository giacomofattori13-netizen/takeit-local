"""Prenotazioni Corte del Sole: niente tavoli né turni, coperti della serata contro
max_covers, preferenza di sala segnalata (mai garantita), note del cliente abituale,
gruppi oltre max_party_size_auto passati al locale o richiamati.

Base44 e Twilio sono simulati: nessuna scrittura in produzione.
"""
import asyncio
import os
import types
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

import app.routes.voice as voice_module
import tests.test_opening_hours_flow as hours
from app.services import conversation_service as service

CDS_RESERVATIONS = {
    **hours.CDS,
    "table_assignment_enabled": False,
    "max_covers": 150,
    "handoff_phone": "+393889972292",
    "max_party_size_auto": 10,
}
KNOWN_CUSTOMER = {"full_name": "Giacomo", "usual_table": "12, vicino alla finestra", "usual_notes": "Ama il tavolo tranquillo"}


class _ReservationHarness(hours._CorteHarness):
    restaurant = CDS_RESERVATIONS

    def setUp(self):
        super().setUp()
        self.booked = []        # prenotazioni confermate di quella data, per il conteggio coperti
        self.reservations = []  # prenotazioni salvate dall'agente
        self.fetch_queries = []
        self.customer = None

        def fake_fetch(date, required=False, restaurant_id=""):
            self.fetch_queries.append((date, restaurant_id))
            return list(self.booked)

        def fake_save(**kwargs):
            self.reservations.append(kwargs)
            return "res-1"

        for p in (
            patch.object(service, "_fetch_reservations_for_date", side_effect=fake_fetch),
            patch.object(service, "_fetch_tables_from_base44", side_effect=AssertionError("niente tavoli per CdS")),
            patch.object(hours.chat_module, "save_reservation_to_base44", side_effect=fake_save),
            patch.object(hours.chat_module, "lookup_customer", side_effect=lambda phone, restaurant_id="": self.customer),
            patch.object(hours.chat_module, "_enqueue_reservation_sms_side_effect", return_value=None),
        ):
            p.start()
            self.addCleanup(p.stop)

    def book(self, *messages):
        r = None
        for message in messages:
            r = self.say(message)
        return r


class CoversTests(_ReservationHarness):
    def test_reservation_without_table_and_counted_on_the_evening(self):
        self.booked = [{"party_size": 140, "time": "20:00"}, {"party_size": 6, "time": "21:30"}]
        r = self.book("vorrei prenotare un tavolo", "stasera", "alle 20", "4 persone", "Giacomo")
        self.assertEqual(r.state, "awaiting_reservation_confirmation")
        self.assertEqual(r.response_message, "Allora: Giacomo, stasera, giovedì 1 ottobre alle 20:00, 4 persone. Confermo?")
        r = self.say("sì")
        self.assertEqual(r.state, "reservation_completed")
        saved = self.reservations[0]
        self.assertIsNone(saved.get("table_id"))
        self.assertIsNone(saved.get("table_name"))
        self.assertEqual(saved.get("status", "confermata"), "confermata")
        self.assertEqual(self.fetch_queries[0], ("2026-10-01", hours.CDS_ID))

    def test_full_evening_offers_another_date(self):
        self.booked = [{"party_size": 149, "time": "20:00"}]
        r = self.book("vorrei prenotare un tavolo", "stasera", "alle 21", "2 persone")
        self.assertEqual(r.state, "collecting_reservation_date")
        self.assertEqual(r.response_message, "Mi dispiace, per quella sera siamo al completo. Per quale altro giorno?")
        self.assertEqual(self.reservations, [])

    def test_no_limit_by_arrival_time(self):
        # 148 coperti alla stessa ora: ne restano 2, qualunque sia l'orario di arrivo
        self.booked = [{"party_size": 148, "time": "20:00"}]
        r = self.book("vorrei prenotare un tavolo", "stasera", "alle 20", "2 persone", "Giacomo")
        self.assertEqual(r.state, "awaiting_reservation_confirmation")

    def test_table_assignment_still_works_where_enabled(self):
        with (
            patch.object(service, "load_restaurant", side_effect=lambda restaurant_id="": {"id": "rest-x"}),
            patch.object(service, "_fetch_tables_from_base44", side_effect=lambda required=False, restaurant_id="": [
                {"id": "t1", "name": "Tavolo 1", "capacity": 4},
            ]) as tables,
        ):
            available, _, table_info = service.check_reservation_availability("2026-10-03", "20:00", 2, restaurant_id="rest-x")
        self.assertTrue(available)
        self.assertEqual(table_info["table_name"], "Tavolo 1")
        self.assertEqual(tables.call_args.kwargs["restaurant_id"], "rest-x")


class PreferencesTests(_ReservationHarness):
    def test_room_preference_is_flagged_never_guaranteed(self):
        self.say("vorrei prenotare un tavolo")
        r = self.say("sabato, se possibile in veranda")
        self.assertEqual(r.response_message, "Lo segnalo, cerchiamo di accontentarla. A che ora?")
        r = self.book("alle 20", "4 persone", "Giacomo")
        self.assertIn("preferenza in veranda: lo segnalo, cerchiamo di accontentarla", r.response_message)
        for promise in ("garantit", "assicur", "sicuramente"):
            self.assertNotIn(promise, r.response_message.lower())
        self.say("sì")
        self.assertEqual(self.reservations[0]["preferred_room"], "in veranda")

    def test_special_requests_go_to_notes(self):
        r = self.book("vorrei prenotare un tavolo", "sabato", "alle 20")
        r = self.say("4 persone, ci serve un seggiolone")
        self.book("Giacomo", "sì")
        self.assertIn("ci serve un seggiolone", self.reservations[0]["notes"])

    def test_known_customer_usual_table_and_notes_are_copied(self):
        self.customer = KNOWN_CUSTOMER
        self.book("vorrei prenotare un tavolo", "sabato", "alle 20", "2 persone", "Giacomo", "sì")
        notes = self.reservations[0]["notes"]
        self.assertIn("Tavolo abituale: 12, vicino alla finestra", notes)
        self.assertIn("Note abituali: Ama il tavolo tranquillo", notes)


class LargeGroupTests(_ReservationHarness):
    def test_large_group_is_handed_off_to_the_restaurant(self):
        r = self.book("vorrei prenotare un tavolo", "sabato", "alle 20", "12 persone")
        self.assertEqual(r.state, "reservation_handoff")
        self.assertEqual(r.response_message, "Per i gruppi numerosi la passo subito al locale, resti in linea.")

        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": ""}):
            twiml = asyncio.run(voice_module._build_response_twiml(r, "s-1"))
        dial = ET.fromstring(twiml).find("Dial")
        self.assertIsNotNone(dial)
        self.assertEqual(dial.text, "+393889972292")
        self.assertEqual(dial.get("timeout"), "20")
        self.assertEqual(dial.get("action"), "/voice/dial-result?session_id=s-1")
        self.assertEqual(self.fetch_queries, [])  # nessun controllo automatico dei coperti

    def test_answered_handoff_ends_the_call(self):
        self.book("vorrei prenotare un tavolo", "sabato", "alle 20", "12 persone")
        with patch.object(voice_module, "_db_engine", self.engine):
            self.assertEqual(voice_module._dial_result_update("s-1", "completed"), (True, None))

    def test_unanswered_handoff_collects_data_and_saves_for_callback(self):
        self.book("vorrei prenotare un tavolo", "sabato", "alle 20", "12 persone")
        with patch.object(voice_module, "_db_engine", self.engine):
            transferred, message = voice_module._dial_result_update("s-1", "no-answer")
        self.assertFalse(transferred)
        self.assertEqual(
            message,
            "Mi dispiace, al momento il locale non risponde. Prendo i dati e la richiamiamo noi per confermare. A nome di chi?",
        )
        self.assertEqual(self.conversation().state, "collecting_reservation_callback_name")

        r = self.say("Giacomo")
        self.assertEqual(r.response_message, "Grazie. La richiamiamo al numero da cui sta chiamando?")
        r = self.say("sì")
        self.assertEqual(r.state, "reservation_completed")
        self.assertIn("la richiameremo il prima possibile per confermare", r.response_message)

        saved = self.reservations[0]
        self.assertEqual(saved["status"], "da_confermare")
        self.assertEqual(saved["party_size"], 12)
        self.assertEqual(saved["customer_phone"], "+393331234567")
        self.assertEqual((saved["date"], saved["time"]), ("2026-10-03", "20:00"))
        self.assertIn("Gruppo di 12 persone", saved["review_reason"])

    def test_chat_without_dial_falls_back_to_callback(self):
        self.book("vorrei prenotare un tavolo", "sabato", "alle 20", "12 persone")
        r = self.say("pronto?")
        self.assertEqual(r.state, "collecting_reservation_callback_name")
        self.book("Giacomo")
        r = self.say("al 333 765 4321")
        self.assertEqual(r.state, "reservation_completed")
        self.assertEqual(self.reservations[0]["customer_phone"], "+393337654321")

    def test_ten_people_is_still_automatic(self):
        r = self.book("vorrei prenotare un tavolo", "sabato", "alle 20", "10 persone")
        self.assertEqual(r.state, "collecting_reservation_name")


class Base44QueriesTests(unittest.TestCase):
    def test_reservations_are_filtered_on_base44_by_date_status_and_restaurant(self):
        with (
            patch.object(service, "base44_token", return_value="t"),
            patch.object(service.base44_client, "query_entities", return_value=[
                {"date": "2026-10-03", "status": "confermata", "party_size": 4},
            ]) as query,
        ):
            result = service._fetch_reservations_for_date("2026-10-03", restaurant_id="rest-cds")
        self.assertEqual(len(result), 1)
        self.assertEqual(query.call_args.args[:2], (
            "Reservation", {"date": "2026-10-03", "status": "confermata", "restaurant_id": "rest-cds"},
        ))

    def test_tables_are_filtered_by_restaurant(self):
        with (
            patch.object(service, "base44_token", return_value="t"),
            patch.object(service.base44_client, "query_entities", return_value=[
                {"id": "t1", "capacity": 4}, {"id": "t2", "capacity": 2, "status": "maintenance"},
            ]) as query,
        ):
            tables = service._fetch_tables_from_base44(restaurant_id="rest-x")
        self.assertEqual([t["id"] for t in tables], ["t1"])
        self.assertEqual(query.call_args.args[:2], ("Table", {"restaurant_id": "rest-x"}))

    def test_cancelled_or_pending_reservations_do_not_count(self):
        with (
            patch.object(service, "base44_token", return_value="t"),
            patch.object(service.base44_client, "query_entities", return_value=[
                {"date": "2026-10-03", "status": "da_confermare", "party_size": 12},
            ]),
        ):
            self.assertEqual(service._fetch_reservations_for_date("2026-10-03", restaurant_id="rest-cds"), [])


if __name__ == "__main__":
    unittest.main()
