"""Logging estruturado restrito a metadados operacionais (conformidade eBay e segurança)."""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TextIO

ALLOWED_LOG_FIELDS: frozenset[str] = frozenset(
    {
        "request_id",
        "stage",
        "method",
        "path",
        "status_code",
        "duration_ms",
        "item_count",
        "candidate_count",
        "result_count",
        "ebay_calls",
        "llm_calls",
        "relaxation_count",
        "error_type",
        "provider",
        "marketplace_id",
        "quota_remaining",
        "retry_attempt",
    }
)

FORBIDDEN_KEY_FRAGMENTS: tuple[str, ...] = (
    "authorization",
    "token",
    "secret",
    "api_key",
    "apikey",
    "password",
    "credential",
    "cookie",
    "bearer",
    "title",
    "price",
    "description",
    "url",
    "permalink",
    "image",
    "thumbnail",
    "seller",
    "username",
    "aspect",
    "payload",
    "body",
    "itemsummaries",
    "item_summaries",
    "product",
    "candidate",
    "attribute",
)

_STANDARD_RECORD_ATTRS: frozenset[str] = frozenset(
    logging.LogRecord("buscai", logging.INFO, "", 0, "", (), None).__dict__.keys()
) | {"operational_fields", "message", "asctime"}

_BEARER_PATTERN = re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/-]+=*")
_BASIC_PATTERN = re.compile(r"(?i)\b(Basic)\s+[A-Za-z0-9+/=]+")
_GOOGLE_KEY_PATTERN = re.compile(r"AIza[0-9A-Za-z_-]{10,}")
_KV_SECRET_PATTERN = re.compile(
    r"(?i)\b([a-z0-9_-]*(?:api[_-]?keys?|client[_-]?secret|access[_-]?token|secret[_-]?key))\b(\s*[:=]\s*)([^\s;'\")\}\]]+(?<![,]))"
)


def sanitize_text(text: str) -> str:
    """Redige tokens, headers de autenticação e chaves em mensagens textuais."""
    sanitized = _BEARER_PATTERN.sub(r"\1 [REDACTED]", text)
    sanitized = _BASIC_PATTERN.sub(r"\1 [REDACTED]", sanitized)
    sanitized = _KV_SECRET_PATTERN.sub(r"\1\2[REDACTED]", sanitized)
    sanitized = _GOOGLE_KEY_PATTERN.sub("[REDACTED_KEY]", sanitized)
    return sanitized


def filter_operational_fields(fields: Mapping[str, object]) -> dict[str, object]:
    """Filtra somente metadados operacionais permitidos com valores primitivos seguros."""
    filtered: dict[str, object] = {}
    for key, value in fields.items():
        if key not in ALLOWED_LOG_FIELDS:
            continue
        if value is not None and not isinstance(value, (str, int, float, bool)):
            continue
        if isinstance(value, float) and not math.isfinite(value):
            continue
        if isinstance(value, str):
            value = sanitize_text(value)
        filtered[key] = value
    return filtered


class SafeJsonFormatter(logging.Formatter):
    """Formata eventos como JSON linha-única sem stacktraces, locals ou payloads de anúncios."""

    def format(self, record: logging.LogRecord) -> str:
        raw_fields: dict[str, object] = {}
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS:
                raw_fields[key] = value

        operational = getattr(record, "operational_fields", None)
        if isinstance(operational, Mapping):
            raw_fields.update(operational)

        filtered_fields = filter_operational_fields(raw_fields)

        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": sanitize_text(record.getMessage()),
        }
        payload.update(filtered_fields)

        if record.exc_info and record.exc_info[0] is not None:
            payload.setdefault("error_type", record.exc_info[0].__name__)

        return json.dumps(payload, ensure_ascii=False)


def configure_logging(
    level: str = "INFO",
    *,
    stream: TextIO | None = None,
    logger_name: str = "buscai",
) -> logging.Logger:
    """Configura o logger estruturado da aplicação com formatação JSON sanitizada."""
    normalized = level.strip().upper()
    numeric_level = getattr(logging, normalized, None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Nível de log inválido: {level!r}.")

    logger = logging.getLogger(logger_name)
    logger.setLevel(numeric_level)
    logger.handlers.clear()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SafeJsonFormatter())
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def get_logger(name: str = "buscai") -> logging.Logger:
    """Retorna o logger operacional configurado."""
    return logging.getLogger(name)


def log_operational_event(
    logger: logging.Logger,
    level: int,
    event: str,
    **fields: object,
) -> None:
    """Registra um evento operacional mantendo apenas os campos permitidos."""
    logger.log(level, event, extra={"operational_fields": filter_operational_fields(fields)})
