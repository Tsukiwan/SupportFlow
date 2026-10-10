"""Structured JSON logging with secret redaction.

Usage::

    from app.logging_config import setup_logging

    setup_logging()

Rules:
- No API keys, tokens, payment credentials or unnecessary personal data in
  log output. The scrub filter is a key-name-based safety net; callers must
  never log secret *values* under neutral key names.
- Every record is emitted as one JSON line via :class:`JsonFormatter`.
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|apikey|token|secret|password|credential|authorization|cookie)",
    re.IGNORECASE,
)

_SECRET_ASSIGNMENT_RE = re.compile(
    r"((?:api[_-]?key|secret|token|password|credential|authorization)\s*[=:]\s*)\S+",
    re.IGNORECASE,
)


def _is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY_RE.search(key))


def _redact_text(text: str) -> str:
    return _SECRET_ASSIGNMENT_RE.sub(r"\1***", text)


def scrub_secrets(value: Any) -> Any:
    """Recursively replace secret-like keys with ``***`` in dicts/lists."""
    if isinstance(value, dict):
        return {
            key: "***" if _is_secret_key(str(key)) else scrub_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub_secrets(item) for item in value]
    return value


class ScrubFilter(logging.Filter):
    """Redact secret-shaped content from every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_text(record.getMessage())
        record.args = ()
        for key in vars(record):
            if key in {"msg", "args"}:
                continue
            if _is_secret_key(key):
                setattr(record, key, "***")
        return True


class JsonFormatter(logging.Formatter):
    """Format records as a single JSON line, including ``extra`` fields."""

    _STANDARD_FIELDS = frozenset(
        {
            "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
            "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "created", "msecs", "relativeCreated", "thread", "threadName",
            "processName", "process", "taskName", "message",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in self._STANDARD_FIELDS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO") -> None:
    """Replace the root logger's handlers with the JSON+scrub configuration."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ScrubFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
