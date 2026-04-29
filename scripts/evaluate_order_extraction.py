#!/usr/bin/env python3
"""Offline/live evaluation harness for order extraction regressions."""

from __future__ import annotations

import argparse
import json
import os
import sys
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


def _load_menu_and_doughs() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    menu_items = _load_json(MENU_PATH)
    dough_items = _load_json(DOUGH_PATH)
    if not isinstance(menu_items, list) or not isinstance(dough_items, list):
        raise ValueError("menu_data.json and dough_data.json must contain JSON lists")
    return menu_items, dough_items


def run_live_eval(cases: list[dict[str, Any]]) -> int:
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
    failures = 0

    for case in cases:
        actual = extract_order_from_text(
            case["message"],
            menu_items,
            dough_items,
            state=case["state"],
            existing_items=case.get("existing_items") or [],
            customer_name=case.get("customer_name"),
        )
        errors = _matches_expected(actual, case["expected"])
        if errors:
            failures += 1
            print(f"FAIL {case['id']}")
            for error in errors:
                print(f"  - {error}")
            print(f"  actual={json.dumps(actual, ensure_ascii=False, sort_keys=True)}")
        else:
            print(f"PASS {case['id']}")

    print(f"\n{len(cases) - failures}/{len(cases)} cases passed")
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
    args = parser.parse_args()

    cases = load_cases(args.cases)
    schema_failures = validate_cases(cases)
    if schema_failures:
        for case_id, error in schema_failures:
            print(f"{case_id}: {error}", file=sys.stderr)
        return 1

    if not args.live:
        print(f"Validated {len(cases)} order extraction cases.")
        print("Use --live to run them through extract_order_from_text/OpenAI.")
        return 0

    return run_live_eval(cases)


if __name__ == "__main__":
    raise SystemExit(main())
