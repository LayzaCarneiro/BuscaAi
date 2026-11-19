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
    dedup_probable_threshold: float = Field(default=0.86, ge=0.0, le=1.0)
    dedup_similar_threshold: float = Field(default=0.68, ge=0.0, le=1.0)
    api_keys: tuple[SecretStr, ...] = ()
    rate_limit_per_minute: int = Field(default=30, ge=1)
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
        return self.gemini_api_key is not None and bool(self.gemini_api_key.get_secret_value().strip())

    @property
    def is_ebay_configured(self) -> bool:
        return (
            bool(self.ebay_client_id and self.ebay_client_id.strip())
            and self.ebay_client_secret is not None
            and bool(self.ebay_client_secret.get_secret_value().strip())
        )

    @property
    def is_api_auth_configured(self) -> bool:
        return any(bool(key.get_secret_value().strip()) for key in self.api_keys)

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
            "api_auth_configured": self.is_api_auth_configured,
            "rate_limit_per_minute": self.rate_limit_per_minute,
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
        dedup_probable_threshold=_parse_float_env(env, "BUSCAAI_DEDUP_PROBABLE_THRESHOLD", 0.86),
        dedup_similar_threshold=_parse_float_env(env, "BUSCAAI_DEDUP_SIMILAR_THRESHOLD", 0.68),
        api_keys=_parse_api_keys(env, "BUSCAAI_API_KEYS"),
        rate_limit_per_minute=_parse_int_env(env, "BUSCAAI_RATE_LIMIT_PER_MINUTE", 30),
        log_level=_clean_env_value(env, "BUSCAAI_LOG_LEVEL") or "INFO",
    )
