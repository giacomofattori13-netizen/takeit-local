"""Numero d'ordine per ristorante e per giorno, definitivo prima della conferma."""
import datetime
import os
import threading
import time
import unittest
from unittest.mock import patch

from app.services import order_sync

TODAY = "2026-09-29"
ITEM = {
    "pizza_name": "Bufala",
    "pizza_type": "Normale",
    "quantity": 0.2,
    "sale_unit": "kg",
    "size": "mezza",
    "temperature": "calda",
    "base_price": 18.5,
    "extras_price": 0.0,
    "total_price": 3.7,
}


class FakeBase44:
    """Store in memoria con la semantica usata da order_sync (filtri esatti, created_date)."""

    def __init__(self, records=None, create_delay=0.0):
        self.records = list(records or [])
        self.create_delay = create_delay
        self.updates = []
        self._lock = threading.Lock()
        self._seq = 0

    def query_entities(self, entity, query, timeout=10.0):
        assert entity == "Order"
        with self._lock:
            return [dict(r) for r in self.records if all(r.get(k) == v for k, v in query.items())]

    def create_entity(self, entity, data, timeout=10.0):
        assert entity == "Order"
        time.sleep(self.create_delay)  # allarga la finestra di gara tra due conferme
        with self._lock:
            self._seq += 1
            record = {
                **data,
                "id": f"order-{self._seq:03d}",
                "created_date": datetime.datetime.now(datetime.timezone.utc).isoformat() + "Z",
            }
            self.records.append(record)
            return dict(record)

    def update_entity(self, entity, entity_id, patch, timeout=10.0):
        with self._lock:
            self.updates.append((entity_id, patch))
            for record in self.records:
                if record["id"] == entity_id:
                    record.update(patch)
                    return dict(record)
        raise AssertionError(f"record {entity_id} inesistente")


class OrderSyncTests(unittest.TestCase):
    def setUp(self):
        os.environ["BASE44_TOKEN"] = "test-key"

    def tearDown(self):
        os.environ.pop("BASE44_TOKEN", None)

    def _patch(self, fake):
        patches = [
            patch.object(order_sync.base44_client, name, side_effect=getattr(fake, name))
            for name in ("query_entities", "create_entity", "update_entity")
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _save(self, session_id, restaurant_id="rest-pap", **overrides):
        kwargs = {
            "session_id": session_id,
            "restaurant_id": restaurant_id,
            "customer_name": "Giacomo",
            "customer_phone": "+393331234567",
            "pickup_time": "20:00",
            "ai_confidence": 0.95,
            "items": [ITEM],
            "order_date": TODAY,
            **overrides,
        }
        return order_sync.save_order_to_base44(**kwargs)

    def test_number_continues_from_todays_orders_of_the_same_restaurant(self):
        fake = FakeBase44([
            {"id": "a", "restaurant_id": "rest-pap", "order_date": TODAY, "order_number": 1, "created_date": "2026-09-29T08:00:00"},
            {"id": "b", "restaurant_id": "rest-pap", "order_date": TODAY, "order_number": 2, "created_date": "2026-09-29T09:00:00"},
            # altro ristorante e altro giorno: non contano
            {"id": "c", "restaurant_id": "rest-cds", "order_date": TODAY, "order_number": 40, "created_date": "2026-09-29T09:00:00"},
            {"id": "d", "restaurant_id": "rest-pap", "order_date": "2026-09-28", "order_number": 17, "created_date": "2026-09-28T09:00:00"},
        ])
        self._patch(fake)

        result = self._save("s-1")

        self.assertEqual(result["order_number"], 3)
        self.assertEqual(fake.records[-1]["order_date"], TODAY)
        self.assertEqual(fake.records[-1]["session_id"], "s-1")

    def test_each_restaurant_starts_its_own_sequence_every_day(self):
        fake = FakeBase44([
            {"id": "c", "restaurant_id": "rest-cds", "order_date": TODAY, "order_number": 5, "created_date": "2026-09-29T09:00:00"},
            {"id": "d", "restaurant_id": "rest-pap", "order_date": "2026-09-28", "order_number": 8, "created_date": "2026-09-28T09:00:00"},
        ])
        self._patch(fake)

        self.assertEqual(self._save("s-pap")["order_number"], 1)
        self.assertEqual(self._save("s-cds", restaurant_id="rest-cds")["order_number"], 6)

    def test_weight_order_is_saved_in_kg_without_portion(self):
        fake = FakeBase44()
        self._patch(fake)

        self._save("s-1", pickup_date="2026-09-30", items=[{**ITEM, "order_unit": "kg"}])

        sent = fake.records[-1]
        item = sent["items"][0]
        self.assertEqual(sent["pickup_date"], "2026-09-30")
        self.assertEqual((item["order_unit"], item["quantity"], item["temperature"]), ("kg", ITEM["quantity"], "calda"))
        self.assertNotIn("portion", item)
        self.assertNotIn("slices", item)
        self.assertIsNotNone(sent["total_amount"])

    def test_slice_order_is_saved_with_count_and_portion_and_empty_total(self):
        fake = FakeBase44()
        self._patch(fake)

        slices = {**ITEM, "order_unit": "tranci", "quantity": 2, "size": "mezza", "total_price": None}
        self._save("s-1", items=[slices])

        sent = fake.records[-1]
        item = sent["items"][0]
        self.assertEqual(
            (item["order_unit"], item["slices"], item["portion"], item["size"], item["temperature"]),
            ("tranci", 2, "mezza", "mezza", "calda"),
        )
        self.assertIsNone(item["total_price"])
        self.assertIsNone(sent["total_amount"])  # prezzo a peso al ritiro

    def test_retry_for_same_session_does_not_create_a_second_order(self):
        fake = FakeBase44()
        self._patch(fake)

        first = self._save("s-1")
        second = self._save("s-1")

        self.assertEqual(first, second)
        self.assertEqual(len(fake.records), 1)

    def test_concurrent_confirmations_get_distinct_numbers(self):
        fake = FakeBase44(create_delay=0.05)
        self._patch(fake)
        results = []
        errors = []

        def confirm(session_id):
            try:
                results.append(self._save(session_id))
            except Exception as exc:  # pragma: no cover - fallirebbe il test sotto
                errors.append(exc)

        threads = [threading.Thread(target=confirm, args=(f"s-{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(sorted(r["order_number"] for r in results), [1, 2, 3, 4, 5])
        self.assertEqual(fake.updates, [])  # il lock evita doppioni già alla creazione

    def test_duplicate_from_another_instance_is_renumbered_before_returning(self):
        """Un'altra istanza ha creato prima lo stesso numero: il nostro Order ne prende
        uno nuovo PRIMA che save_order_to_base44 ritorni (quindi prima della conferma)."""
        other_instance_order = {
            "id": "order-other",
            "restaurant_id": "rest-pap",
            "order_date": TODAY,
            "order_number": 1,
            "created_date": "2000-01-01T00:00:00",  # più vecchio del nostro
        }
        fake = FakeBase44()
        real_create = fake.create_entity

        def create_racing(entity, data, timeout=10.0):
            # l'ordine dell'altra istanza diventa visibile solo dopo la nostra lettura del massimo
            fake.records.append(dict(other_instance_order))
            return real_create(entity, data, timeout)

        fake.create_entity = create_racing
        self._patch(fake)

        result = self._save("s-1")

        self.assertEqual(result["order_number"], 2)
        self.assertEqual(fake.updates, [(result["id"], {"order_number": 2})])
        numbers = sorted(r["order_number"] for r in fake.records)
        self.assertEqual(numbers, [1, 2])

    def test_missing_token_raises_instead_of_silently_skipping(self):
        os.environ.pop("BASE44_TOKEN", None)
        with self.assertRaises(order_sync.Base44OrderError):
            self._save("s-1")

    def test_base44_error_propagates_so_the_job_retries(self):
        fake = FakeBase44()
        self._patch(fake)
        with patch.object(order_sync.base44_client, "create_entity", side_effect=RuntimeError("HTTP 500")):
            with self.assertRaises(RuntimeError):
                self._save("s-1")


if __name__ == "__main__":
    unittest.main()
