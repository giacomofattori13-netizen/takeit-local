import os
import time
import unittest

from sqlmodel import SQLModel, Session, create_engine, select

import app.routes.voice as voice_module
from app.privacy import describe_text_for_log
from app.routes.voice import (
    AUDIO_DIR,
    _AUDIO_CACHE,
    _AUDIO_CACHE_META,
    _audio_cache_get,
    _audio_cache_put,
    _apply_customer_profile_to_conversation,
    _cleanup_stale_pending_responses,
    _get_tts_stream_client,
    _needs_filler,
    _pending_response_created_at,
    _pending_responses,
    _prune_audio_cache,
    _resolve_customer_lookup_task,
    _run_chat_with_fresh_session,
    _store_customer_profile_sync,
    _voice_greeting_lookup_timeout_seconds,
    close_tts_stream_client,
)


class VoiceLogicTests(unittest.TestCase):
    def setUp(self):
        self.previous_ttl = os.environ.get("VOICE_AUDIO_CACHE_TTL_SECONDS")
        self.previous_max = os.environ.get("VOICE_AUDIO_CACHE_MAX_ITEMS")
        self.previous_lookup_timeout = os.environ.get("CUSTOMER_LOOKUP_TIMEOUT_SECONDS")
        self.previous_greeting_lookup_timeout = os.environ.get("VOICE_GREETING_LOOKUP_TIMEOUT_SECONDS")
        self.previous_pending_ttl = os.environ.get("VOICE_PENDING_RESPONSE_TTL_SECONDS")
        _AUDIO_CACHE.clear()
        _AUDIO_CACHE_META.clear()
        _pending_responses.clear()
        _pending_response_created_at.clear()
        voice_module._tts_stream_client = None
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
        if self.previous_lookup_timeout is None:
            os.environ.pop("CUSTOMER_LOOKUP_TIMEOUT_SECONDS", None)
        else:
            os.environ["CUSTOMER_LOOKUP_TIMEOUT_SECONDS"] = self.previous_lookup_timeout
        if self.previous_greeting_lookup_timeout is None:
            os.environ.pop("VOICE_GREETING_LOOKUP_TIMEOUT_SECONDS", None)
        else:
            os.environ["VOICE_GREETING_LOOKUP_TIMEOUT_SECONDS"] = self.previous_greeting_lookup_timeout
        if self.previous_pending_ttl is None:
            os.environ.pop("VOICE_PENDING_RESPONSE_TTL_SECONDS", None)
        else:
            os.environ["VOICE_PENDING_RESPONSE_TTL_SECONDS"] = self.previous_pending_ttl
        _pending_responses.clear()
        _pending_response_created_at.clear()
        voice_module._tts_stream_client = None

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

    def test_describe_text_for_log_does_not_include_raw_text(self):
        label = describe_text_for_log("Mario ordina una margherita")

        self.assertIn("chars=", label)
        self.assertIn("sha256=", label)
        self.assertNotIn("Mario", label)
        self.assertNotIn("margherita", label)

    def test_cleanup_stale_pending_response_cancels_task(self):
        os.environ["VOICE_PENDING_RESPONSE_TTL_SECONDS"] = "0.001"

        async def run_cleanup():
            task = voice_module.asyncio.create_task(voice_module.asyncio.sleep(10))
            _pending_responses["session-old"] = task
            _pending_response_created_at["session-old"] = time.time() - 1
            removed = _cleanup_stale_pending_responses(force=True)
            await voice_module.asyncio.sleep(0)
            return removed, task.cancelled()

        removed, cancelled = voice_module.asyncio.run(run_cleanup())

        self.assertEqual(removed, 1)
        self.assertTrue(cancelled)
        self.assertNotIn("session-old", _pending_responses)
        self.assertNotIn("session-old", _pending_response_created_at)

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

    def test_customer_lookup_task_timeout_returns_none(self):
        os.environ["CUSTOMER_LOOKUP_TIMEOUT_SECONDS"] = "0.001"

        async def run_lookup():
            task = voice_module.asyncio.create_task(voice_module.asyncio.sleep(10))
            result = await _resolve_customer_lookup_task(task, "+393331234567")
            return result, task.cancelled()

        result, cancelled = voice_module.asyncio.run(run_lookup())

        self.assertIsNone(result)
        self.assertTrue(cancelled)

    def test_customer_lookup_task_can_timeout_without_cancel(self):
        async def run_lookup():
            task = voice_module.asyncio.create_task(voice_module.asyncio.sleep(10))
            try:
                result = await _resolve_customer_lookup_task(
                    task,
                    "+393331234567",
                    timeout_seconds=0.001,
                    cancel_on_timeout=False,
                )
                return result, task.cancelled(), task.done()
            finally:
                task.cancel()

        result, cancelled, done = voice_module.asyncio.run(run_lookup())

        self.assertIsNone(result)
        self.assertFalse(cancelled)
        self.assertFalse(done)

    def test_voice_greeting_lookup_timeout_default_is_short(self):
        os.environ.pop("VOICE_GREETING_LOOKUP_TIMEOUT_SECONDS", None)

        self.assertEqual(_voice_greeting_lookup_timeout_seconds(), 0.25)

    def test_customer_lookup_task_returns_profile(self):
        async def run_lookup():
            task = voice_module.asyncio.create_task(
                voice_module.asyncio.sleep(0, result={"full_name": "Mario Rossi"})
            )
            return await _resolve_customer_lookup_task(task, "+393331234567")

        result = voice_module.asyncio.run(run_lookup())

        self.assertEqual(result, {"full_name": "Mario Rossi"})

    def test_chat_thread_uses_fresh_session(self):
        test_engine = create_engine("sqlite:///:memory:")
        original_engine = voice_module._db_engine
        captured_binds = []

        def fake_chat(request, db):
            captured_binds.append(db.get_bind())
            return "ok"

        voice_module._db_engine = test_engine
        try:
            result = _run_chat_with_fresh_session(fake_chat, object())
        finally:
            voice_module._db_engine = original_engine

        self.assertEqual(result, "ok")
        self.assertEqual(captured_binds, [test_engine])

    def test_customer_profile_updates_name_and_favorites(self):
        conversation = voice_module.ConversationSession(session_id="s1", items_json="[]")

        found_name, changed = _apply_customer_profile_to_conversation(
            conversation,
            {
                "full_name": "Mario Rossi",
                "favorite_pizzas": "Margherita, Diavola",
            },
        )

        self.assertEqual(found_name, "Mario Rossi")
        self.assertTrue(changed)
        self.assertEqual(conversation.customer_name, "Mario Rossi")
        self.assertEqual(conversation.favorite_pizzas_json, '["Margherita", "Diavola"]')

    def test_store_customer_profile_sync_updates_existing_session(self):
        test_engine = create_engine("sqlite:///:memory:")
        SQLModel.metadata.create_all(test_engine)
        original_engine = voice_module._db_engine
        with Session(test_engine) as db:
            db.add(voice_module.ConversationSession(session_id="s1", items_json="[]"))
            db.commit()

        voice_module._db_engine = test_engine
        try:
            _store_customer_profile_sync(
                "s1",
                {
                    "full_name": "Mario Rossi",
                    "favorite_pizzas": ["Margherita"],
                },
            )
        finally:
            voice_module._db_engine = original_engine

        with Session(test_engine) as db:
            conversation = db.exec(
                select(voice_module.ConversationSession).where(
                    voice_module.ConversationSession.session_id == "s1"
                )
            ).first()

        self.assertEqual(conversation.customer_name, "Mario Rossi")
        self.assertEqual(conversation.favorite_pizzas_json, '["Margherita"]')

    def test_tts_stream_client_is_reused_and_closed(self):
        class FakeAsyncClient:
            instances = []

            def __init__(self, *args, **kwargs):
                self.is_closed = False
                self.args = args
                self.kwargs = kwargs
                FakeAsyncClient.instances.append(self)

            async def aclose(self):
                self.is_closed = True

        original_client = voice_module.httpx.AsyncClient
        voice_module.httpx.AsyncClient = FakeAsyncClient
        try:
            first = voice_module.asyncio.run(_get_tts_stream_client())
            second = voice_module.asyncio.run(_get_tts_stream_client())
            voice_module.asyncio.run(close_tts_stream_client())
        finally:
            voice_module.httpx.AsyncClient = original_client
            voice_module._tts_stream_client = None

        self.assertIs(first, second)
        self.assertEqual(len(FakeAsyncClient.instances), 1)
        self.assertTrue(first.is_closed)


if __name__ == "__main__":
    unittest.main()
