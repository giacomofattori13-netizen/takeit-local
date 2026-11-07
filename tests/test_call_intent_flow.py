"""Ordine o prenotazione, cucina chiusa e "no": le chiamate reali del 4 e 5 novembre a Corte
del Sole, turno per turno, con le parole esatte delle trascrizioni. Base44 e LLM sono simulati."""
import datetime
import json
import unittest
import unittest.mock

import app.routes.chat as chat_module
import tests.test_opening_hours_flow as hours
import tests.test_reservation_covers_flow as covers
from app.models import MenuItem
from app.services import conversation_service as service
from tests.test_opening_hours_flow import MARGHERITA, NOTHING

CLOSED = datetime.datetime(2026, 11, 4, 21, 46, tzinfo=hours.ROME)
OPEN = datetime.datetime(2026, 11, 4, 16, 0, tzinfo=hours.ROME)
CLOSED_QUESTION = "La cucina stasera è chiusa. Vuole ordinare per domani o prenotare un tavolo?"
ADD_MARGHERITA = {**NOTHING, "intent": "add_items", "items": [dict(MARGHERITA)]}
ADD_CAPRICCIOSA = {**NOTHING, "intent": "add_items", "items": [{**MARGHERITA, "pizza_name": "Capricciosa"}]}
NOV_5_OPEN = datetime.datetime(2026, 11, 5, 11, 0, tzinfo=hours.ROME)
NOV_5_CLOSED = datetime.datetime(2026, 11, 5, 22, 30, tzinfo=hours.ROME)
MENU = [
    {"name": "Margherita", "ingredients": ["pomodoro", "mozzarella"]},
    {"name": "Capricciosa", "ingredients": ["pomodoro", "funghi trifolati", "olive nere greche"]},
    {"name": "Corte", "ingredients": ["Asiago", "carciofi", "salamino piccante"]},
]


class _Call(covers._ReservationHarness):
    restaurant = {**covers.CDS_RESERVATIONS, "name": "Corte Del Sole"}

    def setUp(self):
        super().setUp()
        p = unittest.mock.patch.object(chat_module, "get_proposable_menu", return_value=MENU)
        p.start()
        self.addCleanup(p.stop)
        self.session.add(MenuItem(
            name="Capricciosa", category="rosse", pizza_type="Normale", price=9.0, restaurant_id=hours.CDS_ID,
        ))
        self.session.commit()

    def reservation(self):
        return json.loads(self.conversation().reservation_json)


class RealCallsClosedKitchenTests(_Call):
    now = CLOSED

    def test_fb627c3b_corte_del_sole_then_a_table(self):
        r = self.say("Corte del sole.")
        self.assertEqual((r.state, r.response_message), ("confirming_next_day", CLOSED_QUESTION))
        self.assertEqual(json.loads(self.conversation().items_json), [])
        r = self.say("Posso prenotare un tavolo per 2 sabato 20 novembre.")
        self.assertEqual(r.state, "collecting_reservation_date")
        self.assertEqual(r.response_message, "Venerdì è il 20, sabato è il 21: quale dei due?")
        self.assertEqual(self.reservation()["party_size"], 2)
        r = self.say("Venerdì.")
        self.assertEqual((r.state, r.response_message), ("collecting_reservation_time", "A che ora?"))
        r = self.book("20.", "Giacomo")
        self.assertEqual(r.state, "awaiting_reservation_confirmation")
        self.assertIn("venerdì 20 novembre alle 20:00, 2 persone", r.response_message)
        r = self.say("Sì sì. Sì, Sì,")
        self.assertEqual(r.state, "reservation_completed")
        saved = self.reservations[0]
        self.assertEqual((saved["date"], saved["time"], saved["party_size"]), ("2026-11-20", "20:00", 2))
        self.assertEqual(self.saved, [])  # nessun ordine

    def test_c2dbefef_a_margherita_is_an_order_not_a_table(self):
        r = self.say("Vorrei prenotare una margherita per domani.", dict(ADD_MARGHERITA))
        self.assertNotIn("persone", r.response_message)
        self.assertFalse(r.state.startswith("collecting_reservation"))
        conversation = self.conversation()
        self.assertEqual(conversation.pickup_date, "2026-11-05")
        self.assertEqual([i["pizza_name"] for i in json.loads(conversation.items_json)], ["Margherita"])
        self.assertEqual(self.reservation(), {})

    def test_377c8aa0_a_real_table_is_still_a_table(self):
        r = self.say("Vorrei prenotare un tavolo per 3 persone domani alle 8.")
        self.assertEqual(r.state, "collecting_reservation_name")
        r = self.book("Giacomo", "Sì grazie.")
        self.assertEqual(r.state, "reservation_completed")
        saved = self.reservations[0]
        self.assertEqual((saved["date"], saved["time"], saved["party_size"]), ("2026-11-05", "20:00", 3))

    def test_answers_to_the_closed_kitchen_question(self):
        self.say("Corte del sole.")
        r = self.say("sì")
        self.assertEqual(r.state, "confirming_next_day")  # il sì da solo non sceglie
        r = self.say("voglio ordinare")
        self.assertEqual(r.response_message, "Perfetto, il ritiro è per domani, giovedì. Cosa desidera ordinare?")
        self.assertEqual(self.conversation().pickup_date, "2026-11-05")

    def test_prenotare_answers_the_closed_kitchen_question(self):
        self.say("Buonasera")
        r = self.say("prenotare")
        self.assertEqual(r.state, "collecting_reservation_date")

    def test_a_product_at_closed_kitchen_keeps_the_usual_question(self):
        r = self.say("una margherita", dict(ADD_MARGHERITA))
        self.assertEqual(r.response_message, "Per stasera la cucina è chiusa, vuole ordinare per domani?")


class IntentTests(_Call):
    now = OPEN

    def test_no_product_and_no_table_asks(self):
        r = self.say("vorrei prenotare per domani alle 8")
        self.assertEqual((r.state, r.response_message), ("collecting_items", chat_module.INTENT_QUESTION))

    def test_the_answer_keeps_what_was_said_for_a_table(self):
        self.say("vorrei prenotare per domani alle 8")
        r = self.say("un tavolo")
        self.assertEqual(r.state, "collecting_reservation_party")
        self.assertEqual((self.reservation()["date"], self.reservation()["time"]), ("2026-11-05", "20:00"))

    def test_the_answer_keeps_what_was_said_for_an_order(self):
        self.say("vorrei prenotare per domani alle 8")
        r = self.say("da asporto")
        self.assertEqual((r.state, r.response_message), ("collecting_items", "Cosa desidera ordinare?"))
        conversation = self.conversation()
        self.assertEqual((conversation.pickup_date, conversation.pickup_time), ("2026-11-05", "20:00"))

    def test_unknown_first_sentence_asks_once(self):
        self.next_extraction = dict(NOTHING)
        r = self.say("mah non so")
        self.assertEqual(r.response_message, chat_module.INTENT_QUESTION)
        r = self.say("boh vediamo")
        self.assertNotEqual(r.response_message, chat_module.INTENT_QUESTION)

    def test_reservations_off_turns_prenotare_away_but_not_a_product(self):
        self.restaurant = {**self.restaurant, "reservations_enabled": False}
        r = self.say("vorrei prenotare")
        self.assertIn("solo ordini da asporto", r.response_message)
        r = self.say("vorrei prenotare una margherita", dict(ADD_MARGHERITA))
        self.assertEqual([i["pizza_name"] for i in json.loads(self.conversation().items_json)], ["Margherita"])

    def test_classifier(self):
        menu = lambda: MENU  # noqa: E731
        classify = lambda text, **kw: service.classify_call_intent(text, menu, "Corte Del Sole", **kw)  # noqa: E731
        for text, expected in (
            ("vorrei prenotare una margherita", "order"),
            ("prenotarmi due pizze", "order"),
            ("mi tiene da parte qualcosa", "order"),
            ("una pizza per 2 persone", "order"),
            ("vorrei un tavolo", "reservation"),
            ("un tavolo e una margherita", "reservation"),
            ("due posti per stasera", "reservation"),
            ("siamo in 4", "reservation"),
            ("vorrei prenotare", "unknown"),
            ("vorrei prenotare alla Corte del Sole", "unknown"),  # non è la pizza Corte
            ("una margherita", None),  # niente da decidere: ordine normale
            ("buonasera", None),
        ):
            with self.subTest(text=text):
                self.assertEqual(classify(text), expected)
        self.assertEqual(classify("Monti", strict=True), None)  # un cognome al nome della prenotazione
        self.assertEqual(classify("no, una margherita", strict=True), "order")


class RealCallsNovember5Tests(_Call):
    now = NOV_5_OPEN

    def _known_as(self, name):
        conversation = self.conversation()
        conversation.customer_name = name
        self.session.add(conversation)
        self.session.commit()

    def test_32da8401_tomorrow_survives_an_order_and_the_new_time_wins(self):
        self._known_as("Giacomo")
        r = self.say("Ciao volevo prenotare per domani alle 8.")
        self.assertEqual((r.state, r.response_message), ("collecting_items", chat_module.INTENT_QUESTION))
        r = self.say("Una capricciosa lo sport.", dict(ADD_CAPRICCIOSA))
        self.assertEqual(r.state, "awaiting_confirmation")
        self.assertEqual(r.response_message, "Allora: una capricciosa, per domani venerdì alle 20, a nome Giacomo. Confermo?")
        r = self.say("Per 8.45.")
        self.assertEqual(r.state, "awaiting_confirmation")
        self.assertEqual(r.response_message, "Allora: una capricciosa, per domani venerdì alle 20:45, a nome Giacomo. Confermo?")
        self.say("sì")
        saved = self.saved[0]
        self.assertEqual((saved["pickup_date"], saved["pickup_time"]), ("2026-11-06", "20:45"))

    def test_regression_prenotare_for_tomorrow_then_an_order_saves_tomorrow(self):
        self.say("volevo prenotare per domani alle 8")
        r = self.say("una margherita", dict(ADD_MARGHERITA))
        self.assertEqual((r.state, r.response_message), ("collecting_name", "A che nome?"))
        self.assertNotIn("A che ora", r.response_message)
        r = self.say("Giacomo")
        self.assertIn("per domani venerdì alle 20", r.response_message)
        self.say("sì")
        saved = self.saved[0]
        self.assertEqual((saved["pickup_date"], saved["pickup_time"]), ("2026-11-06", "20:00"))

    def test_regression_new_time_with_the_product_keeps_the_day(self):
        self.say("volevo prenotare per domani alle 8")
        self.say("una margherita per le 9", {**ADD_MARGHERITA, "pickup_time": "21:00"})
        conversation = self.conversation()
        self.assertEqual((conversation.pickup_date, conversation.pickup_time), ("2026-11-06", "21:00"))

    def test_regression_a_new_day_in_the_answer_wins(self):
        self.say("volevo prenotare per domani alle 8")
        self.say("una margherita per sabato", dict(ADD_MARGHERITA))
        self.assertEqual(self.conversation().pickup_date, "2026-11-07")

    def test_regression_tonight_in_the_answer_wins_and_is_saved_with_the_date(self):
        self._known_as("Giacomo")
        self.say("volevo prenotare per domani alle 8")
        r = self.say("una margherita per stasera", dict(ADD_MARGHERITA))
        self.assertIn("per stasera alle 20", r.response_message)
        self.say("sì")
        self.assertEqual(self.saved[0]["pickup_date"], "2026-11-05")

    def test_regression_product_with_asporto_is_not_lost(self):
        self.say("volevo prenotare per domani alle 8")
        self.say("Una capricciosa da asporto.", dict(ADD_CAPRICCIOSA))
        conversation = self.conversation()
        self.assertEqual([i["pizza_name"] for i in json.loads(conversation.items_json)], ["Capricciosa"])
        self.assertEqual((conversation.pickup_date, conversation.pickup_time), ("2026-11-06", "20:00"))

    def test_regression_lo_sport_alone_is_an_order(self):
        self.say("volevo prenotare per domani alle 8")
        r = self.say("Lo sport.")
        self.assertEqual((r.state, r.response_message), ("collecting_items", "Cosa desidera ordinare?"))
        self.assertEqual(self.conversation().pickup_date, "2026-11-06")

    def test_regression_product_recognised_only_by_the_llm(self):
        self.say("volevo prenotare per domani alle 8")
        self.say("una capriciosa", dict(ADD_CAPRICCIOSA))
        self.assertEqual(self.conversation().pickup_date, "2026-11-06")

    def test_regression_earlier_closed_day_is_proposed_not_taken(self):
        self.restaurant = {**self.restaurant, "opening_hours": {**self.restaurant["opening_hours"], "friday": ""}}
        self.say("volevo prenotare per domani alle 8")
        r = self.say("una margherita", dict(ADD_MARGHERITA))
        self.assertEqual(r.state, "confirming_next_day")
        self.assertIn("siamo chiusi", r.response_message)
        self.assertIsNone(self.conversation().pickup_date)

    def test_regression_earlier_weekday_and_date_that_disagree_are_asked(self):
        self.say("volevo prenotare per sabato 20 novembre alle 8")
        r = self.say("una margherita", dict(ADD_MARGHERITA))
        self.assertEqual(r.state, "confirming_next_day")
        self.assertEqual(r.response_message, "Venerdì è il 20, sabato è il 21: quale dei due?")

    def test_regression_a_table_still_keeps_the_day(self):
        self.say("Ciao volevo prenotare per domani alle 8.")
        r = self.say("un tavolo")
        self.assertEqual(r.state, "collecting_reservation_party")
        self.assertEqual(self.reservation()["date"], "2026-11-06")
        self.assertEqual(self.saved, [])

    def test_time_at_the_recap_outside_the_hours_is_refused_and_the_old_one_stays(self):
        self._known_as("Giacomo")
        self.say("Ciao volevo prenotare per domani alle 8.")
        self.say("Una capricciosa lo sport.", dict(ADD_CAPRICCIOSA))
        r = self.say("Per le 3.")
        self.assertEqual(r.state, "collecting_pickup_time")
        self.assertIn("siamo aperti", r.response_message)
        self.assertEqual(self.conversation().pickup_time, "20:00")
        self.assertEqual(self.saved, [])

    def test_same_time_at_the_recap_confirms(self):
        self._known_as("Giacomo")
        self.say("Ciao volevo prenotare per domani alle 8.")
        self.say("Una capricciosa lo sport.", dict(ADD_CAPRICCIOSA))
        r = self.say("sì, alle 8")
        self.assertEqual(r.state, "completed")
        self.assertEqual(self.saved[0]["pickup_time"], "20:00")

    def test_a_confirmation_with_no_time_is_not_a_time(self):
        self.assertFalse(chat_module._is_new_time_at_recap("sì grazie", "20:00"))
        self.assertFalse(chat_module._is_new_time_at_recap("per favore sì", "20:00"))
        self.assertFalse(chat_module._is_new_time_at_recap("alle 8", "20:00"))
        self.assertFalse(chat_module._is_new_time_at_recap("3 pizze", "20:00"))
        self.assertTrue(chat_module._is_new_time_at_recap("Per 8.45.", "20:00"))

    def test_5770365c_a_greeting_asks_order_or_table(self):
        r = self.say("Salve.")
        self.assertEqual((r.state, r.response_message), ("collecting_items", chat_module.INTENT_QUESTION))

    def test_8d7db458_a_generic_sentence_asks_order_or_table(self):
        r = self.say("Forte del sole", dict(NOTHING))
        self.assertEqual((r.state, r.response_message), ("collecting_items", chat_module.INTENT_QUESTION))

    def test_greeting_then_an_order_or_a_table(self):
        self.say("Salve.")
        r = self.say("una margherita", dict(ADD_MARGHERITA))
        self.assertEqual(r.state, "collecting_name")
        self.assertIsNone(self.conversation().pickup_date)

    def test_greeting_then_a_table(self):
        self.say("Salve.")
        self.assertEqual(self.say("un tavolo").state, "collecting_reservation_date")

    def test_greeting_after_the_question_keeps_what_was_said_before(self):
        self.say("volevo prenotare per domani alle 8")
        self.say("Salve.")
        self.say("una margherita", dict(ADD_MARGHERITA))
        self.assertEqual(self.conversation().pickup_date, "2026-11-06")

    def test_greeting_after_choosing_an_order_asks_what_to_order(self):
        self.say("Salve.")
        self.say("da asporto")
        r = self.say("Pronto?")
        self.assertEqual(r.response_message, "Cosa desidera ordinare?")

    def test_greeting_with_reservations_off_asks_what_to_order(self):
        self.restaurant = {**self.restaurant, "reservations_enabled": False}
        r = self.say("Salve.")
        self.assertEqual(r.response_message, "Cosa desidera ordinare?")


class RealCallsNovember5EveningTests(_Call):
    """Chiamate del 5 novembre dopo le 22, cucina chiusa."""

    now = NOV_5_CLOSED
    TONIGHT_CLOSED = "Stasera purtroppo la cucina è chiusa. Va bene per domani, venerdì?"

    def setUp(self):
        super().setUp()
        conversation = self.conversation()
        conversation.customer_name = "Giacomo"
        self.session.add(conversation)
        self.session.commit()

    def test_00a50cc0_tonight_is_refused_confirmed_and_the_time_is_kept(self):
        r = self.say("Salve Giacomo.", dict(NOTHING))
        self.assertEqual((r.state, r.response_message), ("confirming_next_day", CLOSED_QUESTION))
        # L'LLM di quel turno non ha estratto l'orario: deve bastare la frase
        r = self.say("Vorrei prenotare una pizza per stasera alle 8.", dict(NOTHING))
        self.assertEqual((r.state, r.response_message), ("confirming_next_day", self.TONIGHT_CLOSED))
        self.assertNotIn("Perfetto", r.response_message)
        self.assertIsNone(self.conversation().pickup_date)  # nessuno spostamento prima del sì
        r = self.say("Sì.")
        self.assertEqual(r.response_message, "Perfetto, il ritiro è per domani, venerdì. Cosa desidera ordinare?")
        self.assertEqual((self.conversation().pickup_date, self.conversation().pickup_time), ("2026-11-06", "20:00"))
        r = self.say("Una margherita.", dict(ADD_MARGHERITA))
        self.assertEqual(r.response_message, "Allora: una margherita, per domani venerdì alle 20, a nome Giacomo. Confermo?")
        self.say("Sì.")
        self.assertEqual((self.saved[0]["pickup_date"], self.saved[0]["pickup_time"]), ("2026-11-06", "20:00"))

    def test_tonight_with_a_product_keeps_the_product_and_waits_for_yes(self):
        self.say("Salve Giacomo.", dict(NOTHING))
        r = self.say("una margherita per stasera alle 8", dict(ADD_MARGHERITA))
        self.assertEqual(r.response_message, self.TONIGHT_CLOSED)
        self.assertEqual([i["pizza_name"] for i in json.loads(self.conversation().items_json)], ["Margherita"])
        r = self.say("sì")
        self.assertIn("per domani venerdì alle 20, a nome Giacomo. Confermo?", r.response_message)

    def test_ordinare_per_stasera_is_refused_too(self):
        self.say("Salve Giacomo.", dict(NOTHING))
        r = self.say("vorrei ordinare per stasera", dict(NOTHING))
        self.assertEqual(r.response_message, self.TONIGHT_CLOSED)
        self.assertIsNone(self.conversation().pickup_date)

    def test_no_to_the_proposal_asks_the_day_and_moves_nothing(self):
        self.say("Salve Giacomo.", dict(NOTHING))
        self.say("Vorrei prenotare una pizza per stasera alle 8.", dict(NOTHING))
        r = self.say("no")
        self.assertEqual(r.response_message, chat_module.PICKUP_DAY_QUESTION)
        self.assertIsNone(self.conversation().pickup_date)

    def test_a_time_not_valid_on_the_new_day_is_said_and_a_valid_one_proposed(self):
        self.restaurant = {**self.restaurant, "opening_hours": {**self.restaurant["opening_hours"], "friday": "19:00-21:00"}}
        self.say("Salve Giacomo.", dict(NOTHING))
        self.say("vorrei ordinare per stasera alle 9 e mezza", dict(NOTHING))
        r = self.say("sì")
        self.assertEqual(r.state, "collecting_pickup_time")
        self.assertEqual(
            r.response_message,
            "Perfetto, il ritiro è per domani, venerdì. Alle 21:30 domani venerdì non è possibile, "
            "siamo aperti dalle 19:00 alle 21:00. Va bene alle 19? Mi dica sì o no.",
        )
        self.say("sì")
        self.assertEqual(self.conversation().pickup_time, "19:00")

    def test_known_defect_a_product_instead_of_yes_is_yes_and_the_product(self):
        self.say("Salve Giacomo.", dict(NOTHING))
        self.say("Vorrei prenotare una pizza per stasera alle 8.", dict(NOTHING))
        r = self.say("Una margherita.", dict(ADD_MARGHERITA))
        self.assertNotIn("non ho capito", r.response_message)
        self.assertEqual(
            r.response_message,
            "Perfetto, il ritiro è per domani, venerdì. Allora: una margherita, per domani venerdì alle 20, a nome Giacomo. Confermo?",
        )

    def test_known_defect_b_tonight_at_8_without_a_product_is_refused_and_the_choice_asked(self):
        self.say("Salve Giacomo.", dict(NOTHING))
        r = self.say("Per stasera alle 8", dict(NOTHING))
        self.assertEqual(
            (r.state, r.response_message),
            ("confirming_next_day", "Stasera purtroppo la cucina è chiusa. Vuole ordinare per domani o prenotare un tavolo?"),
        )
        self.assertIsNone(self.conversation().pickup_date)
        r = self.say("da asporto")
        self.assertEqual(r.response_message, "Perfetto, il ritiro è per domani, venerdì. Cosa desidera ordinare?")
        self.assertEqual((self.conversation().pickup_date, self.conversation().pickup_time), ("2026-11-06", "20:00"))

    def test_2736d6cb_table_then_order_is_preserved(self):
        r = self.say("Ciao, vorrei prenotare per domani alle 8.")
        self.assertEqual(r.response_message, CLOSED_QUESTION)
        r = self.say("Vorrei prenotare per domani alle 8.")
        self.assertEqual(r.response_message, "Certo, domani, venerdì 6 novembre alle 20:00. Per quante persone?")
        r = self.say("Vorrei una capricciosa da sport.", dict(ADD_CAPRICCIOSA))
        self.assertEqual(r.response_message, "Allora: una capricciosa, per domani venerdì alle 20, a nome Giacomo. Confermo?")
        r = self.say("Sono per le 8.45.", {**NOTHING, "intent": "set_pickup_time", "pickup_time": "20:45"})
        self.assertEqual(r.response_message, "Allora: una capricciosa, per domani venerdì alle 20:45, a nome Giacomo. Confermo?")
        r = self.say("Sì grazie.")
        self.assertEqual(r.state, "completed")
        saved = self.saved[0]
        self.assertEqual((saved["pickup_date"], saved["pickup_time"]), ("2026-11-06", "20:45"))
        self.assertEqual([i["pizza_name"] for i in saved["items"]], ["Capricciosa"])
        self.assertEqual(self.reservations, [])


class DayChangeAtTheRecapTests(_Call):
    now = NOV_5_OPEN

    def test_new_day_where_the_time_is_not_valid_says_it_and_proposes_one(self):
        self.restaurant = {**self.restaurant, "opening_hours": {**self.restaurant["opening_hours"], "saturday": "19:00-21:00"}}
        self.say("una margherita per le 9 e mezza, sono Giacomo",
                 {**ADD_MARGHERITA, "customer_name": "Giacomo", "pickup_time": "21:30"})
        r = self.say("no, per sabato")
        self.assertEqual(r.state, "collecting_pickup_time")
        self.assertIn("Alle 21:30 sabato non è possibile, siamo aperti dalle 19:00 alle 21:00. Va bene alle 19?", r.response_message)
        self.assertEqual(self.conversation().pickup_date, "2026-11-07")


class NextDayOnlyUnchangedTests(_Call):
    """Preordine fisso (Pizza a Pezzi), anche con le prenotazioni accese: nulla cambia."""

    now = NOV_5_OPEN
    restaurant = {**covers.CDS_RESERVATIONS, "name": "Pizza a Pezzi", "phone_orders_next_day_only": True}

    def test_greeting_still_asks_what_to_order(self):
        self.assertEqual(self.say("Salve.").response_message, "Cosa desidera ordinare?")

    def test_time_at_the_recap_still_repeats_the_recap(self):
        self.say("una margherita per le 8, sono Giacomo", {**ADD_MARGHERITA, "customer_name": "Giacomo", "pickup_time": "20:00"})
        r = self.say("Per 8.45.")
        self.assertEqual(r.state, "awaiting_confirmation")
        self.assertIn("alle 20,", r.response_message)
        self.assertEqual(self.conversation().pickup_time, "20:00")

    def test_tonight_is_still_redirected_with_the_fixed_day(self):
        r = self.say("Due tranci per stasera")
        self.assertIn("prendiamo solo ordini per il giorno dopo", r.response_message)
        self.assertTrue(r.response_message.endswith("Va bene per domani, venerdì?"))

    def test_order_is_saved_for_the_fixed_day(self):
        self.say("una margherita per le 8, sono Giacomo", {**ADD_MARGHERITA, "customer_name": "Giacomo", "pickup_time": "20:00"})
        self.say("sì")
        self.assertEqual(self.saved[0]["pickup_date"], "2026-11-06")


class ClosedKitchenAnswersTests(_Call):
    now = NOV_5_CLOSED

    def test_a_product_answers_the_closed_kitchen_question(self):
        self.say("Salve.")
        r = self.say("una margherita", dict(ADD_MARGHERITA))
        self.assertEqual(self.conversation().pickup_date, "2026-11-06")
        self.assertEqual(r.state, "collecting_name")
        self.assertNotIn("non ho capito", r.response_message)

    def test_earlier_day_and_time_survive_the_closed_kitchen_question(self):
        self.say("volevo prenotare per sabato alle 8")
        self.say("Una capricciosa lo sport.", dict(ADD_CAPRICCIOSA))
        conversation = self.conversation()
        self.assertEqual((conversation.pickup_date, conversation.pickup_time), ("2026-11-07", "20:00"))

    def test_yes_alone_still_does_not_choose(self):
        self.say("Salve.")
        self.assertEqual(self.say("sì").state, "confirming_next_day")


class IntentChangeTests(_Call):
    now = OPEN

    def test_order_to_table_keeps_day_time_name_and_drops_the_items(self):
        r = self.say("una margherita per domani alle 8", {**ADD_MARGHERITA, "pickup_time": "20:00"})
        self.say("Giacomo")
        r = self.say("anzi, vorrei un tavolo per 4")
        self.assertEqual(r.state, "awaiting_reservation_confirmation")
        self.assertEqual(r.response_message, "Allora: Giacomo, domani, giovedì 5 novembre alle 20:00, 4 persone. Confermo?")
        conversation = self.conversation()
        self.assertEqual(json.loads(conversation.items_json), [])
        self.assertIsNone(conversation.pickup_date)
        self.assertIsNone(conversation.pickup_time)

    def test_a_cart_is_not_dropped_for_persone_alone(self):
        self.say("una margherita", dict(ADD_MARGHERITA))
        self.say("siamo in 4", dict(NOTHING))
        self.assertEqual(len(json.loads(self.conversation().items_json)), 1)

    def test_table_to_order_keeps_day_time_and_name(self):
        self.book("vorrei un tavolo", "domani", "alle 20", "4 persone", "Giacomo")
        r = self.say("no, volevo una margherita", dict(ADD_MARGHERITA))
        conversation = self.conversation()
        self.assertEqual([i["pizza_name"] for i in json.loads(conversation.items_json)], ["Margherita"])
        self.assertEqual(
            (conversation.pickup_date, conversation.pickup_time, conversation.customer_name),
            ("2026-11-05", "20:00", "Giacomo"),
        )
        self.assertEqual(self.reservation(), {})
        self.assertEqual(r.state, "awaiting_confirmation")
        self.assertEqual(self.reservations, [])


class NoAnswerTests(_Call):
    now = OPEN

    def _at_party(self):
        return self.book("vorrei un tavolo", "domani", "alle 20")

    def test_no_at_the_party_question_is_not_a_failure(self):
        self.assertEqual(self._at_party().response_message, "Per quante persone?")
        r = self.say("No no no no.")
        self.assertEqual((r.state, r.response_message), ("collecting_reservation_party", chat_module.CORRECTION_QUESTION))

    def test_repeated_no_never_repeats_and_goes_to_the_restaurant(self):
        self._at_party()
        replies = [self.say("no").response_message for _ in range(3)]
        self.assertEqual(replies, [
            chat_module.CORRECTION_QUESTION, chat_module.CORRECTION_FIELDS_QUESTION, chat_module.CALLBACK_HANDOFF_MESSAGE,
        ])
        self.assertEqual(self.conversation().state, "reservation_handoff")

    def test_correction_of_each_field_in_a_reservation(self):
        for answer, state, question, key in (
            ("il giorno", "collecting_reservation_date", "Per quale giorno?", "date"),
            ("l'ora", "collecting_reservation_time", "A che ora?", "time"),
        ):
            with self.subTest(answer=answer):
                self.session.rollback()
                conv = self.conversation()
                conv.state, conv.reservation_json, conv.slot_attempts_json = (
                    "collecting_reservation_party", json.dumps({"date": "2026-11-05", "time": "20:00"}), "{}",
                )
                self.session.add(conv)
                self.session.commit()
                self.say("no")
                r = self.say(answer)
                self.assertEqual((r.state, r.response_message), (state, question))
                self.assertNotIn(key, self.reservation())

    def test_correction_with_the_value_goes_on(self):
        self._at_party()
        self.say("no")
        r = self.say("per sabato")
        self.assertEqual(r.state, "collecting_reservation_party")
        self.assertEqual(r.response_message, "Per quante persone?")
        self.assertEqual(self.reservation()["date"], "2026-11-07")

    def test_correction_at_the_recap_rechecks_and_recaps(self):
        self.book("vorrei un tavolo", "domani", "alle 20", "4 persone", "Giacomo")
        r = self.say("no")
        self.assertEqual(r.response_message, chat_module.CORRECTION_QUESTION)
        r = self.say("siamo in 6")
        self.assertEqual(r.state, "awaiting_reservation_confirmation")
        self.assertIn("6 persone", r.response_message)
        self.assertEqual(self.reservations, [])

    def test_no_with_a_value_in_a_reservation(self):
        self.book("vorrei un tavolo", "domani", "alle 20", "4 persone", "Giacomo")
        r = self.say("no, alle 21")
        self.assertEqual(r.state, "awaiting_reservation_confirmation")
        self.assertIn("alle 21:00", r.response_message)

    def test_explicit_cancel_at_the_recap_still_cancels(self):
        self.book("vorrei un tavolo", "domani", "alle 20", "4 persone", "Giacomo")
        r = self.say("annulla")
        self.assertEqual(r.response_message, "Prenotazione annullata. Per quale altro giorno vuole prenotare?")

    def test_correction_to_an_order_from_a_reservation(self):
        self._at_party()
        self.say("no")
        r = self.say("volevo ordinare da asporto")
        self.assertEqual((r.state, r.response_message), ("collecting_items", "Cosa desidera ordinare?"))
        self.assertEqual(self.conversation().pickup_date, "2026-11-05")

    def _recap(self):
        self.order("una margherita per domani alle 8", pickup_time="20:00")
        return self.say("Giacomo")

    def test_no_at_the_order_recap_does_not_cancel_the_order(self):
        self.assertEqual(self._recap().state, "awaiting_confirmation")
        r = self.say("no")
        self.assertEqual(r.response_message, chat_module.CORRECTION_QUESTION)
        self.assertEqual(len(json.loads(self.conversation().items_json)), 1)

    def test_order_correction_by_field(self):
        self._recap()
        self.say("no")
        r = self.say("il nome")
        self.assertEqual((r.state, r.response_message), ("collecting_name", "A che nome?"))
        r = self.say("Marco")
        self.assertEqual(r.state, "awaiting_confirmation")
        self.assertIn("a nome Marco", r.response_message)

    def test_order_correction_of_the_day_by_field(self):
        self._recap()
        self.say("no")
        r = self.say("il giorno")
        self.assertEqual((r.state, r.response_message), ("confirming_next_day", chat_module.PICKUP_DAY_QUESTION))
        r = self.say("sabato")
        self.assertEqual(self.conversation().pickup_date, "2026-11-07")

    def test_nome_at_the_recap_does_not_read_no_inside_the_word(self):
        self._recap()
        r = self.say("nome")
        self.assertEqual(len(json.loads(self.conversation().items_json)), 1)
        self.assertNotIn("annullato", r.response_message)

    def test_no_with_a_value_at_the_order_recap_is_a_correction(self):
        self._recap()
        r = self.say("no, per le 9", {**NOTHING, "intent": "set_pickup_time", "pickup_time": "21:00"})
        conversation = self.conversation()
        self.assertEqual(conversation.pickup_time, "21:00")
        self.assertEqual(len(json.loads(conversation.items_json)), 1)
        self.assertEqual(r.state, "awaiting_confirmation")

    def test_no_keeps_its_meaning_on_a_proposal(self):
        conv = self.conversation()
        conv.pending_customer_name = "Giacomo"
        self.session.add(conv)
        self.session.commit()
        r = self.say("no", dict(NOTHING))
        self.assertNotEqual(r.response_message, chat_module.CORRECTION_QUESTION)


if __name__ == "__main__":
    unittest.main()
