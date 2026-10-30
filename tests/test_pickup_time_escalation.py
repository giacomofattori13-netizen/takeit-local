"""Orario (e nome) non capito: parsing tollerante prima dell'LLM, mai la stessa
domanda due volte di fila, proposta sì/no e ordine da verificare al terzo fallimento.

Caso reale: CallLog 6ae3803d9a8552f9999f8609, "per le otto" trascritto
"Per le hotpoint." e poi "Parte.", e l'agente ripeteva "Per che ora?".
"""
import asyncio
import contextlib
import io
import os
import types
import unittest
from unittest.mock import patch

import app.routes.chat as chat_module
import app.routes.voice as voice_module
import tests.test_next_day_preorder_flow as flow
from app.routes.chat import (
    NAME_MISS_MESSAGE,
    PICKUP_TIME_MISS_MESSAGE,
    _extract_local_pickup_time,
)

NOTHING = flow.NOTHING


class PickupTimeParsingTests(unittest.TestCase):
    def test_spoken_times(self):
        cases = {
            "alle otto": "20:00",
            "per le 8": "20:00",
            "le otto e mezza": "20:30",
            "Alle sette e un quarto.": "19:15",
            "alle otto meno un quarto": "19:45",
            "per le nove": "21:00",
            "alle 20:30": "20:30",
            "sei e mezza": "18:30",
            "dieci": "22:00",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(_extract_local_pickup_time(text), expected)

    def test_stt_distortions_of_otto(self):
        for text in ("Per l'otto? Perde 8", "allotto", "per le ottoe mezza", "alle 8pm"):
            with self.subTest(text=text):
                self.assertIn(_extract_local_pickup_time(text), ("20:00", "20:30"))

    def test_not_a_time(self):
        for text in ("Per le hotpoint.", "Parte.", "il 3 ottobre", "boh"):
            with self.subTest(text=text):
                self.assertIsNone(_extract_local_pickup_time(text))

    def test_morning_hours_stay_morning_when_not_evening_only(self):
        self.assertEqual(_extract_local_pickup_time("alle otto", evening_only=False), "8:00")
        self.assertEqual(_extract_local_pickup_time("alle otto di sera", evening_only=False), "20:00")


class _AtPickupTime(flow._FlowHarness):
    def setUp(self):
        super().setUp()
        self.order_up_to_name()
        self.entry = self.say("Giacomo")
        self.assertEqual(self.entry.state, "collecting_pickup_time")

    def say_logged(self, message, extraction=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = self.say(message, extraction)
        lines = [line for line in out.getvalue().splitlines() if line.startswith("[PickupTime]")]
        return result, lines


class PickupTimeEscalationTests(_AtPickupTime):
    """Pizza a Pezzi: preordine per martedì, apertura 17:00."""

    def test_hotpoint_gets_the_first_escalation_message(self):
        r = self.say("Per le hotpoint.")
        self.assertEqual((r.state, r.response_message), ("collecting_pickup_time", PICKUP_TIME_MISS_MESSAGE))

    def test_spoken_times_are_recognised_without_llm(self):
        for text, expected in (("alle otto", "20:00"), ("per le 8", "20:00"), ("le otto e mezza", "20:30")):
            with self.subTest(text=text):
                conv = self.conversation()
                conv.state, conv.pickup_time = "collecting_pickup_time", None
                self.session.add(conv)
                self.session.commit()
                calls = self.llm_calls
                r = self.say(text)
                self.assertEqual(r.state, "awaiting_confirmation")
                self.assertEqual(r.merged_order["pickup_time"], expected)
                self.assertEqual(self.llm_calls, calls)

    def test_three_failures_save_the_order_at_the_first_slot_with_needs_review(self):
        r1 = self.say("Per le hotpoint.")
        r2 = self.say("Parte.")
        self.assertEqual((r2.state, r2.response_message), ("collecting_pickup_time", "Va bene alle 17? Mi dica sì o no."))
        r3 = self.say("boh")
        self.assertEqual(r3.state, "completed")
        self.assertEqual(r3.response_message, "Segno alle 17, il locale la richiama se serve. Grazie, a presto!")
        self.assertIsNotNone(r3.order_id)
        self.assertEqual(len(self.saved), 1)
        saved = self.saved[0]
        self.assertEqual(saved["pickup_time"], "17:00")
        self.assertEqual(saved["pickup_date"], "2026-10-06")
        self.assertTrue(any(reason.startswith("Orario non capito") for reason in saved["review_reasons"]))
        self.assertEqual(r1.response_message, PICKUP_TIME_MISS_MESSAGE)

    def test_no_to_the_proposal_is_the_third_failure(self):
        self.say("Per le hotpoint.")
        self.say("Parte.")
        calls = self.llm_calls
        r = self.say("no")
        self.assertEqual(r.state, "completed")
        self.assertEqual(self.llm_calls, calls)
        self.assertTrue(self.saved[0]["review_reasons"])

    def test_yes_to_the_proposal_sets_the_slot_without_review(self):
        self.say("Per le hotpoint.")
        self.say("Parte.")
        r = self.say("sì va bene")
        self.assertEqual(r.state, "awaiting_confirmation")
        self.assertEqual(r.merged_order["pickup_time"], "17:00")
        self.say("sì")
        self.assertIsNone(self.saved[0].get("review_reasons"))

    def test_a_time_after_a_failure_resets_the_count(self):
        self.say("Per le hotpoint.")
        r = self.say("alle otto")
        self.assertEqual(r.state, "awaiting_confirmation")
        self.assertEqual(self.conversation().slot_attempts_json, "{}")

    def test_never_the_same_answer_twice_in_a_row(self):
        responses = [self.entry.response_message]
        for text in ("Per le hotpoint.", "Parte."):
            responses.append(self.say(text).response_message)
        responses.append(self.say("ehm").response_message)
        for previous, current in zip(responses, responses[1:]):
            self.assertNotEqual(previous, current)

    def test_closed_time_said_twice_gets_a_proposal_not_the_same_error(self):
        r1 = self.say("alle 23")
        r2 = self.say("alle 23")
        self.assertEqual(r1.state, "collecting_pickup_time")
        self.assertNotEqual(r1.response_message, r2.response_message)
        self.assertEqual(r2.response_message, "Va bene alle 17? Mi dica sì o no.")

    def test_log_reports_source_and_failures_without_user_text(self):
        _, lines = self.say_logged("Per le hotpoint.")
        self.assertEqual(lines, ["[PickupTime] riconosciuto=nessuno fallimenti=1 stato=collecting_pickup_time"])
        _, lines = self.say_logged("dopo cena", {**NOTHING, "intent": "set_pickup_time", "pickup_time": "20:00"})
        self.assertEqual(lines, ["[PickupTime] riconosciuto=llm fallimenti=1 stato=awaiting_confirmation"])

    def test_log_for_rules(self):
        _, lines = self.say_logged("alle otto")
        self.assertEqual(lines, ["[PickupTime] riconosciuto=regole fallimenti=0 stato=awaiting_confirmation"])
        self.assertNotIn("otto", lines[0])


class PickupTimeSameDayTests(_AtPickupTime):
    """Corte del Sole: ritiro in giornata, aperto tutto il giorno (non solo sera)."""

    restaurant_id = flow.CDS_ID

    def test_alle_otto_in_the_afternoon_is_20(self):
        r = self.say("alle otto")
        self.assertEqual(r.merged_order["pickup_time"], "20:00")

    def test_three_failures_use_the_first_slot_today(self):
        self.say("Per le hotpoint.")
        r = self.say("Parte.")
        # domenica 15:00 + 20 minuti di preparazione → 15:30
        self.assertEqual(r.response_message, "Va bene alle 15:30? Mi dica sì o no.")
        r = self.say("boh")
        self.assertEqual(r.state, "completed")
        self.assertEqual(self.saved[0]["pickup_time"], "15:30")


class NameEscalationTests(flow._FlowHarness):
    def setUp(self):
        super().setUp()
        r = self.order_up_to_name()
        self.assertEqual((r.state, r.response_message), ("collecting_name", "A che nome?"))

    def test_second_failure_proposes_what_was_heard(self):
        r = self.say("aspetti 1 attimo")
        self.assertEqual((r.state, r.response_message), ("collecting_name", NAME_MISS_MESSAGE))
        r = self.say("Marco 3")
        self.assertEqual(r.response_message, "Ho capito Marco, giusto? Mi dica sì o no.")
        r = self.say("sì")
        self.assertEqual(r.state, "collecting_pickup_time")
        self.assertEqual(self.conversation().customer_name, "Marco")

    def test_three_failures_continue_without_name_and_need_review(self):
        self.say("aspetti 1 attimo")
        self.say("Marco 3")
        r = self.say("no")
        self.assertEqual(r.state, "collecting_pickup_time")
        self.assertTrue(r.response_message.startswith("Segno l'ordine senza nome, il locale la richiama se serve."))
        r = self.say("alle 8")
        self.assertNotIn("a nome", r.response_message)
        self.say("sì")
        saved = self.saved[0]
        self.assertEqual(saved["customer_name"], chat_module.UNKNOWN_CUSTOMER_NAME)
        self.assertIn("Nome non capito: chiedere il nome al cliente", saved["review_reasons"])


class GatherHintsTests(unittest.TestCase):
    def _twiml(self, state):
        result = types.SimpleNamespace(response_message="A che ora passa?", state=state)
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": ""}):
            return asyncio.run(voice_module._build_response_twiml(result, "s-1"))

    def test_pickup_time_and_next_day_gathers_have_italian_time_hints(self):
        for state in ("collecting_pickup_time", "confirming_next_day"):
            with self.subTest(state=state):
                twiml = self._twiml(state)
                self.assertIn('language="it-IT"', twiml)
                self.assertIn("alle otto e mezza", twiml)
                self.assertIn("prima possibile", twiml)

    def test_other_gathers_have_no_hints(self):
        self.assertNotIn("hints=", self._twiml("collecting_items"))

    def test_existing_hints_are_merged_without_duplicates(self):
        with patch.object(voice_module, "_GATHER_ATTRS", voice_module._GATHER_ATTRS + ' hints="alle otto,margherita"'):
            attrs = voice_module._gather_attrs("collecting_pickup_time")
        self.assertEqual(attrs.count("hints="), 1)
        hints = attrs.split('hints="')[1].split('"')[0].split(",")
        self.assertEqual(hints[:2], ["alle otto", "margherita"])
        self.assertEqual(len(hints), len(set(hints)))


if __name__ == "__main__":
    unittest.main()
