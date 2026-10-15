"""Latenza per turno: timestamp in memoria durante la chiamata, salvati sul CallLog a fine chiamata."""
import asyncio
import os
import re
import types
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine
from sqlmodel.pool import StaticPool

from app import turn_latency
from app.db import get_session
from app.models import ConversationSession
from app.routes import voice as voice_module
from app.services import base44_client


class TurnLatencyUnitTests(unittest.TestCase):
    def setUp(self):
        turn_latency.clear()
        self.addCleanup(turn_latency.clear)

    def test_summary_computes_stage_durations_and_perceived_latency(self):
        turn = {
            "turn": 1, "state": "collecting_items", "path": "direct", "audio_source": "stream",
            "user_speech_end": None, "stt_final": 1000, "llm_request_sent": 1040,
            "llm_first_token": None, "llm_complete": 1900, "tts_request_sent": 2000,
            "tts_first_byte": 2300, "first_audio_sent_to_twilio": 2310,
            "tools": [{"name": "base44_menu", "start": 1005, "end": 1030}], "_t0": 0.0,
        }
        summary = turn_latency.summarize_turn(turn)
        self.assertNotIn("_t0", summary)
        self.assertEqual(summary["durations"]["llm_total"], 860)
        self.assertEqual(summary["durations"]["tts_ttfb"], 300)
        self.assertIsNone(summary["durations"]["llm_ttft"])
        self.assertEqual(summary["durations"]["tools_total"], 25)
        self.assertEqual(summary["tools"][0]["duration"], 25)
        # user_speech_end non misurabile: la latenza percepita parte da stt_final
        self.assertEqual(summary["perceived_from"], "stt_final")
        self.assertEqual(summary["perceived_latency"], 1310)

    def test_marks_are_noops_without_a_current_turn(self):
        turn_latency.mark("llm_request_sent")
        turn_latency.annotate(path="filler")

        @turn_latency.traced_tool("x")
        def f():
            return 3

        self.assertEqual(f(), 3)

    def test_turn_context_reaches_threads_and_tools(self):
        turn_latency.start_call("CA1", "s-1")

        @turn_latency.traced_tool("base44_menu")
        def fetch_menu():
            turn_latency.mark("llm_request_sent")
            return []

        async def run():
            turn_latency.start_turn("CA1", "s-1", state="collecting_items")
            await asyncio.to_thread(fetch_menu)

        asyncio.run(run())
        trace = turn_latency.pop_call("CA1")
        turn = trace["turns"][0]
        self.assertIsNotNone(turn["stt_final"])
        self.assertIsNotNone(turn["llm_request_sent"])
        self.assertEqual([t["name"] for t in turn["tools"]], ["base44_menu"])
        self.assertIsNone(turn_latency.pop_call("CA1"))


class VoiceTurnLatencyFlowTests(unittest.TestCase):
    def setUp(self):
        turn_latency.clear()
        self.addCleanup(turn_latency.clear)
        p = patch.dict(os.environ, {
            "SKIP_TWILIO_SIGNATURE_VALIDATION": "true",
            "ELEVENLABS_API_KEY": "k",
            "ELEVENLABS_VOICE_ID": "v",
        })
        p.start()
        self.addCleanup(p.stop)
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as db:
            db.add(ConversationSession(session_id="s-1", items_json="[]", state="awaiting_confirmation"))
            db.commit()

        def _session():
            with Session(self.engine) as db:
                yield db

        app = FastAPI()
        app.include_router(voice_module.router)
        app.dependency_overrides[get_session] = _session
        self.client = TestClient(app)

        def fake_chat(_chat, _request):
            # Gira nel thread di asyncio.to_thread: il turno corrente arriva dalla ContextVar
            turn_latency.mark("llm_request_sent")
            turn_latency.mark("llm_complete")
            return types.SimpleNamespace(
                response_message="Perfetto, confermo il suo ordine numero sette?",
                state="awaiting_confirmation", order_id=None, merged_order={},
            )

        for target, value in (
            ("_run_chat_with_fresh_session", fake_chat),
            ("_prefetch_openai_connection", AsyncMock()),
            ("_call_log_update", AsyncMock()),
        ):
            p = patch.object(voice_module, target, value)
            p.start()
            self.addCleanup(p.stop)

        tts = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"\xff\xfb" * 3000)
        ))
        p = patch.object(voice_module, "_get_tts_stream_client", AsyncMock(return_value=tts))
        p.start()
        self.addCleanup(p.stop)

    def test_gather_and_stream_fill_the_turn_then_flush_saves_it_on_the_call_log(self):
        turn_latency.start_call("CA1", "s-1")
        response = self.client.post(
            "/voice/gather?session_id=s-1", data={"SpeechResult": "sì confermo", "CallSid": "CA1"},
        )
        self.assertEqual(response.status_code, 200)
        stream_id = re.search(r"/voice/stream/([0-9a-f-]+)", response.text).group(1)
        audio = self.client.get(f"/voice/stream/{stream_id}")
        self.assertEqual(audio.status_code, 200)

        updates = []
        with patch.object(base44_client, "find_call_log_by_sid", return_value={"id": "log-1"}), \
                patch.object(base44_client, "update_call_log", side_effect=lambda i, d: updates.append((i, d))):
            asyncio.run(turn_latency.flush_call("CA1"))

        self.assertEqual(len(updates), 1)
        log_id, patch_data = updates[0]
        self.assertEqual(log_id, "log-1")
        [turn] = patch_data["latency_turns"]
        self.assertEqual(turn["audio_source"], "stream")
        for name in ("stt_final", "llm_request_sent", "llm_complete", "tts_request_sent",
                     "tts_first_byte", "first_audio_sent_to_twilio", "tts_complete"):
            self.assertIsNotNone(turn[name], name)
        self.assertIsNone(turn["user_speech_end"])
        self.assertIsNone(turn["llm_first_token"])
        self.assertGreaterEqual(turn["perceived_latency"], 0)

    def test_final_status_schedules_the_flush(self):
        with patch.object(voice_module, "_call_log_close_from_status", AsyncMock()), \
                patch.object(turn_latency, "flush_call", AsyncMock()) as flush:
            self.client.post("/voice/status", data={"CallSid": "CA1", "CallStatus": "completed"})
        flush.assert_called_once_with("CA1")


if __name__ == "__main__":
    unittest.main()
