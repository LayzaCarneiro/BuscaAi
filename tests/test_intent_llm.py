"""Testes unitários da Etapa 3 para IntentExtractor e merge_explicit_budget."""

from __future__ import annotations

import io
import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from google.genai import errors as genai_errors
from pydantic import ValidationError

from buscai.config import load_settings
from buscai.observability import configure_logging
from userintent import (
    IntentExtractionError,
    IntentExtractor,
    LLMUnavailableError,
    PriceConstraint,
    UserIntent,
    merge_explicit_budget,
)


class _StubModels:
    def __init__(self, handler) -> None:
        self._handler = handler
        self.calls: list[dict[str, object]] = []

    def generate_content(self, *, model: str, contents: str, config: object) -> object:
        self.calls.append({"model": model, "contents": contents, "config": config})
        return self._handler(model=model, contents=contents, config=config)


class _StubClient:
    def __init__(self, handler) -> None:
        self.models = _StubModels(handler)


def test_intent_extractor_from_settings_timeout_and_repr() -> None:
    settings_missing = load_settings(environ={})
    with pytest.raises(IntentExtractionError, match="GEMINI_API_KEY"):
        IntentExtractor.from_settings(settings_missing)

    secret = "AIzaFakeSecretKey1234567890"
    settings_ok = load_settings(
        environ={
            "GEMINI_API_KEY": secret,
            "GEMINI_MODEL": "gemini-3.5-flash-lite",
        }
    )
    stub_client = _StubClient(
        lambda **_kwargs: SimpleNamespace(
            parsed=UserIntent(category="notebook", query="notebook Lenovo"),
            text=None,
        )
    )
    extractor = IntentExtractor.from_settings(
        settings_ok,
        client=stub_client,
        timeout_seconds=7.5,
        max_retries=1,
    )
    assert secret not in repr(extractor)

    result = extractor.extract("  Quero um notebook Lenovo  ", request_id="req-llm-1")
    assert result.category == "notebook"
    cfg = stub_client.models.calls[0]["config"]
    assert getattr(cfg, "http_options").timeout == 7500


def test_intent_extractor_supports_conversation_history_and_validates_inputs() -> None:
    stub = _StubClient(
        lambda **_kwargs: SimpleNamespace(
            parsed=UserIntent(
                category="notebook",
                brands=["Lenovo"],
                price=PriceConstraint(max_price=4500, is_hard_constraint=True, currency="BRL"),
                query="notebook Lenovo",
            ),
            text=None,
        )
    )
    extractor = IntentExtractor(model="gemini-3.5-flash-lite", client=stub)

    intent = extractor.extract(
        "Até R$ 4.500",
        conversation_history=[
            {"role": "user", "content": "Quero um notebook Lenovo"},
            {"role": "assistant", "content": "Qual é o seu orçamento máximo?"},
        ],
    )
    assert intent.price.max_price == 4500
    sent_contents = str(stub.models.calls[0]["contents"])
    assert "Histórico recente da conversa:" in sent_contents
    assert "Usuário: Quero um notebook Lenovo" in sent_contents
    assert "Assistente: Qual é o seu orçamento máximo?" in sent_contents
    assert "Mensagem atual do usuário:\nAté R$ 4.500" in sent_contents

    with pytest.raises(ValueError, match="vazia"):
        extractor.extract("   ")

    for bad_history in (
        "texto solto",
        [{"role": "system", "content": "x"}],
        [{"role": "user", "content": "   "}],
        [{"role": 123, "content": "oi"}],
    ):
        with pytest.raises(ValueError):
            extractor.extract("notebook", conversation_history=bad_history)  # type: ignore[arg-type]


def test_intent_extractor_retries_transient_errors_and_sanitizes_logs() -> None:
    attempts = 0
    sleeps: list[float] = []
    secret = "AIzaFakeSecretKey999999999"

    def flaky_handler(**_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise genai_errors.APIError(
                503,
                {"error": {"message": f"overloaded key={secret}", "status": "UNAVAILABLE"}},
            )
        if attempts == 2:
            raise httpx.ReadTimeout("read timeout")
        payload = json.dumps({"category": "monitor", "query": "monitor 4K", "required_features": ["4K"]})
        return SimpleNamespace(parsed=None, text=payload)

    log_stream = io.StringIO()
    logger = configure_logging("INFO", stream=log_stream, logger_name="buscai.test.llm_retry")
    extractor = IntentExtractor(
        model="gemini-3.5-flash-lite",
        client=_StubClient(flaky_handler),
        max_retries=2,
        base_backoff_seconds=0.25,
        max_backoff_seconds=2.0,
        sleep=sleeps.append,
        logger=logger,
    )

    intent = extractor.extract("Quero monitor 4K", request_id="req-retry-1")
    assert intent.category == "monitor"
    assert attempts == 3
    assert sleeps == [0.25, 0.5]

    logs_raw = log_stream.getvalue()
    assert secret not in logs_raw
    assert "intent_extraction_retry" in logs_raw
    assert "intent_extraction_completed" in logs_raw


def test_intent_extractor_exhausts_retries_and_does_not_retry_non_transient_or_validation() -> None:
    sleeps: list[float] = []
    log_stream = io.StringIO()
    logger = configure_logging("INFO", stream=log_stream, logger_name="buscai.test.llm_fail")
    secret = "AIzaSecretLeakAttempt12345"

    def always_timeout(**_kwargs):
        raise TimeoutError(f"timeout api_key={secret}")

    timeout_extractor = IntentExtractor(
        model="gemini-3.5-flash-lite",
        client=_StubClient(always_timeout),
        max_retries=1,
        base_backoff_seconds=0.1,
        max_backoff_seconds=1.0,
        sleep=sleeps.append,
        logger=logger,
    )
    with pytest.raises(LLMUnavailableError) as exc_info:
        timeout_extractor.extract("notebook", request_id="req-to-1")
    assert secret not in str(exc_info.value)
    assert secret not in log_stream.getvalue()
    assert sleeps == [0.1]

    # Erro 403 não é transitório: não deve executar retries
    auth_calls = 0

    def forbidden(**_kwargs):
        nonlocal auth_calls
        auth_calls += 1
        raise genai_errors.APIError(403, {"error": {"message": "Permission denied", "status": "PERMISSION_DENIED"}})

    auth_extractor = IntentExtractor(
        model="gemini-3.5-flash-lite",
        client=_StubClient(forbidden),
        max_retries=2,
        sleep=sleeps.append,
    )
    with pytest.raises(IntentExtractionError) as auth_exc:
        auth_extractor.extract("notebook")
    assert not isinstance(auth_exc.value, LLMUnavailableError)
    assert auth_calls == 1

    # JSON inválido na resposta não faz retry e propaga ValidationError
    json_calls = 0

    def invalid_json(**_kwargs):
        nonlocal json_calls
        json_calls += 1
        return SimpleNamespace(parsed=None, text="{broken json")

    json_extractor = IntentExtractor(
        model="gemini-3.5-flash-lite",
        client=_StubClient(invalid_json),
        max_retries=2,
    )
    with pytest.raises(ValidationError):
        json_extractor.extract("notebook")
    assert json_calls == 1


def test_intent_extractor_rejects_invalid_constructor_params() -> None:
    stub = _StubClient(lambda **_k: SimpleNamespace(parsed=None, text=None))
    with pytest.raises(ValueError, match="timeout_seconds"):
        IntentExtractor(model="gemini-3.5-flash-lite", client=stub, timeout_seconds=0)
    with pytest.raises(ValueError, match="max_retries"):
        IntentExtractor(model="gemini-3.5-flash-lite", client=stub, max_retries=-1)
    with pytest.raises(ValueError, match="max_backoff_seconds"):
        IntentExtractor(
            model="gemini-3.5-flash-lite",
            client=stub,
            base_backoff_seconds=2.0,
            max_backoff_seconds=1.0,
        )


def test_merge_explicit_budget_precedence_and_edge_cases() -> None:
    base_no_price = UserIntent(category="notebook", query="notebook Lenovo")
    assert merge_explicit_budget(base_no_price, None) is base_no_price

    # Texto sem teto recebe orçamento explícito rígido em BRL
    applied = merge_explicit_budget(base_no_price, Decimal("4500.00"))
    assert applied.price.max_price == 4500.0
    assert applied.price.max_inclusive is True
    assert applied.price.is_hard_constraint is True
    assert applied.price.currency == "BRL"
    assert base_no_price.price.max_price is None  # imutável

    # Texto com teto flexível (is_hard_constraint=False) é substituído pelo orçamento explícito rígido
    soft_intent = UserIntent(
        category="notebook",
        query="notebook",
        price=PriceConstraint(max_price=4000, is_hard_constraint=False, currency="BRL"),
    )
    merged_soft = merge_explicit_budget(soft_intent, 6000)
    assert merged_soft.price.max_price == 6000.0
    assert merged_soft.price.is_hard_constraint is True

    # Texto com teto rígido aplica o mais restritivo min(texto, budget_brl)
    hard_intent = UserIntent(
        category="monitor",
        query="monitor 4K",
        price=PriceConstraint(max_price=2500, max_inclusive=False, is_hard_constraint=True, currency="BRL"),
    )
    tighter_param = merge_explicit_budget(hard_intent, 2000)
    assert tighter_param.price.max_price == 2000.0
    assert tighter_param.price.max_inclusive is True

    tighter_text = merge_explicit_budget(hard_intent, 3000)
    assert tighter_text.price.max_price == 2500.0
    assert tighter_text.price.max_inclusive is False

    same_cap = merge_explicit_budget(hard_intent, 2500)
    assert same_cap.price.max_price == 2500.0
    assert same_cap.price.max_inclusive is False

    # Conflito entre mínimo do texto e orçamento máximo explícito
    min_only = UserIntent(
        category="smartphone",
        query="smartphone",
        price=PriceConstraint(min_price=3000, is_hard_constraint=True, currency="BRL"),
    )
    with pytest.raises(ValueError, match="menor que o valor mínimo"):
        merge_explicit_budget(min_only, 2500)

    # Valores inválidos de budget
    for bad in (0, -100, True, "NaN", "abc"):
        with pytest.raises(ValueError):
            merge_explicit_budget(base_no_price, bad)  # type: ignore[arg-type]
