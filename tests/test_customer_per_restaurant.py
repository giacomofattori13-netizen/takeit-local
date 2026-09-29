"""Customer distinti per ristorante: stesso telefono su due locali = due record."""
import os
import unittest
from unittest.mock import MagicMock, patch

from app.services import conversation_service as service

PHONE = "+393331234567"
CUSTOMERS = [
    {"id": "cust-pap", "full_name": "Giacomo PaP", "phone": PHONE, "restaurant_id": "rest-pap", "total_orders": 2, "total_spend": 7.4},
    {"id": "cust-cds", "full_name": "Giacomo CdS", "phone": PHONE, "restaurant_id": "rest-cds", "total_orders": 5, "total_spend": 40.0},
    {"id": "cust-legacy", "full_name": "Giacomo vecchio", "phone": PHONE, "total_orders": 8},
]


def _response(body):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.return_value = body
    return resp


class CustomerPerRestaurantTests(unittest.TestCase):
    def setUp(self):
        p = patch.dict(os.environ, {"BASE44_TOKEN": "test-key"})
        p.start()
        self.addCleanup(p.stop)
        service.reset_customer_lookup_cache()
        self.addCleanup(service.reset_customer_lookup_cache)
        p = patch.object(service.httpx, "get", return_value=_response({"entities": [dict(c) for c in CUSTOMERS]}))
        p.start()
        self.addCleanup(p.stop)

    def test_lookup_returns_the_record_of_the_called_restaurant(self):
        self.assertEqual(service.lookup_customer(PHONE, "rest-pap")["id"], "cust-pap")
        self.assertEqual(service.lookup_customer(PHONE, "rest-cds")["id"], "cust-cds")

    def test_legacy_record_without_restaurant_is_not_attributed_to_a_restaurant(self):
        self.assertIsNone(service.lookup_customer(PHONE, "rest-nuovo"))

    def test_upsert_for_new_restaurant_creates_separate_record_and_keeps_the_others(self):
        posts, puts, deletes = [], [], []
        with patch.object(service.httpx, "post", side_effect=lambda url, **kw: posts.append(kw["json"]) or _response({"id": "new"})), \
                patch.object(service.httpx, "put", side_effect=lambda url, **kw: puts.append(url) or _response({})), \
                patch.object(service.httpx, "delete", side_effect=lambda url, **kw: deletes.append(url) or _response({})):
            service.upsert_customer("Giacomo", PHONE, ["Bufala"], 3.7, restaurant_id="rest-nuovo")

        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["restaurant_id"], "rest-nuovo")
        self.assertEqual(posts[0]["total_orders"], 1)
        self.assertEqual(puts, [])
        self.assertEqual(deletes, [])  # nessuna "deduplica" tra locali diversi

    def test_upsert_updates_only_the_record_of_the_same_restaurant(self):
        puts = []
        with patch.object(service.httpx, "put", side_effect=lambda url, **kw: puts.append((url, kw["json"])) or _response({})), \
                patch.object(service.httpx, "post") as post, \
                patch.object(service.httpx, "delete") as delete:
            service.upsert_customer("Giacomo", PHONE, ["Bufala"], 3.7, restaurant_id="rest-pap")

        self.assertEqual(len(puts), 1)
        url, payload = puts[0]
        self.assertTrue(url.endswith("/cust-pap"))
        self.assertEqual(payload["total_orders"], 3)
        post.assert_not_called()
        delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
