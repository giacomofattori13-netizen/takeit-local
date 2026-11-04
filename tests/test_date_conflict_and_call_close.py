"""Giorno e data in conflitto si chiedono, la prenotazione confermata chiude la chiamata,
nessuna richiamata promessa senza una richiesta registrata, niente abbreviazioni al TTS.

Mercoledì 4 novembre 2026, ora di Roma: il 20 novembre è venerdì, il 21 sabato.
"""
import asyncio
import datetime
import json
import os
import re
import types
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import app.routes.chat as chat_module
import app.routes.voice as voice_module
import tests.test_opening_hours_flow as hours
import tests.test_reservation_covers_flow as covers
from app.models import ConversationSession
from sqlmodel import SQLModel, Session, create_engine
from sqlmodel.pool import StaticPool

NOV_4 = datetime.datetime(2026, 11, 4, 16, 0, tzinfo=hours.ROME)
QUESTION = "Venerdì è il 20, sabato è il 21: quale dei due?"


class ConflictDetectionTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(chat_module, "datetime", hours._frozen_datetime_module(NOV_4))
        p.start()
        self.addCleanup(p.stop)

    def question(self, message):
        days = chat_module._weekday_date_conflict(message, bare_day=True)
        return chat_module._date_conflict_question(*days) if days else None

    def test_matching_day_and_date_are_not_a_conflict(self):
        for message in ("venerdì 20 novembre", "sabato 21", "il 20 novembre, venerdì", "domani, giovedì 5 novembre"):
            with self.subTest(message=message):
                self.assertIsNone(self.question(message))

    def test_mismatch_offers_both_days_earliest_first(self):
        self.assertEqual(self.question("venerdì 21 novembre"), QUESTION)
        self.assertEqual(self.question("sabato 20 novembre"), QUESTION)
        self.assertEqual(self.question("sabato il 20"), QUESTION)
        self.assertEqual(self.question("venerdì 21/11"), QUESTION)

    def test_question_reads_naturally(self):
        self.assertEqual(self.question("sabato 8 novembre"), "Sabato è il 7, domenica è l'8: quale dei due?")
        self.assertEqual(
            self.question("lunedì primo dicembre"),
            "Lunedì è il 30 novembre, martedì è il primo dicembre: quale dei due?",
        )

    def test_the_named_weekday_is_never_in_the_past(self):
        # Il 5 è giovedì; il lunedì più vicino, il 2, è già passato
        self.assertEqual(self.question("lunedì 5 novembre"), "Giovedì è il 5, lunedì è il 9: quale dei due?")

    def test_numbers_that_are_not_dates_are_ignored(self):
        for message in ("sabato 4 persone", "sabato alle 20", "sabato 2 margherite", "sabato 8 e mezza"):
            with self.subTest(message=message):
                self.assertIsNone(chat_module._weekday_date_conflict(message))

    def test_a_customer_called_sabato_is_not_a_weekday(self):
        self.assertIsNone(chat_module._weekday_date_conflict("a nome Sabato, il 20 novembre"))

    def test_read_dates(self):
        extract = chat_module._extract_reservation_date
        self.assertEqual(extract("venerdì 20 novembre"), "2026-11-20")
        self.assertEqual(extract("il 21"), "2026-11-21")
        self.assertEqual(extract("per il 3"), "2026-12-03")  # il 3 di novembre è passato
        self.assertEqual(extract("l'8"), "2026-11-08")
        self.assertEqual(extract("sabato 14"), "2026-11-14")
        self.assertEqual(extract("sabato"), "2026-11-07")
        self.assertIsNone(extract("il 4 persone"))
        self.assertIsNone(extract("alle 21"))

    def test_resolve_answers(self):
        days = ["2026-11-20", "2026-11-21"]
        resolve = chat_module._resolve_date_conflict
        fri, sat = datetime.date(2026, 11, 20), datetime.date(2026, 11, 21)
        for answer, expected in (
            ("venerdì", fri), ("il 21", sat), ("sabato", sat), ("quello di sabato", sat), ("21", sat),
            ("il primo", fri), ("il secondo", sat), ("sabato alle 20", sat),
            ("boh", None), ("sì", None), ("venerdì o sabato", None),
        ):
            with self.subTest(answer=answer):
                self.assertEqual(resolve(answer, days), expected)


class ReservationConflictFlowTests(covers._ReservationHarness):
    now = NOV_4

    def date_saved(self):
        return json.loads(self.conversation().reservation_json).get("date")

    def test_matching_day_and_date_goes_on_to_the_time(self):
        self.say("vorrei prenotare un tavolo")
        r = self.say("venerdì 20 novembre")
        self.assertEqual((r.state, r.response_message), ("collecting_reservation_time", "A che ora?"))
        self.assertEqual(self.date_saved(), "2026-11-20")

    def test_mismatch_asks_and_waits_for_the_answer(self):
        self.say("vorrei prenotare un tavolo")
        r = self.say("sabato 20 novembre")
        self.assertEqual((r.state, r.response_message), ("collecting_reservation_date", QUESTION))
        self.assertIsNone(self.date_saved())
        # "sabato" è il 21, non il prossimo sabato (il 7)
        r = self.say("sabato")
        self.assertEqual(r.state, "collecting_reservation_time")
        self.assertEqual(self.date_saved(), "2026-11-21")
        self.assertNotIn("date_conflict", json.loads(self.conversation().reservation_json))

    def test_day_number_alone_is_that_day(self):
        self.say("vorrei prenotare un tavolo")
        r = self.say("il 21")
        self.assertEqual(r.state, "collecting_reservation_time")
        self.assertEqual(self.date_saved(), "2026-11-21")

    def test_unclear_answer_repeats_the_question(self):
        self.say("vorrei prenotare un tavolo")
        self.say("venerdì 21 novembre")
        r = self.say("boh")
        self.assertEqual((r.state, r.response_message), ("collecting_reservation_date", QUESTION))

    def test_another_date_replaces_the_question(self):
        self.say("vorrei prenotare un tavolo")
        self.say("venerdì 21 novembre")
        r = self.say("allora domani")
        self.assertEqual(r.state, "collecting_reservation_time")
        self.assertEqual(self.date_saved(), "2026-11-05")

    def test_new_conflict_asks_again(self):
        self.say("vorrei prenotare un tavolo")
        self.say("venerdì 21 novembre")
        r = self.say("sabato 8 novembre")
        self.assertEqual(r.response_message, "Sabato è il 7, domenica è l'8: quale dei due?")

    def test_conflict_in_the_first_sentence_keeps_the_party(self):
        r = self.say("vorrei prenotare un tavolo sabato 20 novembre per 4 persone")
        self.assertEqual((r.state, r.response_message), ("collecting_reservation_date", QUESTION))
        reservation = json.loads(self.conversation().reservation_json)
        self.assertEqual(reservation["party_size"], 4)
        self.assertNotIn("date", reservation)

    def test_chosen_day_reaches_the_saved_reservation(self):
        self.say("vorrei prenotare un tavolo")
        self.say("venerdì 21 novembre")
        r = self.book("il 20", "alle 20", "4 persone", "Giacomo", "sì")
        self.assertEqual(r.state, "reservation_completed")
        self.assertEqual(self.reservations[0]["date"], "2026-11-20")

    def test_closed_chosen_day_says_so(self):
        week = {d: "19:00-22:00" for d in ("monday", "tuesday", "wednesday", "thursday", "sunday")}
        self.restaurant = {**self.restaurant, "opening_hours": {**week, "friday": "19:00-22:00"}}
        self.say("vorrei prenotare un tavolo")
        self.say("venerdì 21 novembre")
        r = self.say("sabato")
        self.assertEqual(r.state, "collecting_reservation_date")
        self.assertIn("sabato siamo chiusi", r.response_message)


class PickupConflictFlowTests(hours._CorteHarness):
    now = NOV_4

    def test_matching_day_and_date_is_used(self):
        r = self.order("una margherita per venerdì 20 novembre")
        self.assertNotEqual(r.state, "confirming_next_day")
        self.assertEqual(self.conversation().pickup_date, "2026-11-20")

    def test_mismatch_asks_and_the_answer_is_that_day(self):
        r = self.order("una margherita per sabato 20 novembre")
        self.assertEqual((r.state, r.response_message), ("confirming_next_day", QUESTION))
        self.assertIsNone(self.conversation().pickup_date)
        r = self.say("sabato")
        self.assertEqual(self.conversation().pickup_date, "2026-11-21")
        self.assertIn("sabato 21 novembre", r.response_message)

    def test_answer_with_more_details_is_not_read_as_next_saturday(self):
        self.order("una margherita per sabato 20 novembre")
        self.say("sabato alle 20")
        self.assertEqual(self.conversation().pickup_date, "2026-11-21")

    def test_yes_does_not_choose(self):
        self.order("una margherita per sabato 20 novembre")
        r = self.say("sì")
        self.assertEqual(r.state, "confirming_next_day")
        self.assertIn(QUESTION, r.response_message)
        self.assertIsNone(self.conversation().pickup_date)

    def test_no_asks_which_day_instead_of_closing(self):
        self.order("una margherita per sabato 20 novembre")
        r = self.say("no")
        self.assertEqual((r.state, r.response_message), ("confirming_next_day", chat_module.PICKUP_DAY_QUESTION))


class ReservationClosesTheCallTests(covers._ReservationHarness):
    now = NOV_4

    def _twiml(self, result):
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": ""}):
            return asyncio.run(voice_module._build_response_twiml(result, "s-1"))

    def test_confirmed_reservation_says_goodbye_and_hangs_up(self):
        r = self.book("vorrei prenotare un tavolo", "domani", "alle 20", "4 persone", "Giacomo", "sì")
        self.assertEqual(r.state, "reservation_completed")
        self.assertTrue(r.response_message.endswith("A presto!"), r.response_message)
        root = ET.fromstring(self._twiml(r))
        self.assertEqual(root.findall("Gather"), [])
        self.assertEqual(len(root.findall("Hangup")), 1)

    def test_every_ending_state_hangs_up_and_open_states_do_not(self):
        for state in voice_module._CALL_ENDING_STATES:
            with self.subTest(state=state):
                result = types.SimpleNamespace(response_message="A presto!", state=state)
                self.assertIn("<Hangup/>", self._twiml(result))
        result = types.SimpleNamespace(response_message="A che ora?", state="collecting_reservation_time")
        self.assertNotIn("<Hangup/>", self._twiml(result))


class _FakeRequest:
    async def form(self):
        return {"CallSid": "CA1"}


class CallbackPromiseTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.addCleanup(self.session.close)

        async def spoken(text, **kwargs):
            return f"<Say>{text}</Say>"

        async def no_verify(request):
            return None

        async def no_call_log(*args, **kwargs):
            return None

        for p in (
            patch.object(voice_module, "_db_engine", self.engine),
            patch.object(voice_module, "_verify_twilio_request", no_verify),
            patch.object(voice_module, "_audio_element_async", spoken),
            patch.object(voice_module, "_call_log_update", no_call_log),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _farewell(self, reservation=None):
        self.session.add(ConversationSession(
            session_id="s-1", state="collecting_items", no_input_count=1, restaurant_id="r",
            reservation_json=json.dumps(reservation or {}),
        ))
        self.session.commit()

        async def run():
            response = await voice_module.voice_gather(
                _FakeRequest(), session_id="s-1", SpeechResult="", session=self.session,
            )
            await asyncio.gather(*(t for t in asyncio.all_tasks() if t is not asyncio.current_task()))
            return response.body.decode()

        return asyncio.run(run())

    def test_silence_without_a_request_promises_no_callback(self):
        twiml = self._farewell()
        self.assertIn("<Hangup/>", twiml)
        self.assertNotRegex(twiml.lower(), r"la richiam")
        self.assertIn("Provi a richiamarci", twiml)

    def test_silence_with_a_registered_request_keeps_the_promise(self):
        twiml = self._farewell({"pending_reservation_id": "res-1", "party_size": 12, "date": "2026-11-07", "time": "20:00"})
        self.assertIn("La richiameremo appena possibile", twiml)

    def test_transfer_without_a_session_promises_no_callback(self):
        async def run():
            response = await voice_module.voice_dial_result(_FakeRequest(), session_id="gone", DialCallStatus="no-answer")
            return response.body.decode()

        twiml = asyncio.run(run())
        self.assertNotRegex(twiml.lower(), r"la richiam")
        self.assertIn("<Hangup/>", twiml)


class SpokenTextTests(unittest.TestCase):
    def test_no_abbreviated_examples_in_the_spoken_sources(self):
        root = Path(voice_module.__file__).resolve().parent.parent
        for name in ("routes/chat.py", "routes/voice.py", "services/conversation_service.py"):
            source = (root / name).read_text()
            with self.subTest(file=name):
                self.assertEqual(re.findall(r"\(Es[:.]|\bEs:|\bad es\.", source), [])


class TimeRetryFlowTests(covers._ReservationHarness):
    now = NOV_4

    def test_time_retry_says_per_esempio(self):
        self.book("vorrei prenotare un tavolo", "domani")
        r = self.say("boh")
        self.assertEqual(r.response_message, "Non ho capito l'orario. Può indicarmi a che ora? Per esempio alle 20, o alle 8 di sera.")


if __name__ == "__main__":
    unittest.main()
