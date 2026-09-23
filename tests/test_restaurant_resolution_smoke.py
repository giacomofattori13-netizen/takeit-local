"""Test di fumo: /voice/incoming e SMS del titolare passano da resolve_restaurant_from_phone.

Base44 è simulato a livello HTTP (httpx.get in base44_client), così il test esercita
davvero gli import e la logica di risoluzione del ristorante.
"""
import os
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine, select
from sqlmodel.pool import StaticPool

from app.db import get_session
from app.models import ConversationSession
from app.routes import sms as sms_module
from app.routes import voice as voice_module
from app.services import conversation_service as service

PAP_PHONE = "+390212345678"
CDS_PHONE = "+390287654321"
OWNER_PHONE = "+393331112222"

RESTAURANTS = [
    {
        "id": "rest-pap",
        "name": "Pizza a Pezzi",
        "agent_phone": PAP_PHONE,
        "agent_active": True,
        "agent_greeting": "Pizza a Pezzi, buonasera. Come posso aiutarla?",
        "reservations_enabled": False,
    },
    {
        "id": "rest-cds",
        "name": "Corte del Sole",
        "agent_phone": CDS_PHONE,
        "agent_active": True,
        "agent_greeting": "Corte del Sole, buonasera. Come posso aiutarla?",
        "reservations_enabled": True,
    },
]


def _fake_base44_get(restaurants):
    def _get(url, params=None, timeout=None):
        response = MagicMock()
        response.raise_for_status.return_value = None
        if url.endswith("/Restaurant"):
            response.json.return_value = restaurants
        else:
            response.json.return_value = []
        return response
    return _get


class _EnvMixin:
    ENV = {}

    def _set_env(self):
        self._previous_env = {key: os.environ.get(key) for key in self.ENV}
        for key, value in self.ENV.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _restore_env(self):
        for key, value in self._previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class VoiceIncomingSmokeTests(_EnvMixin, unittest.TestCase):
    ENV = {
        "SKIP_TWILIO_SIGNATURE_VALIDATION": "true",
        "BASE44_API_KEY": "test-key",
        "DEFAULT_RESTAURANT_ID": None,
        "ELEVENLABS_API_KEY": None,
        "ELEVENLABS_VOICE_ID": None,
    }

    def setUp(self):
        self._set_env()
        service.reset_restaurant_cache()
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(self.engine)

        def _session_override():
            with Session(self.engine) as session:
                yield session

        self.app = FastAPI()
        self.app.include_router(voice_module.router)
        self.app.dependency_overrides[get_session] = _session_override
        self.client = TestClient(self.app)

        self.patches = [
            patch("app.services.base44_client.httpx.get", side_effect=_fake_base44_get(RESTAURANTS)),
            patch.object(voice_module, "lookup_customer", return_value=None),
            patch("app.services.base44_client.create_call_log", return_value=None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        service.reset_restaurant_cache()
        self._restore_env()

    def _sessions(self):
        with Session(self.engine) as session:
            return session.exec(select(ConversationSession)).all()

    def test_incoming_call_resolves_restaurant_from_called_number(self):
        response = self.client.post(
            "/voice/incoming", data={"From": "+393339998888", "To": CDS_PHONE}
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("<Gather", response.text)
        self.assertIn("Corte del Sole", response.text)
        sessions = self._sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].restaurant_id, "rest-cds")

    def test_incoming_call_to_unknown_number_gets_service_unavailable(self):
        response = self.client.post(
            "/voice/incoming", data={"From": "+393339998888", "To": "+390299999999"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(service.SERVICE_UNAVAILABLE_MESSAGE, response.text)
        self.assertIn("<Hangup/>", response.text)
        self.assertNotIn("<Gather", response.text)
        self.assertEqual(self._sessions(), [])

    def test_incoming_call_with_base44_down_gets_service_unavailable(self):
        with patch("app.services.base44_client.httpx.get", side_effect=RuntimeError("down")):
            response = self.client.post(
                "/voice/incoming", data={"From": "+393339998888", "To": CDS_PHONE}
            )

        self.assertIn(service.SERVICE_UNAVAILABLE_MESSAGE, response.text)
        self.assertIn("<Hangup/>", response.text)
        self.assertEqual(self._sessions(), [])


class OwnerSmsSmokeTests(_EnvMixin, unittest.TestCase):
    ENV = {
        "BASE44_API_KEY": "test-key",
        "OWNER_PHONE": OWNER_PHONE,
        "DEFAULT_RESTAURANT_ID": None,
    }

    def setUp(self):
        self._set_env()
        service.reset_restaurant_cache()
        self.app = FastAPI()
        self.app.include_router(sms_module.router)
        self.client = TestClient(self.app)

        self.applied: list[tuple[str, str]] = []
        self.replies: list[str] = []
        self.patches = [
            patch("app.services.base44_client.httpx.get", side_effect=_fake_base44_get(RESTAURANTS)),
            patch("app.services.base44_client.create_owner_command", return_value={"id": "cmd-1"}),
            patch("app.services.base44_client.update_owner_command", return_value={}),
            patch.object(
                sms_module,
                "_interpret_command",
                return_value={"action": "sold_out", "ingredient": "bufala", "item_name": None},
            ),
            patch.object(
                sms_module,
                "_apply_sold_out",
                side_effect=lambda ingredient, restaurant_id="": self.applied.append((ingredient, restaurant_id)) or "ok",
            ),
            patch.object(sms_module, "_send_reply", side_effect=lambda to, body: self.replies.append(body)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        service.reset_restaurant_cache()
        self._restore_env()

    def test_owner_sms_applies_command_to_restaurant_of_called_number(self):
        response = self.client.post(
            "/sms/incoming",
            data={"From": OWNER_PHONE, "To": PAP_PHONE, "Body": "finita la bufala"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.applied, [("bufala", "rest-pap")])
        self.assertEqual(self.replies, ["ok"])

    def test_owner_sms_to_unknown_number_is_not_applied(self):
        from app.services import base44_client

        response = self.client.post(
            "/sms/incoming",
            data={"From": OWNER_PHONE, "To": "+390299999999", "Body": "finita la bufala"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.applied, [])
        base44_client.create_owner_command.assert_not_called()
        self.assertEqual(len(self.replies), 1)
        self.assertIn("non disponibile", self.replies[0])


if __name__ == "__main__":
    unittest.main()
