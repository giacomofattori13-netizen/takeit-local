"""SMS di conferma prenotazione firmato con il nome del ristorante (Restaurant.name),
PIZZERIA_NAME solo se il nome manca. Twilio è simulato."""
import os
import unittest
from unittest.mock import MagicMock, patch

import tests.test_reservation_covers_flow as covers
from app.services import conversation_service as service

_TWILIO_ENV = {
    "TWILIO_ACCOUNT_SID": "AC1",
    "TWILIO_AUTH_TOKEN": "tok",
    "TWILIO_NUMBER": "+15550001111",
    "PIZZERIA_NAME": "Pizzeria di Prova",
    "PIZZERIA_PHONE": "",
}


def _sent_body(**kwargs) -> str:
    with (
        patch.dict(os.environ, _TWILIO_ENV),
        patch.object(service.httpx, "post", return_value=MagicMock(status_code=201)) as post,
    ):
        status = service.send_reservation_sms(
            customer_name="Giacomo", customer_phone="+393331234567",
            date="2026-10-03", time="20:00", party_size=4, **kwargs,
        )
    assert status.startswith("sms_inviato"), status
    return post.call_args.kwargs["data"]["Body"]


class ReservationSmsSenderTests(unittest.TestCase):
    def test_restaurant_name_signs_the_sms(self):
        body = _sent_body(restaurant_name="Corte Del Sole")
        self.assertTrue(body.startswith("Corte Del Sole ✅"))
        self.assertNotIn("Pizzeria di Prova", body)

    def test_pizzeria_name_is_the_fallback(self):
        for name in (None, "", "  "):
            with self.subTest(name=name):
                self.assertTrue(_sent_body(restaurant_name=name).startswith("Pizzeria di Prova ✅"))


class ReservationSmsEnqueueTests(covers._ReservationHarness):
    def test_confirmed_reservation_queues_the_sms_with_the_restaurant_name(self):
        with patch.object(covers.hours.chat_module, "_enqueue_reservation_sms_side_effect") as enqueue:
            self.book("vorrei prenotare un tavolo", "stasera", "alle 20", "4 persone", "Giacomo", "sì")
        self.assertEqual(enqueue.call_args.kwargs["restaurant_name"], "Corte Del Sole")

    def test_outbox_job_passes_the_name_to_the_sender(self):
        with patch.object(covers.hours.chat_module, "send_reservation_sms", return_value="sms_inviato:201") as send:
            covers.hours.chat_module._execute_order_side_effect("reservation_sms", {
                "customer_name": "Giacomo", "customer_phone": "+393331234567",
                "date": "2026-10-03", "time": "20:00", "party_size": 4,
                "table_name": None, "restaurant_name": "Corte Del Sole",
            })
        self.assertEqual(send.call_args.kwargs["restaurant_name"], "Corte Del Sole")

    def test_jobs_queued_before_the_change_still_send(self):
        with patch.object(covers.hours.chat_module, "send_reservation_sms", return_value="sms_inviato:201") as send:
            covers.hours.chat_module._execute_order_side_effect("reservation_sms", {
                "customer_name": "Giacomo", "customer_phone": "+393331234567",
                "date": "2026-10-03", "time": "20:00", "party_size": 4, "table_name": None,
            })
        self.assertIsNone(send.call_args.kwargs["restaurant_name"])


if __name__ == "__main__":
    unittest.main()
