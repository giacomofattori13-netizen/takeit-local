"""Conferma ordine: numero definitivo prima della risposta, retry, avviso al titolare."""
import json
import os
import time
import unittest
from unittest.mock import patch

from sqlmodel import SQLModel, Session, create_engine, select
from sqlmodel.pool import StaticPool

import app.routes.chat as chat_module
from app.models import ConversationSession, Order, OrderSideEffect
from app.services import conversation_service

ITEMS = [{
    "pizza_name": "Bufala",
    "pizza_type": "Normale",
    "quantity": 0.2,
    "sale_unit": "kg",
    "size": "mezza",
    "temperature": "calda",
    "base_price": 18.5,
    "extras_price": 0.0,
    "total_price": 3.7,
}]


class OrderConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(self.engine)
        for p in (
            patch.object(chat_module, "_db_engine", self.engine),
            patch.object(chat_module, "_schedule_order_side_effect_job", return_value=None),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _new_order(self, session):
        conversation = ConversationSession(
            session_id="sess-1", customer_phone="+393331234567", restaurant_id="rest-pap",
        )
        order = Order(conversation_session_id="sess-1", customer_name="Giacomo", pickup_time="20:00")
        session.add(conversation)
        session.add(order)
        session.commit()
        session.refresh(order)
        return conversation, order

    def _finalize(self, session, conversation, order):
        chat_module._finalize_new_order(
            session=session,
            order=order,
            conversation=conversation,
            merged_order={
                "customer_name": "Giacomo",
                "pickup_time": "20:00",
                "items": ITEMS,
                "pickup_date": "2026-09-30",
            },
            enriched_items=ITEMS,
            total_amount=3.7,
            restaurant_id="rest-pap",
            ai_confidence=0.95,
        )

    def _jobs(self, session):
        return session.exec(select(OrderSideEffect)).all()

    def test_number_is_final_before_confirmation_side_effects(self):
        events = []

        def fake_save(**kwargs):
            events.append("base44_saved")
            return {"id": "b44-1", "order_number": 7}

        real_enqueue = chat_module._enqueue_order_side_effects

        def spy_enqueue(**kwargs):
            events.append("side_effects_enqueued")
            return real_enqueue(**kwargs)

        with Session(self.engine) as session:
            conversation, order = self._new_order(session)
            with patch.object(chat_module, "save_order_to_base44", side_effect=fake_save), \
                    patch.object(chat_module, "_enqueue_order_side_effects", side_effect=spy_enqueue):
                self._finalize(session, conversation, order)
            session.refresh(order)
            jobs = self._jobs(session)

        # Base44 (e il controllo doppioni) prima di WhatsApp/SMS e della risposta al cliente
        self.assertEqual(events, ["base44_saved", "side_effects_enqueued"])
        self.assertEqual((order.order_number, order.base44_id), (7, "b44-1"))
        self.assertEqual({j.kind for j in jobs}, {"whatsapp_confirmation", "customer_upsert"})

    def test_base44_down_at_confirmation_moves_order_to_retry_job(self):
        with Session(self.engine) as session:
            conversation, order = self._new_order(session)
            with patch.object(chat_module, "save_order_to_base44", side_effect=RuntimeError("HTTP 503")):
                self._finalize(session, conversation, order)
            session.refresh(order)
            jobs = {j.kind: j for j in self._jobs(session)}

        self.assertIsNone(order.order_number)
        self.assertIn("base44_order", jobs)
        payload = json.loads(jobs["base44_order"].payload_json)
        self.assertEqual(payload["session_id"], "sess-1")
        self.assertEqual(payload["restaurant_id"], "rest-pap")
        self.assertEqual(payload["pickup_date"], "2026-09-30")

    def test_retry_job_success_stores_final_number_on_local_order(self):
        with Session(self.engine) as session:
            _conversation, order = self._new_order(session)
            order_id = order.id
        payload = {
            "session_id": "sess-1", "restaurant_id": "rest-pap", "customer_name": "Giacomo",
            "customer_phone": "+393331234567", "pickup_time": "20:00", "ai_confidence": 0.95,
            "items": ITEMS, "total_amount": 3.7, "order_date": "2026-09-29",
        }
        with patch.object(chat_module, "save_order_to_base44", return_value={"id": "b44-9", "order_number": 4}):
            chat_module._execute_order_side_effect("base44_order", payload)

        with Session(self.engine) as session:
            order = session.get(Order, order_id)
        self.assertEqual((order.order_number, order.base44_id), (4, "b44-9"))

    def _failing_job(self, attempts_done):
        payload = {
            "session_id": "sess-1", "restaurant_id": "rest-pap", "customer_name": "Giacomo",
            "customer_phone": "+393331234567", "pickup_time": "20:00", "pickup_date": "2026-09-30",
            "ai_confidence": 0.95, "items": ITEMS, "total_amount": 3.7, "order_date": "2026-09-29",
        }
        with Session(self.engine) as session:
            job = OrderSideEffect(
                order_number=1, kind="base44_order", payload_json=json.dumps(payload),
                status="retry", attempts=attempts_done, next_attempt_at=time.time() - 1,
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            return job.id

    def test_owner_is_alerted_when_order_is_not_saved_after_all_attempts(self):
        job_id = self._failing_job(chat_module._SIDE_EFFECT_MAX_ATTEMPTS - 1)
        alerts = []
        with patch.object(chat_module, "save_order_to_base44", side_effect=RuntimeError("HTTP 503")), \
                patch.object(chat_module, "send_owner_alert", side_effect=lambda body: alerts.append(body) or "wa_inviato:201"):
            chat_module._process_order_side_effect_job(job_id)

        with Session(self.engine) as session:
            job = session.get(OrderSideEffect, job_id)
        self.assertEqual(job.status, "failed")
        self.assertEqual(len(alerts), 1)
        body = alerts[0]
        self.assertIn("ORDINE NON SALVATO", body)
        self.assertIn("Giacomo +393331234567", body)
        self.assertIn("Ritiro: 2026-09-30 20:00", body)
        self.assertIn("- 200g Bufala (calda)", body)
        self.assertIn("€3.70", body)
        self.assertIn("HTTP 503", body)

    def test_owner_is_not_alerted_while_attempts_remain(self):
        job_id = self._failing_job(0)
        with patch.object(chat_module, "save_order_to_base44", side_effect=RuntimeError("HTTP 503")), \
                patch.object(chat_module, "send_owner_alert") as alert:
            chat_module._process_order_side_effect_job(job_id)

        with Session(self.engine) as session:
            self.assertEqual(session.get(OrderSideEffect, job_id).status, "retry")
        alert.assert_not_called()


class OwnerAlertTests(unittest.TestCase):
    ENV = {
        "OWNER_PHONE": "+393339990000",
        "TWILIO_ACCOUNT_SID": "AC-test",
        "TWILIO_AUTH_TOKEN": "tok",
        "TWILIO_WHATSAPP_FROM": "whatsapp:+14155238886",
        "TWILIO_NUMBER": "+16067334996",
    }

    def setUp(self):
        p = patch.dict(os.environ, self.ENV)
        p.start()
        self.addCleanup(p.stop)
        self.sent = []

    def _fake_send(self, results):
        def send(to, body, from_number, account_sid, auth_token):
            self.sent.append((to, from_number))
            return results.pop(0)
        return send

    def test_whatsapp_first(self):
        with patch.object(conversation_service, "_send_twilio_message", side_effect=self._fake_send(["inviato:201"])):
            status = conversation_service.send_owner_alert("avviso")

        self.assertEqual(status, "wa_inviato:201")
        self.assertEqual(self.sent, [("whatsapp:+393339990000", "whatsapp:+14155238886")])

    def test_falls_back_to_sms_when_whatsapp_is_refused(self):
        with patch.object(
            conversation_service, "_send_twilio_message",
            side_effect=self._fake_send(["errore:HTTP_400", "inviato:201"]),
        ):
            status = conversation_service.send_owner_alert("avviso")

        self.assertEqual(status, "wa_errore:HTTP_400|sms_inviato:201")
        self.assertEqual(self.sent[1], ("+393339990000", "+16067334996"))

    def test_skips_without_owner_phone(self):
        with patch.dict(os.environ, {"OWNER_PHONE": ""}), \
                patch.object(conversation_service, "_send_twilio_message") as send:
            self.assertEqual(conversation_service.send_owner_alert("avviso"), "skip:configurazione_mancante")
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
