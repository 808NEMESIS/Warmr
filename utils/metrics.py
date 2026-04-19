"""
utils/metrics.py — lightweight Prometheus-compatible metrics.

No external deps — writes plain Prometheus text format directly.
Tracks request count, latency histogram, and error count per endpoint.

Wire in main.py:
    from utils.metrics import MetricsMiddleware, metrics_text
    app.add_middleware(MetricsMiddleware)

    @app.get("/metrics")
    async def metrics():
        return Response(content=metrics_text(), media_type="text/plain")
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest

# Histogram buckets (seconds) — standard Prometheus values
_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

_lock = threading.Lock()
_request_count: dict[tuple[str, str, int], int] = defaultdict(int)
_error_count: dict[tuple[str, str], int] = defaultdict(int)
_latency_buckets: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0] * (len(_BUCKETS) + 1))
_latency_sum: dict[tuple[str, str], float] = defaultdict(float)
_latency_count: dict[tuple[str, str], int] = defaultdict(int)


def _normalize_path(path: str) -> str:
    """Collapse UUIDs and numeric IDs so metrics don't explode cardinality."""
    parts = []
    for seg in path.split("/"):
        if not seg:
            parts.append(seg)
            continue
        if len(seg) >= 20 and seg.count("-") >= 3:
            parts.append(":id")
        elif seg.isdigit():
            parts.append(":id")
        else:
            parts.append(seg)
    return "/".join(parts)


def record(method: str, path: str, status_code: int, duration_s: float) -> None:
    key = (method, _normalize_path(path))
    with _lock:
        _request_count[(method, key[1], status_code)] += 1
        _latency_sum[key] += duration_s
        _latency_count[key] += 1
        # Find bucket
        for i, threshold in enumerate(_BUCKETS):
            if duration_s <= threshold:
                _latency_buckets[key][i] += 1
                break
        else:
            _latency_buckets[key][-1] += 1  # +Inf bucket
        if status_code >= 500:
            _error_count[key] += 1


def metrics_text() -> str:
    """Render all metrics in Prometheus text format."""
    lines: list[str] = []

    lines.append("# HELP warmr_requests_total Total HTTP requests")
    lines.append("# TYPE warmr_requests_total counter")
    with _lock:
        for (method, path, code), count in sorted(_request_count.items()):
            lines.append(f'warmr_requests_total{{method="{method}",path="{path}",code="{code}"}} {count}')

        lines.append("# HELP warmr_request_duration_seconds Request latency")
        lines.append("# TYPE warmr_request_duration_seconds histogram")
        for (method, path), buckets in sorted(_latency_buckets.items()):
            cumulative = 0
            for i, threshold in enumerate(_BUCKETS):
                cumulative += buckets[i]
                lines.append(
                    f'warmr_request_duration_seconds_bucket{{method="{method}",path="{path}",le="{threshold}"}} {cumulative}'
                )
            cumulative += buckets[-1]
            lines.append(
                f'warmr_request_duration_seconds_bucket{{method="{method}",path="{path}",le="+Inf"}} {cumulative}'
            )
            lines.append(
                f'warmr_request_duration_seconds_sum{{method="{method}",path="{path}"}} {_latency_sum[(method, path)]:.4f}'
            )
            lines.append(
                f'warmr_request_duration_seconds_count{{method="{method}",path="{path}"}} {_latency_count[(method, path)]}'
            )

        lines.append("# HELP warmr_errors_total HTTP 5xx errors")
        lines.append("# TYPE warmr_errors_total counter")
        for (method, path), count in sorted(_error_count.items()):
            lines.append(f'warmr_errors_total{{method="{method}",path="{path}"}} {count}')

    return "\n".join(lines) + "\n"


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):
        start = time.perf_counter()
        try:
            response = await call_next(request)
            record(request.method, request.url.path, response.status_code, time.perf_counter() - start)
            return response
        except Exception:
            record(request.method, request.url.path, 500, time.perf_counter() - start)
            raise
