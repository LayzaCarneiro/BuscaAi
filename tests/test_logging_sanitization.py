"""Testes de conformidade e sanitização do logger operacional."""

from __future__ import annotations

import io
import json
import logging

from buscai.observability import (
    SafeJsonFormatter,
    configure_logging,
    log_operational_event,
    sanitize_text,
)


def test_logs_valid_json_with_allowed_operational_fields() -> None:
    stream = io.StringIO()
    logger = configure_logging("INFO", stream=stream, logger_name="buscai.test.ok")

    log_operational_event(
        logger,
        logging.INFO,
        "search_completed",
        request_id="req-123",
        stage="search",
        status_code=200,
        duration_ms=42.5,
        item_count=10,
        ebay_calls=1,
    )

    lines = stream.getvalue().strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["level"] == "INFO"
    assert payload["event"] == "search_completed"
    assert payload["request_id"] == "req-123"
    assert payload["stage"] == "search"
    assert payload["status_code"] == 200
    assert payload["duration_ms"] == 42.5
    assert payload["item_count"] == 10
    assert payload["ebay_calls"] == 1


def test_drops_forbidden_listing_and_credential_fields() -> None:
    stream = io.StringIO()
    logger = configure_logging("DEBUG", stream=stream, logger_name="buscai.test.drop")

    log_operational_event(
        logger,
        logging.INFO,
        "ebay_call_finished",
        request_id="req-456",
        item_count=3,
        title="SENTINEL_TITLE_XYZ",
        price="999.99",
        description="SENTINEL_DESC_XYZ",
        itemWebUrl="https://ebay.example/item/1",
        seller="seller_xyz",
        Authorization="Bearer secret-tok",
        api_key="my-secret-api-key",
        client_secret="my-client-secret",
        token="oauth-tok",
        payload={"itemSummaries": []},
        products=["p1"],
    )

    raw = stream.getvalue()
    assert "SENTINEL_TITLE_XYZ" not in raw
    assert "999.99" not in raw
    assert "SENTINEL_DESC_XYZ" not in raw
    assert "https://ebay.example" not in raw
    assert "seller_xyz" not in raw
    assert "secret-tok" not in raw
    assert "my-secret-api-key" not in raw
    assert "my-client-secret" not in raw
    assert "oauth-tok" not in raw

    parsed = json.loads(raw.strip())
    assert set(parsed.keys()) == {"timestamp", "level", "logger", "event", "request_id", "item_count"}


def test_redacts_sensitive_patterns_in_message() -> None:
    text = (
        "Falha ao chamar com Authorization: Bearer abc.def.123, "
        "Basic YWJjOmRlZg==, x-api-key: cliente_secret_1, "
        "client_secret=supersegredo e AIzaSyFakeGoogleKey12345"
    )
    sanitized = sanitize_text(text)

    assert "abc.def.123" not in sanitized
    assert "YWJjOmRlZg==" not in sanitized
    assert "cliente_secret_1" not in sanitized
    assert "supersegredo" not in sanitized
    assert "AIzaSyFakeGoogleKey12345" not in sanitized
    assert "[REDACTED]" in sanitized


def test_redacts_prefixed_env_var_secrets_and_comma_lists() -> None:
    text = (
        "Config falhou: GEMINI_API_KEY=segredo123, "
        "EBAY_CLIENT_SECRET=segredo456, BUSCAAI_API_KEYS=key1,key2; fim"
    )
    sanitized = sanitize_text(text)

    assert "segredo123" not in sanitized
    assert "segredo456" not in sanitized
    assert "key1" not in sanitized
    assert "key2" not in sanitized
    assert "GEMINI_API_KEY=[REDACTED]" in sanitized
    assert "EBAY_CLIENT_SECRET=[REDACTED]" in sanitized
    assert "BUSCAAI_API_KEYS=[REDACTED]" in sanitized


def test_exception_logging_records_only_error_type_without_traceback_or_payload() -> None:
    stream = io.StringIO()
    logger = configure_logging("ERROR", stream=stream, logger_name="buscai.test.exc")

    try:
        local_secret_listing = "SENTINEL_LOCAL_LISTING_TITLE"
        raise ValueError(f"Erro com payload {local_secret_listing}")
    except ValueError:
        logger.error("search_failed", exc_info=True, extra={"request_id": "req-err"})

    raw = stream.getvalue()
    assert "SENTINEL_LOCAL_LISTING_TITLE" not in raw
    assert "Traceback" not in raw
    parsed = json.loads(raw.strip())
    assert parsed["error_type"] == "ValueError"
    assert parsed["request_id"] == "req-err"
    assert isinstance(SafeJsonFormatter(), SafeJsonFormatter)
