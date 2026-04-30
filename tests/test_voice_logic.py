import os
import time
import unittest

import app.routes.voice as voice_module
from app.routes.voice import (
    AUDIO_DIR,
    _AUDIO_CACHE,
    _AUDIO_CACHE_META,
    _audio_cache_get,
    _audio_cache_put,
    _needs_filler,
    _prune_audio_cache,
)


class VoiceLogicTests(unittest.TestCase):
    def setUp(self):
        self.previous_ttl = os.environ.get("VOICE_AUDIO_CACHE_TTL_SECONDS")
        self.previous_max = os.environ.get("VOICE_AUDIO_CACHE_MAX_ITEMS")
        _AUDIO_CACHE.clear()
        _AUDIO_CACHE_META.clear()
        self.created_files = []

    def tearDown(self):
        _AUDIO_CACHE.clear()
        _AUDIO_CACHE_META.clear()
        for path in self.created_files:
            path.unlink(missing_ok=True)
        if self.previous_ttl is None:
            os.environ.pop("VOICE_AUDIO_CACHE_TTL_SECONDS", None)
        else:
            os.environ["VOICE_AUDIO_CACHE_TTL_SECONDS"] = self.previous_ttl
        if self.previous_max is None:
            os.environ.pop("VOICE_AUDIO_CACHE_MAX_ITEMS", None)
        else:
            os.environ["VOICE_AUDIO_CACHE_MAX_ITEMS"] = self.previous_max

    def _write_audio_file(self, filename: str):
        path = AUDIO_DIR / filename
        path.write_bytes(b"mp3")
        self.created_files.append(path)
        return path

    def test_fast_path_name_and_time_do_not_need_filler(self):
        self.assertFalse(_needs_filler("Mario Rossi", "collecting_name"))
        self.assertFalse(_needs_filler("alle 8 e mezza", "collecting_pickup_time"))

    def test_mixed_name_or_time_messages_still_use_filler(self):
        self.assertTrue(_needs_filler("aggiungi una margherita", "collecting_name"))
        self.assertTrue(_needs_filler("alle 8 e aggiungi una margherita", "collecting_pickup_time"))

    def test_audio_cache_get_drops_missing_files(self):
        _audio_cache_put("Ok!", "missing-audio.mp3")

        self.assertIsNone(_audio_cache_get("Ok!"))
        self.assertNotIn("Ok!", _AUDIO_CACHE)
        self.assertNotIn("Ok!", _AUDIO_CACHE_META)

    def test_audio_cache_prunes_expired_unpinned_entries_but_keeps_pinned(self):
        os.environ["VOICE_AUDIO_CACHE_TTL_SECONDS"] = "1"
        stale = self._write_audio_file("stale.mp3")
        pinned = self._write_audio_file("pinned.mp3")
        _audio_cache_put("stale", stale.name)
        _audio_cache_put("pinned", pinned.name, pinned=True)
        old_timestamp = time.time() - 10
        _AUDIO_CACHE_META["stale"]["created_at"] = old_timestamp
        _AUDIO_CACHE_META["pinned"]["created_at"] = old_timestamp

        _prune_audio_cache()

        self.assertNotIn("stale", _AUDIO_CACHE)
        self.assertFalse(stale.exists())
        self.assertEqual(_AUDIO_CACHE["pinned"], pinned.name)
        self.assertTrue(pinned.exists())

    def test_audio_cache_prunes_lru_when_over_limit(self):
        os.environ["VOICE_AUDIO_CACHE_MAX_ITEMS"] = "2"
        first = self._write_audio_file("first.mp3")
        second = self._write_audio_file("second.mp3")
        third = self._write_audio_file("third.mp3")
        _audio_cache_put("first", first.name)
        _audio_cache_put("second", second.name)
        _AUDIO_CACHE_META["first"]["last_used_at"] = 1
        _AUDIO_CACHE_META["second"]["last_used_at"] = 2

        _audio_cache_put("third", third.name)

        self.assertNotIn("first", _AUDIO_CACHE)
        self.assertEqual(set(_AUDIO_CACHE), {"second", "third"})
        self.assertFalse(first.exists())

    def test_synthesize_async_stores_generated_audio_in_cache(self):
        filename = "generated.mp3"

        class FakeResponse:
            status_code = 200
            content = b"mp3"
            text = ""

            def raise_for_status(self):
                return None

        class FakeAsyncClient:
            def __init__(self, timeout):
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, *args, **kwargs):
                return FakeResponse()

        original_client = voice_module.httpx.AsyncClient
        original_uuid4 = voice_module.uuid.uuid4
        previous_key = os.environ.get("ELEVENLABS_API_KEY")
        previous_voice = os.environ.get("ELEVENLABS_VOICE_ID")
        os.environ["ELEVENLABS_API_KEY"] = "test-key"
        os.environ["ELEVENLABS_VOICE_ID"] = "voice-id"
        voice_module.httpx.AsyncClient = FakeAsyncClient
        voice_module.uuid.uuid4 = lambda: filename.removesuffix(".mp3")
        self.created_files.append(AUDIO_DIR / filename)
        try:
            result = voice_module.asyncio.run(voice_module._synthesize_async("Ok!"))
        finally:
            voice_module.httpx.AsyncClient = original_client
            voice_module.uuid.uuid4 = original_uuid4
            if previous_key is None:
                os.environ.pop("ELEVENLABS_API_KEY", None)
            else:
                os.environ["ELEVENLABS_API_KEY"] = previous_key
            if previous_voice is None:
                os.environ.pop("ELEVENLABS_VOICE_ID", None)
            else:
                os.environ["ELEVENLABS_VOICE_ID"] = previous_voice

        self.assertEqual(result, filename)
        self.assertEqual(_AUDIO_CACHE["Ok!"], filename)
        self.assertTrue((AUDIO_DIR / filename).exists())


if __name__ == "__main__":
    unittest.main()
