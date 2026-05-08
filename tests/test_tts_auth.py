import os
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes import tts as tts_module


class TTSAuthTests(unittest.TestCase):
    def setUp(self):
        self.previous_admin_key = os.environ.get("ADMIN_API_KEY")
        self.previous_elevenlabs_api_key = os.environ.get("ELEVENLABS_API_KEY")
        self.previous_elevenlabs_voice_id = os.environ.get("ELEVENLABS_VOICE_ID")

        os.environ["ADMIN_API_KEY"] = "test-admin-key"
        os.environ.pop("ELEVENLABS_API_KEY", None)
        os.environ.pop("ELEVENLABS_VOICE_ID", None)

        self.app = FastAPI()
        self.app.include_router(tts_module.router)
        self.client = TestClient(self.app)

    def tearDown(self):
        if self.previous_admin_key is None:
            os.environ.pop("ADMIN_API_KEY", None)
        else:
            os.environ["ADMIN_API_KEY"] = self.previous_admin_key

        if self.previous_elevenlabs_api_key is None:
            os.environ.pop("ELEVENLABS_API_KEY", None)
        else:
            os.environ["ELEVENLABS_API_KEY"] = self.previous_elevenlabs_api_key

        if self.previous_elevenlabs_voice_id is None:
            os.environ.pop("ELEVENLABS_VOICE_ID", None)
        else:
            os.environ["ELEVENLABS_VOICE_ID"] = self.previous_elevenlabs_voice_id

    def test_tts_requires_admin_key(self):
        response = self.client.post("/tts/", json={"text": "ciao"})

        self.assertEqual(response.status_code, 401)

    def test_tts_rejects_wrong_admin_key(self):
        response = self.client.post(
            "/tts/",
            headers={"X-Admin-Api-Key": "wrong-key"},
            json={"text": "ciao"},
        )

        self.assertEqual(response.status_code, 401)

    def test_tts_rejects_oversized_text(self):
        response = self.client.post(
            "/tts/",
            headers={"X-Admin-Api-Key": "test-admin-key"},
            json={"text": "x" * (tts_module.MAX_TTS_TEXT_LENGTH + 1)},
        )

        self.assertEqual(response.status_code, 422)

    def test_tts_accepts_admin_key_before_provider_config_check(self):
        response = self.client.post(
            "/tts/",
            headers={"X-Admin-Api-Key": "test-admin-key"},
            json={"text": "ciao"},
        )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "ELEVENLABS_API_KEY mancante")


if __name__ == "__main__":
    unittest.main()
