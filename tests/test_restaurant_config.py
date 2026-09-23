import asyncio
import json
import os
import unittest
from unittest.mock import patch

from sqlmodel import SQLModel, Session, create_engine, select

import app.routes.chat as chat_module
import app.routes.voice as voice_module
from app.models import ConversationLog, ConversationSession, Order
from app.schemas import ChatRequest, ChatResponse
from app.services import conversation_service as service

FULL_CONFIG = {"id": "rest-cds", "opening_hours": {"monday": "19:00-23:00"}, "agent_active": True}


class EnsureRestaurantConfigTests(unittest.TestCase):
    def setUp(self):
        service.reset_restaurant_cache()

    def tearDown(self):
        service.reset_restaurant_cache()

    def test_legacy_flow_without_restaurant_id_is_always_ok(self):
        with patch.object(service, "_refresh_restaurant_cache_blocking", side_effect=AssertionError("no fetch")):
            self.assertTrue(service.ensure_restaurant_config(""))

    def test_cached_complete_config_needs_no_fetch(self):
        with (
            patch.object(service, "load_restaurant", return_value=FULL_CONFIG),
            patch.object(service, "_refresh_restaurant_cache_blocking", side_effect=AssertionError("no fetch")),
        ):
            self.assertTrue(service.ensure_restaurant_config("rest-cds"))

    def test_missing_config_is_fetched_blocking(self):
        with (
            patch.object(service, "load_restaurant", return_value={}),
            patch.object(service, "_refresh_restaurant_cache_blocking", return_value=FULL_CONFIG) as refresh,
        ):
            self.assertTrue(service.ensure_restaurant_config("rest-cds"))
        refresh.assert_called_once_with("rest-cds")

    def test_config_without_opening_hours_is_refetched(self):
        with (
            patch.object(service, "load_restaurant", return_value={"id": "rest-cds"}),
            patch.object(service, "_refresh_restaurant_cache_blocking", return_value=FULL_CONFIG) as refresh,
        ):
            self.assertTrue(service.ensure_restaurant_config("rest-cds"))
        refresh.assert_called_once()

    def test_base44_down_with_no_config_is_not_ok(self):
        with (
            patch.object(service, "load_restaurant", return_value={}),
            patch.object(service, "_refresh_restaurant_cache_blocking", return_value=None),
        ):
            self.assertFalse(service.ensure_restaurant_config("rest-cds"))

    def test_cold_cache_does_not_use_other_restaurant_file(self):
        other = {"id": "rest-pap", "opening_hours": {"monday": "11:00-14:00"}}
        with (
            patch.object(service, "_load_restaurant_from_file", return_value=other),
            patch.object(service, "_start_restaurant_refresh_background", return_value=True),
            patch.object(service, "_fetch_restaurant_from_base44_for", return_value=None),
        ):
            self.assertFalse(service.ensure_restaurant_config("rest-cds"))


class ChatServiceUnavailableTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)

    def _conversation(self, session, restaurant_id):
        conversation = ConversationSession(
            session_id="cfg-missing",
            items_json=json.dumps([{"pizza_name": "Diavola", "quantity": 1}]),
            state="collecting_name",
            completed=False,
            restaurant_id=restaurant_id,
        )
        session.add(conversation)
        session.commit()

    def test_missing_config_returns_service_message_and_touches_nothing(self):
        with Session(self.engine) as session:
            self._conversation(session, "rest-cds")
            with (
                patch.object(chat_module, "ensure_restaurant_config", return_value=False) as ensure,
                patch.object(chat_module, "is_agent_active", side_effect=AssertionError("must stop before")),
                patch.object(chat_module, "extract_order_from_text", side_effect=AssertionError("no LLM")),
            ):
                response = chat_module.chat(ChatRequest(session_id="cfg-missing", message="Mario"), session)

            ensure.assert_called_once_with("rest-cds")
            self.assertEqual(response.state, "unavailable")
            self.assertEqual(response.response_message, service.SERVICE_UNAVAILABLE_MESSAGE)
            self.assertIsNone(response.order_id)
            conversation = session.exec(select(ConversationSession)).one()
            self.assertEqual(conversation.state, "collecting_name")
            self.assertFalse(conversation.completed)
            self.assertIsNone(conversation.customer_name)
            self.assertEqual(session.exec(select(Order)).all(), [])
            self.assertEqual(session.exec(select(ConversationLog)).all(), [])

    def test_available_config_continues_normal_flow(self):
        with Session(self.engine) as session:
            self._conversation(session, "rest-cds")
            with (
                patch.object(chat_module, "ensure_restaurant_config", return_value=True),
                patch.object(chat_module, "is_agent_active", return_value=False),
                patch.object(chat_module, "build_closed_message", return_value="Siamo chiusi."),
            ):
                response = chat_module.chat(ChatRequest(session_id="cfg-missing", message="Mario"), session)

        self.assertEqual(response.state, "closed")


class VoiceUnavailableTwimlTests(unittest.TestCase):
    def setUp(self):
        self.previous = {k: os.environ.get(k) for k in ("ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID")}
        for key in self.previous:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self.previous.items():
            if value is not None:
                os.environ[key] = value

    def _twiml(self, state):
        result = ChatResponse(
            session_id="s", user_message="", extracted_order={}, merged_order={}, valid=False,
            missing_items=[], response_message=service.SERVICE_UNAVAILABLE_MESSAGE, state=state,
        )
        return asyncio.run(voice_module._build_response_twiml(result, "s"))

    def test_unavailable_state_hangs_up(self):
        twiml = self._twiml("unavailable")

        self.assertIn("<Hangup/>", twiml)
        self.assertNotIn("<Gather", twiml)

    def test_other_states_keep_gathering(self):
        self.assertIn("<Gather", self._twiml("collecting_name"))


if __name__ == "__main__":
    unittest.main()
