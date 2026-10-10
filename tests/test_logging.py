"""Structured logging: JSON output, secret redaction."""

import io
import json
import logging
import re

from app.logging_config import JsonFormatter, ScrubFilter, scrub_secrets


def _record(message: str, **extra) -> dict:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ScrubFilter())
    logger = logging.getLogger("supportflow.tests")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.info(message, extra=extra)
    return json.loads(stream.getvalue())


def test_output_is_a_single_json_line():
    payload = _record("order looked up", order_id="ORD-1")
    assert payload["message"] == "order looked up"
    assert payload["order_id"] == "ORD-1"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "supportflow.tests"
    assert re.match(r"^\d{4}-\d{2}-\d{2}T", payload["timestamp"])


def test_api_key_in_extra_is_masked():
    payload = _record("checking config", deepseek_api_key="sk-deepseek-secret")
    assert payload["deepseek_api_key"] == "***"
    assert "sk-ant-secret" not in json.dumps(payload)


def test_api_key_in_message_text_is_redacted():
    payload = _record("loaded api_key=sk-ant-abc from env")
    assert "sk-ant-abc" not in json.dumps(payload)
    assert "***" in payload["message"]


def test_scrub_secrets_recurses_into_nested_structures():
    cleaned = scrub_secrets(
        {
            "order_id": "ORD-1",
            "auth": {"access_token": "tok-123", "note": "kept"},
            "items": [{"amount": 10}, {"password": "pw"}],
        }
    )
    assert cleaned["order_id"] == "ORD-1"
    assert cleaned["auth"]["access_token"] == "***"
    assert cleaned["auth"]["note"] == "kept"
    assert cleaned["items"][1]["password"] == "***"


def test_scrub_secrets_keeps_unrelated_values():
    assert scrub_secrets({"amount": 12.34, "order_id": "ORD-2"}) == {
        "amount": 12.34,
        "order_id": "ORD-2",
    }
