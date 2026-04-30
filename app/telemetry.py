import math
import threading
import time
from collections import defaultdict, deque
from typing import Any


_MAX_SAMPLES_PER_KEY = 500
_latency_samples: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(
    lambda: deque(maxlen=_MAX_SAMPLES_PER_KEY)
)
_lock = threading.Lock()


def _percentile(values: list[int], percentile: int) -> int:
    if not values:
        return 0
    index = math.ceil((percentile / 100) * len(values)) - 1
    index = max(0, min(index, len(values) - 1))
    return sorted(values)[index]


def record_latency(metric: str, path: str, elapsed_ms: int | float, **fields: Any) -> None:
    sample = {
        "elapsed_ms": int(round(elapsed_ms)),
        "timestamp": round(time.time(), 3),
        "fields": {key: str(value) for key, value in fields.items()},
    }
    with _lock:
        _latency_samples[(metric, path)].append(sample)


def get_latency_snapshot() -> dict[str, Any]:
    with _lock:
        items = [
            (metric, path, list(samples))
            for (metric, path), samples in _latency_samples.items()
        ]

    metrics = []
    for metric, path, samples in sorted(items):
        values = [sample["elapsed_ms"] for sample in samples]
        metrics.append(
            {
                "metric": metric,
                "path": path,
                "count": len(values),
                "min_ms": min(values) if values else 0,
                "p50_ms": _percentile(values, 50),
                "p95_ms": _percentile(values, 95),
                "p99_ms": _percentile(values, 99),
                "max_ms": max(values) if values else 0,
                "latest": samples[-1] if samples else None,
            }
        )

    return {
        "window_size": _MAX_SAMPLES_PER_KEY,
        "metrics": metrics,
    }


def clear_latency_metrics() -> None:
    with _lock:
        _latency_samples.clear()
