import os
import unittest
from unittest.mock import MagicMock, patch

import httpx

from app.services import base44_client
from app.services import conversation_service as service


def _response(body):
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = body
    return response


class GetAllRestaurantsOrNoneTests(unittest.TestCase):
    def setUp(self):
        self.previous_key = os.environ.get("BASE44_API_KEY")
        os.environ["BASE44_API_KEY"] = "test-key"

    def tearDown(self):
        if self.previous_key is None:
            os.environ.pop("BASE44_API_KEY", None)
        else:
            os.environ["BASE44_API_KEY"] = self.previous_key

    def test_missing_api_key_returns_none(self):
        os.environ.pop("BASE44_API_KEY", None)
        with patch.object(base44_client.httpx, "get", side_effect=AssertionError("no HTTP without key")):
            self.assertIsNone(base44_client.get_all_restaurants_or_none())

    def test_http_error_returns_none(self):
        with patch.object(base44_client.httpx, "get", side_effect=httpx.ConnectTimeout("timeout")):
            self.assertIsNone(base44_client.get_all_restaurants_or_none())

    def test_invalid_body_returns_none(self):
        with patch.object(base44_client.httpx, "get", return_value=_response({"entities": "boom"})):
            self.assertIsNone(base44_client.get_all_restaurants_or_none())

    def test_empty_list_is_not_none(self):
        with patch.object(base44_client.httpx, "get", return_value=_response([])):
            self.assertEqual(base44_client.get_all_restaurants_or_none(), [])

    def test_entities_wrapper_is_unwrapped(self):
        body = {"entities": [{"id": "a"}, "junk", {"id": "b"}]}
        with patch.object(base44_client.httpx, "get", return_value=_response(body)):
            self.assertEqual(base44_client.get_all_restaurants_or_none(), [{"id": "a"}, {"id": "b"}])

    def test_get_all_restaurants_keeps_empty_list_on_error(self):
        with patch.object(base44_client.httpx, "get", side_effect=httpx.ConnectTimeout("timeout")):
            self.assertEqual(base44_client.get_all_restaurants(), [])


class MatchRestaurantByPhoneTests(unittest.TestCase):
    RESTAURANTS = [
        {"id": "no-phone", "agent_phone": ""},
        {"id": "pap", "agent_phone": "+39 02 1234 5678"},
        {"id": "cds", "agent_phone": "0039 02 8765 4321"},
    ]

    def test_exact_match_after_normalization(self):
        match = base44_client.match_restaurant_by_phone("+390212345678", self.RESTAURANTS)
        self.assertEqual(match["id"], "pap")

    def test_double_zero_prefix_matches_plus(self):
        match = base44_client.match_restaurant_by_phone("+390287654321", self.RESTAURANTS)
        self.assertEqual(match["id"], "cds")

    def test_suffix_match_without_country_code(self):
        match = base44_client.match_restaurant_by_phone("0212345678", self.RESTAURANTS)
        self.assertEqual(match["id"], "pap")

    def test_unknown_number_returns_none(self):
        self.assertIsNone(base44_client.match_restaurant_by_phone("+390299999999", self.RESTAURANTS))

    def test_empty_number_never_matches_restaurant_without_phone(self):
        self.assertIsNone(base44_client.match_restaurant_by_phone("", self.RESTAURANTS))


class ResolveRestaurantFromPhoneTests(unittest.TestCase):
    PAP = {"id": "rest-pap", "agent_phone": "+390212345678", "agent_active": True}
    CDS = {"id": "rest-cds", "agent_phone": "+390287654321", "agent_active": True}

    def setUp(self):
        self.previous_default = os.environ.get("DEFAULT_RESTAURANT_ID")
        os.environ.pop("DEFAULT_RESTAURANT_ID", None)
        service.reset_restaurant_cache()

    def tearDown(self):
        if self.previous_default is None:
            os.environ.pop("DEFAULT_RESTAURANT_ID", None)
        else:
            os.environ["DEFAULT_RESTAURANT_ID"] = self.previous_default
        service.reset_restaurant_cache()

    def _resolve(self, to_number, restaurants):
        with patch.object(base44_client, "get_all_restaurants_or_none", return_value=restaurants):
            return service.resolve_restaurant_from_phone(to_number)

    def test_agent_phone_match_caches_restaurant(self):
        restaurant, rid, method = self._resolve("+390287654321", [self.PAP, self.CDS])

        self.assertEqual((rid, method), ("rest-cds", "agent_phone"))
        self.assertIs(restaurant, self.CDS)
        self.assertIs(service._restaurant_cache["rest-cds"], self.CDS)

    def test_base44_down_is_unavailable_even_with_default_id(self):
        os.environ["DEFAULT_RESTAURANT_ID"] = "rest-pap"

        restaurant, rid, method = self._resolve("+390212345678", None)

        self.assertEqual((restaurant, rid, method), ({}, "", "unavailable"))

    def test_resolver_exception_is_unavailable(self):
        with patch.object(base44_client, "get_all_restaurants_or_none", side_effect=RuntimeError("boom")):
            result = service.resolve_restaurant_from_phone("+390212345678")

        self.assertEqual(result, ({}, "", "unavailable"))

    def test_unknown_number_without_default_is_unavailable(self):
        result = self._resolve("+390299999999", [self.PAP, self.CDS])

        self.assertEqual(result, ({}, "", "unavailable"))

    def test_default_id_used_only_with_single_active_restaurant(self):
        os.environ["DEFAULT_RESTAURANT_ID"] = "rest-pap"
        inactive_cds = {**self.CDS, "agent_active": "false"}

        restaurant, rid, method = self._resolve("+390299999999", [self.PAP, inactive_cds])

        self.assertEqual((rid, method), ("rest-pap", "default_restaurant_id"))
        self.assertIs(restaurant, self.PAP)

    def test_default_id_ignored_with_multiple_active_restaurants(self):
        os.environ["DEFAULT_RESTAURANT_ID"] = "rest-pap"

        result = self._resolve("+390299999999", [self.PAP, self.CDS])

        self.assertEqual(result, ({}, "", "unavailable"))

    def test_default_id_ignored_when_it_is_not_the_active_restaurant(self):
        os.environ["DEFAULT_RESTAURANT_ID"] = "rest-pap"

        result = self._resolve("+390299999999", [self.CDS])

        self.assertEqual(result, ({}, "", "unavailable"))

    def test_unavailable_never_loads_local_restaurant_file(self):
        with patch.object(
            service,
            "_load_restaurant_from_file",
            side_effect=AssertionError("local file must not be used"),
        ):
            result = self._resolve("+390299999999", [self.PAP, self.CDS])

        self.assertEqual(result[2], "unavailable")


if __name__ == "__main__":
    unittest.main()
