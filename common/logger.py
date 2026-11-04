"""Structured JSON logging shared by every PingHub service.

Every log line is a JSON object.  The important field is ``request_id``: the
gateway creates it (or reuses ``X-Request-ID``) and every downstream service
puts it in its own logs.  That is what makes a single request traceable across
gateway -> queue -> worker -> database.

Example line:
    {"ts":"2026-11-05T10:00:00.123Z","level":"INFO","service":"worker",
     "request_id":"r-abc123","event":"db_saved","status":"PROCESSING"}
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextvars import ContextVar
from typing import Any

# ContextVar is important: it keeps the request id attached to the current
# async task without passing it through every function argument.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_SERVICE = os.getenv("SERVICE_NAME", "pinghub")

# Keys that Python's logging module already uses internally.  Everything else
# passed through ``extra=`` is copied into the JSON object.
_RESERVED = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
    "message",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as one compact JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "service": getattr(record, "service", _SERVICE),
            "request_id": getattr(record, "request_id", request_id_var.get()),
            "event": record.getMessage(),
        }

        # Copy custom fields supplied with logger.info(..., extra={...}).
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(service: str | None = None) -> None:
    """Attach one JSON handler to the root logger.

    Called once at the top of each service module.
    """

    global _SERVICE
    if service:
        _SERVICE = service

    root = logging.getLogger()
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())

    # Avoid adding duplicate handlers if the module is imported twice.
    already_configured = any(
        isinstance(handler.formatter, JsonFormatter) for handler in root.handlers
    )
    if not already_configured:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        root.handlers = [handler]

    # Make uvicorn's own loggers flow through the same JSON formatter.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
