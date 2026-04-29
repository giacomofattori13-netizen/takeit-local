#!/usr/bin/env python3
"""Offline/live evaluation harness for order extraction regressions."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES_PATH = REPO_ROOT / "tests" / "fixtures" / "order_extraction_cases.json"
MENU_PATH = REPO_ROOT / "app" / "menu_data.json"
DOUGH_PATH = REPO_ROOT / "app" / "dough_data.json"


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_cases(path: Path = DEFAULT_CASES_PATH) -> list[dict[str, Any]]:
    cases = _load_json(path)
    if not isinstance(cases, list):
        raise ValueError(f"{path} must contain a JSON list")
    return cases


def validate_case_schema(case: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = {"id", "state", "message", "expected"}
    missing = required - set(case)
    if missing:
        errors.append(f"missing keys: {sorted(missing)}")
    if not isinstance(case.get("id"), str) or not case.get("id"):
        errors.append("id must be a non-empty string")
    if not isinstance(case.get("message"), str) or not case.get("message"):
        errors.append("message must be a non-empty string")
    if not isinstance(case.get("state"), str) or not case.get("state"):
        errors.append("state must be a non-empty string")
    expected = case.get("expected")
    if not isinstance(expected, dict):
        errors.append("expected must be an object")
    elif "intent" not in expected:
        errors.append("expected.intent is required")
    if "existing_items" in case and not isinstance(case["existing_items"], list):
        errors.append("existing_items must be a list when provided")
    return errors


def validate_cases(cases: list[dict[str, Any]]) -> list[tuple[str, str]]:
    failures: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    for index, case in enumerate(cases, start=1):
        case_id = str(case.get("id") or f"case-{index}")
        if case_id in seen_ids:
            failures.append((case_id, "duplicate id"))
        seen_ids.add(case_id)
        for error in validate_case_schema(case):
            failures.append((case_id, error))
    return failures


def select_cases(
    cases: list[dict[str, Any]],
    case_ids: list[str] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    selected = cases
    if case_ids:
        available_ids = {case.get("id") for case in cases}
        missing_ids = [case_id for case_id in case_ids if case_id not in available_ids]
        if missing_ids:
            raise ValueError(f"unknown case id(s): {', '.join(missing_ids)}")
        wanted_ids = set(case_ids)
        selected = [case for case in cases if case["id"] in wanted_ids]

    if limit is not None:
        if limit < 1:
            raise ValueError("--limit must be greater than 0")
        selected = selected[:limit]

    return selected


def _matches_expected(actual: Any, expected: Any, path: str = "$") -> list[str]:
    """Compare expected as a partial contract against the actual extraction."""
    errors: list[str] = []
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path}: expected object, got {type(actual).__name__}"]
        for key, expected_value in expected.items():
            if key not in actual:
                errors.append(f"{path}.{key}: missing")
                continue
            errors.extend(_matches_expected(actual[key], expected_value, f"{path}.{key}"))
        return errors

    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [f"{path}: expected list, got {type(actual).__name__}"]
        if len(actual) != len(expected):
            errors.append(f"{path}: expected {len(expected)} items, got {len(actual)}")
            return errors
        for index, expected_item in enumerate(expected):
            errors.extend(_matches_expected(actual[index], expected_item, f"{path}[{index}]"))
        return errors

    if actual != expected:
        errors.append(f"{path}: expected {expected!r}, got {actual!r}")
    return errors


def _percentile(values: list[int], percentile: int) -> int:
    if not values:
        raise ValueError("values cannot be empty")
    index = math.ceil((percentile / 100) * len(values)) - 1
    index = max(0, min(index, len(values) - 1))
    return sorted(values)[index]


def format_latency_summary(results: list[dict[str, Any]]) -> str:
    latencies = [int(result["latency_ms"]) for result in results]
    if not latencies:
        return "No live cases were run."
    return (
        "Latency ms: "
        f"min={min(latencies)} "
        f"p50={_percentile(latencies, 50)} "
        f"p95={_percentile(latencies, 95)} "
        f"max={max(latencies)}"
    )


def write_jsonl_results(path: Path, results: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
            handle.write("\n")


def _load_menu_and_doughs() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    menu_items = _load_json(MENU_PATH)
    dough_items = _load_json(DOUGH_PATH)
    if not isinstance(menu_items, list) or not isinstance(dough_items, list):
        raise ValueError("menu_data.json and dough_data.json must contain JSON lists")
    return menu_items, dough_items


def run_live_eval(
    cases: list[dict[str, Any]],
    *,
    fail_fast: bool = False,
    jsonl_output: Path | None = None,
    max_latency_ms: int | None = None,
) -> int:
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None

    if load_dotenv is not None:
        load_dotenv(REPO_ROOT / ".env")

    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is required for --live", file=sys.stderr)
        return 2

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    from app.services.conversation_service import extract_order_from_text

    menu_items, dough_items = _load_menu_and_doughs()
    results: list[dict[str, Any]] = []

    for case in cases:
        started_at = time.perf_counter()
        actual = extract_order_from_text(
            case["message"],
            menu_items,
            dough_items,
            state=case["state"],
            existing_items=case.get("existing_items") or [],
            customer_name=case.get("customer_name"),
        )
        latency_ms = int(round((time.perf_counter() - started_at) * 1000))
        errors = _matches_expected(actual, case["expected"])

        if max_latency_ms is not None and latency_ms > max_latency_ms:
            errors.append(
                f"latency_ms: expected <= {max_latency_ms}, got {latency_ms}"
            )

        passed = not errors
        result = {
            "case_id": case["id"],
            "state": case["state"],
            "message": case["message"],
            "passed": passed,
            "latency_ms": latency_ms,
            "errors": errors,
            "expected": case["expected"],
            "actual": actual,
        }
        results.append(result)

        status = "PASS" if passed else "FAIL"
        print(f"{status} {case['id']} ({latency_ms}ms)")
        if errors:
            for error in errors:
                print(f"  - {error}")
            print(f"  actual={json.dumps(actual, ensure_ascii=False, sort_keys=True)}")

        if fail_fast and errors:
            break

    failures = sum(1 for result in results if not result["passed"])
    if jsonl_output is not None:
        write_jsonl_results(jsonl_output, results)
        print(f"\nWrote live results to {jsonl_output}")

    print(f"\n{len(results) - failures}/{len(results)} completed cases passed")
    if len(results) < len(cases):
        print(f"Stopped before {len(cases) - len(results)} remaining case(s).")
    print(format_latency_summary(results))
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases",
        type=Path,
        default=DEFAULT_CASES_PATH,
        help="Path to the JSON cases file.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Call the live extractor/OpenAI API. Default only validates fixture schema.",
    )
    parser.add_argument(
        "--case-id",
        action="append",
        default=None,
        help="Run only a specific case id. Can be passed multiple times.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Run only the first N selected cases.",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop the live run after the first failing case.",
    )
    parser.add_argument(
        "--jsonl-output",
        type=Path,
        default=None,
        help="Write live evaluation results as JSONL for later comparison.",
    )
    parser.add_argument(
        "--max-latency-ms",
        type=int,
        default=None,
        help="Fail a live case when extraction exceeds this per-case latency budget.",
    )
    args = parser.parse_args()

    cases = load_cases(args.cases)
    schema_failures = validate_cases(cases)
    if schema_failures:
        for case_id, error in schema_failures:
            print(f"{case_id}: {error}", file=sys.stderr)
        return 1

    try:
        selected_cases = select_cases(cases, args.case_id, args.limit)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not args.live:
        print(f"Validated {len(selected_cases)} order extraction cases.")
        print("Use --live to run them through extract_order_from_text/OpenAI.")
        return 0

    return run_live_eval(
        selected_cases,
        fail_fast=args.fail_fast,
        jsonl_output=args.jsonl_output,
        max_latency_ms=args.max_latency_ms,
    )


if __name__ == "__main__":
    raise SystemExit(main())
