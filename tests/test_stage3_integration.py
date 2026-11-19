"""Testes integrados da Etapa 3 com mocks (PT-BR -> IntentExtractor -> QueryBuilder -> EbayLiveBackend)."""

from __future__ import annotations

import io
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from buscai.config import load_settings
from buscai.observability import configure_logging
from buscai.search import (
    EbayLiveBackend,
    EbayOAuthClient,
    IntentQueryPlanResult,
    QuotaGuard,
    build_ebay_search_params,
    build_search_plan_from_text,
)
from buscai.search.query_builder import QueryBuilder
from userintent import IntentExtractionError, IntentExtractor, UserIntent


class _StubModels:
    def __init__(self, handler) -> None:
        self._handler = handler
        self.calls: list[dict[str, object]] = []

    def generate_content(self, *, model: str, contents: str, config: object) -> object:
        self.calls.append({"model": model, "contents": contents, "config": config})
        return self._handler(model=model, contents=contents, config=config)


class _StubGenAIClient:
    def __init__(self, handler) -> None:
        self.models = _StubModels(handler)


_SIMULATED_CASES = [
    {
        "id": "caso_1_notebook",
        "text": "Quero um notebook Lenovo com até R$ 5.000 para programação e estudos.",
        "llm_payload": {
            "category": "notebook",
            "price": {
                "min_price": None,
                "max_price": 5000,
                "min_inclusive": True,
                "max_inclusive": True,
                "is_hard_constraint": True,
                "currency": "BRL",
            },
            "brands": ["Lenovo"],
            "models": [],
            "excluded_brands": [],
            "use_cases": ["programação", "estudos"],
            "profile_context": [],
            "required_features": [],
            "preferred_features": [],
            "excluded_features": [],
            "condition": None,
            "query": "notebook Lenovo para programação e estudos",
            "clarification_needed": False,
            "clarification_question": None,
        },
        "expected_category": "notebook",
        "expected_brand": "Lenovo",
        "expected_query_contains": ("Lenovo", "laptop"),
        "expected_max_usd": Decimal("909.09"),
    },
    {
        "id": "caso_2_smartphone_iphone",
        "text": "Procuro um iPhone com pelo menos 256 GB de armazenamento.",
        "llm_payload": {
            "category": "smartphone",
            "price": {
                "min_price": None,
                "max_price": None,
                "min_inclusive": True,
                "max_inclusive": True,
                "is_hard_constraint": False,
                "currency": None,
            },
            "brands": ["Apple"],
            "models": ["iPhone"],
            "excluded_brands": [],
            "use_cases": [],
            "profile_context": [],
            "required_features": ["pelo menos 256 GB de armazenamento"],
            "preferred_features": [],
            "excluded_features": [],
            "condition": None,
            "query": "iPhone com pelo menos 256 GB de armazenamento",
            "clarification_needed": False,
            "clarification_question": None,
        },
        "expected_category": "smartphone",
        "expected_brand": "Apple",
        "expected_query_contains": ("Apple", "iPhone", "256GB"),
        "expected_max_usd": None,
    },
    {
        "id": "caso_3_monitor_4k",
        "text": "Preciso de um monitor 4K para trabalhar.",
        "llm_payload": {
            "category": "monitor",
            "price": {
                "min_price": None,
                "max_price": None,
                "min_inclusive": True,
                "max_inclusive": True,
                "is_hard_constraint": False,
                "currency": None,
            },
            "brands": [],
            "models": [],
            "excluded_brands": [],
            "use_cases": ["trabalho"],
            "profile_context": [],
            "required_features": ["4K"],
            "preferred_features": [],
            "excluded_features": [],
            "condition": None,
            "query": "monitor 4K para trabalho",
            "clarification_needed": False,
            "clarification_question": None,
        },
        "expected_category": "monitor",
        "expected_brand": None,
        "expected_query_contains": ("4K", "monitor"),
        "expected_max_usd": None,
    },
    {
        "id": "caso_4_fones_anc",
        "text": "Quero fones Bluetooth com cancelamento de ruído.",
        "llm_payload": {
            "category": "fone de ouvido",
            "price": {
                "min_price": None,
                "max_price": None,
                "min_inclusive": True,
                "max_inclusive": True,
                "is_hard_constraint": False,
                "currency": None,
            },
            "brands": [],
            "models": [],
            "excluded_brands": [],
            "use_cases": [],
            "profile_context": [],
            "required_features": ["Bluetooth", "cancelamento de ruído"],
            "preferred_features": [],
            "excluded_features": [],
            "condition": None,
            "query": "fone Bluetooth com cancelamento de ruído",
            "clarification_needed": False,
            "clarification_question": None,
        },
        "expected_category": "fone de ouvido",
        "expected_brand": None,
        "expected_query_contains": ("Bluetooth", "noise cancelling", "headphones"),
        "expected_max_usd": None,
    },
    {
        "id": "caso_5_orcamento_smartphone",
        "text": "Quero o melhor smartphone possível sem ultrapassar R$ 3.500.",
        "llm_payload": {
            "category": "smartphone",
            "price": {
                "min_price": None,
                "max_price": 3500,
                "min_inclusive": True,
                "max_inclusive": True,
                "is_hard_constraint": True,
                "currency": "BRL",
            },
            "brands": [],
            "models": [],
            "excluded_brands": [],
            "use_cases": [],
            "profile_context": [],
            "required_features": [],
            "preferred_features": ["melhor possível"],
            "excluded_features": [],
            "condition": None,
            "query": "smartphone melhor possível",
            "clarification_needed": False,
            "clarification_question": None,
        },
        "expected_category": "smartphone",
        "expected_brand": None,
        "expected_query_contains": ("smartphone",),
        "expected_max_usd": Decimal("636.36"),
    },
    {
        "id": "caso_6_info_incompleta",
        "text": "Quero um notebook bom para estudar.",
        "llm_payload": {
            "category": "notebook",
            "price": {
                "min_price": None,
                "max_price": None,
                "min_inclusive": True,
                "max_inclusive": True,
                "is_hard_constraint": False,
                "currency": None,
            },
            "brands": [],
            "models": [],
            "excluded_brands": [],
            "use_cases": ["estudos"],
            "profile_context": [],
            "required_features": [],
            "preferred_features": ["bom"],
            "excluded_features": [],
            "condition": None,
            "query": "notebook bom para estudar",
            "clarification_needed": False,
            "clarification_question": None,
        },
        "expected_category": "notebook",
        "expected_brand": None,
        "expected_query_contains": ("laptop",),
        "expected_max_usd": None,
    },
]


@pytest.mark.parametrize("scenario", _SIMULATED_CASES, ids=[c["id"] for c in _SIMULATED_CASES])
def test_stage3_full_mocked_flow_six_mandatory_scenarios(
    scenario: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    secret_gem = "AIzaStage3MockSecret123456"
    secret_ebay = "EBAY_MOCK_SECRET_XYZ"

    settings = load_settings(
        environ={
            "GEMINI_API_KEY": secret_gem,
            "GEMINI_MODEL": "gemini-3.5-flash-lite",
            "EBAY_CLIENT_ID": "fake_ebay_client_id",
            "EBAY_CLIENT_SECRET": secret_ebay,
            "EBAY_MARKETPLACE_ID": "EBAY_US",
            "USD_BRL_RATE": "5.50",
            "USD_BRL_RATE_UPDATED_AT": "2026-11-19",
            "USD_BRL_RATE_SOURCE": "env_fixed_rate",
        }
    )

    log_stream = io.StringIO()
    logger = configure_logging("INFO", stream=log_stream, logger_name=f"buscai.test.stage3.{scenario['id']}")

    stub_llm = _StubGenAIClient(
        lambda **_kwargs: SimpleNamespace(parsed=None, text=json.dumps(scenario["llm_payload"]))
    )
    extractor = IntentExtractor.from_settings(settings, client=stub_llm, logger=logger)
    query_builder = QueryBuilder.from_settings(settings)

    captured_requests: list[httpx.Request] = []

    def ebay_browse_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={
                "total": 1,
                "limit": 10,
                "offset": 0,
                "itemSummaries": [
                    {
                        "itemId": "v1|555|0",
                        "title": "SENTINEL_EBAY_ITEM_TITLE",
                        "price": {"value": "499.00", "currency": "USD"},
                        "itemWebUrl": "https://ebay.example/SENTINEL_EBAY_URL",
                        "seller": {"username": "SENTINEL_SELLER"},
                    }
                ],
            },
        )

    oauth = EbayOAuthClient.from_settings(
        settings,
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(200, json={"access_token": "SENTINEL_EBAY_TOKEN", "expires_in": 3600})
        ),
        logger=logger,
    )
    guard = QuotaGuard(daily_limit=settings.ebay_daily_quota_limit, logger=logger)
    backend = EbayLiveBackend(
        oauth_client=oauth,
        quota_guard=guard,
        default_marketplace_id=settings.ebay_marketplace_id,
        transport=httpx.MockTransport(ebay_browse_handler),
        logger=logger,
    )

    try:
        plan_result = build_search_plan_from_text(
            str(scenario["text"]),
            extractor=extractor,
            query_builder=query_builder,
            request_id="req-s3-1",
            limit=10,
        )
        assert isinstance(plan_result, IntentQueryPlanResult)
        assert plan_result.clarification_needed is False
        assert plan_result.intent.category == scenario["expected_category"]
        assert plan_result.plan.canonical_category == scenario["expected_category"]
        assert plan_result.plan.is_supported_mvp_category is True
        assert plan_result.intent.condition is None  # nunca inventado

        primary = plan_result.primary_query
        assert primary is not None
        for token in scenario["expected_query_contains"]:  # type: ignore[attr-defined]
            assert token in primary.query_text

        expected_brand = scenario["expected_brand"]
        if expected_brand is None:
            assert plan_result.intent.brands == []
        else:
            assert expected_brand in plan_result.intent.brands

        expected_max_usd = scenario["expected_max_usd"]
        assert primary.max_price_usd == expected_max_usd

        # Confere parâmetros compatíveis com a Browse API antes de enviar ao backend mockado
        browse_params = build_ebay_search_params(primary)
        assert browse_params["q"] == primary.query_text
        assert "buyingOptions:{FIXED_PRICE}" in browse_params["filter"]
        if expected_max_usd is not None:
            assert f"price:[..{expected_max_usd}]," in browse_params["filter"]
            assert "priceCurrency:USD" in browse_params["filter"]
        else:
            assert "price:" not in browse_params["filter"]

        # Caso 5 não deve colocar "melhor possível" na query; Caso 6 não deve inventar specs de "bom"/"estudar"
        if scenario["id"] == "caso_5_orcamento_smartphone":
            assert "melhor" not in primary.query_text.casefold()
            assert plan_result.intent.required_features == []
        if scenario["id"] == "caso_6_info_incompleta":
            assert primary.query_text == "laptop"
            assert plan_result.intent.brands == []
            assert plan_result.intent.required_features == []
            assert "use_case:estudos" in plan_result.plan.ignored_preferences
            assert "subjective:bom" in plan_result.plan.ignored_preferences

        budget = guard.new_request_budget(max_search_calls=1, max_get_item_calls=0)
        response = backend.search(primary, budget=budget, request_id="req-s3-1")
        assert len(captured_requests) == 1
        assert len(response.candidates) == 1
        assert guard.snapshot().used_in_window == 1

        logs_raw = log_stream.getvalue()
        for forbidden in (
            secret_gem,
            secret_ebay,
            "SENTINEL_EBAY_TOKEN",
            "SENTINEL_EBAY_ITEM_TITLE",
            "SENTINEL_EBAY_URL",
            "SENTINEL_SELLER",
        ):
            assert forbidden not in logs_raw
        assert list(tmp_path.rglob("*")) == []
    finally:
        backend.close()
        oauth.close()


def test_stage3_integrated_budget_override_clarification_history_and_errors() -> None:
    settings = load_settings(environ={"USD_BRL_RATE": "5.50"})
    query_builder = QueryBuilder.from_settings(settings)

    # 1) Orçamento explícito (budget_brl) prevalece sobre teto ausente no texto
    stub_no_budget = _StubGenAIClient(
        lambda **_k: SimpleNamespace(
            parsed=UserIntent(category="notebook", brands=["Lenovo"], query="notebook Lenovo"),
            text=None,
        )
    )
    extractor_ok = IntentExtractor(model="gemini-3.5-flash-lite", client=stub_no_budget)
    reconciled = build_search_plan_from_text(
        "Quero um notebook Lenovo",
        extractor=extractor_ok,
        query_builder=query_builder,
        budget_brl=Decimal("4400.00"),
        conversation_history=[{"role": "user", "content": "Olá"}],
    )
    assert reconciled.primary_query is not None
    assert reconciled.primary_query.max_price_usd == Decimal("800.00")
    assert reconciled.intent.price.is_hard_constraint is True

    # 2) Pedido vago dispara clarificação e não planeja chamadas à eBay
    stub_vague = _StubGenAIClient(
        lambda **_k: SimpleNamespace(
            parsed=UserIntent(
                category=None,
                query="presente",
                clarification_needed=True,
                clarification_question="Qual tipo de produto e orçamento você procura?",
            ),
            text=None,
        )
    )
    clarify_res = build_search_plan_from_text(
        "Quero um presente",
        extractor=IntentExtractor(model="gemini-3.5-flash-lite", client=stub_vague),
        query_builder=query_builder,
    )
    assert clarify_res.clarification_needed is True
    assert clarify_res.search_queries == ()

    # 3) Erro de LLM propaga com tipo controlado sem chamar QueryBuilder
    failing_extractor = IntentExtractor(
        model="gemini-3.5-flash-lite",
        client=_StubGenAIClient(lambda **_k: SimpleNamespace(parsed=None, text="")),
        max_retries=0,
    )
    with pytest.raises(IntentExtractionError):
        build_search_plan_from_text(
            "Quero um monitor 4K",
            extractor=failing_extractor,
            query_builder=query_builder,
        )
