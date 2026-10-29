"""Notifiche per locale: avviso al titolare a ogni nuovo ordine (owner_notify_phone,
OWNER_PHONE solo come ripiego) e SMS al cliente firmati con nome e telefono del
Restaurant del tenant. Twilio e Base44 sono simulati."""
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from sqlmodel import SQLModel, Session, create_engine, select
from sqlmodel.pool import StaticPool

import app.routes.chat as chat_module
from app.models import ConversationSession, Order, OrderSideEffect
from app.services import conversation_service as service

PAP_ID = "rest-pap"
CDS_ID = "rest-cds"
RESTAURANTS = {
    PAP_ID: {"id": PAP_ID, "name": "Pizza a Pezzi", "phone": "+39 011 111111", "owner_notify_phone": "+393330000001"},
    CDS_ID: {"id": CDS_ID, "name": "Corte Del Sole", "phone": "+39 022 222222", "owner_notify_phone": "+393330000002"},
    "rest-empty": {"id": "rest-empty", "name": "Senza Numero", "phone": None, "owner_notify_phone": None},
}

TWILIO_ENV = {
    "TWILIO_ACCOUNT_SID": "AC-test",
    "TWILIO_AUTH_TOKEN": "tok",
    "TWILIO_NUMBER": "+15550001111",
    "OWNER_PHONE": "+393339990000",
    "PIZZERIA_NAME": "Nome Globale",
    "PIZZERIA_PHONE": "+39 000 000000",
}

ITEMS = [{
    "pizza_name": "Bufala",
    "pizza_type": "Normale",
    "quantity": 2,
    "sale_unit": "kg",
    "order_unit": "tranci",
    "size": "piena",
    "temperature": "calda",
    "total_price": None,
}]


def _fake_load_restaurant(restaurant_id: str = "") -> dict:
    return dict(RESTAURANTS.get(restaurant_id, {}))


class _Harness(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        self.sent: list[tuple[str, str]] = []
        for p in (
            patch.dict(os.environ, TWILIO_ENV),
            patch.object(chat_module, "_db_engine", self.engine),
            patch.object(chat_module, "_schedule_order_side_effect_job", return_value=None),
            patch.object(service, "load_restaurant", side_effect=_fake_load_restaurant),
            patch.object(chat_module, "load_restaurant", side_effect=_fake_load_restaurant),
            patch.object(service, "_send_twilio_message", side_effect=self._fake_twilio),
        ):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("TWILIO_WHATSAPP_FROM", None)

    def _fake_twilio(self, to, body, from_number, account_sid, auth_token):
        self.sent.append((to, body))
        return "inviato:201"

    def _finalize(self, restaurant_id, review_reasons=None):
        with Session(self.engine) as session:
            conversation = ConversationSession(
                session_id=f"sess-{restaurant_id}", customer_phone="+393331234567", restaurant_id=restaurant_id,
            )
            order = Order(conversation_session_id=conversation.session_id, customer_name="Giacomo", pickup_time="12:30")
            session.add(conversation)
            session.add(order)
            session.commit()
            session.refresh(order)
            with patch.object(chat_module, "save_order_to_base44", return_value={"id": "b44-1", "order_number": 7}):
                chat_module._finalize_new_order(
                    session=session,
                    order=order,
                    conversation=conversation,
                    merged_order={
                        "customer_name": "Giacomo",
                        "pickup_time": "12:30",
                        "items": ITEMS,
                        "pickup_date": "2026-11-03",
                    },
                    enriched_items=ITEMS,
                    total_amount=None,
                    restaurant_id=restaurant_id,
                    ai_confidence=0.9,
                    review_reasons=review_reasons,
                )
            jobs = {j.kind: j for j in session.exec(select(OrderSideEffect)).all()}
        return jobs

    def _run_owner_job(self, jobs):
        job = jobs["owner_new_order"]
        chat_module._process_order_side_effect_job(job.id)
        with Session(self.engine) as session:
            return session.get(OrderSideEffect, job.id)


class NewOrderOwnerNotificationTests(_Harness):
    def test_normal_order_notifies_the_owner_of_that_restaurant(self):
        jobs = self._finalize(PAP_ID)
        job = self._run_owner_job(jobs)

        self.assertEqual(job.status, "done")
        self.assertEqual(len(self.sent), 1)
        to, body = self.sent[0]
        self.assertEqual(to, "+393330000001")
        self.assertTrue(body.startswith("Nuovo ordine #7"))
        self.assertIn("Cliente: Giacomo +393331234567", body)
        self.assertIn("Ritiro: martedì 3 novembre, ore 12:30", body)
        self.assertIn("- 2 tranci interi Bufala (caldi)", body)
        self.assertNotIn("DA VERIFICARE", body)

    def test_other_restaurant_order_goes_to_its_own_owner(self):
        self._run_owner_job(self._finalize(CDS_ID))
        self.assertEqual([to for to, _ in self.sent], ["+393330000002"])

    def test_order_to_verify_starts_with_da_verificare(self):
        reasons = ["Caldo/freddo non capito dopo 2 tentativi per Bufala: chiedere al cliente"]
        self._run_owner_job(self._finalize(PAP_ID, review_reasons=reasons))

        _to, body = self.sent[0]
        self.assertTrue(body.startswith("DA VERIFICARE"))
        self.assertIn(reasons[0], body)

    def test_empty_owner_notify_phone_falls_back_to_owner_phone_with_warning(self):
        jobs = self._finalize("rest-empty")
        output = io.StringIO()
        with redirect_stdout(output):
            self._run_owner_job(jobs)

        self.assertEqual([to for to, _ in self.sent], ["+393339990000"])
        self.assertIn("WARNING owner_notify_phone vuoto per restaurant_id='rest-empty'", output.getvalue())

    def test_failed_send_is_logged_with_restaurant_and_order_and_retried(self):
        jobs = self._finalize(PAP_ID)
        output = io.StringIO()
        with patch.object(service, "_send_twilio_message", return_value="errore:HTTP_500"), redirect_stdout(output):
            job = self._run_owner_job(jobs)

        self.assertEqual(job.status, "retry")
        logs = output.getvalue()
        self.assertIn("[OwnerNotify] invio fallito restaurant_id='rest-pap'", logs)
        self.assertIn(f"order_id={job.order_number} order_number=7", logs)

    def test_order_saved_by_retry_job_notifies_and_notify_errors_do_not_fail_the_save(self):
        payload = {
            "restaurant_id": PAP_ID, "customer_name": "Giacomo", "customer_phone": "+393331234567",
            "pickup_time": "12:30", "pickup_date": "2026-11-03", "ai_confidence": 0.9,
            "items": ITEMS, "total_amount": None, "order_date": "2026-11-02",
        }
        with patch.object(chat_module, "save_order_to_base44", return_value={"id": "b44-2", "order_number": 3}):
            chat_module._execute_order_side_effect("base44_order", payload)
        with Session(self.engine) as session:
            kinds = [j.kind for j in session.exec(select(OrderSideEffect)).all()]
        self.assertEqual(kinds, ["owner_new_order"])

        # Se l'accodamento fallisce, il job base44_order non deve sollevare (eviterebbe un doppione)
        with patch.object(chat_module, "save_order_to_base44", return_value={"id": "b44-3", "order_number": 4}), \
                patch.object(chat_module, "_enqueue_owner_new_order", side_effect=RuntimeError("db locked")):
            chat_module._execute_order_side_effect("base44_order", payload)

    def test_not_saved_alert_goes_to_the_restaurant_owner(self):
        chat_module._alert_owner_order_not_saved(
            {"restaurant_id": CDS_ID, "customer_name": "Giacomo", "items": ITEMS, "pickup_time": "20:00"},
            attempts=5, last_error="HTTP 503",
        )
        self.assertEqual(self.sent[0][0], "+393330000002")
        self.assertIn("ORDINE NON SALVATO", self.sent[0][1])


class OwnerOrderSmsEnabledTests(_Harness):
    """Restaurant.owner_order_sms_enabled: false spegne la notifica per ogni nuovo
    ordine, tranne per quelli da verificare; null vale true."""

    def _with_flag(self, value):
        restaurants = {**RESTAURANTS, PAP_ID: {**RESTAURANTS[PAP_ID], "owner_order_sms_enabled": value}}
        for target in (service, chat_module):
            p = patch.object(target, "load_restaurant", side_effect=lambda restaurant_id="": dict(restaurants.get(restaurant_id, {})))
            p.start()
            self.addCleanup(p.stop)

    def test_false_skips_normal_order_and_closes_the_job(self):
        self._with_flag(False)
        output = io.StringIO()
        with redirect_stdout(output):
            job = self._run_owner_job(self._finalize(PAP_ID))
        self.assertEqual(self.sent, [])
        self.assertEqual(job.status, "done")
        self.assertIn("owner_order_sms_enabled=false: notifica saltata", output.getvalue())

    def test_false_still_sends_orders_to_verify(self):
        self._with_flag(False)
        self._run_owner_job(self._finalize(PAP_ID, review_reasons=["Quantità non capita"]))
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0][1].startswith("DA VERIFICARE"))

    def test_false_still_sends_low_confidence_orders(self):
        self._with_flag(False)
        payload = chat_module._owner_new_order_payload(
            {"customer_name": "Giacomo", "items": ITEMS, "ai_confidence": 0.5}, 1, 9, PAP_ID,
        )
        chat_module._send_owner_new_order(payload)
        self.assertEqual(len(self.sent), 1)

    def test_null_keeps_current_behaviour(self):
        self._with_flag(None)
        self._run_owner_job(self._finalize(PAP_ID))
        self.assertEqual(len(self.sent), 1)

    def test_true_sends_normal_order(self):
        self._with_flag(True)
        self._run_owner_job(self._finalize(PAP_ID))
        self.assertEqual(len(self.sent), 1)

class CustomerConfirmationSmsTests(_Harness):
    def _sms_body(self, restaurant_id, pickup_date="2026-11-03"):
        with patch.object(service.httpx, "post", return_value=MagicMock(status_code=201, text="")) as post:
            status = service.send_whatsapp_confirmation(
                customer_name="Giacomo", customer_phone="+393331234567", pickup_time="12:30",
                items=ITEMS, total_amount=None, pickup_date=pickup_date, restaurant_id=restaurant_id,
            )
        self.assertTrue(status.startswith("sms_inviato"), status)
        return post.call_args.kwargs["data"]["Body"]

    def test_two_restaurants_do_not_swap_name_or_phone(self):
        pap = self._sms_body(PAP_ID)
        cds = self._sms_body(CDS_ID)

        self.assertTrue(pap.startswith("Pizza a Pezzi"))
        self.assertIn("Per modifiche chiama il +39 011 111111", pap)
        self.assertNotIn("Corte Del Sole", pap)
        self.assertNotIn("+39 022 222222", pap)

        self.assertTrue(cds.startswith("Corte Del Sole"))
        self.assertIn("Per modifiche chiama il +39 022 222222", cds)
        self.assertNotIn("Pizza a Pezzi", cds)
        self.assertNotIn("+39 011 111111", cds)

        for body in (pap, cds):
            self.assertNotIn("Nome Globale", body)
            self.assertNotIn("+39 000 000000", body)

    def test_pickup_day_is_spelled_out(self):
        self.assertIn("Ritiro martedì 3 novembre, ore 12:30", self._sms_body(PAP_ID))

    def test_restaurant_without_phone_omits_the_contact_line(self):
        body = self._sms_body("rest-empty")
        self.assertTrue(body.startswith("Senza Numero"))
        self.assertNotIn("Per modifiche", body)

    def test_outbox_job_passes_day_and_restaurant(self):
        jobs = self._finalize(PAP_ID)
        payload = json.loads(jobs["whatsapp_confirmation"].payload_json)
        with patch.object(chat_module, "send_whatsapp_confirmation", return_value="sms_inviato:201") as send:
            chat_module._execute_order_side_effect("whatsapp_confirmation", payload)
        self.assertEqual(send.call_args.kwargs["restaurant_id"], PAP_ID)
        self.assertEqual(send.call_args.kwargs["pickup_date"], "2026-11-03")

    def test_reservation_sms_uses_the_restaurant_phone(self):
        with patch.object(service.httpx, "post", return_value=MagicMock(status_code=201)) as post:
            service.send_reservation_sms(
                customer_name="Giacomo", customer_phone="+393331234567", date="2026-11-03", time="20:00",
                party_size=2, restaurant_name="Corte Del Sole", restaurant_phone="+39 022 222222",
            )
        body = post.call_args.kwargs["data"]["Body"]
        self.assertIn("Per modifiche chiama il +39 022 222222", body)
        self.assertNotIn("+39 000 000000", body)


class PickupDayFormatTests(unittest.TestCase):
    def test_format(self):
        self.assertEqual(service.format_pickup_day("2026-11-03", "12:30"), "martedì 3 novembre, ore 12:30")
        self.assertEqual(service.format_pickup_day(None, "12:30"), "ore 12:30")


if __name__ == "__main__":
    unittest.main()
