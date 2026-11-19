"""Testes de carregamento, validação e proteção de segredos em buscai.config."""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from buscai.config import Settings, load_settings


def test_load_settings_uses_safe_defaults_when_env_is_empty() -> None:
    settings = load_settings(environ={})

    assert settings.is_gemini_configured is False
    assert settings.is_ebay_configured is False
    assert settings.is_api_auth_configured is False
    assert settings.gemini_model == "gemini-3.5-flash-lite"
    assert settings.ebay_marketplace_id == "EBAY_US"
    assert settings.ebay_daily_quota_limit == 5000
    assert settings.usd_brl_rate == Decimal("5.50")
    assert settings.usd_brl_rate_source == "env_fixed_rate"
    assert settings.embedding_model_name == "intfloat/multilingual-e5-small"
    assert settings.embedding_backend == "sentence_transformers"
    assert settings.embedding_batch_size == 32
    assert settings.dedup_probable_threshold == 0.86
    assert settings.dedup_similar_threshold == 0.68
    assert settings.rate_limit_per_minute == 30
    assert settings.log_level == "INFO"


def test_load_settings_parses_all_values_and_api_keys() -> None:
    settings = load_settings(
        environ={
            "GEMINI_API_KEY": " gem_dummy_123 ",
            "GEMINI_MODEL": " gemini-2.5-pro ",
            "EBAY_CLIENT_ID": " ebay_id_123 ",
            "EBAY_CLIENT_SECRET": " ebay_secret_456 ",
            "EBAY_MARKETPLACE_ID": " ebay_us ",
            "EBAY_DAILY_QUOTA_LIMIT": "4500",
            "USD_BRL_RATE": "5.75",
            "USD_BRL_RATE_UPDATED_AT": "2026-11-19",
            "USD_BRL_RATE_SOURCE": "manual_env",
            "BUSCAAI_EMBEDDING_MODEL": " sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 ",
            "BUSCAAI_EMBEDDING_BACKEND": " fastembed ",
            "BUSCAAI_EMBEDDING_BATCH_SIZE": "16",
            "BUSCAAI_DEDUP_PROBABLE_THRESHOLD": "0.90",
            "BUSCAAI_DEDUP_SIMILAR_THRESHOLD": "0.70",
            "BUSCAAI_API_KEYS": " key_a , , key_b ",
            "BUSCAAI_RATE_LIMIT_PER_MINUTE": "60",
            "BUSCAAI_LOG_LEVEL": "debug",
        }
    )

    assert settings.is_gemini_configured is True
    assert settings.is_ebay_configured is True
    assert settings.is_api_auth_configured is True
    assert settings.gemini_model == "gemini-2.5-pro"
    assert settings.ebay_client_id == "ebay_id_123"
    assert settings.ebay_marketplace_id == "EBAY_US"
    assert settings.ebay_daily_quota_limit == 4500
    assert settings.usd_brl_rate == Decimal("5.75")
    assert settings.usd_brl_rate_updated_at == "2026-11-19"
    assert settings.usd_brl_rate_source == "manual_env"
    assert settings.embedding_model_name == "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    assert settings.embedding_backend == "fastembed"
    assert settings.embedding_batch_size == 16
    assert settings.dedup_probable_threshold == 0.90
    assert settings.dedup_similar_threshold == 0.70
    assert settings.safe_summary()["embedding_backend"] == "fastembed"
    assert [k.get_secret_value() for k in settings.api_keys] == ["key_a", "key_b"]
    assert settings.rate_limit_per_minute == 60
    assert settings.log_level == "DEBUG"


@pytest.mark.parametrize(
    "env",
    [
        {"USD_BRL_RATE": "0"},
        {"USD_BRL_RATE": "-1.5"},
        {"USD_BRL_RATE": "abc"},
        {"USD_BRL_RATE": "NaN"},
        {"EBAY_DAILY_QUOTA_LIMIT": "0"},
        {"EBAY_DAILY_QUOTA_LIMIT": "dez"},
        {"BUSCAAI_RATE_LIMIT_PER_MINUTE": "0"},
        {"BUSCAAI_LOG_LEVEL": "INVALID"},
        {"BUSCAAI_EMBEDDING_BACKEND": "openai"},
        {"BUSCAAI_EMBEDDING_BATCH_SIZE": "0"},
        {"BUSCAAI_DEDUP_PROBABLE_THRESHOLD": "1.2"},
        {"BUSCAAI_DEDUP_PROBABLE_THRESHOLD": "NaN"},
        {"BUSCAAI_DEDUP_PROBABLE_THRESHOLD": "0.60", "BUSCAAI_DEDUP_SIMILAR_THRESHOLD": "0.80"},
    ],
)
def test_load_settings_rejects_invalid_values(env: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        load_settings(environ=env)


def test_secrets_are_not_exposed_in_repr_str_or_safe_summary() -> None:
    secret_gem = "GEM_SUPER_SECRET_XYZ"
    secret_ebay = "EBAY_SUPER_SECRET_ABC"
    secret_api = "API_KEY_SUPER_SECRET_999"

    settings = load_settings(
        environ={
            "GEMINI_API_KEY": secret_gem,
            "EBAY_CLIENT_ID": "public_client_id",
            "EBAY_CLIENT_SECRET": secret_ebay,
            "BUSCAAI_API_KEYS": secret_api,
        }
    )

    serialized = f"{repr(settings)} {str(settings)} {settings.safe_summary()}"
    assert secret_gem not in serialized
    assert secret_ebay not in serialized
    assert secret_api not in serialized
    assert isinstance(settings, Settings)


def test_userintent_llm_does_not_call_load_dotenv_at_import_time() -> None:
    llm_path = Path(__file__).resolve().parents[1] / "src" / "userintent" / "llm.py"
    tree = ast.parse(llm_path.read_text(encoding="utf-8"))

    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            if isinstance(func, ast.Name) and func.id == "load_dotenv":
                raise AssertionError("userintent.llm não deve chamar load_dotenv() no nível de módulo.")
            if isinstance(func, ast.Attribute) and func.attr == "load_dotenv":
                raise AssertionError("userintent.llm não deve chamar load_dotenv() no nível de módulo.")
