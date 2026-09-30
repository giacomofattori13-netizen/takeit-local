"""Ogni risposta che fa una domanda deve tenere aperta la chiamata: il TwiML deve
contenere un <Gather> con action (e actionOnEmptyResult, così anche il silenzio
torna al server) oppure un <Redirect>. Senza, Twilio chiude la chiamata."""
import asyncio
import os
import types
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

import app.routes.voice as voice_module
from tests.test_next_day_preorder_flow import CorteDelSoleUnaffectedTests, PreorderFlowTests

_TERMINAL_STATES = {"completed", "unavailable"}


def _build(result) -> str:
    with patch.dict(os.environ, {"ELEVENLABS_API_KEY": ""}):
        return asyncio.run(voice_module._build_response_twiml(result, "s-1"))


class _TwimlAssertions:
    def assertKeepsCallOpen(self, twiml: str, context: str = ""):
        root = ET.fromstring(twiml)
        gathers = root.findall("Gather")
        if root.findall("Redirect"):
            return
        self.assertTrue(gathers, f"nessun Gather né Redirect: {context!r}\n{twiml}")
        for gather in gathers:
            self.assertTrue(gather.get("action"), f"Gather senza action: {context!r}")
            self.assertEqual(gather.get("actionOnEmptyResult"), "true", f"Gather senza actionOnEmptyResult: {context!r}")
            self.assertTrue(gather.get("timeout"), f"Gather senza timeout: {context!r}")

    def assertQuestionKeepsCallOpen(self, result):
        is_question = result.response_message.rstrip().endswith("?")
        if result.state in _TERMINAL_STATES:
            self.assertFalse(is_question, f"domanda in stato terminale {result.state!r}: {result.response_message!r}")
            return
        self.assertKeepsCallOpen(_build(result), result.response_message)


class GatherBuildersTests(_TwimlAssertions, unittest.TestCase):
    def test_every_open_state_keeps_the_call_open(self):
        states = [
            "collecting_items", "collecting_kg_portion", "collecting_kg_temperature",
            "collecting_name", "collecting_pickup_time", "awaiting_confirmation",
            "collecting_reservation_date", "collecting_reservation_time",
            "collecting_reservation_party", "collecting_reservation_name",
            "awaiting_reservation_confirmation",
        ]
        for state in states:
            with self.subTest(state=state):
                result = types.SimpleNamespace(response_message="A che ora passa?", state=state)
                self.assertKeepsCallOpen(_build(result), state)

    def test_retry_gather_keeps_the_call_open(self):
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": ""}):
            twiml = asyncio.run(voice_module._build_retry_gather_twiml("s-1"))
        self.assertKeepsCallOpen(twiml, "retry")


def _checking_say(test_class):
    """Rilancia gli scenari del flusso controllando il TwiML di ogni risposta."""

    class Checked(_TwimlAssertions, test_class):
        def say(self, message, extraction=None):
            result = super().say(message, extraction)
            self.assertQuestionKeepsCallOpen(result)
            return result

    Checked.__name__ = f"{test_class.__name__}TwimlCheck"
    Checked.__qualname__ = Checked.__name__
    return Checked


PreorderFlowTwimlTests = _checking_say(PreorderFlowTests)
CorteDelSoleTwimlTests = _checking_say(CorteDelSoleUnaffectedTests)


if __name__ == "__main__":
    unittest.main()
