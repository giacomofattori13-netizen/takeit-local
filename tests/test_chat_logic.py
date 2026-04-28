import unittest

from sqlmodel import SQLModel, Session, create_engine, select

from app.models import ConversationSession, Order, OrderItem
from app.routes.chat import (
    _persist_order_once,
    determine_state,
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
