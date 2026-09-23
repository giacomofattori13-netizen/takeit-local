"""Comandi "finito"/"esaurito" del titolare: devono scrivere su Base44 (MenuItem o
sold_out_ingredients), non solo su menu_data.json, e restare nel locale giusto.

Coprono sia gli SMS (/sms/incoming → _apply_*) sia l'endpoint /owner-command.
"""
import json
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine, select
from sqlmodel.pool import StaticPool

import app.routes.owner_command as owner_command_module
from app.models import MenuItem
from app.routes import sms as sms_module
from app.services import base44_client
from app.services import conversation_service as service

PAP_ID = "rest-pap"
CDS_ID = "rest-cds"


class FakeBase44:
    def __init__(self):
        self.restaurants = {
            PAP_ID: {"id": PAP_ID, "name": "Pizza a Pezzi", "sold_out_ingredients": []},
            CDS_ID: {"id": CDS_ID, "name": "Corte del Sole", "sold_out_ingredients": []},
        }
        self.menu_items = {
            "p1": {"id": "p1", "name": "Margherita", "ingredients": ["pomodoro", "fiordilatte"],
                   "dough_type": "classica", "available": True, "restaurant_id": PAP_ID, "price": 18.5},
            "p2": {"id": "p2", "name": "Bufala al taglio", "ingredients": ["pomodoro", "bufala"],
                   "dough_type": "classica", "available": True, "restaurant_id": PAP_ID, "price": 18.5},
            "p3": {"id": "p3", "name": "Integrale ortolana", "ingredients": ["zucchine"],
                   "dough_type": "integrale", "available": True, "restaurant_id": PAP_ID, "price": 18.5},
            "c1": {"id": "c1", "name": "Margherita", "ingredients": ["pomodoro", "mozzarella"],
                   "dough_type": "classica", "available": True, "restaurant_id": CDS_ID, "price": 7.0},
            "c2": {"id": "c2", "name": "Bufalina", "ingredients": ["pomodoro", "bufala"],
                   "dough_type": "classica", "available": True, "restaurant_id": CDS_ID, "price": 9.0},
        }
        self.failing_menu_writes: set[str] = set()
        self.menu_writes: list[str] = []

    def get_menu_items(self, restaurant_id=None, timeout=10.0):
        return [dict(i) for i in self.menu_items.values() if not restaurant_id or i["restaurant_id"] == restaurant_id]

    def update_menu_item(self, item_id, patch_, timeout=10.0):
        self.menu_writes.append(item_id)
        if item_id in self.failing_menu_writes:
            return None
        self.menu_items[item_id].update(patch_)
        return dict(self.menu_items[item_id])

    def get_restaurant_by_id(self, restaurant_id, timeout=10.0):
        r = self.restaurants.get(restaurant_id)
        return dict(r) if r else None

    def get_restaurant(self, timeout=10.0):
        return dict(next(iter(self.restaurants.values())))

    def update_restaurant(self, patch_, restaurant_id=None, timeout=10.0):
        self.restaurants[restaurant_id].update(patch_)
        return dict(self.restaurants[restaurant_id])

    def patches(self):
        names = ("get_menu_items", "update_menu_item", "get_restaurant_by_id", "get_restaurant", "update_restaurant")
        return [patch.object(base44_client, n, side_effect=getattr(self, n)) for n in names]


class OwnerCommandTestCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeBase44()
        self.tempdir = tempfile.TemporaryDirectory()
        self.menu_path = os.path.join(self.tempdir.name, "menu_data.json")
        self.file_menu = [
            {"name": "Margherita", "ingredients": ["pomodoro", "fiordilatte"], "available": True, "restaurant_id": PAP_ID},
            {"name": "Bufala al taglio", "ingredients": ["pomodoro", "bufala"], "available": True, "restaurant_id": PAP_ID},
            {"name": "Margherita", "ingredients": ["pomodoro", "mozzarella"], "available": True, "restaurant_id": CDS_ID},
        ]
        self._write_file(self.file_menu)
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            for rid, price in ((PAP_ID, 18.5), (CDS_ID, 7.0)):
                session.add(MenuItem(name="Margherita", category="", pizza_type="Normale", price=price,
                                     available=True, restaurant_id=rid))
            session.commit()
        service.reset_menu_cache()
        self._patches = self.fake.patches() + [
            patch.object(service, "MENU_JSON_PATH", self.menu_path),
            patch.object(owner_command_module, "MENU_JSON_PATH", self.menu_path),
            patch("app.db.engine", self.engine),
            patch.object(service, "fetch_and_save_restaurant", return_value={}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        service.reset_menu_cache()
        self.tempdir.cleanup()

    def _write_file(self, menu):
        with open(self.menu_path, "w", encoding="utf-8") as f:
            json.dump(menu, f)

    def _file(self):
        with open(self.menu_path, encoding="utf-8") as f:
            return json.load(f)

    def _db_available(self):
        with Session(self.engine) as session:
            return {r.restaurant_id: r.available for r in session.exec(select(MenuItem)).all()}

    def _agent_menu_names(self, restaurant_id):
        """Menu che l'agente propone (Base44 → load_menu_from_base44 → get_proposable_menu)."""
        service.reset_menu_cache()
        with patch.object(service, "load_restaurant", side_effect=lambda restaurant_id="": self.fake.restaurants[restaurant_id]):
            return sorted(i["name"] for i in service.get_proposable_menu(restaurant_id=restaurant_id))


class SmsSoldOutCommandTests(OwnerCommandTestCase):
    def test_finita_ingredient_writes_sold_out_on_base44_and_hides_dishes(self):
        result = sms_module._apply_sold_out("bufala", restaurant_id=PAP_ID)

        self.assertIn("segnato come finito", result)
        self.assertEqual(self.fake.restaurants[PAP_ID]["sold_out_ingredients"], ["bufala"])
        self.assertEqual(self.fake.restaurants[CDS_ID]["sold_out_ingredients"], [])
        self.assertEqual(self._file(), self.file_menu)
        self.assertEqual(self._agent_menu_names(PAP_ID), ["Integrale ortolana", "Margherita"])
        self.assertEqual(self._agent_menu_names(CDS_ID), ["Bufalina", "Margherita"])

    def test_esaurita_dish_writes_menu_item_on_base44_for_that_restaurant_only(self):
        result = sms_module._apply_item_off("margherita", restaurant_id=PAP_ID)

        self.assertIn("rimosso dal", result)
        self.assertFalse(self.fake.menu_items["p1"]["available"])
        self.assertTrue(self.fake.menu_items["c1"]["available"])
        self.assertEqual(self._db_available(), {PAP_ID: False, CDS_ID: True})
        file_availability = {(i["restaurant_id"], i["name"]): i["available"] for i in self._file()}
        self.assertFalse(file_availability[(PAP_ID, "Margherita")])
        self.assertTrue(file_availability[(CDS_ID, "Margherita")])
        self.assertNotIn("Margherita", self._agent_menu_names(PAP_ID))
        self.assertIn("Margherita", self._agent_menu_names(CDS_ID))

    def test_failed_base44_write_is_reported_and_local_copies_untouched(self):
        self.fake.failing_menu_writes.add("p1")

        result = sms_module._apply_item_off("margherita", restaurant_id=PAP_ID)

        self.assertIn("Errore aggiornamento Base44", result)
        self.assertEqual(self._db_available(), {PAP_ID: True, CDS_ID: True})
        self.assertEqual(self._file(), self.file_menu)

    def test_unknown_restaurant_never_falls_back_to_first_restaurant(self):
        result = sms_module._apply_sold_out("bufala", restaurant_id="rest-unknown")

        self.assertIn("Errore", result)
        self.assertEqual(self.fake.restaurants[PAP_ID]["sold_out_ingredients"], [])
        self.assertEqual(self.fake.restaurants[CDS_ID]["sold_out_ingredients"], [])


class OwnerCommandEndpointBase44Tests(OwnerCommandTestCase):
    def setUp(self):
        super().setUp()
        self.previous_env = {k: os.environ.get(k) for k in ("ADMIN_API_KEY", "ANTHROPIC_API_KEY")}
        os.environ["ADMIN_API_KEY"] = "test-admin-key"
        os.environ["ANTHROPIC_API_KEY"] = "test-anthropic-key"
        self.previous_anthropic = sys.modules.get("anthropic")
        self.synced: list[int] = []
        p = patch.object(owner_command_module, "sync_menu_to_db", side_effect=lambda: self.synced.append(1) or 1)
        p.start()
        self._patches.append(p)

    def tearDown(self):
        for key, value in self.previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if self.previous_anthropic is None:
            sys.modules.pop("anthropic", None)
        else:
            sys.modules["anthropic"] = self.previous_anthropic
        super().tearDown()

    def _post(self, action: dict, **body):
        prompts: list[str] = []

        class FakeMessages:
            def create(self, **kwargs):
                prompts.append(kwargs["messages"][0]["content"])
                return types.SimpleNamespace(content=[types.SimpleNamespace(text=json.dumps(action))])

        class FakeAnthropic:
            def __init__(self, api_key):
                self.messages = FakeMessages()

        sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=FakeAnthropic)
        app = FastAPI()
        app.include_router(owner_command_module.router)
        response = TestClient(app).post(
            "/owner-command/",
            headers={"X-Admin-Api-Key": "test-admin-key"},
            json={"command": "comando", **body},
        )
        return response, prompts

    def test_disable_pizza_writes_base44_menu_item_of_the_restaurant(self):
        response, prompts = self._post({"action": "disable_pizza", "pizza_name": "Margherita"}, restaurant_id=PAP_ID)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertFalse(self.fake.menu_items["p1"]["available"])
        self.assertTrue(self.fake.menu_items["c1"]["available"])
        self.assertEqual(self.synced, [1])
        self.assertNotIn("Margherita", self._agent_menu_names(PAP_ID))
        self.assertIn("Integrale ortolana", prompts[0])

    def test_remove_ingredient_writes_base44_ingredients_and_keeps_the_dish(self):
        response, _prompts = self._post({"action": "remove_ingredient", "ingredient": "bufala"}, restaurant_id=PAP_ID)

        self.assertTrue(response.json()["ok"])
        self.assertEqual(self.fake.menu_items["p2"]["ingredients"], ["pomodoro"])
        self.assertTrue(self.fake.menu_items["p2"]["available"])
        self.assertEqual(self.fake.menu_items["c2"]["ingredients"], ["pomodoro", "bufala"])
        pap_file = [i for i in self._file() if i["restaurant_id"] == PAP_ID and i["name"] == "Bufala al taglio"]
        self.assertEqual(pap_file[0]["ingredients"], ["pomodoro"])
        self.assertIn("Bufala al taglio", self._agent_menu_names(PAP_ID))

    def test_disable_dough_type_writes_base44(self):
        response, _prompts = self._post({"action": "disable_dough_type", "dough_type": "integrale"}, restaurant_id=PAP_ID)

        self.assertTrue(response.json()["ok"])
        self.assertFalse(self.fake.menu_items["p3"]["available"])
        self.assertEqual(self.fake.menu_writes, ["p3"])

    def test_restaurant_defaults_to_the_one_in_menu_file(self):
        self._write_file([i for i in self.file_menu if i["restaurant_id"] == PAP_ID])

        response, _prompts = self._post({"action": "disable_pizza", "pizza_name": "Margherita"})

        self.assertTrue(response.json()["ok"])
        self.assertFalse(self.fake.menu_items["p1"]["available"])
        self.assertTrue(self.fake.menu_items["c1"]["available"])

    def test_ambiguous_restaurant_is_rejected(self):
        response, _prompts = self._post({"action": "disable_pizza", "pizza_name": "Margherita"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.fake.menu_writes, [])

    def test_base44_write_failure_is_not_reported_as_success(self):
        self.fake.failing_menu_writes.add("p1")

        response, _prompts = self._post({"action": "disable_pizza", "pizza_name": "Margherita"}, restaurant_id=PAP_ID)

        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertIn("Errore Base44", payload["details"])
        self.assertEqual(self._file(), self.file_menu)

    def test_base44_menu_unavailable_returns_503(self):
        with patch.object(base44_client, "get_menu_items", return_value=[]):
            response, _prompts = self._post({"action": "disable_pizza", "pizza_name": "Margherita"}, restaurant_id=PAP_ID)

        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
