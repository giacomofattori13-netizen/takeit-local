import unittest

import app.services.conversation_service as conversation_service
from app.services.conversation_service import (
    extract_order_from_text,
    _normalize_extracted_payload,
    _parse_llm_json_payload,
)


class ExtractionValidationTests(unittest.TestCase):
    def test_normalizes_malformed_item_fields(self):
        payload = {
            "intent": "add_items",
            "customer_name": "  Mario  ",
            "pickup_time": "",
            "items": [{
                "pizza_name": "  Margherita  ",
                "dough_type": "integrale",
                "quantity": "2",
                "size": "gigante",
                "add_ingredients": "patatine",
                "remove_ingredients": [None, " olive "],
            }],
        }

        parsed = _normalize_extracted_payload(
            payload,
            dough_items=[{"code": "classica"}, {"code": "integrale"}],
        )

        self.assertEqual(parsed["intent"], "add_items")
        self.assertEqual(parsed["customer_name"], "Mario")
        self.assertIsNone(parsed["pickup_time"])
        self.assertEqual(parsed["items"], [{
            "pizza_name": "Margherita",
            "dough_type": "integrale",
            "quantity": 2,
            "size": "normale",
            "add_ingredients": ["patatine"],
            "remove_ingredients": ["olive"],
        }])

    def test_invalid_dough_falls_back_to_classica(self):
        payload = {
            "intent": "add_items",
            "items": [{
                "pizza_name": "Diavola",
                "dough_type": "fantasia",
                "quantity": 1,
                "size": "normale",
            }],
        }

        parsed = _normalize_extracted_payload(payload, dough_items=[{"code": "classica"}])

        self.assertEqual(parsed["items"][0]["dough_type"], "classica")

    def test_non_object_payload_uses_fallback(self):
        parsed = _normalize_extracted_payload(["not", "an", "object"])

        self.assertTrue(parsed["_llm_fallback"])
        self.assertEqual(parsed["items"], [])

    def test_parse_llm_json_payload_requires_object(self):
        self.assertEqual(
            _parse_llm_json_payload('{"intent": "unknown", "items": []}'),
            {"intent": "unknown", "items": []},
        )
        self.assertIsNone(_parse_llm_json_payload(""))
        self.assertIsNone(_parse_llm_json_payload("[]"))
        self.assertIsNone(_parse_llm_json_payload("{broken"))

    def test_parse_llm_json_payload_recovers_embedded_object(self):
        parsed = _parse_llm_json_payload(
            '```json\n{"intent": "add_items", "items": []}\n```'
        )

        self.assertEqual(parsed, {"intent": "add_items", "items": []})

    def test_extract_order_requests_json_mode_and_deterministic_output(self):
        calls = []
        original_get_client = conversation_service.get_openai_client

        class FakeCompletions:
            def create(self, **kwargs):
                calls.append(kwargs)
                return type(
                    "FakeResponse",
                    (),
                    {
                        "usage": None,
                        "choices": [
                            type(
                                "FakeChoice",
                                (),
                                {
                                    "message": type(
                                        "FakeMessage",
                                        (),
                                        {
                                            "content": (
                                                '{"intent": "unknown", '
                                                '"customer_name": null, '
                                                '"pickup_time": null, '
                                                '"items": []}'
                                            )
                                        },
                                    )()
                                },
                            )()
                        ],
                    },
                )()

        fake_client = type(
            "FakeClient",
            (),
            {
                "chat": type(
                    "FakeChat",
                    (),
                    {"completions": FakeCompletions()},
                )()
            },
        )()

        conversation_service.get_openai_client = lambda: fake_client
        try:
            parsed = extract_order_from_text(
                "ciao",
                [{"name": "Margherita", "ingredients": []}],
                [{"name": "Classica", "code": "classica", "surcharge": 0.0}],
            )
        finally:
            conversation_service.get_openai_client = original_get_client

        self.assertEqual(parsed["intent"], "unknown")
        self.assertEqual(calls[0]["response_format"], {"type": "json_object"})
        self.assertEqual(calls[0]["temperature"], 0)


if __name__ == "__main__":
    unittest.main()
