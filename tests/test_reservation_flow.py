import datetime as dt
import json
import unittest

from sqlmodel import SQLModel, Session, create_engine, select

import app.routes.chat as chat_module
import app.services.conversation_service as service_module
from app.models import ConversationSession, OrderSideEffect
from app.schemas import ChatRequest


class ReservationFlowTests(unittest.TestCase):
    def test_reservation_save_failure_does_not_confirm(self):
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)

        original_is_agent_active = chat_module.is_agent_active
        original_validate = chat_module.validate_reservation_time
        original_check = chat_module.check_reservation_availability
        original_save = chat_module.save_reservation_to_base44
        original_sms = chat_module.send_reservation_sms
        sms_calls = []

        chat_module.is_agent_active = lambda: True
        chat_module.validate_reservation_time = lambda *args, **kwargs: (True, None)
        chat_module.check_reservation_availability = lambda *args, **kwargs: (
            True,
            None,
            {
                "table_id": "t1",
                "table_name": "Tavolo 1",
                "combined_tables": [],
                "extended": False,
            },
        )
        chat_module.save_reservation_to_base44 = lambda **kwargs: None
        chat_module.send_reservation_sms = lambda **kwargs: sms_calls.append(kwargs)

        try:
            with Session(engine) as session:
                conversation = ConversationSession(
                    session_id="reservation-1",
                    customer_name="Mario Rossi",
                    customer_phone="+393331234567",
                    items_json="[]",
                    state="awaiting_reservation_confirmation",
                    completed=False,
                    reservation_json=json.dumps({
                        "date": "2026-05-15",
                        "time": "20:00",
                        "party_size": 2,
                        "table_id": "old",
                        "table_name": "Vecchio tavolo",
                    }),
                )
                session.add(conversation)
                session.commit()

                response = chat_module.chat(
                    ChatRequest(session_id="reservation-1", message="sì"),
                    session,
                )
                updated = session.exec(
                    select(ConversationSession).where(
                        ConversationSession.session_id == "reservation-1"
                    )
                ).one()
        finally:
            chat_module.is_agent_active = original_is_agent_active
            chat_module.validate_reservation_time = original_validate
            chat_module.check_reservation_availability = original_check
            chat_module.save_reservation_to_base44 = original_save
            chat_module.send_reservation_sms = original_sms

        self.assertEqual(response.state, "awaiting_reservation_confirmation")
        self.assertFalse(response.valid)
        self.assertFalse(updated.completed)
        self.assertEqual(updated.state, "awaiting_reservation_confirmation")
        self.assertIn("non riesco a registrare", response.response_message)
        self.assertEqual(sms_calls, [])

    def test_availability_fetches_tables_once_and_returns_suggested_table(self):
        original_restaurant = service_module.load_restaurant
        original_reservations = service_module._fetch_reservations_for_date
        original_tables = service_module._fetch_tables_from_base44
        table_fetches = []

        service_module.load_restaurant = lambda: {"reservation_slot_minutes": 90}
        service_module._fetch_reservations_for_date = lambda date, **kwargs: [
            {"date": date, "time": "20:00", "table_id": "t1", "status": "confermata"}
        ]

        def fake_tables(**kwargs):
            table_fetches.append("fetch")
            return [{"id": "t1", "name": "Tavolo 1", "capacity": 4}]

        service_module._fetch_tables_from_base44 = fake_tables

        try:
            available, next_slot, table_info = service_module.check_reservation_availability(
                "2026-05-15",
                "20:00",
                2,
            )
        finally:
            service_module.load_restaurant = original_restaurant
            service_module._fetch_reservations_for_date = original_reservations
            service_module._fetch_tables_from_base44 = original_tables

        self.assertFalse(available)
        self.assertEqual(next_slot, "21:30")
        self.assertEqual(table_info["table_id"], "t1")
        self.assertEqual(table_fetches, ["fetch"])

    def test_availability_does_not_suggest_slots_after_closing(self):
        today = dt.date.today()
        days_until_friday = (4 - today.weekday()) % 7 or 7
        request_date = today + dt.timedelta(days=days_until_friday)

        original_restaurant = service_module.load_restaurant
        original_reservations = service_module._fetch_reservations_for_date
        original_tables = service_module._fetch_tables_from_base44

        service_module.load_restaurant = lambda: {
            "reservation_slot_minutes": 90,
            "opening_hours": {
                "monday": "18:00-23:00",
                "tuesday": "18:00-23:00",
                "wednesday": "18:00-23:00",
                "thursday": "18:00-23:00",
                "friday": "18:00-21:00",
                "saturday": "18:00-23:00",
                "sunday": "18:00-23:00",
            },
        }
        service_module._fetch_reservations_for_date = lambda date, **kwargs: [
            {"date": date, "time": "20:00", "table_id": "t1", "status": "confermata"}
        ]
        service_module._fetch_tables_from_base44 = lambda **kwargs: [
            {"id": "t1", "name": "Tavolo 1", "capacity": 4}
        ]

        try:
            available, next_slot, table_info = service_module.check_reservation_availability(
                request_date.isoformat(),
                "20:00",
                2,
            )
        finally:
            service_module.load_restaurant = original_restaurant
            service_module._fetch_reservations_for_date = original_reservations
            service_module._fetch_tables_from_base44 = original_tables

        self.assertFalse(available)
        self.assertIsNone(next_slot)
        self.assertIsNone(table_info)

    def test_negative_confirmation_is_not_treated_as_confirm(self):
        self.assertEqual(
            chat_module._reservation_confirmation_intent("non confermo"),
            "cancel",
        )
        self.assertEqual(
            chat_module._reservation_confirmation_intent("non va bene"),
            "cancel",
        )
        self.assertEqual(
            chat_module._reservation_confirmation_intent("sì confermo"),
            "confirm",
        )

    def test_reservation_completed_repeat_uses_reservation_response(self):
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)
        original_is_agent_active = chat_module.is_agent_active
        chat_module.is_agent_active = lambda: True
        try:
            with Session(engine) as session:
                session.add(ConversationSession(
                    session_id="reservation-done",
                    items_json="[]",
                    state="reservation_completed",
                    completed=True,
                    reservation_json=json.dumps({
                        "date": "2026-05-15",
                        "time": "20:00",
                        "party_size": 2,
                    }),
                ))
                session.commit()

                response = chat_module.chat(
                    ChatRequest(session_id="reservation-done", message="grazie"),
                    session,
                )
        finally:
            chat_module.is_agent_active = original_is_agent_active

        self.assertEqual(response.state, "reservation_completed")
        self.assertIn("prenotazione", response.response_message.lower())

    def test_availability_error_keeps_reservation_unconfirmed(self):
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)
        original_is_agent_active = chat_module.is_agent_active
        original_check = chat_module.check_reservation_availability

        chat_module.is_agent_active = lambda: True

        def unavailable(*args, **kwargs):
            raise service_module.ReservationAvailabilityError("Base44 down")

        chat_module.check_reservation_availability = unavailable
        try:
            with Session(engine) as session:
                session.add(ConversationSession(
                    session_id="reservation-availability-error",
                    items_json="[]",
                    state="collecting_reservation_party",
                    completed=False,
                    reservation_json=json.dumps({
                        "date": "2026-05-15",
                        "time": "20:00",
                    }),
                ))
                session.commit()

                response = chat_module.chat(
                    ChatRequest(session_id="reservation-availability-error", message="due"),
                    session,
                )
        finally:
            chat_module.is_agent_active = original_is_agent_active
            chat_module.check_reservation_availability = original_check

        self.assertEqual(response.state, "collecting_reservation_party")
        self.assertFalse(response.valid)
        self.assertIn("controllare la disponibilità", response.response_message)

    def test_closed_day_reservation_time_is_rejected(self):
        original_restaurant = service_module.load_restaurant
        service_module.load_restaurant = lambda: {
            "opening_hours": {
                "monday": "closed",
                "tuesday": "18:00-23:00",
                "wednesday": "18:00-23:00",
                "thursday": "18:00-23:00",
                "friday": "18:00-23:00",
                "saturday": "18:00-23:00",
                "sunday": "18:00-23:00",
            }
        }
        try:
            today = dt.date.today()
            days_until_monday = (0 - today.weekday()) % 7 or 7
            closed_date = today + dt.timedelta(days=days_until_monday)
            valid, message = service_module.validate_reservation_time(closed_date.isoformat(), "20:00")
        finally:
            service_module.load_restaurant = original_restaurant

        self.assertFalse(valid)
        self.assertIn("chiusi", message)

    def test_successful_reservation_enqueues_sms_outbox(self):
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)
        scheduled = []
        original_is_agent_active = chat_module.is_agent_active
        original_validate = chat_module.validate_reservation_time
        original_check = chat_module.check_reservation_availability
        original_save = chat_module.save_reservation_to_base44
        original_schedule = chat_module._schedule_order_side_effect_job

        chat_module.is_agent_active = lambda: True
        chat_module.validate_reservation_time = lambda *args, **kwargs: (True, None)
        chat_module.check_reservation_availability = lambda *args, **kwargs: (
            True,
            None,
            {
                "table_id": "t1",
                "table_name": "Tavolo 1",
                "combined_tables": [],
                "extended": False,
            },
        )
        chat_module.save_reservation_to_base44 = lambda **kwargs: "reservation-123"
        chat_module._schedule_order_side_effect_job = lambda job_id, delay_seconds=0.0: scheduled.append(job_id)

        try:
            with Session(engine) as session:
                session.add(ConversationSession(
                    session_id="reservation-success",
                    customer_name="Mario Rossi",
                    customer_phone="+393331234567",
                    items_json="[]",
                    state="awaiting_reservation_confirmation",
                    completed=False,
                    reservation_json=json.dumps({
                        "date": "2026-05-15",
                        "time": "20:00",
                        "party_size": 2,
                    }),
                ))
                session.commit()

                response = chat_module.chat(
                    ChatRequest(session_id="reservation-success", message="sì"),
                    session,
                )
                jobs = session.exec(select(OrderSideEffect)).all()
        finally:
            chat_module.is_agent_active = original_is_agent_active
            chat_module.validate_reservation_time = original_validate
            chat_module.check_reservation_availability = original_check
            chat_module.save_reservation_to_base44 = original_save
            chat_module._schedule_order_side_effect_job = original_schedule

        self.assertEqual(response.state, "reservation_completed")
        self.assertEqual([job.kind for job in jobs], ["reservation_sms"])
        self.assertEqual(len(scheduled), 1)


if __name__ == "__main__":
    unittest.main()
