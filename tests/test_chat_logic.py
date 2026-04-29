import unittest

from sqlmodel import SQLModel, Session, create_engine, select

import app.routes.chat as chat_module
from app.models import ConversationSession, MenuItem, Order, OrderItem
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
        self.assertEqual(_extract_local_pickup_time("alle 8 e mezza"), "8:30")
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


if __name__ == "__main__":
    unittest.main()
