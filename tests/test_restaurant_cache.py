import time
import unittest
from unittest.mock import patch

from app.services import conversation_service as service


class RestaurantCacheTests(unittest.TestCase):
    def setUp(self):
        service.reset_restaurant_cache()

    def tearDown(self):
        service.reset_restaurant_cache()

    def test_stale_cache_returns_immediately_and_schedules_refresh(self):
        cached = {"agent_active": True, "agent_greeting": "Ciao"}
        service._restaurant_cache[""] = cached
        service._restaurant_cache_ts[""] = time.monotonic() - service.RESTAURANT_CACHE_TTL - 1
        scheduled: list[str] = []

        with (
            patch.object(
                service,
                "_fetch_restaurant_from_base44",
                side_effect=AssertionError("Base44 must not block load_restaurant"),
            ),
            patch.object(
                service,
                "_start_restaurant_refresh_background",
                side_effect=lambda reason, restaurant_id="": scheduled.append(reason) or True,
            ),
        ):
            result = service.load_restaurant()

        self.assertIs(result, cached)
        self.assertEqual(scheduled, ["cache_stale"])

    def test_cold_load_uses_local_file_before_background_refresh(self):
        local = {"agent_active": False, "opening_hours": {"lunedi": []}}
        scheduled: list[str] = []

        with (
            patch.object(service, "_load_restaurant_from_file", return_value=local),
            patch.object(
                service,
                "_fetch_restaurant_from_base44",
                side_effect=AssertionError("Base44 must not block cold local load"),
            ),
            patch.object(
                service,
                "_start_restaurant_refresh_background",
                side_effect=lambda reason, restaurant_id="": scheduled.append(reason) or True,
            ),
        ):
            result = service.load_restaurant()

        self.assertEqual(result, local)
        self.assertEqual(scheduled, ["cold_file_fallback"])

    def test_startup_refresh_updates_cache_when_base44_is_available(self):
        fresh = {"agent_active": True, "agent_greeting": "Buonasera"}

        with patch.object(service, "_fetch_restaurant_from_base44", return_value=fresh):
            result = service.fetch_and_save_restaurant()

        self.assertEqual(result, fresh)
        self.assertIs(service._restaurant_cache[""], fresh)


if __name__ == "__main__":
    unittest.main()
