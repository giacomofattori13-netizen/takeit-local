import unittest
from unittest.mock import patch

from sqlmodel import SQLModel, Session, create_engine, select
from sqlmodel.pool import StaticPool

from app.models import MenuItem
from app.services import daily_reset

PAP_ID = "rest-pap"
CDS_ID = "rest-cds"


class FakeBase44:
    """Base44 in memoria: registra le scritture e permette di simulare errori."""

    def __init__(self, restaurants, menu_items):
        self.restaurants = {r["id"]: dict(r) for r in restaurants}
        self.menu_items = {i["id"]: dict(i) for i in menu_items}
        self.restaurant_writes: list[tuple[str, dict]] = []
        self.menu_writes: list[tuple[str, dict]] = []
        self.failing_restaurant_writes: set[str] = set()
        self.failing_menu_writes: set[str] = set()
        self.restaurants_unavailable = 0  # quante letture dell'elenco falliscono

    def get_all_restaurants_or_none(self, timeout=10.0):
        if self.restaurants_unavailable:
            self.restaurants_unavailable -= 1
            return None
        return [dict(r) for r in self.restaurants.values()]

    def get_menu_items(self, restaurant_id=None, timeout=10.0):
        return [dict(i) for i in self.menu_items.values() if not restaurant_id or i["restaurant_id"] == restaurant_id]

    def update_restaurant(self, patch_, restaurant_id=None, timeout=10.0):
        self.restaurant_writes.append((restaurant_id, patch_))
        if restaurant_id in self.failing_restaurant_writes:
            return None
        self.restaurants[restaurant_id].update(patch_)
        return dict(self.restaurants[restaurant_id])

    def update_menu_item(self, item_id, patch_, timeout=10.0):
        self.menu_writes.append((item_id, patch_))
        if item_id in self.failing_menu_writes:
            return None
        self.menu_items[item_id].update(patch_)
        return dict(self.menu_items[item_id])

    def patches(self):
        return [
            patch.object(daily_reset.base44_client, name, side_effect=getattr(self, name))
            for name in ("get_all_restaurants_or_none", "get_menu_items", "update_restaurant", "update_menu_item")
        ]


def _restaurants(**overrides):
    pap = {"id": PAP_ID, "name": "Pizza a Pezzi", "reservations_enabled": False, "sold_out_ingredients": ["bufala"]}
    cds = {"id": CDS_ID, "name": "Corte del Sole", "reservations_enabled": True, "sold_out_ingredients": ["nduja"]}
    pap.update(overrides.get("pap", {}))
    cds.update(overrides.get("cds", {}))
    return [pap, cds]


MENU = [
    {"id": "m1", "name": "Bufala al taglio", "available": False, "restaurant_id": PAP_ID},
    {"id": "m2", "name": "Margherita", "available": True, "restaurant_id": PAP_ID},
    {"id": "m3", "name": "Diavola", "available": False, "restaurant_id": CDS_ID},
]


class IsDailyResetEnabledTests(unittest.TestCase):
    def test_explicit_field_wins_over_reservations_enabled(self):
        self.assertTrue(daily_reset.is_daily_reset_enabled({"daily_reset_enabled": True, "reservations_enabled": True}))
        self.assertFalse(daily_reset.is_daily_reset_enabled({"daily_reset_enabled": False, "reservations_enabled": False}))

    def test_missing_field_falls_back_to_not_reservations_enabled(self):
        self.assertTrue(daily_reset.is_daily_reset_enabled({"reservations_enabled": False}))
        self.assertFalse(daily_reset.is_daily_reset_enabled({"reservations_enabled": True}))
        self.assertFalse(daily_reset.is_daily_reset_enabled({}))

    def test_string_values_from_base44(self):
        self.assertFalse(daily_reset.is_daily_reset_enabled({"daily_reset_enabled": "false"}))
        self.assertTrue(daily_reset.is_daily_reset_enabled({"reservations_enabled": "false"}))


class DailyResetTestCase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        self.invalidated: list[str] = []
        self._patches = [
            patch.object(daily_reset, "engine", self.engine),
            patch.object(daily_reset, "reset_restaurant_cache", side_effect=lambda restaurant_id=None: self.invalidated.append(restaurant_id)),
            patch.object(daily_reset, "reset_menu_cache", return_value=None),
            patch.object(daily_reset, "fetch_and_save_restaurant", return_value={}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _run(self, fake, runner=None):
        patches = fake.patches()
        for p in patches:
            p.start()
        try:
            return (runner or daily_reset.perform_daily_reset)()
        finally:
            for p in patches:
                p.stop()

    def _add_db_rows(self, rows):
        with Session(self.engine) as session:
            for name, rid, available in rows:
                session.add(MenuItem(name=name, category="", pizza_type="Normale", price=1.0,
                                     available=available, restaurant_id=rid))
            session.commit()

    def _db_available(self):
        with Session(self.engine) as session:
            return {(r.restaurant_id, r.name): r.available for r in session.exec(select(MenuItem)).all()}


class PerformDailyResetTests(DailyResetTestCase):
    def test_sold_out_is_cleared_for_every_restaurant_including_reservations(self):
        fake = FakeBase44(_restaurants(), MENU)

        self._run(fake)

        self.assertEqual(fake.restaurants[PAP_ID]["sold_out_ingredients"], [])
        self.assertEqual(fake.restaurants[CDS_ID]["sold_out_ingredients"], [])

    def test_menu_items_reenabled_only_for_daily_reset_restaurants(self):
        fake = FakeBase44(_restaurants(), MENU)

        self._run(fake)

        self.assertTrue(fake.menu_items["m1"]["available"])
        self.assertFalse(fake.menu_items["m3"]["available"])
        self.assertEqual([item_id for item_id, _ in fake.menu_writes], ["m1"])

    def test_daily_reset_enabled_field_overrides_reservations_mode(self):
        fake = FakeBase44(
            _restaurants(pap={"daily_reset_enabled": False}, cds={"daily_reset_enabled": True}), MENU
        )

        self._run(fake)

        self.assertFalse(fake.menu_items["m1"]["available"])
        self.assertTrue(fake.menu_items["m3"]["available"])

    def test_caches_invalidated_for_every_restaurant(self):
        fake = FakeBase44(_restaurants(cds={"sold_out_ingredients": []}), MENU)

        self._run(fake)

        self.assertEqual(sorted(self.invalidated), [CDS_ID, PAP_ID])

    def test_local_db_rows_reenabled_only_for_that_restaurant(self):
        # #5: le righe locali sono filtrate per restaurant_id
        self._add_db_rows([
            ("Bufala al taglio", PAP_ID, False),
            ("Diavola", CDS_ID, False),
            ("Legacy", None, False),
        ])
        fake = FakeBase44(_restaurants(), MENU)

        self._run(fake)

        self.assertEqual(self._db_available(), {
            (PAP_ID, "Bufala al taglio"): True,
            (CDS_ID, "Diavola"): False,
            (None, "Legacy"): False,
        })


class DailyResetWriteFailureTests(DailyResetTestCase):
    def test_failed_sold_out_write_skips_cache_and_success_log_for_that_restaurant(self):
        fake = FakeBase44(_restaurants(), MENU)
        fake.failing_restaurant_writes.add(CDS_ID)

        with patch("builtins.print") as printed:
            failed = self._run(fake)

        self.assertEqual(failed, [CDS_ID])
        self.assertEqual(self.invalidated, [PAP_ID])
        logs = " ".join(str(c.args[0]) for c in printed.call_args_list if c.args)
        self.assertNotIn("Reset completato", logs)
        self.assertIn("Reset incompleto", logs)

    def test_failed_menu_item_write_leaves_local_db_and_cache_untouched(self):
        self._add_db_rows([("Bufala al taglio", PAP_ID, False)])
        fake = FakeBase44(_restaurants(), MENU)
        fake.failing_menu_writes.add("m1")

        failed = self._run(fake)

        self.assertEqual(failed, [PAP_ID])
        self.assertEqual(self._db_available(), {(PAP_ID, "Bufala al taglio"): False})
        self.assertEqual(self.invalidated, [CDS_ID])

    def test_all_writes_ok_returns_no_failures(self):
        fake = FakeBase44(_restaurants(), MENU)

        self.assertEqual(self._run(fake), [])


class DailyResetRetryTests(DailyResetTestCase):
    def setUp(self):
        super().setUp()
        self.sleeps: list[float] = []
        p = patch.object(daily_reset.time, "sleep", side_effect=self.sleeps.append)
        p.start()
        self._patches.append(p)

    def test_restaurant_list_unavailable_is_not_success(self):
        fake = FakeBase44(_restaurants(), MENU)
        fake.restaurants_unavailable = 1

        self.assertIsNone(self._run(fake))

    def test_retries_until_base44_answers(self):
        fake = FakeBase44(_restaurants(), MENU)
        fake.restaurants_unavailable = 2

        ok = self._run(fake, daily_reset.run_daily_reset_with_retries)

        self.assertTrue(ok)
        self.assertEqual(self.sleeps, [450.0, 450.0])
        self.assertEqual(fake.restaurants[CDS_ID]["sold_out_ingredients"], [])

    def test_three_attempts_within_fifteen_minutes_then_gives_up(self):
        fake = FakeBase44(_restaurants(), MENU)
        fake.restaurants_unavailable = 10

        with patch("builtins.print") as printed:
            ok = self._run(fake, daily_reset.run_daily_reset_with_retries)

        self.assertFalse(ok)
        self.assertEqual(len(self.sleeps), 2)
        self.assertLessEqual(sum(self.sleeps), 15 * 60)
        self.assertEqual(fake.restaurants_unavailable, 7)
        logs = " ".join(str(c.args[0]) for c in printed.call_args_list if c.args)
        self.assertIn("non riuscito dopo 3 tentativi", logs)

    def test_failed_write_is_retried(self):
        fake = FakeBase44(_restaurants(), MENU)
        fake.failing_menu_writes.add("m1")

        def _heal(delay):
            self.sleeps.append(delay)
            fake.failing_menu_writes.clear()

        with patch.object(daily_reset.time, "sleep", side_effect=_heal):
            ok = self._run(fake, daily_reset.run_daily_reset_with_retries)

        self.assertTrue(ok)
        self.assertTrue(fake.menu_items["m1"]["available"])

    def test_empty_restaurant_list_is_success_without_retry(self):
        fake = FakeBase44([], MENU)

        self.assertTrue(self._run(fake, daily_reset.run_daily_reset_with_retries))
        self.assertEqual(self.sleeps, [])


if __name__ == "__main__":
    unittest.main()
