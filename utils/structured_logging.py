"""
utils/structured_logging.py — JSON log formatter + request correlation IDs.

Replaces plain-text logs with structured JSON so you can pipe into
ELK, Loki, Datadog, or grep by field. Adds a per-request correlation ID
(X-Request-ID) so one request can be traced across all engines.

Usage (in main.py):
    from utils.structured_logging import setup_json_logging, CorrelationMiddleware
    setup_json_logging()
    app.add_middleware(CorrelationMiddleware)

Env flag WARMR_JSON_LOGS=1 enables it. Default stays human-readable
for local dev.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest

# Per-request correlation ID, available anywhere via get_correlation_id()
_CORRELATION_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "warmr_correlation_id", default=""
)


def get_correlation_id() -> str:
    return _CORRELATION_ID.get()


class JSONFormatter(logging.Formatter):
    """Log every record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        cid = _CORRELATION_ID.get()
        if cid:
            payload["correlation_id"] = cid

        # Include exception info if present
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # Include any extra fields the caller passed via logger.info(..., extra={})
        for key in ("client_id", "campaign_id", "inbox_id", "lead_id", "event", "duration_ms"):
            val = record.__dict__.get(key)
            if val is not None:
                payload[key] = val

        return json.dumps(payload, default=str, ensure_ascii=False)


def setup_json_logging() -> None:
    """Activate JSON logging if WARMR_JSON_LOGS=1."""
    if os.getenv("WARMR_JSON_LOGS", "0") != "1":
        return
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)
    root.setLevel(logging.INFO)


class CorrelationMiddleware(BaseHTTPMiddleware):
    """
    Attach an X-Request-ID header (generates one if missing) and expose it
    via the correlation_id contextvar for log records in this request.
    """

    async def dispatch(self, request: StarletteRequest, call_next):
        incoming = request.headers.get("x-request-id") or request.headers.get("X-Request-ID")
        cid = incoming or uuid.uuid4().hex[:16]
        token = _CORRELATION_ID.set(cid)
        try:
            response = await call_next(request)
        finally:
            _CORRELATION_ID.reset(token)
        response.headers["X-Request-ID"] = cid
        return response
