import os
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

import app.routes.sessions as sessions_module
from app.models import ConversationSession


class SessionAuthTests(unittest.TestCase):
    def setUp(self):
        self.previous_admin_key = os.environ.get("ADMIN_API_KEY")
        os.environ["ADMIN_API_KEY"] = "test-admin-key"

        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)

        with Session(self.engine) as session:
            session.add(
                ConversationSession(
                    session_id="session-1",
                    customer_name="Mario",
                    customer_phone="+393331234567",
                    pickup_time="20:00",
                    items_json="[]",
                    state="collecting_pickup_time",
                    completed=False,
                )
            )
            session.commit()

        def override_get_session():
            with Session(self.engine) as session:
                yield session

        self.app = FastAPI()
        self.app.include_router(sessions_module.router)
        self.app.dependency_overrides[sessions_module.get_session] = override_get_session
        self.client = TestClient(self.app)

    def tearDown(self):
        if self.previous_admin_key is None:
            os.environ.pop("ADMIN_API_KEY", None)
        else:
            os.environ["ADMIN_API_KEY"] = self.previous_admin_key

    def test_session_read_requires_admin_key(self):
        response = self.client.get("/sessions/session-1")

        self.assertEqual(response.status_code, 401)

    def test_session_read_rejects_wrong_admin_key(self):
        response = self.client.get(
            "/sessions/session-1",
            headers={"X-Admin-Api-Key": "wrong-key"},
        )

        self.assertEqual(response.status_code, 401)

    def test_session_read_accepts_admin_key(self):
        response = self.client.get(
            "/sessions/session-1",
            headers={"X-Admin-Api-Key": "test-admin-key"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["session_id"], "session-1")
        self.assertEqual(response.json()["customer_phone"], "+393331234567")

    def test_session_create_remains_public(self):
        response = self.client.post(
            "/sessions/",
            json={"test_phone": "+393331111111"},
        )

        self.assertEqual(response.status_code, 200)
        session_id = response.json()["session_id"]

        with Session(self.engine) as session:
            created = session.exec(
                select(ConversationSession).where(
                    ConversationSession.session_id == session_id
                )
            ).first()

        self.assertIsNotNone(created)
        self.assertEqual(created.customer_phone, "+393331111111")


if __name__ == "__main__":
    unittest.main()
