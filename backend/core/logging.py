"""Structured (JSON-lines) logging setup.

WHY JSON lines and not plain text: CLAUDE.md §5 requires every Claude call
to be logged with agent, model, tokens in/out, cache hits, latency, and
estimated cost — that's structured, numeric data, not prose. One JSON
object per log line means it can be grepped, loaded into a dataframe, or
summed for a per-run cost total without writing a text parser. No new
dependency: stdlib `logging` plus one Formatter subclass is enough.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

# Attributes every standard LogRecord already carries. Anything else on the
# record was passed via `extra={...}` by the caller and belongs in the
# JSON output.
_STANDARD_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)


class JSONLogFormatter(logging.Formatter):
    """Renders each LogRecord as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_RECORD_ATTRS
        }
        payload.update(extras)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger once, at process start.

    WHY idempotent-ish rather than additive: calling this more than once
    (e.g. in tests that import multiple modules) would otherwise stack
    duplicate handlers and double-print every log line.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONLogFormatter())
    root.addHandler(handler)
