import unittest

from app.services.conversation_service import _normalize_extracted_payload


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


if __name__ == "__main__":
    unittest.main()
