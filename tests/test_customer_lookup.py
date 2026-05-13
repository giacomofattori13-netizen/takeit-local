import io
import os
import unittest
from contextlib import redirect_stdout

import app.services.conversation_service as conversation_service
from app.services.conversation_service import lookup_customer, reset_customer_lookup_cache


class CustomerLookupTests(unittest.TestCase):
    def setUp(self):
        self.previous_token = os.environ.get("BASE44_TOKEN")
        self.previous_timeout = os.environ.get("CUSTOMER_LOOKUP_HTTP_TIMEOUT_SECONDS")
        self.previous_cache_ttl = os.environ.get("CUSTOMER_LOOKUP_CACHE_TTL_SECONDS")
        reset_customer_lookup_cache()

    def tearDown(self):
        reset_customer_lookup_cache()
        if self.previous_token is None:
            os.environ.pop("BASE44_TOKEN", None)
        else:
            os.environ["BASE44_TOKEN"] = self.previous_token
        if self.previous_timeout is None:
            os.environ.pop("CUSTOMER_LOOKUP_HTTP_TIMEOUT_SECONDS", None)
        else:
            os.environ["CUSTOMER_LOOKUP_HTTP_TIMEOUT_SECONDS"] = self.previous_timeout
        if self.previous_cache_ttl is None:
            os.environ.pop("CUSTOMER_LOOKUP_CACHE_TTL_SECONDS", None)
        else:
            os.environ["CUSTOMER_LOOKUP_CACHE_TTL_SECONDS"] = self.previous_cache_ttl

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

        def fake_get(url, headers, timeout):
            calls.append({
                "url": url,
                "headers": headers,
                "timeout": timeout,
            })
            return FakeResponse()

        original_get = conversation_service.httpx.get
        os.environ["BASE44_TOKEN"] = "test-token"
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
        self.assertEqual(calls[0]["headers"], {"Authorization": "Bearer test-token"})
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

        def fake_get(url, headers, timeout):
            calls.append(url)
            return FakeResponse()

        original_get = conversation_service.httpx.get
        os.environ["BASE44_TOKEN"] = "test-token"
        conversation_service.httpx.get = fake_get
        try:
            first = lookup_customer("+39 333 123 4567")
            second = lookup_customer("+393331234567")
        finally:
            conversation_service.httpx.get = original_get

        self.assertEqual(first["full_name"], "Mario Rossi")
        self.assertEqual(second["full_name"], "Mario Rossi")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
