import json
import os
import sys
import tempfile
import types
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.routes.owner_command as owner_command_module
from app.routes.owner_command import _parse_owner_action


class OwnerCommandParsingTests(unittest.TestCase):
    def test_parse_valid_action_with_extra_text(self):
        action = _parse_owner_action(
            'Risposta:\n{"action": "remove_ingredient", "ingredient": " funghi "}'
        )

        self.assertEqual(action["action"], "remove_ingredient")
        self.assertEqual(action["ingredient"], "funghi")

    def test_parse_fenced_json(self):
        action = _parse_owner_action(
            '```json\n{"action": "disable_pizza", "pizza_name": "Margherita"}\n```'
        )

        self.assertEqual(action["action"], "disable_pizza")
        self.assertEqual(action["pizza_name"], "Margherita")

    def test_malformed_json_returns_unknown(self):
        action = _parse_owner_action("{broken")

        self.assertEqual(action["action"], "unknown")
        self.assertEqual(action["reason"], "Claude non ha restituito JSON valido")

    def test_missing_required_field_returns_unknown(self):
        action = _parse_owner_action('{"action": "disable_pizza"}')

        self.assertEqual(action["action"], "unknown")
        self.assertEqual(action["reason"], "Nome pizza mancante")

    def test_invalid_action_returns_unknown(self):
        action = _parse_owner_action('{"action": "delete_everything"}')

        self.assertEqual(action["action"], "unknown")
        self.assertEqual(action["reason"], "Claude ha restituito azione non valida")


class OwnerCommandEndpointTests(unittest.TestCase):
    def setUp(self):
        self.previous_admin_key = os.environ.get("ADMIN_API_KEY")
        self.previous_anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
        self.previous_anthropic_module = sys.modules.get("anthropic")
        self.previous_menu_path = owner_command_module.MENU_JSON_PATH
        self.previous_sync_menu_to_db = owner_command_module.sync_menu_to_db

        os.environ["ADMIN_API_KEY"] = "test-admin-key"
        os.environ["ANTHROPIC_API_KEY"] = "test-anthropic-key"

        self.tempdir = tempfile.TemporaryDirectory()
        self.menu_path = os.path.join(self.tempdir.name, "menu.json")
        with open(self.menu_path, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {
                        "name": "Margherita",
                        "ingredients": ["pomodoro", "mozzarella"],
                        "dough_type": "classica",
                        "available": True,
                    }
                ],
                f,
            )

        owner_command_module.MENU_JSON_PATH = self.menu_path
        owner_command_module.sync_menu_to_db = lambda: 1

    def tearDown(self):
        if self.previous_admin_key is None:
            os.environ.pop("ADMIN_API_KEY", None)
        else:
            os.environ["ADMIN_API_KEY"] = self.previous_admin_key

        if self.previous_anthropic_api_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = self.previous_anthropic_api_key

        if self.previous_anthropic_module is None:
            sys.modules.pop("anthropic", None)
        else:
            sys.modules["anthropic"] = self.previous_anthropic_module

        owner_command_module.MENU_JSON_PATH = self.previous_menu_path
        owner_command_module.sync_menu_to_db = self.previous_sync_menu_to_db
        self.tempdir.cleanup()

    def _client_with_fake_claude_text(self, text: str):
        class FakeMessages:
            def create(self, **kwargs):
                return types.SimpleNamespace(
                    content=[types.SimpleNamespace(text=text)]
                )

        class FakeAnthropic:
            def __init__(self, api_key):
                self.messages = FakeMessages()

        sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=FakeAnthropic)
        app = FastAPI()
        app.include_router(owner_command_module.router)
        return TestClient(app)

    def test_malformed_claude_json_returns_unknown_without_500(self):
        client = self._client_with_fake_claude_text("{broken")

        response = client.post(
            "/owner-command/",
            headers={"X-Admin-Api-Key": "test-admin-key"},
            json={"command": "togli qualcosa"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["action"]["action"], "unknown")
        self.assertEqual(
            payload["details"],
            "Claude non ha restituito JSON valido",
        )


if __name__ == "__main__":
    unittest.main()
