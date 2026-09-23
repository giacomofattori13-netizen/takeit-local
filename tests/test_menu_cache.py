import os
import time
import unittest
from unittest.mock import patch

from app.services import base44_client
from app.services import conversation_service as service

PAP_ITEM = {"name": "Margherita al taglio", "price": 18.0, "sale_unit": "kg", "restaurant_id": "rest-pap"}
CDS_ITEM = {"name": "Diavola", "price": 8.5, "sale_unit": "piece", "restaurant_id": "rest-cds"}


class MenuCacheTests(unittest.TestCase):
    def setUp(self):
        self.previous_ttl = os.environ.get("MENU_CACHE_TTL_SECONDS")
        os.environ.pop("MENU_CACHE_TTL_SECONDS", None)
        service.reset_menu_cache()

    def tearDown(self):
        if self.previous_ttl is None:
            os.environ.pop("MENU_CACHE_TTL_SECONDS", None)
        else:
            os.environ["MENU_CACHE_TTL_SECONDS"] = self.previous_ttl
        service.reset_menu_cache()

    def _load(self, restaurant_id, *, base44, file_items=()):
        """Carica il menu con Base44 e file locale simulati; ritorna (menu, chiamate Base44)."""
        calls: list[str] = []

        def _fake_get_menu_items(restaurant_id=None, timeout=10.0):
            calls.append(restaurant_id)
            if isinstance(base44, Exception):
                raise base44
            return list(base44)

        with (
            patch.object(base44_client, "get_menu_items", side_effect=_fake_get_menu_items),
            patch.object(service, "read_menu_file_raw", return_value=list(file_items)),
        ):
            menu = service.load_menu_from_base44(restaurant_id=restaurant_id)
        return menu, calls

    def _expire(self, restaurant_id):
        service._menu_cache_ts[restaurant_id] = time.monotonic() - service._menu_cache_ttl_seconds() - 1

    # ── TTL ──────────────────────────────────────────────────────────────

    def test_fresh_cache_is_served_without_calling_base44(self):
        self._load("rest-cds", base44=[CDS_ITEM])

        menu, calls = self._load("rest-cds", base44=AssertionError("cache must be used"))

        self.assertEqual(calls, [])
        self.assertEqual([i["name"] for i in menu], ["Diavola"])

    def test_expired_cache_refetches_from_base44(self):
        self._load("rest-cds", base44=[CDS_ITEM])
        self._expire("rest-cds")

        menu, calls = self._load("rest-cds", base44=[{**CDS_ITEM, "price": 9.0}])

        self.assertEqual(calls, ["rest-cds"])
        self.assertEqual(menu[0]["price"], 9.0)

    def test_ttl_is_configurable_via_env(self):
        os.environ["MENU_CACHE_TTL_SECONDS"] = "5"
        self._load("rest-cds", base44=[CDS_ITEM])
        service._menu_cache_ts["rest-cds"] = time.monotonic() - 6

        _menu, calls = self._load("rest-cds", base44=[CDS_ITEM])

        self.assertEqual(calls, ["rest-cds"])

    def test_expired_cache_is_served_when_base44_fails_but_not_refreshed(self):
        self._load("rest-cds", base44=[CDS_ITEM])
        self._expire("rest-cds")
        expired_ts = service._menu_cache_ts["rest-cds"]

        menu, _calls = self._load("rest-cds", base44=RuntimeError("down"), file_items=[PAP_ITEM])

        self.assertEqual([i["name"] for i in menu], ["Diavola"])
        self.assertEqual(service._menu_cache_ts["rest-cds"], expired_ts)

    # ── File di riserva ──────────────────────────────────────────────────

    def test_file_fallback_only_returns_items_of_same_restaurant(self):
        menu, _calls = self._load("rest-cds", base44=[], file_items=[PAP_ITEM, CDS_ITEM])

        self.assertEqual([i["name"] for i in menu], ["Diavola"])

    def test_file_of_another_restaurant_is_never_used(self):
        menu, _calls = self._load("rest-cds", base44=RuntimeError("down"), file_items=[PAP_ITEM])

        self.assertEqual(menu, [])

    def test_file_items_without_restaurant_id_are_not_used_for_a_restaurant(self):
        legacy = {"name": "Capricciosa", "price": 9.0}

        menu, _calls = self._load("rest-cds", base44=[], file_items=[legacy])

        self.assertEqual(menu, [])

    def test_legacy_default_menu_still_reads_whole_file(self):
        menu, calls = self._load("", base44=AssertionError("no Base44 for legacy"), file_items=[PAP_ITEM, CDS_ITEM])

        self.assertEqual(calls, [])
        self.assertEqual(len(menu), 2)

    # ── Niente cache per risultati vuoti o di ripiego ────────────────────

    def test_empty_base44_result_is_not_cached(self):
        self._load("rest-cds", base44=[])

        self.assertNotIn("rest-cds", service._menu_cache)
        _menu, calls = self._load("rest-cds", base44=[CDS_ITEM])
        self.assertEqual(calls, ["rest-cds"])

    def test_only_unavailable_items_is_treated_as_empty(self):
        self._load("rest-cds", base44=[{**CDS_ITEM, "available": False}])

        self.assertNotIn("rest-cds", service._menu_cache)

    def test_file_fallback_result_is_not_cached(self):
        menu, _calls = self._load("rest-cds", base44=RuntimeError("down"), file_items=[CDS_ITEM])

        self.assertEqual(len(menu), 1)
        self.assertNotIn("rest-cds", service._menu_cache)
        _menu, calls = self._load("rest-cds", base44=[CDS_ITEM])
        self.assertEqual(calls, ["rest-cds"])

    def test_empty_legacy_file_is_not_cached(self):
        self._load("", base44=[], file_items=[])

        self.assertNotIn("", service._menu_cache)

    # ── Prompt di sistema ────────────────────────────────────────────────

    def test_changed_menu_invalidates_cached_system_prompt(self):
        self._load("rest-cds", base44=[CDS_ITEM])
        service._system_prompt_cache["rest-cds"] = "old prompt"
        self._expire("rest-cds")

        self._load("rest-cds", base44=[{**CDS_ITEM, "price": 9.0}])

        self.assertNotIn("rest-cds", service._system_prompt_cache)

    def test_unchanged_menu_keeps_cached_system_prompt(self):
        self._load("rest-cds", base44=[CDS_ITEM])
        service._system_prompt_cache["rest-cds"] = "prompt"
        self._expire("rest-cds")

        self._load("rest-cds", base44=[CDS_ITEM])

        self.assertEqual(service._system_prompt_cache["rest-cds"], "prompt")


if __name__ == "__main__":
    unittest.main()
