"""Configuração centralizada e segura da aplicação BuscaAI."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
VALID_EMBEDDING_BACKENDS = frozenset({"sentence_transformers", "fastembed"})

_PLACEHOLDER_EXACT: frozenset[str] = frozenset(
    {
        "changeme",
        "replace_me",
        "placeholder",
        "your_api_key",
        "your-api-key",
        "api_key_here",
        "example_key",
        "sample_key",
        "dummy_key",
        "test_placeholder",
        "todo",
        "none",
        "null",
        "xxx",
        "xxxx",
        "chave_cliente",
        "chave_cliente_1",
        "chave_cliente_2",
        "coloque_sua_chave_aqui",
        "coloque_seu_client_id_aqui",
        "coloque_seu_client_secret_aqui",
    }
)
_PLACEHOLDER_PREFIXES: tuple[str, ...] = (
    "coloque_",
    "insert_",
    "replace_",
    "placeholder",
    "your_",
    "example_",
    "chave_cliente",
)


def is_placeholder_secret(value: str) -> bool:
    """Identifica valores de exemplo/placeholder que não devem ser tratados como credenciais reais."""
    cleaned = value.strip()
    if not cleaned:
        return True
    lowered = cleaned.casefold()
    if lowered in _PLACEHOLDER_EXACT:
        return True
    if lowered.startswith(_PLACEHOLDER_PREFIXES):
        return True
    if ("<" in cleaned and ">" in cleaned) or ("{" in cleaned and "}" in cleaned):
        return True
    return False


def _clean_env_value(environ: Mapping[str, str], key: str) -> str | None:
    raw = environ.get(key)
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    return stripped if stripped else None


def _parse_int_env(environ: Mapping[str, str], key: str, default: int) -> int:
    raw = _clean_env_value(environ, key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} deve ser um número inteiro válido.") from exc


def _parse_decimal_env(environ: Mapping[str, str], key: str, default: Decimal) -> Decimal:
    raw = _clean_env_value(environ, key)
    if raw is None:
        return default
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"{key} deve ser um número decimal válido.") from exc


def _parse_float_env(environ: Mapping[str, str], key: str, default: float) -> float:
    raw = _clean_env_value(environ, key)
    if raw is None:
        return default
    try:
        val = float(raw)
    except ValueError as exc:
        raise ValueError(f"{key} deve ser um número decimal válido.") from exc
    if not math.isfinite(val):
        raise ValueError(f"{key} deve ser um número finito.")
    return val


def _parse_bool_env(environ: Mapping[str, str], key: str, default: bool) -> bool:
    raw = _clean_env_value(environ, key)
    if raw is None:
        return default
    normalized = raw.casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{key} deve ser um booleano válido (true/false/1/0).")


def _parse_api_keys(environ: Mapping[str, str], key: str = "BUSCAAI_API_KEYS") -> tuple[SecretStr, ...]:
    raw = _clean_env_value(environ, key)
    if raw is None:
        return ()
    return tuple(SecretStr(item.strip()) for item in raw.split(",") if item.strip())


class Settings(BaseModel):
    """Configurações tipadas do BuscaAI com proteção de segredos via SecretStr."""

    model_config = ConfigDict(frozen=True)

    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.5-flash-lite"
    ebay_client_id: str | None = None
    ebay_client_secret: SecretStr | None = None
    ebay_marketplace_id: str = "EBAY_US"
    ebay_daily_quota_limit: int = Field(default=5000, ge=1)
    usd_brl_rate: Decimal = Field(default=Decimal("5.50"), gt=0)
    usd_brl_rate_updated_at: str = "unknown"
    usd_brl_rate_source: str = "env_fixed_rate"
    embedding_model_name: str = "intfloat/multilingual-e5-small"
    embedding_backend: str = "sentence_transformers"
    embedding_batch_size: int = Field(default=32, ge=1, le=256)
    embedding_local_files_only: bool = False
    embedding_cache_dir: str | None = None
    dedup_probable_threshold: float = Field(default=0.86, ge=0.0, le=1.0)
    dedup_similar_threshold: float = Field(default=0.68, ge=0.0, le=1.0)
    api_keys: tuple[SecretStr, ...] = ()
    rate_limit_per_minute: int = Field(default=30, ge=1)
    rate_limit_window_seconds: int = Field(default=60, ge=1)
    max_request_body_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)
    search_default_limit: int = Field(default=10, ge=1, le=50)
    search_max_limit: int = Field(default=20, ge=1, le=50)
    chat_max_history_messages: int = Field(default=12, ge=0, le=50)
    chat_max_message_chars: int = Field(default=500, ge=50, le=2000)
    chat_max_history_total_chars: int = Field(default=4000, ge=100, le=20000)
    embedding_preload: bool = False
    log_level: str = "INFO"

    @field_validator("gemini_model", "usd_brl_rate_updated_at", "usd_brl_rate_source", "embedding_model_name")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Campo textual de configuração não pode ser vazio.")
        return cleaned

    @field_validator("embedding_backend")
    @classmethod
    def validate_embedding_backend(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in VALID_EMBEDDING_BACKENDS:
            raise ValueError(
                f"BUSCAAI_EMBEDDING_BACKEND inválido: esperado um de {sorted(VALID_EMBEDDING_BACKENDS)}."
            )
        return normalized

    @field_validator("dedup_probable_threshold", "dedup_similar_threshold", mode="before")
    @classmethod
    def reject_bool_threshold(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("Limiar de deduplicação não pode ser booleano.")
        return value

    @field_validator("dedup_probable_threshold", "dedup_similar_threshold", mode="after")
    @classmethod
    def validate_finite_threshold(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Limiar de deduplicação deve ser finito.")
        return value

    @model_validator(mode="after")
    def validate_dedup_thresholds_order(self) -> Settings:
        if self.dedup_similar_threshold > self.dedup_probable_threshold:
            raise ValueError(
                "dedup_similar_threshold deve ser menor ou igual a dedup_probable_threshold."
            )
        if self.search_default_limit > self.search_max_limit:
            raise ValueError("search_default_limit deve ser menor ou igual a search_max_limit.")
        if self.chat_max_message_chars > self.chat_max_history_total_chars:
            raise ValueError("chat_max_message_chars deve ser menor ou igual a chat_max_history_total_chars.")
        return self

    @field_validator("ebay_marketplace_id")
    @classmethod
    def validate_marketplace_id(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if not cleaned:
            raise ValueError("EBAY_MARKETPLACE_ID não pode ser vazio.")
        return cleaned

    @field_validator("usd_brl_rate")
    @classmethod
    def validate_exchange_rate(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("USD_BRL_RATE deve ser um decimal finito maior que zero.")
        return value

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in VALID_LOG_LEVELS:
            raise ValueError(f"BUSCAAI_LOG_LEVEL inválido: esperado um de {sorted(VALID_LOG_LEVELS)}.")
        return normalized

    @property
    def is_gemini_configured(self) -> bool:
        return self.gemini_api_key is not None and not is_placeholder_secret(
            self.gemini_api_key.get_secret_value()
        )

    @property
    def is_ebay_configured(self) -> bool:
        return (
            isinstance(self.ebay_client_id, str)
            and not is_placeholder_secret(self.ebay_client_id)
            and self.ebay_client_secret is not None
            and not is_placeholder_secret(self.ebay_client_secret.get_secret_value())
        )

    @property
    def is_api_auth_configured(self) -> bool:
        return any(not is_placeholder_secret(key.get_secret_value()) for key in self.api_keys)

    def safe_summary(self) -> dict[str, object]:
        """Resumo operacional seguro para health check e logs (sem expor segredos)."""
        return {
            "gemini_configured": self.is_gemini_configured,
            "gemini_model": self.gemini_model,
            "ebay_configured": self.is_ebay_configured,
            "ebay_marketplace_id": self.ebay_marketplace_id,
            "ebay_daily_quota_limit": self.ebay_daily_quota_limit,
            "usd_brl_rate_source": self.usd_brl_rate_source,
            "usd_brl_rate_updated_at": self.usd_brl_rate_updated_at,
            "embedding_model_name": self.embedding_model_name,
            "embedding_backend": self.embedding_backend,
            "embedding_local_files_only": self.embedding_local_files_only,
            "embedding_cache_dir_configured": self.embedding_cache_dir is not None,
            "api_auth_configured": self.is_api_auth_configured,
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "rate_limit_window_seconds": self.rate_limit_window_seconds,
            "max_request_body_bytes": self.max_request_body_bytes,
            "search_default_limit": self.search_default_limit,
            "search_max_limit": self.search_max_limit,
            "chat_max_history_messages": self.chat_max_history_messages,
            "chat_max_message_chars": self.chat_max_message_chars,
            "chat_max_history_total_chars": self.chat_max_history_total_chars,
            "embedding_preload": self.embedding_preload,
            "log_level": self.log_level,
        }


def load_settings(
    *,
    environ: Mapping[str, str] | None = None,
    load_dotenv_file: bool = False,
) -> Settings:
    """Carrega Settings sob demanda sem efeitos colaterais no import do módulo."""
    if environ is None:
        if load_dotenv_file:
            load_dotenv(override=False)
        env: Mapping[str, str] = os.environ
    else:
        env = environ

    gemini_api_key = _clean_env_value(env, "GEMINI_API_KEY")
    ebay_client_secret = _clean_env_value(env, "EBAY_CLIENT_SECRET")

    return Settings(
        gemini_api_key=SecretStr(gemini_api_key) if gemini_api_key is not None else None,
        gemini_model=_clean_env_value(env, "GEMINI_MODEL") or "gemini-3.5-flash-lite",
        ebay_client_id=_clean_env_value(env, "EBAY_CLIENT_ID"),
        ebay_client_secret=SecretStr(ebay_client_secret) if ebay_client_secret is not None else None,
        ebay_marketplace_id=_clean_env_value(env, "EBAY_MARKETPLACE_ID") or "EBAY_US",
        ebay_daily_quota_limit=_parse_int_env(env, "EBAY_DAILY_QUOTA_LIMIT", 5000),
        usd_brl_rate=_parse_decimal_env(env, "USD_BRL_RATE", Decimal("5.50")),
        usd_brl_rate_updated_at=_clean_env_value(env, "USD_BRL_RATE_UPDATED_AT") or "unknown",
        usd_brl_rate_source=_clean_env_value(env, "USD_BRL_RATE_SOURCE") or "env_fixed_rate",
        embedding_model_name=_clean_env_value(env, "BUSCAAI_EMBEDDING_MODEL") or "intfloat/multilingual-e5-small",
        embedding_backend=_clean_env_value(env, "BUSCAAI_EMBEDDING_BACKEND") or "sentence_transformers",
        embedding_batch_size=_parse_int_env(env, "BUSCAAI_EMBEDDING_BATCH_SIZE", 32),
        embedding_local_files_only=_parse_bool_env(env, "BUSCAAI_EMBEDDING_LOCAL_FILES_ONLY", False),
        embedding_cache_dir=_clean_env_value(env, "BUSCAAI_EMBEDDING_CACHE_DIR"),
        dedup_probable_threshold=_parse_float_env(env, "BUSCAAI_DEDUP_PROBABLE_THRESHOLD", 0.86),
        dedup_similar_threshold=_parse_float_env(env, "BUSCAAI_DEDUP_SIMILAR_THRESHOLD", 0.68),
        api_keys=_parse_api_keys(env, "BUSCAAI_API_KEYS"),
        rate_limit_per_minute=_parse_int_env(env, "BUSCAAI_RATE_LIMIT_PER_MINUTE", 30),
        rate_limit_window_seconds=_parse_int_env(env, "BUSCAAI_RATE_LIMIT_WINDOW_SECONDS", 60),
        max_request_body_bytes=_parse_int_env(env, "BUSCAAI_MAX_REQUEST_BODY_BYTES", 65_536),
        search_default_limit=_parse_int_env(env, "BUSCAAI_SEARCH_DEFAULT_LIMIT", 10),
        search_max_limit=_parse_int_env(env, "BUSCAAI_SEARCH_MAX_LIMIT", 20),
        chat_max_history_messages=_parse_int_env(env, "BUSCAAI_CHAT_MAX_HISTORY_MESSAGES", 12),
        chat_max_message_chars=_parse_int_env(env, "BUSCAAI_CHAT_MAX_MESSAGE_CHARS", 500),
        chat_max_history_total_chars=_parse_int_env(env, "BUSCAAI_CHAT_MAX_HISTORY_TOTAL_CHARS", 4000),
        embedding_preload=_parse_bool_env(env, "BUSCAAI_EMBEDDING_PRELOAD", False),
        log_level=_clean_env_value(env, "BUSCAAI_LOG_LEVEL") or "INFO",
    )
