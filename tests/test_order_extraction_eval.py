import unittest

from scripts.evaluate_order_extraction import (
    _matches_expected,
    load_cases,
    validate_cases,
)


class OrderExtractionEvalTests(unittest.TestCase):
    def test_fixture_cases_are_well_formed(self):
        cases = load_cases()

        self.assertGreaterEqual(len(cases), 5)
        self.assertEqual(validate_cases(cases), [])

    def test_expected_matching_is_partial_and_recursive(self):
        actual = {
            "intent": "add_items",
            "items": [{
                "pizza_name": "Margherita",
                "dough_type": "classica",
                "quantity": 1,
                "pizza_type": "Normale",
            }],
            "customer_name": None,
        }
        expected = {
            "intent": "add_items",
            "items": [{
                "pizza_name": "Margherita",
                "quantity": 1,
            }],
        }

        self.assertEqual(_matches_expected(actual, expected), [])

    def test_expected_matching_reports_nested_mismatch(self):
        actual = {"items": [{"pizza_name": "Diavola"}]}
        expected = {"items": [{"pizza_name": "Margherita"}]}

        errors = _matches_expected(actual, expected)

        self.assertEqual(errors, ["$.items[0].pizza_name: expected 'Margherita', got 'Diavola'"])


if __name__ == "__main__":
    unittest.main()
