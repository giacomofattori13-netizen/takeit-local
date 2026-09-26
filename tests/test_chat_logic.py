import json
import os
import unittest

from sqlmodel import SQLModel, Session, create_engine, select

import app.routes.chat as chat_module
from app.models import ConversationSession, MenuItem, Order, OrderItem, OrderSideEffect
from app.routes.chat import (
    _extract_local_customer_name,
    _extract_local_pickup_time,
    _persist_order_once,
    determine_state,
    enrich_items_with_pricing,
    merge_items,
    remove_items_from_order,
)


class ChatLogicTests(unittest.TestCase):
    def setUp(self):
        self.previous_lookup_timeout = os.environ.get("CUSTOMER_LOOKUP_TIMEOUT_SECONDS")

    def tearDown(self):
        if self.previous_lookup_timeout is None:
            os.environ.pop("CUSTOMER_LOOKUP_TIMEOUT_SECONDS", None)
        else:
            os.environ["CUSTOMER_LOOKUP_TIMEOUT_SECONDS"] = self.previous_lookup_timeout

    def test_merge_same_item_accumulates_quantity(self):
        existing = [{
            "pizza_name": "Margherita",
            "pizza_type": "Normale",
            "dough_type": "classica",
            "quantity": 1,
            "size": "normale",
            "add_ingredients": [],
            "remove_ingredients": [],
        }]
        new = [{
            "pizza_name": "Margherita",
            "pizza_type": "Normale",
            "dough_type": "classica",
            "quantity": 2,
            "size": "normale",
            "add_ingredients": [],
            "remove_ingredients": [],
        }]

        merged = merge_items(existing, new)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["quantity"], 3)

    def test_merge_different_size_keeps_separate_items(self):
        existing = [{
            "pizza_name": "Diavola",
            "pizza_type": "Normale",
            "dough_type": "classica",
            "quantity": 1,
            "size": "normale",
            "add_ingredients": [],
            "remove_ingredients": [],
        }]
        new = [{
            "pizza_name": "Diavola",
            "pizza_type": "Normale",
            "dough_type": "classica",
            "quantity": 1,
            "size": "mini",
            "add_ingredients": [],
            "remove_ingredients": [],
        }]

        merged = merge_items(existing, new)

        self.assertEqual(len(merged), 2)
        self.assertEqual({item["size"] for item in merged}, {"normale", "mini"})

    def test_remove_items_preserves_modifiers_when_quantity_remains(self):
        existing = [{
            "pizza_name": "Capricciosa",
            "pizza_type": "Normale",
            "dough_type": "integrale",
            "quantity": 2,
            "size": "doppio",
            "add_ingredients": ["patatine"],
            "remove_ingredients": ["olive"],
        }]
        to_remove = [{
            "pizza_name": "Capricciosa",
            "pizza_type": "Normale",
            "quantity": 1,
        }]

        updated = remove_items_from_order(existing, to_remove)

        self.assertEqual(updated, [{
            "pizza_name": "Capricciosa",
            "pizza_type": "Normale",
            "dough_type": "integrale",
            "quantity": 1,
            "size": "doppio",
            "add_ingredients": ["patatine"],
            "remove_ingredients": ["olive"],
        }])

    def test_determine_state_waits_for_declared_quantity(self):
        merged_order = {
            "customer_name": "Mario",
            "pickup_time": "20:00",
            "items": [{"pizza_name": "Margherita", "quantity": 1}],
        }

        state = determine_state(
            merged_order=merged_order,
            missing_messages=[],
            completed=False,
            intended_quantity=2,
        )

        self.assertEqual(state, "collecting_items")

    def test_local_customer_name_accepts_plain_name_only(self):
        self.assertEqual(_extract_local_customer_name("Mi chiamo mario rossi"), "Mario Rossi")
        self.assertEqual(_extract_local_customer_name("Giulia"), "Giulia")
        self.assertIsNone(_extract_local_customer_name("aggiungi una pizza margherita"))
        self.assertIsNone(_extract_local_customer_name("sono io"))

    def test_local_pickup_time_parses_simple_times_only(self):
        self.assertEqual(_extract_local_pickup_time("alle 8 e mezza"), "20:30")
        self.assertEqual(_extract_local_pickup_time("alle 8 e mezza di mattina"), "8:30")
        self.assertEqual(_extract_local_pickup_time("prima possibile"), "prima_possibile")
        self.assertIsNone(_extract_local_pickup_time("alle 8 e aggiungi una margherita"))

    def test_enrich_items_with_pricing_uses_shared_rules(self):
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)

        with Session(engine) as session:
            session.add(MenuItem(name="Margherita", category="rosse", pizza_type="Normale", price=7.5))
            session.add(MenuItem(name="Diavola", category="rosse", pizza_type="Normale", price=9.0))
            session.commit()

            enriched, total = enrich_items_with_pricing(session, [
                {
                    "pizza_name": "Diavola",
                    "pizza_type": "Normale",
                    "dough_type": "classica",
                    "quantity": 2,
                    "size": "mini",
                    "add_ingredients": ["patatine"],
                    "remove_ingredients": [],
                },
                {
                    "pizza_name": "Personalizzata",
                    "pizza_type": "Normale",
                    "dough_type": "classica",
                    "quantity": 1,
                    "size": "doppio",
                    "add_ingredients": ["wurstel", "funghi"],
                    "remove_ingredients": [],
                },
            ])

        self.assertEqual(enriched[0]["base_price"], 7.5)
        self.assertEqual(enriched[0]["extras_price"], 2.0)
        self.assertEqual(enriched[0]["total_price"], 19.0)
        self.assertEqual(enriched[1]["base_price"], 7.5)
        self.assertEqual(enriched[1]["extras_price"], 6.0)
        self.assertEqual(enriched[1]["total_price"], 13.5)
        self.assertEqual(total, 32.5)

    def test_order_side_effects_continue_after_one_failure(self):
        calls = []
        original_save = chat_module.save_order_to_base44
        original_send = chat_module.send_whatsapp_confirmation
        original_upsert = chat_module.upsert_customer

        def failing_save(**kwargs):
            calls.append(("save", kwargs["order_number"]))
            raise RuntimeError("base44 down")

        def fake_send(**kwargs):
            calls.append(("send", kwargs["total_amount"]))

        def fake_upsert(**kwargs):
            calls.append(("upsert", kwargs["pizzas"]))

        chat_module.save_order_to_base44 = failing_save
        chat_module.send_whatsapp_confirmation = fake_send
        chat_module.upsert_customer = fake_upsert
        try:
            chat_module._run_order_side_effects({
                "customer_name": "Mario",
                "customer_phone": "+393331234567",
                "pickup_time": "20:00",
                "order_number": 42,
                "ai_confidence": 0.95,
                "items": [{
                    "pizza_name": "Margherita",
                    "pizza_type": "Normale",
                    "quantity": 1,
                    "base_price": 7.5,
                    "extras_price": 0.0,
                    "total_price": 7.5,
                }],
                "total_amount": 7.5,
                "pizza_names": ["Margherita"],
            })
        finally:
            chat_module.save_order_to_base44 = original_save
            chat_module.send_whatsapp_confirmation = original_send
            chat_module.upsert_customer = original_upsert

        self.assertEqual(calls, [
            ("save", 42),
            ("send", 7.5),
            ("upsert", ["Margherita"]),
        ])

    def test_enqueue_order_side_effects_persists_recoverable_jobs(self):
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)
        scheduled = []
        original_schedule = chat_module._schedule_order_side_effect_job

        def fake_schedule(job_id, delay_seconds=0.0):
            scheduled.append((job_id, delay_seconds))

        chat_module._schedule_order_side_effect_job = fake_schedule
        try:
            with Session(engine) as session:
                chat_module._enqueue_order_side_effects(
                    session=session,
                    customer_name="Mario",
                    customer_phone="+393331234567",
                    pickup_time="20:00",
                    order_number=42,
                    ai_confidence=0.95,
                    items=[{
                        "pizza_name": "Margherita",
                        "pizza_type": "Normale",
                        "quantity": 1,
                        "base_price": 7.5,
                        "extras_price": 0.0,
                        "total_price": 7.5,
                    }],
                    total_amount=7.5,
                    pizza_names=["Margherita"],
                )
                jobs = session.exec(select(OrderSideEffect)).all()
        finally:
            chat_module._schedule_order_side_effect_job = original_schedule

        self.assertEqual(
            {job.kind for job in jobs},
            {"base44_order", "whatsapp_confirmation", "customer_upsert"},
        )
        self.assertEqual({job.status for job in jobs}, {"pending"})
        self.assertEqual(len(scheduled), 3)

    def test_schedule_delayed_side_effect_uses_timer_not_worker(self):
        events = []
        original_timer = chat_module.threading.Timer
        original_executor = chat_module._ORDER_SIDE_EFFECTS_EXECUTOR
        chat_module._ORDER_SIDE_EFFECT_TIMERS.clear()

        class FakeTimer:
            def __init__(self, delay_seconds, callback):
                self.delay_seconds = delay_seconds
                self.callback = callback
                self.daemon = False

            def start(self):
                events.append(("timer_start", self.delay_seconds))

            def cancel(self):
                events.append(("timer_cancel", self.delay_seconds))

        class FakeExecutor:
            def submit(self, *args, **kwargs):
                events.append(("executor_submit", args, kwargs))

        chat_module.threading.Timer = FakeTimer
        chat_module._ORDER_SIDE_EFFECTS_EXECUTOR = FakeExecutor()
        try:
            chat_module._schedule_order_side_effect_job(7, delay_seconds=45.0)
        finally:
            chat_module.threading.Timer = original_timer
            chat_module._ORDER_SIDE_EFFECTS_EXECUTOR = original_executor
            chat_module._ORDER_SIDE_EFFECT_TIMERS.clear()

        self.assertEqual(events, [("timer_start", 45.0)])

    def test_recover_order_side_effects_batches_and_orders_jobs(self):
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)
        now = 1_000.0
        scheduled = []
        original_engine = chat_module._db_engine
        original_schedule = chat_module._schedule_order_side_effect_job
        original_time = chat_module.time.time

        chat_module._db_engine = engine
        chat_module._schedule_order_side_effect_job = lambda job_id, delay_seconds=0.0: scheduled.append((job_id, delay_seconds))
        chat_module.time.time = lambda: now
        try:
            with Session(engine) as session:
                due = OrderSideEffect(
                    order_number=42,
                    kind="base44_order",
                    payload_json="{}",
                    status="pending",
                    next_attempt_at=now,
                )
                mid = OrderSideEffect(
                    order_number=42,
                    kind="whatsapp_confirmation",
                    payload_json="{}",
                    status="retry",
                    next_attempt_at=now + 10,
                )
                late = OrderSideEffect(
                    order_number=42,
                    kind="customer_upsert",
                    payload_json="{}",
                    status="retry",
                    next_attempt_at=now + 30,
                )
                session.add(due)
                session.add(mid)
                session.add(late)
                session.commit()
                expected_order = [due.id, mid.id, late.id]

            recovered = chat_module.recover_order_side_effects(limit=2)
        finally:
            chat_module._db_engine = original_engine
            chat_module._schedule_order_side_effect_job = original_schedule
            chat_module.time.time = original_time

        self.assertEqual(recovered, 3)
        self.assertEqual([job_id for job_id, _ in scheduled], expected_order)
        self.assertEqual([delay for _, delay in scheduled], [0.0, 10.0, 30.0])

    def test_persist_order_once_is_idempotent_per_conversation(self):
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)
        merged_order = {
            "customer_name": "Mario",
            "pickup_time": "20:00",
            "items": [{
                "pizza_name": "Margherita",
                "pizza_type": "Normale",
                "dough_type": "classica",
                "quantity": 1,
                "size": "normale",
                "add_ingredients": [],
                "remove_ingredients": [],
            }],
        }

        with Session(engine) as session:
            conversation = ConversationSession(
                session_id="session-1",
                customer_name="Mario",
                pickup_time="20:00",
                items_json="[]",
                state="awaiting_confirmation",
                completed=False,
            )
            session.add(conversation)
            session.commit()
            session.refresh(conversation)

            first_order, first_created = _persist_order_once(session, conversation, merged_order)
            second_order, second_created = _persist_order_once(session, conversation, merged_order)

            orders = session.exec(select(Order)).all()
            items = session.exec(select(OrderItem)).all()

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first_order.id, second_order.id)
        self.assertEqual(len(orders), 1)
        self.assertEqual(len(items), 1)

    def test_customer_lookup_future_timeout_does_not_block_start(self):
        os.environ["CUSTOMER_LOOKUP_TIMEOUT_SECONDS"] = "0.25"

        class SlowFuture:
            cancelled = False
            timeout_seen = None

            def result(self, timeout=None):
                self.timeout_seen = timeout
                raise chat_module.FutureTimeoutError()

            def cancel(self):
                self.cancelled = True

        future = SlowFuture()

        customer = chat_module._resolve_customer_lookup_future(
            future,
            "+393331234567",
        )

        self.assertIsNone(customer)
        self.assertTrue(future.cancelled)
        self.assertEqual(future.timeout_seen, 0.25)

    # ── restaurant_id forwarding to Base44 order ──────────────────────────────

    def _base_order_payload(self, **overrides):
        payload = {
            "customer_name": "Mario",
            "customer_phone": "+393331234567",
            "pickup_time": "20:00",
            "order_number": 99,
            "ai_confidence": 0.95,
            "items": [{
                "pizza_name": "Margherita",
                "pizza_type": "Normale",
                "quantity": 1,
                "base_price": 7.5,
                "extras_price": 0.0,
                "total_price": 7.5,
            }],
            "total_amount": 7.5,
        }
        payload.update(overrides)
        return payload

    def test_execute_order_side_effect_forwards_restaurant_id_pizza_a_pezzi(self):
        """restaurant_id from the payload must reach save_order_to_base44 (Pizza a Pezzi)."""
        PIZZA_A_PEZZI_ID = "6a22d781b615baedb412be35"
        captured = {}
        original = chat_module.save_order_to_base44

        def fake_save(**kwargs):
            captured.update(kwargs)

        chat_module.save_order_to_base44 = fake_save
        try:
            chat_module._execute_order_side_effect(
                "base44_order",
                self._base_order_payload(restaurant_id=PIZZA_A_PEZZI_ID),
            )
        finally:
            chat_module.save_order_to_base44 = original

        self.assertEqual(captured.get("restaurant_id"), PIZZA_A_PEZZI_ID)

    def test_execute_order_side_effect_empty_restaurant_id_corte_del_sole(self):
        """When no restaurant_id in payload and DEFAULT_RESTAURANT_ID unset, passes ''."""
        captured = {}
        original = chat_module.save_order_to_base44
        original_env = os.environ.pop("DEFAULT_RESTAURANT_ID", None)

        def fake_save(**kwargs):
            captured.update(kwargs)

        chat_module.save_order_to_base44 = fake_save
        try:
            chat_module._execute_order_side_effect(
                "base44_order",
                self._base_order_payload(),  # no restaurant_id key
            )
        finally:
            chat_module.save_order_to_base44 = original
            if original_env is not None:
                os.environ["DEFAULT_RESTAURANT_ID"] = original_env

        self.assertEqual(captured.get("restaurant_id"), "")

    # ── preorder / pickup_date / portion / today-guard ────────────────────────

    def test_persist_order_sets_pickup_date_tomorrow(self):
        """A phone order for pizza al taglio gets pickup_date = tomorrow."""
        import datetime
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)

        with Session(engine) as session:
            conv = ConversationSession(
                session_id="pd-test",
                customer_name="Mario",
                pickup_time="20:00",
                items_json="[]",
                state="awaiting_confirmation",
                completed=False,
            )
            session.add(conv)
            session.commit()
            session.refresh(conv)

            tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
            merged = {
                "customer_name": "Mario",
                "pickup_time": "20:00",
                "pickup_date": tomorrow,
                "items": [],
            }
            order, created = chat_module._persist_order_once(session, conv, merged)
            saved_pickup_date = order.pickup_date
            saved_created = created

        self.assertTrue(saved_created)
        self.assertEqual(saved_pickup_date, tomorrow)

    def test_persist_order_item_portion_and_temperature(self):
        """kg items get portion ('piena'/'mezza') and temperature saved on OrderItem."""
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)

        with Session(engine) as session:
            session.add(MenuItem(
                name="Margherita al taglio",
                category="rosse",
                pizza_type="Normale",
                price=0.0,
                sale_unit="kg",
            ))
            session.commit()
            conv = ConversationSession(
                session_id="pt-test",
                customer_name="Luigi",
                pickup_time="19:00",
                items_json="[]",
                state="awaiting_confirmation",
                completed=False,
            )
            session.add(conv)
            session.commit()
            session.refresh(conv)

            merged = {
                "customer_name": "Luigi",
                "pickup_time": "19:00",
                "items": [{
                    "pizza_name": "Margherita al taglio",
                    "pizza_type": "Normale",
                    "quantity": 0.5,
                    "sale_unit": "kg",
                    "size": "piena",
                    "temperature": "calda",
                    "add_ingredients": [],
                    "remove_ingredients": [],
                    "dough_type": "classica",
                }],
            }
            order, _ = chat_module._persist_order_once(session, conv, merged)
            items = session.exec(select(OrderItem).where(OrderItem.order_id == order.id)).all()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].portion, "piena")
        self.assertEqual(items[0].temperature, "calda")

    def test_order_domani_mezza_calda_end_to_end(self):
        """Full pipeline for a phone al-taglio order 'per domani / mezza / calda':
        local DB (Order.pickup_date, OrderItem.portion/temperature/sale_unit) AND
        the Base44 payload (pickup_date + item sale_unit/temperature/portion) must
        both carry the values through — not just one of the two sinks."""
        import datetime
        from unittest.mock import MagicMock, patch
        import app.services.conversation_service as svc

        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)

        with Session(engine) as session:
            session.add(MenuItem(
                name="Margherita al taglio",
                category="rosse",
                pizza_type="Normale",
                price=19.90,
                sale_unit="kg",
            ))
            session.commit()

            conv = ConversationSession(
                session_id="domani-mezza-calda",
                customer_name="Elena",
                pickup_time="19:30",
                items_json="[]",
                state="awaiting_confirmation",
                completed=False,
            )
            session.add(conv)
            session.commit()
            session.refresh(conv)

            tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
            merged = {
                "customer_name": "Elena",
                "pickup_time": "19:30",
                "pickup_date": tomorrow,
                "items": [{
                    "pizza_name": "Margherita al taglio",
                    "pizza_type": "Normale",
                    "quantity": 0.5,
                    "sale_unit": "kg",
                    "size": "mezza",
                    "temperature": "calda",
                    "add_ingredients": [],
                    "remove_ingredients": [],
                    "dough_type": "classica",
                }],
            }

            # 1) Local DB persistence
            order, created = chat_module._persist_order_once(session, conv, merged)
            self.assertTrue(created)
            self.assertEqual(order.pickup_date, tomorrow)

            saved_items = session.exec(select(OrderItem).where(OrderItem.order_id == order.id)).all()
            self.assertEqual(len(saved_items), 1)
            self.assertEqual(saved_items[0].sale_unit, "kg")
            self.assertEqual(saved_items[0].portion, "mezza")
            self.assertEqual(saved_items[0].temperature, "calda")

            # 2) Base44 payload (same merged_order, through the real pricing/enrichment step)
            enriched_items, _total = chat_module.enrich_items_with_pricing(session, merged["items"])

        captured: dict = {}

        def fake_post(url, *, json, headers, timeout):
            captured.update(json)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = '{"id": "test-id"}'
            mock_resp.raise_for_status = lambda: None
            mock_resp.json.return_value = {"id": "test-id"}
            return mock_resp

        with (
            patch.dict(os.environ, {"BASE44_TOKEN": "test-key"}),
            patch("app.services.conversation_service.httpx.post", side_effect=fake_post),
        ):
            svc.save_order_to_base44(
                customer_name="Elena",
                customer_phone="+393331234567",
                pickup_time="19:30",
                order_number=2001,
                ai_confidence=0.95,
                items=enriched_items,
                pickup_date=merged["pickup_date"],
            )

        self.assertEqual(captured.get("pickup_date"), tomorrow)
        sent_item = captured.get("items", [])[0]
        self.assertEqual(sent_item.get("sale_unit"), "kg")
        self.assertEqual(sent_item.get("temperature"), "calda")
        self.assertEqual(sent_item.get("portion"), "mezza")

    # ── slot-filling: nome cliente non inquinato, porzione mai null ──────────

    def test_local_customer_name_rejects_order_fragments(self):
        """_extract_local_customer_name must reject al-taglio order fragments
        (weight/portion/temperature words) instead of treating them as a name."""
        self.assertIsNone(chat_module._extract_local_customer_name("Intero è fredda"))
        self.assertIsNone(chat_module._extract_local_customer_name("intero, fredda"))
        self.assertIsNone(chat_module._extract_local_customer_name("due etti di bufala fredda"))
        self.assertIsNone(chat_module._extract_local_customer_name("mezza porzione calda"))
        self.assertIsNone(chat_module._extract_local_customer_name("trancio piena grazie"))
        # Legit names must still work
        self.assertEqual(chat_module._extract_local_customer_name("Mario Rossi"), "Mario Rossi")
        self.assertEqual(chat_module._extract_local_customer_name("mi chiamo Elena"), "Elena")

    def test_normalize_extracted_payload_rejects_order_vocab_customer_name(self):
        """The LLM-extraction normalizer must null out a customer_name that is
        actually an order-vocabulary fragment (e.g. LLM hallucinated a name from
        the client's answer to the portion/temperature question)."""
        from app.services import conversation_service as svc

        result = svc._normalize_extracted_payload({
            "intent": "set_customer_name",
            "customer_name": "Intero è fredda",
            "pickup_time": None,
            "items": [],
        })
        self.assertIsNone(result["customer_name"])

        # A real name must survive normalization unchanged
        result_ok = svc._normalize_extracted_payload({
            "intent": "set_customer_name",
            "customer_name": "Giulia Bianchi",
            "pickup_time": None,
            "items": [],
        })
        self.assertEqual(result_ok["customer_name"], "Giulia Bianchi")

    def test_two_kg_items_both_get_portion_and_temperature_default(self):
        """Ordering two al-taglio flavors in one turn, with no explicit portion,
        must default BOTH items to a sensible portion ('piena') and temperature
        ('fredda') — not just the first one — and ask the courtesy question once
        for both, naming both pizzas."""
        import datetime
        from app.schemas import ChatRequest

        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)

        original_is_agent_active = chat_module.is_agent_active
        original_is_reservations_enabled = chat_module.is_reservations_enabled
        original_get_next_open_day = chat_module.get_next_open_day
        original_get_sold_out = chat_module.get_sold_out_item_names
        original_extract = chat_module.extract_order_from_text

        chat_module.is_agent_active = lambda restaurant_id="": True
        chat_module.is_reservations_enabled = lambda restaurant_id="": False
        chat_module.get_next_open_day = lambda restaurant_id="": (
            datetime.date.today() + datetime.timedelta(days=1), "domani"
        )
        chat_module.get_sold_out_item_names = lambda *a, **k: set()

        def fake_extract(message, menu_items, dough_items=None, state="collecting_items",
                          existing_items=None, customer_name=None, restaurant_id=""):
            return {
                "intent": "add_items",
                "customer_name": None,
                "pickup_time": None,
                "items": [
                    {
                        "pizza_name": "Bufala al taglio",
                        "pizza_type": "Normale",
                        "dough_type": "classica",
                        "quantity": 0.2,
                        "size": "normale",
                        "add_ingredients": [],
                        "remove_ingredients": [],
                        "temperature": "",
                    },
                    {
                        "pizza_name": "Porchetta al taglio",
                        "pizza_type": "Normale",
                        "dough_type": "classica",
                        "quantity": 0.2,
                        "size": "normale",
                        "add_ingredients": [],
                        "remove_ingredients": [],
                        "temperature": "",
                    },
                ],
            }
        chat_module.extract_order_from_text = fake_extract

        try:
            with Session(engine) as session:
                session.add(MenuItem(
                    name="Bufala al taglio", category="rosse", pizza_type="Normale",
                    price=18.50, sale_unit="kg",
                ))
                session.add(MenuItem(
                    name="Porchetta al taglio", category="rosse", pizza_type="Normale",
                    price=19.90, sale_unit="kg",
                ))
                session.commit()

                conversation = ConversationSession(
                    session_id="two-kg-items",
                    customer_name=None,
                    pickup_time=None,
                    items_json="[]",
                    state="collecting_items",
                    completed=False,
                )
                session.add(conversation)
                session.commit()

                response = chat_module.chat(
                    ChatRequest(session_id="two-kg-items", message="una bufala e una porchetta al taglio, due etti ciascuna"),
                    session,
                )
                updated = session.exec(
                    select(ConversationSession).where(ConversationSession.session_id == "two-kg-items")
                ).one()
                saved_items = json.loads(updated.items_json)
        finally:
            chat_module.is_agent_active = original_is_agent_active
            chat_module.is_reservations_enabled = original_is_reservations_enabled
            chat_module.get_next_open_day = original_get_next_open_day
            chat_module.get_sold_out_item_names = original_get_sold_out
            chat_module.extract_order_from_text = original_extract

        self.assertEqual(len(saved_items), 2)
        for item in saved_items:
            self.assertEqual(item.get("size"), "piena", msg=f"{item['pizza_name']} missing portion default")
            self.assertEqual(item.get("temperature"), "fredda")
        # Courtesy question mentions both pizzas, asked once
        self.assertIn("Bufala al taglio", response.response_message)
        self.assertIn("Porchetta al taglio", response.response_message)

        # Confirm the defaults actually reach the persisted OrderItem rows too
        # (this is where the original bug surfaced: portion null on Base44/local DB).
        with Session(engine) as session:
            conv = session.exec(
                select(ConversationSession).where(ConversationSession.session_id == "two-kg-items")
            ).one()
            merged = {
                "customer_name": "Elena",
                "pickup_time": "20:00",
                "items": saved_items,
            }
            conv.customer_name = "Elena"
            conv.pickup_time = "20:00"
            order, _ = chat_module._persist_order_once(session, conv, merged)
            order_items = session.exec(select(OrderItem).where(OrderItem.order_id == order.id)).all()

        self.assertEqual(len(order_items), 2)
        for oi in order_items:
            self.assertEqual(oi.portion, "piena", msg=f"{oi.pizza_name} missing persisted portion")
            self.assertEqual(oi.temperature, "fredda")

    def test_kg_slot_answer_does_not_pollute_customer_name(self):
        """Reproduces the reported bug: after a kg item is added (name still
        unknown → state=collecting_name), a message meant to answer the
        portion/temperature courtesy question must NOT be accepted as the
        customer name."""
        import datetime
        from app.schemas import ChatRequest

        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)

        original_is_agent_active = chat_module.is_agent_active
        original_is_reservations_enabled = chat_module.is_reservations_enabled
        original_get_next_open_day = chat_module.get_next_open_day
        original_get_sold_out = chat_module.get_sold_out_item_names
        original_extract = chat_module.extract_order_from_text

        chat_module.is_agent_active = lambda restaurant_id="": True
        chat_module.is_reservations_enabled = lambda restaurant_id="": False
        chat_module.get_next_open_day = lambda restaurant_id="": (
            datetime.date.today() + datetime.timedelta(days=1), "domani"
        )
        chat_module.get_sold_out_item_names = lambda *a, **k: set()

        def fake_extract(message, menu_items, dough_items=None, state="collecting_items",
                          existing_items=None, customer_name=None, restaurant_id=""):
            if "bufala" in message.lower():
                return {
                    "intent": "add_items",
                    "customer_name": None,
                    "pickup_time": None,
                    "items": [{
                        "pizza_name": "Bufala al taglio",
                        "pizza_type": "Normale",
                        "dough_type": "classica",
                        "quantity": 0.2,
                        "size": "normale",
                        "add_ingredients": [],
                        "remove_ingredients": [],
                        "temperature": "",
                    }],
                }
            # Simulates the real LLM guard (system prompt + normalizer) also
            # declining to guess a name from an order-vocabulary fragment.
            return {"intent": "unknown", "customer_name": None, "pickup_time": None, "items": []}
        chat_module.extract_order_from_text = fake_extract

        try:
            with Session(engine) as session:
                session.add(MenuItem(
                    name="Bufala al taglio", category="rosse", pizza_type="Normale",
                    price=18.50, sale_unit="kg",
                ))
                session.commit()

                conversation = ConversationSession(
                    session_id="kg-name-guard",
                    customer_name=None,
                    pickup_time=None,
                    items_json="[]",
                    state="collecting_items",
                    completed=False,
                )
                session.add(conversation)
                session.commit()

                first = chat_module.chat(
                    ChatRequest(session_id="kg-name-guard", message="una bufala al taglio, due etti"),
                    session,
                )
                self.assertEqual(first.state, "collecting_name")

                # Customer replies to the (misheard) portion/temperature question
                # instead of the name question — this must NOT become the name.
                second = chat_module.chat(
                    ChatRequest(session_id="kg-name-guard", message="Intero è fredda"),
                    session,
                )
                updated = session.exec(
                    select(ConversationSession).where(ConversationSession.session_id == "kg-name-guard")
                ).one()
        finally:
            chat_module.is_agent_active = original_is_agent_active
            chat_module.is_reservations_enabled = original_is_reservations_enabled
            chat_module.get_next_open_day = original_get_next_open_day
            chat_module.get_sold_out_item_names = original_get_sold_out
            chat_module.extract_order_from_text = original_extract

        self.assertIsNone(updated.customer_name)
        self.assertEqual(updated.state, "collecting_name")
        self.assertIn("nome", second.response_message.lower())

    def test_is_today_order_request_detects_keywords(self):
        """_is_today_order_request fires on 'per oggi', 'in giornata', 'oggi stesso'."""
        self.assertTrue(chat_module._is_today_order_request("vorrei ordinare per oggi"))
        self.assertTrue(chat_module._is_today_order_request("posso avere qualcosa in giornata?"))
        self.assertTrue(chat_module._is_today_order_request("mi serve oggi stesso"))
        self.assertFalse(chat_module._is_today_order_request("vorrei ordinare per domani"))
        self.assertFalse(chat_module._is_today_order_request("prima possibile domani sera"))

    def test_get_next_open_day_skips_closed_days(self):
        """get_next_open_day returns the first open day when tomorrow is closed."""
        import datetime
        from unittest.mock import patch
        from app.services import conversation_service as svc

        rome_today = datetime.date(2026, 7, 20)  # lunedì
        # opening_hours: lunedì chiuso, martedì aperto
        fake_hours = {
            "monday": "closed",
            "tuesday": "18:00-22:00",
            "wednesday": "18:00-22:00",
            "thursday": "18:00-22:00",
            "friday": "18:00-22:00",
            "saturday": "18:00-22:00",
            "sunday": "closed",
        }
        fake_restaurant = {"opening_hours": fake_hours}

        with (
            patch.object(svc, "load_restaurant", return_value=fake_restaurant),
            patch("app.services.conversation_service.datetime") as mock_dt,
        ):
            mock_dt.datetime.now.return_value.date.return_value = rome_today
            mock_dt.timedelta = datetime.timedelta
            mock_dt.date = datetime.date
            result_date, result_day = svc.get_next_open_day()

        # Domani è martedì 21/7 → aperto → deve restituire martedì
        self.assertEqual(result_date, datetime.date(2026, 7, 21))
        self.assertEqual(result_day, "martedì")

    def test_get_next_open_day_skips_to_wednesday_when_tuesday_closed(self):
        """get_next_open_day skips Tuesday (closed) and lands on Wednesday."""
        import datetime
        from unittest.mock import patch
        from app.services import conversation_service as svc

        rome_today = datetime.date(2026, 7, 20)  # lunedì
        fake_hours = {
            "monday": "closed",
            "tuesday": "closed",
            "wednesday": "18:00-22:00",
            "thursday": "18:00-22:00",
            "friday": "18:00-22:00",
            "saturday": "18:00-22:00",
            "sunday": "closed",
        }
        fake_restaurant = {"opening_hours": fake_hours}

        with (
            patch.object(svc, "load_restaurant", return_value=fake_restaurant),
            patch("app.services.conversation_service.datetime") as mock_dt,
        ):
            mock_dt.datetime.now.return_value.date.return_value = rome_today
            mock_dt.timedelta = datetime.timedelta
            mock_dt.date = datetime.date
            result_date, result_day = svc.get_next_open_day()

        self.assertEqual(result_date, datetime.date(2026, 7, 22))
        self.assertEqual(result_day, "mercoledì")

    def test_corte_del_sole_order_has_no_pickup_date(self):
        """Corte del Sole orders (reservations_enabled) don't set pickup_date."""
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)

        with Session(engine) as session:
            conv = ConversationSession(
                session_id="cds-no-pd",
                customer_name="Giulia",
                pickup_time="20:00",
                items_json="[]",
                state="awaiting_confirmation",
                completed=False,
            )
            session.add(conv)
            session.commit()
            session.refresh(conv)

            # No pickup_date in merged_order → simulates reservations_enabled path
            merged = {
                "customer_name": "Giulia",
                "pickup_time": "20:00",
                "items": [],
            }
            order, created = chat_module._persist_order_once(session, conv, merged)
            saved_pickup_date = order.pickup_date
            saved_created = created

        self.assertTrue(saved_created)
        self.assertIsNone(saved_pickup_date)

    # ── Base44 payload: al taglio kg fields ─────────────────────────────────

    def test_save_order_to_base44_kg_payload_fields(self):
        """save_order_to_base44 sends pickup_date, sale_unit, temperature, portion for kg items."""
        from unittest.mock import MagicMock, patch
        import app.services.conversation_service as svc

        kg_item = {
            "pizza_name": "Margherita al taglio",
            "quantity": 0.5,
            "sale_unit": "kg",
            "size": "mezza",
            "temperature": "calda",
            "dough_type": "classica",
            "add_ingredients": [],
            "remove_ingredients": [],
            "base_price": 9.95,
            "extras_price": 0.0,
            "total_price": 9.95,
        }

        captured: dict = {}

        def fake_post(url, *, json, headers, timeout):
            captured.update(json)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = '{"id": "test-id"}'
            mock_resp.raise_for_status = lambda: None
            mock_resp.json.return_value = {"id": "test-id"}
            return mock_resp

        with (
            patch.dict(os.environ, {"BASE44_TOKEN": "test-key"}),
            patch("app.services.conversation_service.httpx.post", side_effect=fake_post),
        ):
            svc.save_order_to_base44(
                customer_name="Mario",
                customer_phone="+393331234567",
                pickup_time="19:00",
                order_number=1001,
                ai_confidence=0.95,
                items=[kg_item],
                restaurant_id="6a22d781b615baedb412be35",
                pickup_date="2026-07-23",
            )

        self.assertEqual(captured.get("pickup_date"), "2026-07-23")
        items_sent = captured.get("items", [])
        self.assertEqual(len(items_sent), 1)
        sent = items_sent[0]
        self.assertEqual(sent.get("sale_unit"), "kg")
        self.assertEqual(sent.get("temperature"), "calda")
        self.assertEqual(sent.get("portion"), "mezza")
        self.assertEqual(sent.get("size"), "mezza")

    def test_save_order_to_base44_piece_item_no_kg_fields(self):
        """save_order_to_base44 does not add temperature or portion for piece items."""
        from unittest.mock import MagicMock, patch
        import app.services.conversation_service as svc

        piece_item = {
            "pizza_name": "FIT Bresaola",
            "quantity": 1,
            "sale_unit": "piece",
            "dough_type": "classica",
            "add_ingredients": [],
            "remove_ingredients": [],
            "base_price": 8.5,
            "extras_price": 0.0,
            "total_price": 8.5,
        }

        captured: dict = {}

        def fake_post(url, *, json, headers, timeout):
            captured.update(json)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = '{"id": "test-id"}'
            mock_resp.raise_for_status = lambda: None
            mock_resp.json.return_value = {"id": "test-id"}
            return mock_resp

        with (
            patch.dict(os.environ, {"BASE44_TOKEN": "test-key"}),
            patch("app.services.conversation_service.httpx.post", side_effect=fake_post),
        ):
            svc.save_order_to_base44(
                customer_name="Laura",
                customer_phone=None,
                pickup_time="13:00",
                order_number=1002,
                ai_confidence=0.99,
                items=[piece_item],
                pickup_date=None,
            )

        self.assertNotIn("pickup_date", captured)
        items_sent = captured.get("items", [])
        self.assertEqual(len(items_sent), 1)
        sent = items_sent[0]
        self.assertEqual(sent.get("sale_unit"), "piece")
        self.assertNotIn("temperature", sent)
        self.assertNotIn("portion", sent)

    def test_save_order_to_base44_kg_piena_portion(self):
        """portion='piena' is sent for size='piena' kg items."""
        from unittest.mock import MagicMock, patch
        import app.services.conversation_service as svc

        kg_item = {
            "pizza_name": "Capricciosa al taglio",
            "quantity": 1.0,
            "sale_unit": "kg",
            "size": "piena",
            "temperature": "fredda",
            "dough_type": "classica",
            "add_ingredients": [],
            "remove_ingredients": [],
            "base_price": 18.5,
            "extras_price": 0.0,
            "total_price": 18.5,
        }

        captured: dict = {}

        def fake_post(url, *, json, headers, timeout):
            captured.update(json)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = '{"id": "test-id"}'
            mock_resp.raise_for_status = lambda: None
            mock_resp.json.return_value = {"id": "test-id"}
            return mock_resp

        with (
            patch.dict(os.environ, {"BASE44_TOKEN": "test-key"}),
            patch("app.services.conversation_service.httpx.post", side_effect=fake_post),
        ):
            svc.save_order_to_base44(
                customer_name="Luca",
                customer_phone=None,
                pickup_time="18:30",
                order_number=1003,
                ai_confidence=0.90,
                items=[kg_item],
                pickup_date="2026-07-24",
            )

        sent = captured.get("items", [])[0]
        self.assertEqual(sent.get("portion"), "piena")
        self.assertEqual(sent.get("temperature"), "fredda")
        self.assertEqual(captured.get("pickup_date"), "2026-07-24")

    def test_preorder_confirmation_question_names_tomorrow(self):
        """Al-taglio order reaching awaiting_confirmation builds the 'per domani ...
        Confermo?' question (regression: datetime/ZoneInfo were not imported)."""
        import datetime
        from unittest.mock import patch
        from zoneinfo import ZoneInfo
        from app.schemas import ChatRequest

        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)
        tomorrow = datetime.datetime.now(tz=ZoneInfo("Europe/Rome")).date() + datetime.timedelta(days=1)
        extracted = {
            "intent": "add_items",
            "customer_name": "Elena",
            "pickup_time": "19:30",
            "items": [{
                "pizza_name": "Bufala al taglio",
                "pizza_type": "Normale",
                "dough_type": "classica",
                "quantity": 0.5,
                "size": "piena",
                "temperature": "fredda",
                "add_ingredients": [],
                "remove_ingredients": [],
            }],
        }

        with (
            patch.object(chat_module, "is_agent_active", return_value=True),
            patch.object(chat_module, "is_reservations_enabled", return_value=False),
            patch.object(chat_module, "get_next_open_day", return_value=(tomorrow, "giovedì")),
            patch.object(chat_module, "get_proposable_menu", return_value=[{"name": "Bufala al taglio"}]),
            patch.object(chat_module, "get_sold_out_item_names", return_value=set()),
            patch.object(chat_module, "validate_pickup_time", return_value=(True, None, None)),
            patch.object(chat_module, "detect_reservation_intent", return_value=False),
            patch.object(chat_module, "extract_order_from_text", return_value=extracted),
            Session(engine) as session,
        ):
            session.add(MenuItem(
                name="Bufala al taglio", category="rosse", pizza_type="Normale",
                price=18.50, sale_unit="kg",
            ))
            session.add(ConversationSession(
                session_id="preorder-confirm", items_json="[]", state="collecting_items", completed=False,
            ))
            session.commit()

            response = chat_module.chat(
                ChatRequest(session_id="preorder-confirm", message="mezzo chilo di bufala, Elena, 19:30"),
                session,
            )

        self.assertEqual(response.state, "awaiting_confirmation")
        self.assertIn("per domani giovedì alle 19:30", response.response_message)
        self.assertIn("Confermo?", response.response_message)


if __name__ == "__main__":
    unittest.main()
