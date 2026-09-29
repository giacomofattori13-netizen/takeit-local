"""CallLog ritrovato su Base44 tramite il CallSid di Twilio."""
import asyncio
import os
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine
from sqlmodel.pool import StaticPool

from app.models import Order
from app.routes import voice as voice_module
from app.services import base44_client


class VoiceStatusCallbackTests(unittest.TestCase):
    def setUp(self):
        p = patch.dict(os.environ, {"SKIP_TWILIO_SIGNATURE_VALIDATION": "true"})
        p.start()
        self.addCleanup(p.stop)
        app = FastAPI()
        app.include_router(voice_module.router)
        self.client = TestClient(app)
        self.updates = []
        p = patch.object(base44_client, "update_call_log", side_effect=lambda log_id, data: self.updates.append((log_id, data)) or {})
        p.start()
        self.addCleanup(p.stop)

    def _post_status(self, record, **form):
        with patch.object(base44_client, "find_call_log_by_sid", return_value=record) as find:
            response = self.client.post("/voice/status", data=form)
        self.assertEqual(response.status_code, 200)
        return find

    def test_hangup_closes_open_call_log_found_by_call_sid(self):
        find = self._post_status(
            {"id": "log-1", "started_at": "2026-09-29T08:18:00+00:00", "outcome": "abbandonata", "ended_at": None},
            CallSid="CA123", CallStatus="completed", CallDuration="42",
        )

        find.assert_called_once_with("CA123")
        self.assertEqual(len(self.updates), 1)
        log_id, data = self.updates[0]
        self.assertEqual(log_id, "log-1")
        self.assertTrue(data["ended_at"])
        self.assertEqual(data["duration_seconds"], 42)
        self.assertNotIn("outcome", data)  # resta "abbandonata"

    def test_call_log_already_closed_by_the_conversation_is_left_untouched(self):
        self._post_status(
            {"id": "log-1", "ended_at": "2026-09-29T14:34:24+00:00", "outcome": "ordine"},
            CallSid="CA123", CallStatus="completed", CallDuration="52",
        )
        self.assertEqual(self.updates, [])

    def test_non_final_status_is_ignored(self):
        find = self._post_status(None, CallSid="CA123", CallStatus="in-progress")
        find.assert_not_called()
        self.assertEqual(self.updates, [])

    def test_unknown_call_sid_is_a_no_op(self):
        self._post_status(None, CallSid="CA404", CallStatus="completed", CallDuration="3")
        self.assertEqual(self.updates, [])


class CallLogLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(self.engine)
        p = patch.object(voice_module, "_db_engine", self.engine)
        p.start()
        self.addCleanup(p.stop)

    def test_create_stores_call_sid(self):
        created = []
        with patch.object(base44_client, "create_call_log", side_effect=lambda data: created.append(data) or {"id": "log-1"}):
            asyncio.run(voice_module._call_log_create("sess-1", "rest-pap", "+393331234567", "CA123"))

        self.assertEqual(created[0]["call_sid"], "CA123")
        self.assertEqual(created[0]["outcome"], "abbandonata")

    def test_order_outcome_uses_base44_order_id_and_final_number(self):
        with Session(self.engine) as session:
            session.add(Order(
                conversation_session_id="sess-1", customer_name="Giacomo", pickup_time="20:00",
                order_number=3, base44_id="b44-order-1",
            ))
            session.commit()
        updates = []
        with patch.object(base44_client, "find_call_log_by_sid", return_value={"id": "log-1", "started_at": "2026-09-29T14:33:31+00:00"}) as find, \
                patch.object(base44_client, "update_call_log", side_effect=lambda log_id, data: updates.append((log_id, data))):
            asyncio.run(voice_module._call_log_update("sess-1", "CA123", "ordine"))

        find.assert_called_once_with("CA123")
        log_id, data = updates[0]
        self.assertEqual(log_id, "log-1")
        self.assertEqual(data["outcome"], "ordine")
        self.assertEqual(data["order_id"], "b44-order-1")
        self.assertEqual(data["summary"], "Ordine #3 confermato")
        self.assertIsInstance(data["duration_seconds"], int)

    def test_find_call_log_by_sid_queries_base44_by_call_sid(self):
        with patch.dict(os.environ, {"BASE44_TOKEN": "test-key"}), \
                patch.object(base44_client, "query_entities", return_value=[{"id": "log-1"}]) as query:
            self.assertEqual(base44_client.find_call_log_by_sid("CA123"), {"id": "log-1"})
        query.assert_called_once_with("CallLog", {"call_sid": "CA123"}, timeout=8.0)


if __name__ == "__main__":
    unittest.main()
