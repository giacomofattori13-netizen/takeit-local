"""Prezzi per ristorante: MenuItem sincronizzati con restaurant_id e prezzi filtrati per locale.

Include i test di non-regressione obbligatori: un ordine completo Pizza a Pezzi (al peso,
fredda/calda, pickup_date domani) e uno Corte del Sole (a pezzo, senza pickup_date,
prezzo diverso da zero), entrambi attraverso chat() fino alla conferma.
"""
import datetime
import json
import unittest
from unittest.mock import patch

from sqlmodel import SQLModel, Session, create_engine, select
from sqlmodel.pool import StaticPool

import app.routes.chat as chat_module
from app.models import ConversationSession, MenuItem, Order, OrderItem, OrderSideEffect
from app.schemas import ChatRequest
from app.services import base44_client
from app.services import conversation_service as service
from app.services import menu_sync

PAP_ID = "rest-pap"
CDS_ID = "rest-cds"

PAP = {
    "id": PAP_ID,
    "name": "Pizza a Pezzi",
    "agent_active": True,
    "reservations_enabled": False,
    "price_per_kg_cold": 18.5,
    "price_per_kg_hot": 19.9,
    "opening_hours": {"monday": "11:00-21:00"},
}
CDS = {
    "id": CDS_ID,
    "name": "Corte del Sole",
    "agent_active": True,
    "reservations_enabled": True,
    "opening_hours": {"monday": "19:00-23:00"},
}

# "Margherita" esiste in entrambi i locali con unità e prezzo diversi.
BASE44_MENU = [
    {"name": "Margherita", "price": 18.5, "sale_unit": "kg", "restaurant_id": PAP_ID},
    {"name": "Bufala al taglio", "price": 18.5, "sale_unit": "kg", "restaurant_id": PAP_ID},
    {"name": "Supplì", "price": 2.5, "sale_unit": "piece", "restaurant_id": PAP_ID},
    {"name": "Margherita", "price": 7.0, "sale_unit": "piece", "restaurant_id": CDS_ID},
    {"name": "Diavola", "price": 8.5, "sale_unit": "piece", "restaurant_id": CDS_ID},
]


def _menu_for(restaurant_id=None, timeout=10.0):
    return [dict(i) for i in BASE44_MENU if not restaurant_id or i["restaurant_id"] == restaurant_id]


class _DbTestCase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(self.engine)
        service.reset_menu_cache()
        self._patches = [patch.object(menu_sync, "engine", self.engine)]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        service.reset_menu_cache()

    def _sync(self, restaurants=(PAP, CDS), menu=_menu_for, file_items=()):
        with (
            patch.object(base44_client, "get_all_restaurants_or_none", return_value=None if restaurants is None else list(restaurants)),
            patch.object(base44_client, "get_menu_items", side_effect=menu),
            patch.object(service, "read_menu_file_raw", return_value=list(file_items)),
            patch.object(menu_sync, "read_menu_file_raw", return_value=list(file_items)),
        ):
            return menu_sync.sync_menu_to_db()

    def _rows(self):
        with Session(self.engine) as session:
            return session.exec(select(MenuItem)).all()


class SyncMenuPerRestaurantTests(_DbTestCase):
    def test_every_restaurant_is_synced_with_its_restaurant_id(self):
        synced = self._sync()

        self.assertEqual(synced, 5)
        rows = {(r.restaurant_id, r.name): r for r in self._rows()}
        self.assertEqual(rows[(PAP_ID, "Margherita")].price, 18.5)
        self.assertEqual(rows[(PAP_ID, "Margherita")].sale_unit, "kg")
        self.assertEqual(rows[(CDS_ID, "Margherita")].price, 7.0)
        self.assertEqual(rows[(CDS_ID, "Margherita")].sale_unit, "piece")

    def test_legacy_rows_without_restaurant_id_are_removed(self):
        with Session(self.engine) as session:
            session.add(MenuItem(name="Margherita", category="", pizza_type="Normale", price=18.5, sale_unit="kg"))
            session.commit()

        self._sync()

        self.assertTrue(all(r.restaurant_id for r in self._rows()))

    def test_restaurant_with_empty_menu_keeps_existing_rows(self):
        self._sync()

        def _cds_down(restaurant_id=None, timeout=10.0):
            return [] if restaurant_id == CDS_ID else _menu_for(restaurant_id)

        self._sync(menu=_cds_down)

        cds_rows = [r for r in self._rows() if r.restaurant_id == CDS_ID]
        self.assertEqual(sorted(r.name for r in cds_rows), ["Diavola", "Margherita"])

    def test_nothing_available_leaves_db_untouched(self):
        self._sync()

        synced = self._sync(menu=lambda restaurant_id=None, timeout=10.0: [])

        self.assertEqual(synced, 0)
        self.assertEqual(len(self._rows()), 5)

    def test_partial_menu_below_half_keeps_existing_rows_and_warns(self):
        self._sync()  # PaP: 3 righe nel DB

        def _pap_partial(restaurant_id=None, timeout=10.0):
            items = _menu_for(restaurant_id)
            return items[:1] if restaurant_id == PAP_ID else items  # 1 su 3 (< 50%)

        with patch("builtins.print") as printed:
            self._sync(menu=_pap_partial)

        pap_rows = [r for r in self._rows() if r.restaurant_id == PAP_ID]
        self.assertEqual(sorted(r.name for r in pap_rows), ["Bufala al taglio", "Margherita", "Supplì"])
        logs = " ".join(str(c.args[0]) for c in printed.call_args_list if c.args)
        self.assertIn("WARNING", logs)
        self.assertIn(PAP_ID, logs)

    def test_menu_at_half_of_existing_rows_is_replaced(self):
        self._sync()  # PaP: 3 righe nel DB

        def _pap_shrunk(restaurant_id=None, timeout=10.0):
            items = _menu_for(restaurant_id)
            return items[:2] if restaurant_id == PAP_ID else items

        self._sync(menu=_pap_shrunk)

        pap_rows = [r for r in self._rows() if r.restaurant_id == PAP_ID]
        self.assertEqual(len(pap_rows), 2)

    def test_new_restaurant_without_rows_is_always_synced(self):
        synced = self._sync(restaurants=(CDS,))

        self.assertEqual(synced, 2)

    def test_base44_down_syncs_restaurants_from_local_file(self):
        file_items = [i for i in BASE44_MENU if i["restaurant_id"] == PAP_ID]

        synced = self._sync(restaurants=None, menu=lambda restaurant_id=None, timeout=10.0: [], file_items=file_items)

        self.assertEqual(synced, 3)
        self.assertEqual({r.restaurant_id for r in self._rows()}, {PAP_ID})


class EnrichPricingPerRestaurantTests(_DbTestCase):
    def setUp(self):
        super().setUp()
        self._sync()
        restaurants = {PAP_ID: PAP, CDS_ID: CDS}
        p = patch.object(chat_module, "load_restaurant", side_effect=lambda restaurant_id="": restaurants.get(restaurant_id, {}))
        p.start()
        self._patches.append(p)

    def _enrich(self, items, restaurant_id):
        with Session(self.engine) as session:
            return chat_module.enrich_items_with_pricing(session, items, restaurant_id=restaurant_id)

    def test_cds_items_use_cds_prices_even_when_name_exists_at_pap(self):
        items, total = self._enrich([
            {"pizza_name": "Margherita", "pizza_type": "Normale", "quantity": 2, "add_ingredients": []},
            {"pizza_name": "Diavola", "pizza_type": "Normale", "quantity": 1, "add_ingredients": []},
        ], CDS_ID)

        self.assertEqual([i["sale_unit"] for i in items], ["piece", "piece"])
        self.assertEqual([i["base_price"] for i in items], [7.0, 8.5])
        self.assertTrue(all(i["total_price"] > 0 for i in items))
        self.assertEqual(total, 22.5)

    def test_pap_prices_unchanged(self):
        items, total = self._enrich([
            {"pizza_name": "Margherita", "pizza_type": "Normale", "quantity": 0.5, "temperature": "fredda"},
            {"pizza_name": "Bufala al taglio", "pizza_type": "Normale", "quantity": 0.3, "temperature": "calda"},
            {"pizza_name": "Supplì", "pizza_type": "Normale", "quantity": 2, "add_ingredients": []},
        ], PAP_ID)

        self.assertEqual([i["sale_unit"] for i in items], ["kg", "kg", "piece"])
        self.assertEqual([i["base_price"] for i in items], [18.5, 19.9, 2.5])
        self.assertEqual([i["total_price"] for i in items], [9.25, 5.97, 5.0])
        self.assertEqual(total, 20.22)

    def test_item_of_other_restaurant_is_not_priced(self):
        items, _total = self._enrich(
            [{"pizza_name": "Diavola", "pizza_type": "Normale", "quantity": 1, "add_ingredients": []}], PAP_ID
        )

        self.assertEqual(items[0]["base_price"], 0.0)


class FullOrderNonRegressionTests(_DbTestCase):
    """Ordine completo attraverso chat(): articoli+nome+orario, poi conferma."""

    def setUp(self):
        super().setUp()
        self._sync()
        self.tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        restaurants = {PAP_ID: PAP, CDS_ID: CDS}
        self.extracted: dict = {}
        patches = [
            patch.object(chat_module, "ensure_restaurant_config", return_value=True),
            patch.object(chat_module, "is_agent_active", return_value=True),
            patch.object(chat_module, "is_reservations_enabled", side_effect=lambda restaurant_id="": restaurant_id != PAP_ID),
            patch.object(chat_module, "is_phone_orders_next_day_only", side_effect=lambda restaurant_id="": restaurant_id == PAP_ID),
            patch.object(chat_module, "get_next_open_day", return_value=(self.tomorrow, "domani")),
            patch.object(chat_module, "load_restaurant", side_effect=lambda restaurant_id="": restaurants.get(restaurant_id, {})),
            patch.object(chat_module, "get_proposable_menu", side_effect=lambda restaurant_id="": _menu_for(restaurant_id)),
            patch.object(chat_module, "get_sold_out_item_names", return_value=set()),
            patch.object(chat_module, "validate_pickup_time", return_value=(True, None, None)),
            patch.object(chat_module, "detect_reservation_intent", return_value=False),
            patch.object(chat_module, "is_dough_available", return_value=True),
            patch.object(chat_module, "_schedule_order_side_effect_job", return_value=None),
            patch.object(chat_module, "extract_order_from_text", side_effect=lambda *a, **k: self.extracted),
        ]
        for p in patches:
            p.start()
        self._patches.extend(patches)

    def _start(self, session, session_id, restaurant_id):
        session.add(ConversationSession(
            session_id=session_id,
            customer_phone="+393331234567",
            items_json="[]",
            state="collecting_items",
            completed=False,
            restaurant_id=restaurant_id,
        ))
        session.commit()

    def _item(self, name, quantity, **extra):
        return {
            "pizza_name": name,
            "pizza_type": "Normale",
            "dough_type": "classica",
            "quantity": quantity,
            "size": "normale",
            "add_ingredients": [],
            "remove_ingredients": [],
            **extra,
        }

    def _run_order(self, session_id, restaurant_id, items):
        self.extracted = {"intent": "add_items", "customer_name": "Elena", "pickup_time": "19:30", "items": items}
        with Session(self.engine) as session:
            self._start(session, session_id, restaurant_id)
            first = chat_module.chat(ChatRequest(session_id=session_id, message="ordine completo"), session)
            conversation = session.exec(select(ConversationSession)).one()
            if conversation.state != "awaiting_confirmation":
                self.fail(f"turno 1 non arriva alla conferma: state={first.state!r} msg={first.response_message!r}")
            saved = []
            with patch.object(
                chat_module,
                "save_order_to_base44",
                side_effect=lambda **kwargs: saved.append(kwargs) or {"id": "b44-order", "order_number": 3},
            ):
                final = chat_module.chat(ChatRequest(session_id=session_id, message="sì, confermo"), session)
            order = session.exec(select(Order)).one()
            order_items = session.exec(select(OrderItem).where(OrderItem.order_id == order.id)).all()
            jobs = session.exec(select(OrderSideEffect).where(OrderSideEffect.kind == "base44_order")).all()
        self.assertEqual(final.state, "completed")
        self.assertEqual(final.order_id, order.id)
        # L'Order è creato su Base44 alla conferma, con numero definitivo: nessun job di retry.
        self.assertEqual(len(saved), 1)
        self.assertEqual(jobs, [])
        self.assertEqual((order.order_number, order.base44_id), (3, "b44-order"))
        self.assertEqual(saved[0]["session_id"], session_id)
        return order, order_items, saved[0]

    def test_full_pap_order_by_weight_cold_and_hot_for_tomorrow(self):
        order, order_items, payload = self._run_order("pap-full", PAP_ID, [
            self._item("Margherita", 0.5, temperature="fredda", size="piena"),
            self._item("Bufala al taglio", 0.3, temperature="calda", size="mezza"),
        ])

        self.assertEqual(order.pickup_date, self.tomorrow.isoformat())
        self.assertEqual(payload["pickup_date"], self.tomorrow.isoformat())
        self.assertEqual(payload["restaurant_id"], PAP_ID)
        self.assertEqual([oi.sale_unit for oi in order_items], ["kg", "kg"])
        sent = {i["pizza_name"]: i for i in payload["items"]}
        self.assertEqual(sent["Margherita"]["sale_unit"], "kg")
        self.assertEqual(sent["Margherita"]["temperature"], "fredda")
        self.assertEqual(sent["Margherita"]["base_price"], 18.5)
        self.assertEqual(sent["Margherita"]["total_price"], 9.25)
        self.assertEqual(sent["Bufala al taglio"]["temperature"], "calda")
        self.assertEqual(sent["Bufala al taglio"]["base_price"], 19.9)
        self.assertEqual(sent["Bufala al taglio"]["total_price"], 5.97)

    def test_full_cds_order_by_piece_without_pickup_date_and_nonzero_price(self):
        order, order_items, payload = self._run_order("cds-full", CDS_ID, [
            self._item("Margherita", 2),
            self._item("Diavola", 1),
        ])

        self.assertIsNone(order.pickup_date)
        self.assertIsNone(payload.get("pickup_date"))
        self.assertEqual(payload["restaurant_id"], CDS_ID)
        self.assertEqual([oi.sale_unit for oi in order_items], ["piece", "piece"])
        sent = {i["pizza_name"]: i for i in payload["items"]}
        self.assertEqual(sent["Margherita"]["sale_unit"], "piece")
        self.assertEqual(sent["Margherita"]["base_price"], 7.0)
        self.assertEqual(sent["Diavola"]["base_price"], 8.5)
        self.assertTrue(all(i["total_price"] > 0 for i in payload["items"]))
        self.assertEqual(sum(i["total_price"] for i in payload["items"]), 22.5)


if __name__ == "__main__":
    unittest.main()
