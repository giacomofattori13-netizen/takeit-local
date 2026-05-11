import io
import os
import unittest
from concurrent.futures import Future
from contextlib import redirect_stdout

import app.services.conversation_service as conversation_service
from app.privacy import describe_text_for_log, mask_name, mask_phone
from app.routes.chat import _resolve_customer_lookup_future
from app.services.conversation_service import send_whatsapp_confirmation


class PhoneMaskingTests(unittest.TestCase):
    def setUp(self):
        self.previous_twilio_account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
        self.previous_twilio_auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
        self.previous_base44_api_key = os.environ.get("BASE44_API_KEY")
        os.environ.pop("TWILIO_ACCOUNT_SID", None)
        os.environ.pop("TWILIO_AUTH_TOKEN", None)
        os.environ.pop("BASE44_API_KEY", None)

    def tearDown(self):
        if self.previous_twilio_account_sid is None:
            os.environ.pop("TWILIO_ACCOUNT_SID", None)
        else:
            os.environ["TWILIO_ACCOUNT_SID"] = self.previous_twilio_account_sid

        if self.previous_twilio_auth_token is None:
            os.environ.pop("TWILIO_AUTH_TOKEN", None)
        else:
            os.environ["TWILIO_AUTH_TOKEN"] = self.previous_twilio_auth_token

        if self.previous_base44_api_key is None:
            os.environ.pop("BASE44_API_KEY", None)
        else:
            os.environ["BASE44_API_KEY"] = self.previous_base44_api_key

    def test_mask_phone_hides_all_but_last_four_digits(self):
        masked = mask_phone("+39 333 123 4567")

        self.assertEqual(masked, "********4567")
        self.assertNotIn("+39", masked)
        self.assertNotIn("3331234567", masked)

    def test_mask_phone_handles_empty_values(self):
        self.assertEqual(mask_phone(None), "unknown")
        self.assertEqual(mask_phone(""), "unknown")

    def test_mask_name_hides_raw_name(self):
        masked = mask_name("Mario Rossi")

        self.assertEqual(masked, "M*** R***")
        self.assertNotIn("Mario", masked)
        self.assertNotIn("Rossi", masked)

    def test_describe_text_for_log_hides_raw_text(self):
        label = describe_text_for_log("Mario ordina una Margherita")

        self.assertIn("chars=", label)
        self.assertIn("sha256=", label)
        self.assertNotIn("Mario", label)
        self.assertNotIn("Margherita", label)

    def test_customer_lookup_failure_log_masks_phone(self):
        future = Future()
        future.set_exception(RuntimeError("lookup failed"))
        output = io.StringIO()

        with redirect_stdout(output):
            result = _resolve_customer_lookup_future(future, "+39 333 123 4567")

        logs = output.getvalue()
        self.assertIsNone(result)
        self.assertIn("********4567", logs)
        self.assertNotIn("+39 333 123 4567", logs)
        self.assertNotIn("+393331234567", logs)

    def test_base44_order_logs_mask_customer_phone(self):
        calls = []

        class FakeResponse:
            status_code = 200
            text = '{"id": "order-1", "customer_phone": "+393331234567"}'

            def raise_for_status(self):
                return None

            def json(self):
                return {"id": "order-1"}

        def fake_post(url, params, json, headers, timeout):
            calls.append({
                "url": url,
                "params": params,
                "json": json,
                "headers": headers,
                "timeout": timeout,
            })
            return FakeResponse()

        original_post = conversation_service.httpx.post
        os.environ["BASE44_API_KEY"] = "test-key"
        conversation_service.httpx.post = fake_post
        output = io.StringIO()
        try:
            with redirect_stdout(output):
                conversation_service.save_order_to_base44(
                    customer_name="Mario",
                    customer_phone="+393331234567",
                    pickup_time="20:00",
                    order_number=42,
                    ai_confidence=0.9,
                    items=[{
                        "pizza_name": "Margherita",
                        "quantity": 1,
                        "pizza_type": "Classica",
                        "total_price": 7.0,
                    }],
                )
        finally:
            conversation_service.httpx.post = original_post

        logs = output.getvalue()
        self.assertEqual(calls[0]["json"]["customer_phone"], "+393331234567")
        self.assertEqual(calls[0]["json"]["customer_name"], "Mario")
        self.assertIn("********4567", logs)
        self.assertNotIn("+393331234567", logs)
        self.assertNotIn("Mario", logs)

    def test_confirmation_skip_log_masks_phone(self):
        output = io.StringIO()

        with redirect_stdout(output):
            status = send_whatsapp_confirmation(
                customer_name="Mario",
                customer_phone="+39 333 123 4567",
                pickup_time="20:00",
                items=[],
                total_amount=0.0,
            )

        logs = output.getvalue()
        self.assertEqual(status, "skip:credenziali_mancanti")
        self.assertIn("********4567", logs)
        self.assertIn("M***", logs)
        self.assertNotIn("+39 333 123 4567", logs)
        self.assertNotIn("+393331234567", logs)
        self.assertNotIn("Mario", logs)


if __name__ == "__main__":
    unittest.main()
