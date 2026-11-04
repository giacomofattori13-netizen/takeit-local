"""Chiamata a cucina chiusa: ciò che il cliente ha già detto non si richiede.

Caso reale: CallLog ffbebdcb-54db-4c91-930b-ad023289d3e8 (Corte del Sole, 03/11 21:47).
"2 salamino da asporto per domani alle 8" → l'agente ha chiesto "vuole ordinare per
domani?", poi di nuovo prodotti e orario, e ha ripetuto due volte il giorno di ritiro.
Giovedì 1 ottobre 2026, ore 23:00: domani è venerdì.
"""
import datetime
import os
import unittest
from unittest.mock import patch

import app.routes.chat as chat_module
from app.services import conversation_service as service
from tests.test_opening_hours_flow import CDS, FRIDAY, MARGHERITA, NOTHING, ROME, _CorteHarness

DAY_PHRASE = "il ritiro è per"
QUESTION = "vuole ordinare per"


def margherite(quantity=2):
    return {**MARGHERITA, "quantity": quantity}


class _ClosedKitchen(_CorteHarness):
    now = datetime.datetime(2026, 10, 1, 23, 0, tzinfo=ROME)

    def setUp(self):
        super().setUp()
        self.replies = []

    def say(self, message, extraction=None):
        response = super().say(message, extraction)
        self.replies.append(response.response_message)
        return response

    def order_sentence(self, message, items=None, **extra):
        return self.say(message, {**NOTHING, "intent": "add_items", "items": items or [margherite()], **extra})

    def assert_day_never_repeated(self):
        said = [reply for reply in self.replies if DAY_PHRASE in reply.lower()]
        self.assertLessEqual(len(said), 1, self.replies)


class DayAlreadySaidTests(_ClosedKitchen):
    def test_products_day_time_and_name_go_straight_to_the_recap(self):
        r = self.order_sentence("2 margherite per domani alle 8, sono Giacomo", customer_name="Giacomo", pickup_time="20:00")
        self.assertEqual(r.state, "awaiting_confirmation")
        self.assertEqual(
            r.response_message,
            "La cucina stasera è chiusa, segno per domani venerdì. "
            "Allora: 2 margherite, per domani venerdì alle 20, a nome Giacomo. Confermo?",
        )
        r = self.say("sì")
        self.assertEqual(r.state, "completed")
        self.assertEqual(self.saved[0]["pickup_date"], FRIDAY.isoformat())
        self.assertEqual(self.saved[0]["pickup_time"], "20:00")
        self.assert_day_never_repeated()

    def test_replay_of_the_real_call(self):
        """Prodotto fuori menu + giorno + orario: si chiede solo il nome che manca."""
        salamino = {**MARGHERITA, "pizza_name": "Personalizzata", "quantity": 2, "add_ingredients": ["salamino"]}
        r = self.order_sentence(
            "Ciao, posso ordinarti 2 salamino da asporto per domani alle 8.", [salamino], pickup_time="20:00",
        )
        self.assertEqual(r.state, "collecting_name")
        self.assertEqual(r.response_message, "La cucina stasera è chiusa, segno per domani venerdì. A che nome?")
        r = self.say("Giacomo")
        self.assertEqual(r.state, "awaiting_confirmation")
        self.assertIn("alle 20", r.response_message)
        self.assertIn("salamino", r.response_message)
        self.say("sì")
        self.assertEqual(self.saved[0]["items"][0]["quantity"], 2)
        self.assertEqual(self.saved[0]["items"][0]["add_ingredients"], ["salamino"])
        for reply in self.replies:
            self.assertNotIn(QUESTION, reply)
            self.assertNotIn("Cosa desidera", reply)
            self.assertNotIn("A che ora", reply)
        self.assert_day_never_repeated()

    def test_only_tomorrow_skips_the_question(self):
        # L'LLM restituisce "prima_possibile" per "per domani": non è un orario detto
        r = self.say("vorrei ordinare per domani", {**NOTHING, "intent": "set_pickup_time", "pickup_time": "prima_possibile"})
        self.assertEqual(r.state, "collecting_items")
        self.assertEqual(r.response_message, "La cucina stasera è chiusa, segno per domani venerdì. Cosa desidera ordinare?")
        self.assertIsNone(self.conversation().pickup_time)
        self.assertEqual(self.conversation().pickup_date, FRIDAY.isoformat())

        r = self.order_sentence("2 margherite", customer_name="Giacomo")
        self.assertEqual((r.state, r.response_message), ("collecting_pickup_time", "A che ora passa?"))
        r = self.say("alle 8")
        self.assertEqual(r.state, "awaiting_confirmation")
        self.assertIn("per domani venerdì alle 20", r.response_message)
        for reply in self.replies:
            self.assertNotIn(QUESTION, reply)
        self.assert_day_never_repeated()

    def test_products_and_day_ask_only_the_first_missing_field(self):
        r = self.order_sentence("2 margherite per domani")
        self.assertEqual(r.response_message, "La cucina stasera è chiusa, segno per domani venerdì. A che nome?")
        r = self.say("Giacomo")
        self.assertEqual(r.response_message, "A che ora passa?")
        self.assert_day_never_repeated()

    def test_as_soon_as_possible_tomorrow_is_the_opening_time(self):
        r = self.order_sentence("2 margherite domani il prima possibile, sono Giacomo", customer_name="Giacomo", pickup_time="prima_possibile")
        self.assertEqual(r.state, "awaiting_confirmation")
        self.assertIn("alle 19", r.response_message)

    def test_explicit_weekday(self):
        r = self.order_sentence("2 margherite per sabato alle 8, sono Giacomo", customer_name="Giacomo", pickup_time="20:00")
        self.assertTrue(r.response_message.startswith("La cucina stasera è chiusa, segno per sabato. Allora:"))
        self.say("sì")
        self.assertEqual(self.saved[0]["pickup_date"], "2026-10-03")

    def test_day_after_tomorrow_is_not_tomorrow(self):
        self.order_sentence("2 margherite per dopodomani")
        self.assertEqual(self.conversation().pickup_date, "2026-10-03")

    def test_time_outside_the_hours_of_that_day_is_refused_but_the_day_stays(self):
        r = self.order_sentence("2 margherite per domani alle 23, sono Giacomo", customer_name="Giacomo", pickup_time="23:00")
        self.assertEqual(r.state, "collecting_pickup_time")
        self.assertIn("siamo aperti", r.response_message)
        self.assertEqual(self.conversation().pickup_date, FRIDAY.isoformat())
        self.assertIsNone(self.conversation().pickup_time)

    def test_day_said_while_the_time_is_being_collected(self):
        """Cucina chiusa a metà chiamata, risposta all'orario: "domani alle 8"."""
        self.order_sentence("2 margherite per domani", customer_name="Giacomo")
        conversation = self.conversation()
        conversation.state = "collecting_pickup_time"
        conversation.pickup_date = None
        conversation.slot_attempts_json = "{}"
        self.session.add(conversation)
        self.session.commit()
        r = self.say("domani alle 8")
        self.assertEqual(r.state, "awaiting_confirmation")
        self.assertTrue(r.response_message.startswith("La cucina stasera è chiusa, segno per domani venerdì."))
        self.assertIn("alle 20", r.response_message)

    def test_closed_day_gets_a_proposal_and_the_order_stays(self):
        self.restaurant = {**CDS, "opening_hours": {**CDS["opening_hours"], "saturday": ""}}
        r = self.order_sentence("2 margherite per sabato alle 8", pickup_time="20:00")
        self.assertEqual(r.state, "confirming_next_day")
        self.assertEqual(r.response_message, "Sabato siamo chiusi, il primo giorno disponibile è domenica, va bene?")
        self.assertIsNone(self.conversation().pickup_date)
        self.assertEqual(len(chat_module.json.loads(self.conversation().items_json)), 1)

    def test_reservation_for_tomorrow_does_not_touch_the_pickup_day(self):
        self.say("vorrei prenotare un tavolo per domani")
        self.assertIsNone(self.conversation().pickup_date)

    def test_cancel_does_not_set_the_day(self):
        self.say("annulla, passo domani", {**NOTHING, "intent": "cancel_order"})
        self.assertIsNone(self.conversation().pickup_date)


class InformationKeptAcrossTheQuestionTests(_ClosedKitchen):
    """Giorno non detto: la domanda resta, ma prodotti, orario e nome non si perdono."""

    def test_time_and_products_survive_the_question(self):
        r = self.order_sentence("2 margherite alle 8", pickup_time="20:00")
        self.assertEqual(r.state, "confirming_next_day")
        self.assertIsNone(self.conversation().pickup_time)  # oggi non vale
        r = self.say("sì")
        self.assertEqual(r.state, "collecting_name")
        self.assertEqual(r.response_message, "Perfetto, il ritiro è per domani, venerdì. A che nome?")
        r = self.say("Giacomo")
        self.assertEqual(r.state, "awaiting_confirmation")  # l'orario non si richiede
        self.assertIn("2 margherite, per domani venerdì alle 20", r.response_message)
        self.assert_day_never_repeated()

    def test_name_survives_the_question(self):
        self.order_sentence("2 margherite alle 8, sono Giacomo", customer_name="Giacomo", pickup_time="20:00")
        r = self.say("sì")
        self.assertEqual(r.state, "awaiting_confirmation")
        self.assertIn("a nome Giacomo", r.response_message)

    def test_tomorrow_is_a_valid_answer_to_the_question(self):
        self.order_sentence("2 margherite alle 8", pickup_time="20:00")
        calls = chat_module.extract_order_from_text.call_count
        r = self.say("per domani grazie")
        self.assertEqual(r.state, "collecting_name")
        self.assertEqual(self.conversation().pickup_date, FRIDAY.isoformat())
        self.assertEqual(chat_module.extract_order_from_text.call_count, calls)  # niente LLM

    def test_another_day_as_answer_wins_over_the_next_open_day(self):
        self.order_sentence("2 margherite")
        self.say("sabato")
        self.assertEqual(self.conversation().pickup_date, "2026-10-03")

    def test_answer_with_products_and_time_is_extracted(self):
        self.say("vorrei ordinare", {**NOTHING, "intent": "add_items"})
        r = self.order_sentence("sì, due margherite alle 8", customer_name="Giacomo", pickup_time="20:00")
        self.assertEqual(r.state, "awaiting_confirmation")
        self.assertTrue(r.response_message.startswith("Perfetto, il ritiro è per domani, venerdì. Allora: 2 margherite"))
        self.assert_day_never_repeated()

    def test_bare_yes_with_empty_cart_asks_for_products(self):
        self.say("vorrei ordinare", {**NOTHING, "intent": "add_items"})
        r = self.say("sì")
        self.assertEqual(r.response_message, "Perfetto, il ritiro è per domani, venerdì. Cosa desidera ordinare?")
        r = self.order_sentence("2 margherite", customer_name="Giacomo")
        self.assertEqual(r.response_message, "A che ora passa?")
        self.assert_day_never_repeated()

    def test_no_asks_for_the_day_and_keeps_order_and_time(self):
        self.order_sentence("2 margherite alle 8", pickup_time="20:00")
        r = self.say("no grazie")
        self.assertEqual((r.state, r.response_message), ("confirming_next_day", "Per quale giorno le serve?"))
        self.assertEqual(self.saved, [])
        r = self.say("sabato")
        self.assertEqual(r.state, "collecting_name")
        self.say("Giacomo")
        self.say("sì")
        self.assertEqual((self.saved[0]["pickup_date"], self.saved[0]["pickup_time"]), ("2026-10-03", "20:00"))


class UsualOrderTests(_ClosedKitchen):
    """Cliente con pizze abituali: "2 pizze" apre "le solite?" prima dell'LLM."""

    def test_day_and_time_survive_the_usual_question(self):
        conversation = self.conversation()
        conversation.favorite_pizzas_json = '["Margherita"]'
        self.session.add(conversation)
        self.session.commit()
        r = self.say("2 pizze per domani alle 20")
        self.assertEqual(r.state, "confirming_usual")
        self.assertTrue(r.response_message.startswith("La cucina stasera è chiusa, segno per domani venerdì. Le preparo le solite?"))
        self.assertEqual(self.conversation().pickup_date, FRIDAY.isoformat())
        self.assertEqual(self.conversation().pickup_time, "20:00")


class CustomerConfirmationMessageTests(_ClosedKitchen):
    def _confirm(self):
        self.order_sentence("2 margherite per domani alle 8, sono Giacomo", customer_name="Giacomo", pickup_time="20:00")
        return self.say("sì")

    def test_mobile_is_told_about_the_sms(self):
        self.assertEqual(self._confirm().response_message, "Perfetto, Giacomo! Le arriva un SMS di conferma.")
        self.assertFalse([reply for reply in self.replies if "whatsapp" in reply.lower()])

    def test_landline_is_not_promised_a_message(self):
        conversation = self.conversation()
        conversation.customer_phone = "+390451234567"
        self.session.add(conversation)
        self.session.commit()
        self.assertEqual(self._confirm().response_message, "Perfetto, Giacomo, a presto!")


class WholeQuantityTests(unittest.TestCase):
    """L'LLM restituisce le quantità come float: il cliente non deve sentire "2.0 margherite"."""

    def _quantity(self, quantity):
        payload = {"intent": "add_items", "items": [{"pizza_name": "Margherita", "quantity": quantity}]}
        return service._normalize_extracted_payload(payload)["items"][0]["quantity"]

    def test_whole_quantity_becomes_int_and_weight_stays_decimal(self):
        self.assertEqual(repr(self._quantity(2.0)), "2")
        self.assertEqual(self._quantity(0.5), 0.5)
        self.assertEqual(repr(self._quantity(0)), "0")

    def test_recap_text(self):
        item = {**margherite(self._quantity(2.0)), "sale_unit": "piece"}
        self.assertEqual(chat_module.format_items_for_customer([item]), "2 margherite")


class WhatsappSandboxTests(unittest.TestCase):
    SANDBOX = "whatsapp:+14155238886"
    REAL = "whatsapp:+393330001111"

    def _env(self, whatsapp_from):
        env = {
            "TWILIO_ACCOUNT_SID": "AC-test", "TWILIO_AUTH_TOKEN": "tok", "TWILIO_NUMBER": "+16067334996",
            "OWNER_PHONE": "+393339990000", "TWILIO_WHATSAPP_FROM": whatsapp_from,
        }
        return patch.dict(os.environ, env)

    def test_sender(self):
        for value, expected in (
            (self.SANDBOX, None), ("+14155238886", None), (" whatsapp:+14155238886 ", None),
            ("", None), (self.REAL, "+393330001111"),
        ):
            with self._env(value), self.subTest(value=value):
                self.assertEqual(service.whatsapp_sender(), expected)

    def _confirmation(self, whatsapp_from):
        with self._env(whatsapp_from), \
                patch.object(service, "restaurant_contact", return_value=("Corte", "+39045")), \
                patch.object(service, "_send_sms", return_value="sms_errore:HTTP_500"), \
                patch.object(service, "_send_whatsapp", return_value="wa_inviato:201") as send_wa:
            status = service.send_whatsapp_confirmation(
                "Giacomo", "+393331234567", "20:00", [], 10.0, pickup_date="2026-10-02",
            )
        return status, send_wa

    def test_customer_confirmation_never_tries_the_sandbox(self):
        status, send_wa = self._confirmation(self.SANDBOX)
        send_wa.assert_not_called()
        self.assertEqual(status, "sms_errore:HTTP_500")

    def test_customer_confirmation_falls_back_on_a_real_sender(self):
        status, send_wa = self._confirmation(self.REAL)
        send_wa.assert_called_once()
        self.assertEqual(status, "sms_errore:HTTP_500|wa_inviato:201")

    def test_owner_alert_goes_by_sms_with_the_sandbox(self):
        with self._env(self.SANDBOX), patch.object(service, "_send_twilio_message", return_value="inviato:201") as send:
            status = service.send_owner_alert("avviso")
        self.assertEqual(status, "sms_inviato:201")
        send.assert_called_once()
        self.assertEqual(send.call_args.args[:3], ("+393339990000", "avviso", "+16067334996"))


if __name__ == "__main__":
    unittest.main()
