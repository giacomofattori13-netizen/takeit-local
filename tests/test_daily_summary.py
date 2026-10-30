"""Riepilogo giornaliero al titolare: contenuto, split in più SMS, scheduling
per giorno di apertura e idempotenza su Restaurant.last_daily_summary_date.
Base44 e Twilio sono simulati."""
import datetime
import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from app.services import daily_summary

PAP_ID = "rest-pap"
ROME = daily_summary.ROME
# Giovedì 29 ottobre 2026; lunedì 2 novembre PaP è chiuso.
THURSDAY = datetime.date(2026, 10, 29)
MONDAY = datetime.date(2026, 11, 2)

OPENING_HOURS = {
    "monday": "closed", "tuesday": "17:00-21:00", "wednesday": "17:00-21:00", "thursday": "17:00-21:00",
    "friday": "17:00-21:00", "saturday": "17:00-21:00", "sunday": "17:00-21:00",
}
TWILIO_ENV = {
    "TWILIO_ACCOUNT_SID": "AC-test",
    "TWILIO_AUTH_TOKEN": "tok",
    "TWILIO_NUMBER": "+15550001111",
    "OWNER_PHONE": "+393339990000",
}


def kg(name, quantity, temperature=None):
    item = {"pizza_name": name, "sale_unit": "kg", "order_unit": "kg", "quantity": quantity}
    if temperature:
        item["temperature"] = temperature
    return item


def slices(name, count, portion, temperature=None):
    item = {"pizza_name": name, "sale_unit": "kg", "order_unit": "tranci", "quantity": count, "slices": count,
            "size": portion, "portion": portion}
    if temperature:
        item["temperature"] = temperature
    return item


def order(oid, pickup_time, name, items, **extra):
    return {"id": oid, "restaurant_id": PAP_ID, "pickup_date": THURSDAY.isoformat(), "pickup_time": pickup_time,
            "customer_name": name, "customer_phone": f"+3933300000{oid[-2:]}", "status": "nuovo", "items": items,
            **extra}


def restaurant(**overrides):
    r = {"id": PAP_ID, "name": "Pizza a Pezzi", "owner_notify_phone": "+393330000001", "daily_summary_enabled": True,
         "daily_summary_time": "10:30", "last_daily_summary_date": None, "opening_hours": OPENING_HOURS}
    r.update(overrides)
    return r


class FakeBase44:
    def __init__(self, restaurants, orders):
        self.restaurants = {r["id"]: dict(r) for r in restaurants}
        self.orders = orders
        self.restaurant_writes: list[tuple[str, dict]] = []

    def get_all_restaurants_or_none(self, timeout=10.0):
        return [dict(r) for r in self.restaurants.values()]

    def query_entities(self, entity, query, timeout=10.0):
        assert entity == "Order"
        return [dict(o) for o in self.orders if all(o.get(k) == v for k, v in query.items())]

    def update_restaurant(self, patch_, restaurant_id=None, timeout=10.0):
        self.restaurant_writes.append((restaurant_id, patch_))
        self.restaurants[restaurant_id].update(patch_)
        return dict(self.restaurants[restaurant_id])


class _Harness(unittest.TestCase):
    def setUp(self):
        daily_summary._sent_in_process.clear()
        daily_summary._attempts.clear()
        self.sent: list[tuple[str, str]] = []
        self.base44 = FakeBase44([restaurant()], [])
        patches = [
            patch.dict(os.environ, TWILIO_ENV),
            patch.object(daily_summary, "_send_twilio_message", side_effect=self._fake_twilio),
        ] + [
            patch.object(daily_summary.base44_client, name, side_effect=getattr(self.base44, name))
            for name in ("get_all_restaurants_or_none", "query_entities", "update_restaurant")
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _fake_twilio(self, to, body, from_number, account_sid, auth_token):
        self.sent.append((to, body))
        return "inviato:201"

    def run_at(self, day, hour, minute=0):
        with redirect_stdout(io.StringIO()) as out:
            daily_summary.run_due_summaries(datetime.datetime(day.year, day.month, day.day, hour, minute, tzinfo=ROME))
        return out.getvalue()


class SummaryContentTests(unittest.TestCase):
    def text(self, orders, day=THURSDAY):
        return "\n".join(daily_summary.build_summary_messages(day, orders))

    def test_totals_per_flavour_keep_units_and_temperatures_apart(self):
        orders = [
            order("o01", "18:00", "Anna", [kg("Bufala", 2, "calda"), slices("Bufala", 4, "piena", "fredda")]),
            order("o02", "18:30", "Bruno", [kg("Bufala", 1.5, "fredda"), slices("Bufala", 2, "piena", "fredda"),
                                            slices("Bufala", 1, "mezza", "calda")]),
            order("o03", "19:00", "Carla", [kg("Margherita", 0.5, "calda"),
                                            {"pizza_name": "Diavola", "sale_unit": "piece", "quantity": 2}]),
        ]
        text = self.text(orders)
        self.assertIn(
            "Bufala: 3,5 kg (calda 2 kg, fredda 1,5 kg) · 6 tranci interi freddi · 1 trancio mezzo caldo", text,
        )
        self.assertIn("Margherita: 0,5 kg calda", text)
        self.assertIn("Diavola: 2 pezzi", text)

    def test_missing_temperature_and_portion_stay_visible(self):
        text = self.text([order("o01", "18:00", "Anna", [kg("Bufala", 1), slices("Patate", 2, None)])])
        self.assertIn("Bufala: 1 kg caldo/freddo da definire", text)
        self.assertIn("Patate: 2 tranci porzione da definire caldo/freddo da definire", text)

    def test_header_and_sections_in_order(self):
        orders = [
            order("o01", "19:30", "Anna", [kg("Bufala", 1, "calda")]),
            order("o02", "18:00", "Bruno", [kg("Bufala", 1, "fredda")], needs_review=True,
                  review_reason="Caldo/freddo non capito"),
        ]
        text = self.text(orders)
        self.assertTrue(text.startswith("Ordini di giovedì 29 ottobre — 2 ordini"))
        positions = [text.index(s) for s in ("DA VERIFICARE\n", "TOTALI PER GUSTO", "RITIRI")]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("DA VERIFICARE\n18:00 Bruno +393330000002 — Caldo/freddo non capito", text)

    def test_order_without_pickup_time_is_to_verify_with_time_to_define(self):
        orders = [order("o01", "", "Anna", [kg("Bufala", 1, "calda")], needs_review=True,
                        review_reason="Orario non capito, il cliente ha rifiutato 17:00")]
        text = self.text(orders)
        self.assertIn(
            "DA VERIFICARE\norario da definire Anna +393330000001 — Orario non capito, il cliente ha rifiutato 17:00",
            text,
        )

    def test_pickups_sorted_by_time_with_items(self):
        orders = [
            order("o01", "20:15", "Anna", [slices("Bufala", 2, "piena", "fredda")]),
            order("o02", "17:30", "Bruno", [kg("Porchetta", 0.5, "calda")]),
            order("o03", "", "Carla", [kg("Bufala", 1, "calda")]),
            order("o04", "18:45", "Dario", [kg("Bufala", 1, "calda")]),
        ]
        ritiri = self.text(orders).split("RITIRI\n", 1)[1]
        names = [n for n in ("Bruno", "Dario", "Anna", "Carla") if n in ritiri]
        self.assertEqual(sorted(names, key=ritiri.index), ["Bruno", "Dario", "Anna", "Carla"])
        self.assertIn("17:30 Bruno +393330000002\n- 500g Porchetta (calda)", ritiri)
        self.assertIn("20:15 Anna +393330000001\n- 2 tranci interi Bufala (freddi)", ritiri)

    def test_no_section_to_verify_when_all_orders_are_fine(self):
        self.assertNotIn("DA VERIFICARE", self.text([order("o01", "18:00", "Anna", [kg("Bufala", 1, "calda")])]))

    def test_zero_orders(self):
        self.assertEqual(self.text([]), "Ordini di giovedì 29 ottobre — 0 ordini\nNessun ordine per oggi")

    def test_cancelled_and_picked_up_orders_are_excluded(self):
        base44 = FakeBase44([], [
            order("o01", "18:00", "Anna", [], status="annullato"),
            order("o02", "18:00", "Bruno", [], status="completato"),
            order("o03", "18:00", "Carla", [], status="nuovo"),
            order("o04", "18:00", "Dario", [], pickup_date=None, order_date=THURSDAY.isoformat()),
            order("o05", "18:00", "Elisa", [], pickup_date="2026-10-30"),
        ])
        with patch.object(daily_summary.base44_client, "query_entities", side_effect=base44.query_entities):
            names = sorted(o["customer_name"] for o in daily_summary.fetch_orders_for_pickup(PAP_ID, "2026-10-29"))
        self.assertEqual(names, ["Carla", "Dario"])


class SplitTests(unittest.TestCase):
    def test_long_summary_is_split_without_breaking_an_order(self):
        orders = [
            order(f"o{i:02d}", f"{17 + i // 6}:{(i % 6) * 10:02d}", f"Cliente {i}",
                  [kg(f"Gusto {j}", 1, "calda") for j in range(6)])
            for i in range(30)
        ]
        messages = daily_summary.build_summary_messages(THURSDAY, orders)
        self.assertGreater(len(messages), 1)
        for i, message in enumerate(messages, start=1):
            self.assertLessEqual(len(message), 1500)
            self.assertTrue(message.startswith(f"({i}/{len(messages)})\n"))
        for i in range(30):
            # Ogni ordine (intestazione + 6 righe) sta per intero in un solo SMS
            head = f"Cliente {i} +3933300000{i:02d}"
            holders = [m for m in messages if head in m]
            self.assertEqual(len(holders), 1)
            block = holders[0].split(head, 1)[1].split("\n\n", 1)[0]
            self.assertEqual(block.count("\n- "), 6, head)

    def test_short_summary_is_a_single_unnumbered_sms(self):
        messages = daily_summary.build_summary_messages(THURSDAY, [])
        self.assertEqual(len(messages), 1)
        self.assertFalse(messages[0].startswith("(1/"))


class SummaryTimeTests(unittest.TestCase):
    def test_valid_time(self):
        self.assertEqual(daily_summary.summary_time({"daily_summary_time": "09:45"}), (9, 45))

    def test_empty_or_invalid_time_falls_back_to_11_with_warning(self):
        for raw in (None, "", "25:00", "dieci", "10.30"):
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(daily_summary.summary_time({"id": PAP_ID, "daily_summary_time": raw}), (11, 0), raw)
            self.assertIn("WARNING daily_summary_time", out.getvalue())


class SchedulingTests(_Harness):
    def test_sends_after_time_and_saves_date(self):
        self.base44.orders = [order("o01", "18:00", "Anna", [kg("Bufala", 1, "calda")])]
        self.run_at(THURSDAY, 10, 0)
        self.assertEqual(self.sent, [])

        self.run_at(THURSDAY, 10, 30)
        self.assertEqual(len(self.sent), 1)
        to, body = self.sent[0]
        self.assertEqual(to, "+393330000001")
        self.assertTrue(body.startswith("Ordini di giovedì 29 ottobre — 1 ordine"))
        self.assertEqual(self.base44.restaurant_writes, [(PAP_ID, {"last_daily_summary_date": "2026-10-29"})])

    def test_closed_day_sends_nothing(self):
        self.run_at(MONDAY, 12, 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.base44.restaurant_writes, [])

    def test_disabled_or_null_flag_sends_nothing(self):
        for value in (False, None):
            self.base44.restaurants[PAP_ID]["daily_summary_enabled"] = value
            self.run_at(THURSDAY, 12, 0)
        self.assertEqual(self.sent, [])

    def test_restart_after_sending_does_not_send_again(self):
        self.run_at(THURSDAY, 10, 30)
        self.assertEqual(len(self.sent), 1)
        # Riavvio: la memoria del processo si perde, resta la data su Base44
        daily_summary._sent_in_process.clear()
        daily_summary._attempts.clear()
        self.run_at(THURSDAY, 10, 45)
        self.run_at(THURSDAY, 15, 0)
        self.assertEqual(len(self.sent), 1)

    def test_restart_after_time_without_summary_sends_immediately(self):
        self.base44.restaurants[PAP_ID]["last_daily_summary_date"] = "2026-10-28"
        self.run_at(THURSDAY, 16, 5)
        self.assertEqual(len(self.sent), 1)

    def test_empty_time_uses_11_with_warning(self):
        self.base44.restaurants[PAP_ID]["daily_summary_time"] = ""
        out = self.run_at(THURSDAY, 10, 59)
        self.assertEqual(self.sent, [])
        self.assertIn("WARNING daily_summary_time vuoto", out)
        self.run_at(THURSDAY, 11, 0)
        self.assertEqual(len(self.sent), 1)

    def test_owner_phone_fallback(self):
        self.base44.restaurants[PAP_ID]["owner_notify_phone"] = None
        out = self.run_at(THURSDAY, 11, 0)
        self.assertEqual([to for to, _ in self.sent], ["+393339990000"])
        self.assertIn("WARNING owner_notify_phone vuoto", out)

    def test_failed_base44_date_write_does_not_resend_in_same_process(self):
        with patch.object(daily_summary.base44_client, "update_restaurant", side_effect=lambda *a, **k: None):
            self.run_at(THURSDAY, 10, 30)
            self.run_at(THURSDAY, 10, 45)
        self.assertEqual(len(self.sent), 1)

    def test_failed_sms_is_retried_up_to_three_times(self):
        with patch.object(daily_summary, "_send_twilio_message",
                          side_effect=lambda to, body, *a: self.sent.append((to, body)) or "errore:HTTP_500"):
            for minute in (30, 45, 50, 55):
                self.run_at(THURSDAY, 10, minute)
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.base44.restaurant_writes, [])

    def test_next_check_waits_for_the_earliest_summary_time_at_most_15_minutes(self):
        now = datetime.datetime(2026, 10, 29, 10, 25, tzinfo=ROME)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(daily_summary._seconds_until_next_check(now, [restaurant()]), 5 * 60)
            self.assertEqual(daily_summary._seconds_until_next_check(now, [restaurant(daily_summary_enabled=False)]), 15 * 60)


if __name__ == "__main__":
    unittest.main()
