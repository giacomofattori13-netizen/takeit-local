import os
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes.logs import router as logs_router
from app.telemetry import (
    clear_latency_metrics,
    get_latency_snapshot,
    record_latency,
)


class LatencyTelemetryTests(unittest.TestCase):
    def setUp(self):
        clear_latency_metrics()
        self.previous_admin_key = os.environ.get("ADMIN_API_KEY")
        os.environ["ADMIN_API_KEY"] = "test-admin-key"

    def tearDown(self):
        clear_latency_metrics()
        if self.previous_admin_key is None:
            os.environ.pop("ADMIN_API_KEY", None)
        else:
            os.environ["ADMIN_API_KEY"] = self.previous_admin_key

    def test_latency_snapshot_reports_percentiles_by_metric_path(self):
        record_latency("chat", "local_name", 100, state="collecting_name")
        record_latency("chat", "local_name", 300, state="collecting_name")
        record_latency("chat", "local_name", 200, state="collecting_name")
        record_latency("llm", "extract_order", 900, state="collecting_items")

        snapshot = get_latency_snapshot()
        metrics = {
            (metric["metric"], metric["path"]): metric
            for metric in snapshot["metrics"]
        }

        chat_metric = metrics[("chat", "local_name")]
        self.assertEqual(chat_metric["count"], 3)
        self.assertEqual(chat_metric["min_ms"], 100)
        self.assertEqual(chat_metric["p50_ms"], 200)
        self.assertEqual(chat_metric["p95_ms"], 300)
        self.assertEqual(chat_metric["p99_ms"], 300)
        self.assertEqual(chat_metric["max_ms"], 300)
        self.assertEqual(chat_metric["latest"]["fields"]["state"], "collecting_name")
        self.assertEqual(metrics[("llm", "extract_order")]["p50_ms"], 900)

    def test_latency_endpoint_requires_admin_key(self):
        app = FastAPI()
        app.include_router(logs_router)
        client = TestClient(app)

        response = client.get("/logs/latency")

        self.assertEqual(response.status_code, 401)

    def test_latency_endpoint_returns_snapshot(self):
        record_latency("chat", "llm_path", 250, state="collecting_items")
        app = FastAPI()
        app.include_router(logs_router)
        client = TestClient(app)

        response = client.get(
            "/logs/latency",
            headers={"X-Admin-Api-Key": "test-admin-key"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["metrics"][0]["metric"], "chat")
        self.assertEqual(response.json()["metrics"][0]["path"], "llm_path")
        self.assertEqual(response.json()["metrics"][0]["p50_ms"], 250)


if __name__ == "__main__":
    unittest.main()
