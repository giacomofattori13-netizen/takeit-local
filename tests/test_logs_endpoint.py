import os
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

import app.routes.logs as logs_module
from app.models import ConversationLog


class LogsEndpointTests(unittest.TestCase):
    def setUp(self):
        self.previous_admin_key = os.environ.get("ADMIN_API_KEY")
        os.environ["ADMIN_API_KEY"] = "test-admin-key"

        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)

        def override_get_session():
            with Session(self.engine) as session:
                yield session

        self.app = FastAPI()
        self.app.include_router(logs_module.router)
        self.app.dependency_overrides[logs_module.get_session] = override_get_session
        self.client = TestClient(self.app)

    def tearDown(self):
        if self.previous_admin_key is None:
            os.environ.pop("ADMIN_API_KEY", None)
        else:
            os.environ["ADMIN_API_KEY"] = self.previous_admin_key

    def _add_log(
        self,
        session_id: str,
        user_message: str,
        extracted_order_json: str = "{}",
        merged_order_json: str = "{}",
        missing_items_json: str = "[]",
    ):
        with Session(self.engine) as session:
            session.add(
                ConversationLog(
                    session_id=session_id,
                    user_message=user_message,
                    extracted_order_json=extracted_order_json,
                    merged_order_json=merged_order_json,
                    response_message="ok",
                    valid=True,
                    missing_items_json=missing_items_json,
                    state="collecting_items",
                )
            )
            session.commit()

    def test_logs_requires_admin_key(self):
        response = self.client.get("/logs/")

        self.assertEqual(response.status_code, 401)

    def test_logs_are_ordered_and_limited(self):
        self._add_log("session-1", "first")
        self._add_log("session-1", "second")
        self._add_log("session-1", "third")

        response = self.client.get(
            "/logs/?limit=2",
            headers={"X-Admin-Api-Key": "test-admin-key"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [entry["user_message"] for entry in response.json()],
            ["third", "second"],
        )

    def test_logs_support_offset_and_session_filter(self):
        self._add_log("session-1", "first")
        self._add_log("session-2", "second")
        self._add_log("session-2", "third")

        response = self.client.get(
            "/logs/?session_id=session-2&offset=1&limit=1",
            headers={"X-Admin-Api-Key": "test-admin-key"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.json()[0]["user_message"], "second")

    def test_corrupt_json_fields_do_not_break_logs_endpoint(self):
        self._add_log(
            "session-1",
            "bad json",
            extracted_order_json="{broken",
            merged_order_json="",
            missing_items_json="{broken",
        )

        response = self.client.get(
            "/logs/",
            headers={"X-Admin-Api-Key": "test-admin-key"},
        )

        self.assertEqual(response.status_code, 200)
        entry = response.json()[0]
        self.assertEqual(entry["extracted_order"], {})
        self.assertEqual(entry["merged_order"], {})
        self.assertEqual(entry["missing_items"], [])

    def test_limit_is_capped(self):
        response = self.client.get(
            "/logs/?limit=501",
            headers={"X-Admin-Api-Key": "test-admin-key"},
        )

        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
