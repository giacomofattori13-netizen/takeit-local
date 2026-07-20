import io
import os
import unittest
from contextlib import redirect_stdout

import app.services.conversation_service as conversation_service
from app.services.conversation_service import lookup_customer, reset_customer_lookup_cache


class CustomerLookupTests(unittest.TestCase):
    def setUp(self):
        self.previous_api_key = os.environ.get("BASE44_API_KEY")
        self.previous_timeout = os.environ.get("CUSTOMER_LOOKUP_HTTP_TIMEOUT_SECONDS")
        self.previous_cache_ttl = os.environ.get("CUSTOMER_LOOKUP_CACHE_TTL_SECONDS")
        self.previous_miss_cache_ttl = os.environ.get("CUSTOMER_LOOKUP_MISS_CACHE_TTL_SECONDS")
        self.previous_cache_max = os.environ.get("CUSTOMER_LOOKUP_CACHE_MAX_ITEMS")
        reset_customer_lookup_cache()

    def tearDown(self):
        reset_customer_lookup_cache()
        if self.previous_api_key is None:
            os.environ.pop("BASE44_API_KEY", None)
        else:
            os.environ["BASE44_API_KEY"] = self.previous_api_key
        if self.previous_timeout is None:
            os.environ.pop("CUSTOMER_LOOKUP_HTTP_TIMEOUT_SECONDS", None)
        else:
            os.environ["CUSTOMER_LOOKUP_HTTP_TIMEOUT_SECONDS"] = self.previous_timeout
        if self.previous_cache_ttl is None:
            os.environ.pop("CUSTOMER_LOOKUP_CACHE_TTL_SECONDS", None)
        else:
            os.environ["CUSTOMER_LOOKUP_CACHE_TTL_SECONDS"] = self.previous_cache_ttl
        if self.previous_miss_cache_ttl is None:
            os.environ.pop("CUSTOMER_LOOKUP_MISS_CACHE_TTL_SECONDS", None)
        else:
            os.environ["CUSTOMER_LOOKUP_MISS_CACHE_TTL_SECONDS"] = self.previous_miss_cache_ttl
        if self.previous_cache_max is None:
            os.environ.pop("CUSTOMER_LOOKUP_CACHE_MAX_ITEMS", None)
        else:
            os.environ["CUSTOMER_LOOKUP_CACHE_MAX_ITEMS"] = self.previous_cache_max

    def test_lookup_customer_uses_short_timeout_and_masks_logs(self):
        calls = []

        class FakeResponse:
            status_code = 200
            text = (
                '{"entities": [{"full_name": "Mario Rossi", '
                '"phone": "+393331234567", "secret": "raw-body-secret"}]}'
            )

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "entities": [{
                        "full_name": "Mario Rossi",
                        "phone": "+393331234567",
                        "secret": "raw-body-secret",
                    }]
                }

        def fake_get(url, params, timeout):
            calls.append({
                "url": url,
                "params": params,
                "timeout": timeout,
            })
            return FakeResponse()

        original_get = conversation_service.httpx.get
        os.environ["BASE44_API_KEY"] = "test-key"
        os.environ["CUSTOMER_LOOKUP_HTTP_TIMEOUT_SECONDS"] = "1.25"
        conversation_service.httpx.get = fake_get
        output = io.StringIO()
        try:
            with redirect_stdout(output):
                customer = lookup_customer("+39 333 123 4567")
        finally:
            conversation_service.httpx.get = original_get

        self.assertEqual(customer["full_name"], "Mario Rossi")
        self.assertEqual(calls[0]["timeout"], 1.25)
        self.assertEqual(calls[0]["params"], {"api_key": "test-key"})
        logs = output.getvalue()
        self.assertNotIn("Mario Rossi", logs)
        self.assertNotIn("raw-body-secret", logs)
        self.assertNotIn("+39 333 123 4567", logs)
        self.assertNotIn("+393331234567", logs)

    def test_lookup_customer_uses_cache_for_repeated_phone(self):
        calls = []

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "entities": [{
                        "full_name": "Mario Rossi",
                        "phone": "+393331234567",
                    }]
                }

        def fake_get(url, params, timeout):
            calls.append(url)
            return FakeResponse()

        original_get = conversation_service.httpx.get
        os.environ["BASE44_API_KEY"] = "test-key"
        conversation_service.httpx.get = fake_get
        try:
            first = lookup_customer("+39 333 123 4567")
            second = lookup_customer("+393331234567")
        finally:
            conversation_service.httpx.get = original_get

        self.assertEqual(first["full_name"], "Mario Rossi")
        self.assertEqual(second["full_name"], "Mario Rossi")
        self.assertEqual(len(calls), 1)

    def test_lookup_customer_miss_cache_expires_independently(self):
        calls = []

        class FakeResponse:
            status_code = 200

            def __init__(self, entities):
                self._entities = entities

            def raise_for_status(self):
                return None

            def json(self):
                return {"entities": self._entities}

        def fake_get(url, params, timeout):
            calls.append(url)
            if len(calls) == 1:
                return FakeResponse([])
            return FakeResponse([{
                "full_name": "Mario Rossi",
                "phone": "+393331234567",
            }])

        original_get = conversation_service.httpx.get
        os.environ["BASE44_API_KEY"] = "test-key"
        os.environ["CUSTOMER_LOOKUP_MISS_CACHE_TTL_SECONDS"] = "1"
        conversation_service.httpx.get = fake_get
        try:
            self.assertIsNone(lookup_customer("+393331234567"))
            with conversation_service._customer_lookup_cache_lock:
                cached_at, cached_customer, ttl = conversation_service._customer_lookup_cache["+393331234567"]
                conversation_service._customer_lookup_cache["+393331234567"] = (
                    cached_at - ttl - 0.01,
                    cached_customer,
                    ttl,
                )
            customer = lookup_customer("+393331234567")
        finally:
            conversation_service.httpx.get = original_get

        self.assertEqual(customer["full_name"], "Mario Rossi")
        self.assertEqual(len(calls), 2)

    def test_lookup_customer_cache_prunes_to_max_items(self):
        os.environ["CUSTOMER_LOOKUP_CACHE_MAX_ITEMS"] = "2"

        with conversation_service._customer_lookup_cache_lock:
            conversation_service._customer_lookup_cache["+391"] = (1.0, None, 300.0)
            conversation_service._customer_lookup_cache["+392"] = (2.0, None, 300.0)
            conversation_service._customer_lookup_cache["+393"] = (3.0, None, 300.0)
            conversation_service._prune_customer_lookup_cache(now=4.0)

            keys = list(conversation_service._customer_lookup_cache)

        self.assertEqual(keys, ["+392", "+393"])


if __name__ == "__main__":
    unittest.main()
