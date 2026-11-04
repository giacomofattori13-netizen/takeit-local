"""Terzo "no" e "no" a "Per quale giorno le serve?": mai chiudere senza lasciare traccia.
Si salva una richiesta di richiamata e si prova il trasferimento al locale, come per i
gruppi. Mercoledì 4 novembre 2026. Base44 e Twilio sono simulati."""
import asyncio
import datetime
import json
import os
import unittest.mock
import xml.etree.ElementTree as ET
from unittest.mock import patch

import app.routes.chat as chat_module
import app.routes.voice as voice_module
import tests.test_opening_hours_flow as hours
import tests.test_reservation_covers_flow as covers
from app.services import base44_client

OPEN = datetime.datetime(2026, 11, 4, 16, 0, tzinfo=hours.ROME)
REASON = "Cliente non capito dopo 3 correzioni"
GOODBYE = "La faccio richiamare dal locale appena possibile. A presto!"
HANDOFF = "+393889972292"


class _FakeRequest:
    async def form(self):
        return {"CallSid": "CA1"}


class _Call(covers._ReservationHarness):
    now = OPEN
    restaurant = {**covers.CDS_RESERVATIONS, "name": "Corte Del Sole"}

    def setUp(self):
        super().setUp()
        p = patch.object(voice_module, "_db_engine", self.engine)
        p.start()
        self.addCleanup(p.stop)

    def three_noes_at_the_party_question(self):
        self.book("vorrei un tavolo", "domani", "alle 20")
        self.say("no")
        self.say("no")
        return self.say("No no no no.")

    def no_to_the_day_question(self):
        self.order("una margherita per domani alle 8", pickup_time="20:00")
        self.say("Giacomo")
        self.say("no")
        self.say("il giorno")
        return self.say("no")

    def dial_result(self, status="no-answer"):
        return voice_module._dial_result_update("s-1", status)

    def twiml(self, result):
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": ""}):
            return ET.fromstring(asyncio.run(voice_module._build_response_twiml(result, "s-1")))


class ThirdNoTests(_Call):
    def test_third_no_saves_what_was_said_and_dials_the_restaurant(self):
        r = self.three_noes_at_the_party_question()
        self.assertEqual((r.state, r.response_message), ("reservation_handoff", chat_module.CALLBACK_HANDOFF_MESSAGE))
        self.assertEqual(self.twiml(r).find("Dial").find("Number").text, HANDOFF)
        self.assertEqual(len(self.reservations), 1)
        saved = self.reservations[0]
        self.assertEqual(saved["status"], "da_confermare")
        self.assertEqual(saved["review_reason"], REASON)
        self.assertEqual((saved["date"], saved["time"], saved["party_size"]), ("2026-11-05", "20:00", 0))
        self.assertIn("Intento: prenotazione", saved["notes"])
        self.assertIn("Persone: non dette", saved["notes"])
        self.assertIn("Richiamare al +393331234567", saved["notes"])

    def test_no_answer_says_goodbye_and_keeps_one_request(self):
        self.three_noes_at_the_party_question()
        self.assertEqual(self.dial_result(), (False, GOODBYE))
        self.assertEqual(self.conversation().state, "completed")
        self.assertEqual(len(self.reservations), 1)  # non duplicata
        self.assertEqual(self.saved, [])

    def test_answer_without_pressing_one_counts_as_no_answer(self):
        self.three_noes_at_the_party_question()
        self.assertEqual(self.dial_result("completed"), (False, GOODBYE))

    def test_restaurant_accepting_marks_the_request(self):
        self.three_noes_at_the_party_question()
        reservation_id, reason = voice_module._screen_accept_sync("s-1")
        self.assertEqual(reservation_id, "res-1")
        self.assertEqual(reason, f"{REASON} — passata al locale")
        self.assertEqual(self.dial_result("completed"), (True, None))

    def test_whoever_answers_hears_what_the_customer_said(self):
        self.three_noes_at_the_party_question()
        message = voice_module._screen_message_sync("s-1")
        self.assertTrue(message.startswith("Cliente non capito dall'agente. Intento: prenotazione."))
        self.assertTrue(message.endswith("Premi 1 per accettare."))

    def test_without_a_handoff_phone_it_saves_and_says_goodbye(self):
        self.restaurant = {**self.restaurant, "handoff_phone": ""}
        r = self.three_noes_at_the_party_question()
        self.assertEqual((r.state, r.response_message), ("completed", GOODBYE))
        self.assertEqual(self.reservations[0]["review_reason"], REASON)
        self.assertIn("<Hangup/>", ET.tostring(self.twiml(r)).decode().replace(" />", "/>"))

    def test_chat_without_a_dial_still_finishes(self):
        self.three_noes_at_the_party_question()
        r = self.say("pronto?")
        self.assertEqual((r.state, r.response_message), ("completed", GOODBYE))
        self.assertEqual(len(self.reservations), 1)


class NoToTheDayQuestionTests(_Call):
    def test_no_to_the_day_question_saves_the_order_details_and_dials(self):
        r = self.no_to_the_day_question()
        self.assertEqual((r.state, r.response_message), ("reservation_handoff", chat_module.CALLBACK_HANDOFF_MESSAGE))
        self.assertEqual(self.twiml(r).find("Dial").find("Number").text, HANDOFF)
        saved = self.reservations[0]
        self.assertEqual(saved["review_reason"], REASON)
        self.assertEqual((saved["date"], saved["time"], saved["customer_name"]), ("2026-11-05", "20:00", "Giacomo"))
        self.assertIn("Intento: ordine", saved["notes"])
        self.assertIn("Prodotti: una margherita", saved["notes"])
        self.assertEqual(self.saved, [])  # non è un ordine confermato

    def test_no_answer_says_goodbye(self):
        self.no_to_the_day_question()
        self.assertEqual(self.dial_result(), (False, GOODBYE))
        self.assertEqual(self.conversation().state, "completed")

    def test_explicit_decline_is_not_a_callback(self):
        self.order("una margherita per domani alle 8", pickup_time="20:00")
        self.say("Giacomo")
        self.say("no")
        self.say("il giorno")
        r = self.say("no grazie")
        self.assertEqual(r.state, "completed")
        self.assertEqual(self.reservations, [])


class DialResultTwimlTests(_Call):
    def test_dial_result_hangs_up_with_the_goodbye(self):
        self.three_noes_at_the_party_question()
        updates = []
        find = {"id": "cl-1", "session_id": "s-1", "started_at": "2026-11-04T15:00:00+00:00"}

        async def run():
            response = await voice_module.voice_dial_result(_FakeRequest(), session_id="s-1", DialCallStatus="no-answer")
            await asyncio.gather(*(t for t in asyncio.all_tasks() if t is not asyncio.current_task()))
            return response.body.decode()

        async def spoken(text, **kwargs):
            return f"<Say>{text}</Say>"

        async def no_verify(request):
            return None

        with patch.object(voice_module, "_audio_element_async", spoken), patch.object(
            voice_module, "_verify_twilio_request", no_verify,
        ), patch.object(base44_client, "find_call_log_by_sid", return_value=find), patch.object(
            base44_client, "update_call_log", side_effect=lambda cid, data, **kw: updates.append(data),
        ):
            root = ET.fromstring(asyncio.run(run()))
        self.assertEqual(root.find("Say").text, GOODBYE)
        self.assertIsNotNone(root.find("Hangup"))
        self.assertIsNone(root.find("Gather"))
        self.assertEqual(updates[0]["outcome"], "prenotazione")
        self.assertIn("(rif. res-1)", updates[0]["summary"])


class HonestPromiseTests(_Call):
    def test_failed_save_does_not_promise_a_callback(self):
        with patch.object(chat_module, "save_reservation_to_base44", return_value=None):
            self.three_noes_at_the_party_question()
            transferred, message = self.dial_result()
        self.assertFalse(transferred)
        self.assertEqual(message, chat_module.CALLBACK_NOT_POSSIBLE)
        self.assertNotIn("richiamare dal locale", message)
        self.assertEqual(self.conversation().state, "completed")

    def test_no_caller_number_does_not_promise_a_callback(self):
        conversation = self.conversation()
        conversation.customer_phone = None
        self.session.add(conversation)
        self.session.commit()
        self.three_noes_at_the_party_question()
        self.assertEqual(self.dial_result(), (False, chat_module.CALLBACK_NOT_POSSIBLE))
        self.assertIn("Numero non disponibile", self.reservations[0]["notes"])


class SavedDetailsTests(_Call):
    def test_nothing_said_uses_placeholders_stated_in_the_notes(self):
        replies = [self.say("no", {**hours.NOTHING}).response_message for _ in range(3)]
        self.assertEqual(replies[-1], chat_module.CALLBACK_HANDOFF_MESSAGE)
        saved = self.reservations[0]
        self.assertEqual((saved["date"], saved["time"], saved["party_size"]), ("2026-11-04", "00:00", 0))
        self.assertEqual(saved["customer_name"], "Ospite")
        for text in ("Intento: non chiaro", "Prodotti: nessuno", "Giorno: non detto", "Ora: non detta", "Persone: non dette"):
            self.assertIn(text, saved["notes"])

    def test_call_log_reflects_the_request(self):
        self.no_to_the_day_question()
        self.dial_result()
        outcome, summary = voice_module._session_saved_outcome_sync("s-1")
        self.assertEqual(outcome, "nessun_ordine")  # un ordine non confermato non è una prenotazione
        self.assertIn("Richiesta di richiamata. Intento: ordine", summary)
        self.assertIn("(rif. res-1)", summary)


if __name__ == "__main__":
    unittest.main()
