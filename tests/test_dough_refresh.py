import unittest
from unittest.mock import patch

from app.services import conversation_service as service


class DoughRefreshTests(unittest.TestCase):
    def setUp(self):
        service.reset_dough_cache()

    def tearDown(self):
        service.reset_dough_cache()

    def test_fetch_and_save_doughs_returns_file_and_schedules_refresh(self):
        local = [{"name": "Classica", "code": "classica", "surcharge": 0.0}]
        scheduled: list[bool] = []

        with (
            patch.object(service, "load_doughs", return_value=local),
            patch.object(
                service,
                "_refresh_doughs_from_base44_blocking",
                side_effect=AssertionError("Base44 must not block startup when local doughs exist"),
            ),
            patch.object(
                service,
                "_start_dough_refresh_background",
                side_effect=lambda: scheduled.append(True) or True,
            ),
        ):
            result = service.fetch_and_save_doughs()

        self.assertEqual(result, local)
        self.assertEqual(scheduled, [True])

    def test_fetch_and_save_doughs_uses_short_blocking_refresh_without_file(self):
        fresh = [{"name": "Classica", "code": "classica", "surcharge": 0.0}]

        with (
            patch.object(service, "load_doughs", return_value=[]),
            patch.object(service, "_refresh_doughs_from_base44_blocking", return_value=fresh) as refresh,
        ):
            result = service.fetch_and_save_doughs()

        self.assertEqual(result, fresh)
        refresh.assert_called_once_with(save_to_file=True)

    def test_dough_refresh_timeout_is_configurable(self):
        previous = service.os.environ.get("DOUGH_REFRESH_TIMEOUT_SECONDS")
        service.os.environ["DOUGH_REFRESH_TIMEOUT_SECONDS"] = "1.5"
        try:
            self.assertEqual(service._dough_refresh_timeout_seconds(), 1.5)
        finally:
            if previous is None:
                service.os.environ.pop("DOUGH_REFRESH_TIMEOUT_SECONDS", None)
            else:
                service.os.environ["DOUGH_REFRESH_TIMEOUT_SECONDS"] = previous


if __name__ == "__main__":
    unittest.main()
