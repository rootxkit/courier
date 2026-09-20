"""Structured JSON logging.

One line of JSON per record, on stdout, for every service. Logs are read by
machines first and humans second: an incident review reconstructs a flight from
`events` and from these lines, and grepping prose does not survive that.

Context travels as fields, never interpolated into the message. CLAUDE.md
requires structured context on anything operational, so::

    log = bind(get_logger(__name__), drone_id=drone_id, mission_id=mission_id)
    log.warning("battery below reserve", extra={"batt_pct": 22.5})

produces a record carrying drone_id, mission_id and batt_pct as separate
fields, which a query can filter on.

print() is banned by ruff (T20) across the repository; this module is the
reason there is somewhere else to go.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from collections.abc import MutableMapping
from typing import Any

__all__ = ["JsonFormatter", "bind", "configure_logging", "get_logger"]

# Attributes the logging module puts on every record. Anything else on a
# record was supplied by the caller through `extra` and is real context.
_RESERVED_RECORD_FIELDS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


def _json_default(value: object) -> str:
    """Render anything json cannot.

    A log call must never raise. Losing the type of an odd field is an
    acceptable price; losing the log line while a vehicle is airborne is not.
    """
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    return repr(value)


class JsonFormatter(logging.Formatter):
    """Format a record as a single JSON object."""

    def __init__(self, *, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            # UTC, always, and timezone-aware. The display layer converts.
            "ts": dt.datetime.fromtimestamp(record.created, tz=dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "service": self._service,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_FIELDS or key.startswith("_"):
                continue
            payload[key] = value

        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info is not None:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=_json_default, separators=(",", ":"))


class BoundLogger(logging.LoggerAdapter[logging.Logger]):
    """A logger carrying fixed context, merged into every record it emits."""

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        bound: dict[str, Any] = dict(self.extra or {})
        # Call-site context wins over bound context, so a specific value can
        # override an inherited one.
        bound.update(kwargs.get("extra") or {})
        kwargs["extra"] = bound
        return msg, kwargs

    def bind(self, **context: Any) -> BoundLogger:
        """Return a logger with additional context attached."""
        merged: dict[str, Any] = dict(self.extra or {})
        merged.update(context)
        return BoundLogger(self.logger, merged)


def configure_logging(*, service: str, level: str = "INFO") -> None:
    """Install the JSON handler on the root logger.

    Called once, at service startup, before anything else logs. Existing root
    handlers are removed so that a second call — in tests, typically — does not
    duplicate every line.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service=service))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    """Return the logger for a module. Use `__name__` at the call site."""
    return logging.getLogger(name)


def bind(logger: logging.Logger | BoundLogger, **context: Any) -> BoundLogger:
    """Attach context to a logger so every record it emits carries it."""
    if isinstance(logger, BoundLogger):
        return logger.bind(**context)
    return BoundLogger(logger, dict(context))
