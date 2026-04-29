import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.evaluate_order_extraction import (
    format_latency_summary,
    _matches_expected,
    load_cases,
    select_cases,
    validate_cases,
    write_jsonl_results,
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

    def test_select_cases_filters_by_id_and_limit(self):
        cases = [
            {"id": "one"},
            {"id": "two"},
            {"id": "three"},
        ]

        selected = select_cases(cases, case_ids=["three", "one"], limit=1)

        self.assertEqual(selected, [{"id": "one"}])

    def test_select_cases_rejects_unknown_id(self):
        cases = [{"id": "one"}]

        with self.assertRaisesRegex(ValueError, "unknown case id"):
            select_cases(cases, case_ids=["missing"])

    def test_latency_summary_reports_percentiles(self):
        results = [
            {"latency_ms": 10},
            {"latency_ms": 30},
            {"latency_ms": 20},
            {"latency_ms": 40},
        ]

        self.assertEqual(
            format_latency_summary(results),
            "Latency ms: min=10 p50=20 p95=40 max=40",
        )

    def test_write_jsonl_results_creates_comparable_artifact(self):
        results = [{
            "case_id": "one",
            "passed": True,
            "latency_ms": 123,
            "errors": [],
            "expected": {"intent": "add_items"},
            "actual": {"intent": "add_items"},
        }]

        with TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "nested" / "results.jsonl"
            write_jsonl_results(output_path, results)

            self.assertEqual(
                output_path.read_text(encoding="utf-8"),
                '{"actual": {"intent": "add_items"}, "case_id": "one", '
                '"errors": [], "expected": {"intent": "add_items"}, '
                '"latency_ms": 123, "passed": true}\n',
            )


if __name__ == "__main__":
    unittest.main()
