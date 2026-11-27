"""Testes offline de contrato HTTP, schemas estritos e cenários de POST /v1/search."""

from __future__ import annotations

import math
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from buscai.api import create_app
from buscai.api.schemas import SearchRequest
from buscai.catalog.errors import (
    CatalogError,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from buscai.catalog_registry import load_default_registry
from buscai.config import load_settings
from buscai.orchestration import OrchestrationDependencies, SearchOrchestrator
from buscai.pricing import CurrencyConverter, FixedEnvExchangeRateProvider
from buscai.ranking.pipeline import RankingPipeline
from buscai.search.models import Candidate, SearchQuery, SearchResponse, SellerInfo, ShippingInfo
from buscai.search.query_builder import QueryBuilder
from buscai.search.quota import QuotaGuard, RequestCallBudget
from buscai.semantic.embeddings import (
    DeterministicStubEmbeddingProvider,
    EmbeddingModelLoadError,
    EmbeddingService,
)
from buscai.semantic.pipeline import SemanticSearchProcessor
from buscai.validation.pipeline import ValidationPipeline
from userintent.llm import IntentExtractionError, LLMUnavailableError
from userintent.schema import PriceConstraint, UserIntent

_API_KEY = "buscai_test_key_search_001"


class _ScenarioExtractor:
    def __init__(self, mode: str = "notebook") -> None:
        self.mode = mode

    def extract(self, user_text: str) -> UserIntent:
        if self.mode == "clarify":
            return UserIntent(
                category=None,
                clarification_needed=True,
                clarification_question="Qual produto você procura?",
                query=user_text,
            )
        if self.mode == "invalid_plan":
            return UserIntent(category=None, query="   ")
        if self.mode == "llm_unavailable":
            raise LLMUnavailableError("Gemini offline com segredo GEMINI_API_KEY=segredo123")
        if self.mode == "llm_error":
            raise IntentExtractionError("Erro bruto interno")
        return UserIntent(
            category="notebook",
            brands=["Lenovo"],
            required_features=["16GB RAM"],
            query=user_text,
        )


class _ScenarioBackend:
    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode

    def search(
        self,
        query: SearchQuery,
        *,
        budget: RequestCallBudget | None = None,
        request_id: str | None = None,
    ) -> SearchResponse:
        if budget is not None:
            budget.search_calls_used += 1
        if self.mode == "empty":
            return SearchResponse(candidates=(), total_estimated=0, limit=query.limit, offset=0, calls_used=1)
        if self.mode == "timeout":
            raise ProviderTimeoutError("Timeout para https://api.ebay.com")
        if self.mode == "unavailable":
            raise ProviderUnavailableError("503 eBay")
        if self.mode == "rate_limit":
            raise ProviderRateLimitError("429 eBay", retry_after_seconds=12.0)
        if self.mode == "auth":
            raise ProviderAuthError("Token inválido")
        if self.mode == "catalog":
            raise CatalogError("Payload inesperado")
        if self.mode == "runtime_crash":
            raise RuntimeError("SENTINEL_CRASH_PAYLOAD_DO_NOT_EXPOSE")
        if self.mode == "all_rejected":
            # Todos têm marca excluída ou preço muito acima do orçamento rígido.
            cand = Candidate(
                item_id="rej-1",
                source="synthetic",
                title="Lenovo ThinkPad X1 16GB RAM 512GB SSD",
                price=Decimal("2500.00"),
                currency="USD",
                condition="New",
                categories=("notebook",),
                brand="Lenovo",
                aspects={"RAM Size": ("16 GB",)},
            )
            return SearchResponse(candidates=(cand,), total_estimated=1, limit=query.limit, offset=0, calls_used=1)

        c1 = Candidate(
            item_id="item-1",
            source="synthetic",
            title="Lenovo ThinkPad T14 16GB RAM 512GB SSD Laptop",
            price=Decimal("500.00"),
            currency="USD",
            condition="New",
            categories=("notebook",),
            brand="Lenovo",
            model="ThinkPad T14",
            aspects={"RAM Size": ("16 GB",), "SSD Capacity": ("512 GB",)},
            item_web_url="https://example.invalid/item/1",
            seller=SellerInfo(username="trusted_seller", feedback_score=800, feedback_percentage=Decimal("99.4")),
            shipping=ShippingInfo(cost=Decimal("0"), currency="USD", free_shipping=True, estimated_max_days=5),
            details_loaded=True,
        )
        c2 = Candidate(
            item_id="item-2",
            source="synthetic",
            title="Lenovo ThinkPad T14 16GB RAM 512GB SSD Laptop",
            price=Decimal("520.00"),
            currency="USD",
            condition="New",
            categories=("notebook",),
            brand="Lenovo",
            model="ThinkPad T14",
            aspects={"RAM Size": ("16 GB",), "SSD Capacity": ("512 GB",)},
            item_web_url="https://example.invalid/item/2",
            shipping=ShippingInfo(cost=Decimal("15.00"), currency="USD", free_shipping=False, estimated_max_days=7),
            seller=SellerInfo(username="second_seller", feedback_score=250, feedback_percentage=Decimal("97.0")),
            details_loaded=True,
        )
        c3 = Candidate(
            item_id="item-3",
            source="synthetic",
            title="Lenovo IdeaPad 3 16GB RAM 512GB SSD Laptop",
            price=Decimal("480.00"),
            currency="USD",
            condition="Used",
            categories=("notebook",),
            brand="Lenovo",
            model="IdeaPad 3",
            aspects={"RAM Size": ("16 GB",)},
            shipping=None,
            seller=None,
            details_loaded=False,
        )
        return SearchResponse(candidates=(c1, c2, c3), total_estimated=3, limit=query.limit, offset=0, calls_used=1)

    def get_item_details(self, item_id: str, **kwargs) -> Candidate | None:
        return None


def _build_app(
    *,
    extractor_mode: str = "notebook",
    backend_mode: str = "ok",
    failing_embeddings: bool = False,
):
    converter = CurrencyConverter(
        FixedEnvExchangeRateProvider(rate=Decimal("5.50"), updated_at="2026-11-25", source="env_fixed_rate")
    )
    if failing_embeddings:
        provider = DeterministicStubEmbeddingProvider(
            model_loader=lambda: (_ for _ in ()).throw(EmbeddingModelLoadError("Falha ao carregar modelo"))
        )
        emb = EmbeddingService(provider)
    else:
        provider = DeterministicStubEmbeddingProvider()
        emb = EmbeddingService(provider)
        emb.embed_queries(["warmup"])

    deps = OrchestrationDependencies(
        extractor=_ScenarioExtractor(extractor_mode),
        query_builder=QueryBuilder(currency_converter=converter),
        search_backend=_ScenarioBackend(backend_mode),
        semantic_processor=SemanticSearchProcessor(emb),
        validation_pipeline=ValidationPipeline(currency_converter=converter),
        ranking_pipeline=RankingPipeline(converter=converter),
        quota_guard=QuotaGuard(),
    )
    orch = SearchOrchestrator(deps)
    settings = load_settings(
        environ={
            "BUSCAAI_API_KEYS": _API_KEY,
            "USD_BRL_RATE": "5.50",
            "USD_BRL_RATE_UPDATED_AT": "2026-11-25",
            "BUSCAAI_SEARCH_MAX_LIMIT": "20",
        }
    )
    return create_app(settings=settings, orchestrator=orch, preload_embeddings=False)


def test_search_success_with_budget_and_full_contract() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post(
            "/v1/search",
            headers={"X-API-Key": _API_KEY, "X-Request-ID": "req-search-ok"},
            json={
                "query": "notebook lenovo 16gb ram",
                "budget_brl": 4000,
                "profile_id": "best_value",
                "limit": 5,
            },
        )

    assert resp.status_code == 200
    assert resp.headers["X-Request-ID"] == "req-search-ok"
    body = resp.json()
    assert body["status"] == "ok"
    assert body["request_id"] == "req-search-ok"
    assert body["profile_id"] == "best_value"
    assert body["detected_category"] == "notebook"
    assert body["total_products"] >= 1
    assert body["total_offers"] >= 2
    assert body["exchange_rate_info"]["is_estimate"] is True
    assert body["exchange_rate_info"]["rate"] == "5.50"

    group = body["products"][0]
    assert group["rank"] == 1
    assert group["score_breakdown"]["final_score"] > 0
    assert group["explanation"]["summary"]
    assert len(group["offers"]) >= 1

    offer = group["offers"][0]
    assert offer["estimated_price_brl"]["is_estimate"] is True
    assert offer["estimated_price_brl"]["currency"] == "BRL"
    assert offer["condition"] is not None
    assert "criteria" in offer["validation"]
    assert "unknown_criteria" in offer["validation"]


@pytest.mark.parametrize(
    "profile_id",
    [
        "best_value",
        "best_performance",
        "lowest_price",
        "professional_use",
        "study",
        "everyday_use",
    ],
)
def test_search_supports_all_six_profiles_and_without_budget(profile_id: str) -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post(
            "/v1/search",
            headers={"X-API-Key": _API_KEY},
            json={"query": "notebook lenovo 16gb ram", "profile_id": profile_id},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["profile_id"] == profile_id


def test_search_clarification_empty_and_all_rejected() -> None:
    with TestClient(_build_app(extractor_mode="clarify")) as client:
        clarify = client.post(
            "/v1/search",
            headers={"X-API-Key": _API_KEY},
            json={"query": "quero um presente"},
        )
    assert clarify.status_code == 200
    assert clarify.json()["status"] == "clarification_needed"
    assert clarify.json()["clarification_needed"] is True
    assert clarify.json()["clarification_question"] == "Qual produto você procura?"

    with TestClient(_build_app(backend_mode="empty")) as client:
        empty = client.post(
            "/v1/search",
            headers={"X-API-Key": _API_KEY},
            json={"query": "notebook lenovo"},
        )
    assert empty.status_code == 200
    assert empty.json()["status"] == "no_results"
    assert empty.json()["products"] == []

    with TestClient(_build_app(backend_mode="all_rejected")) as client:
        rejected = client.post(
            "/v1/search",
            headers={"X-API-Key": _API_KEY},
            json={"query": "notebook lenovo ate 500 reais", "budget_brl": 500},
        )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "all_rejected"
    assert rejected.json()["rejected_count"] >= 1


@pytest.mark.parametrize(
    ("extractor_mode", "backend_mode", "failing_emb", "expected_status", "expected_code"),
    [
        ("notebook", "timeout", False, 503, "upstream_timeout"),
        ("notebook", "unavailable", False, 503, "upstream_unavailable"),
        ("notebook", "rate_limit", False, 503, "upstream_rate_limit"),
        ("notebook", "auth", False, 502, "upstream_auth_error"),
        ("notebook", "catalog", False, 502, "upstream_bad_response"),
        ("llm_unavailable", "ok", False, 503, "llm_unavailable"),
        ("llm_error", "ok", False, 502, "llm_error"),
        ("notebook", "ok", True, 503, "embeddings_unavailable"),
        ("notebook", "runtime_crash", False, 500, "internal_error"),
    ],
)
def test_search_error_mapping_never_confuses_failures_with_no_results_or_leaks_exc(
    extractor_mode: str,
    backend_mode: str,
    failing_emb: bool,
    expected_status: int,
    expected_code: str,
) -> None:
    app = _build_app(
        extractor_mode=extractor_mode,
        backend_mode=backend_mode,
        failing_embeddings=failing_emb,
    )
    with TestClient(app) as client:
        resp = client.post(
            "/v1/search",
            headers={"X-API-Key": _API_KEY},
            json={"query": "notebook lenovo 16gb"},
        )

    assert resp.status_code == expected_status
    body = resp.json()
    assert body["error"]["code"] == expected_code
    assert body["error"]["request_id"]
    assert "SENTINEL_CRASH_PAYLOAD_DO_NOT_EXPOSE" not in resp.text
    assert "segredo123" not in resp.text
    if expected_code == "upstream_rate_limit":
        assert resp.headers["Retry-After"] == "12"


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"query": "notebook", "extra_field": 123},
        {"query": ""},
        {"query": "   "},
        {"query": "a" * 501},
        {"query": 123},
        {"query": "notebook", "budget_brl": 0},
        {"query": "notebook", "budget_brl": -100},
        {"query": "notebook", "budget_brl": True},
        {"query": "notebook", "budget_brl": "NaN"},
        {"query": "notebook", "budget_brl": "Infinity"},
        {"query": "notebook", "profile_id": "inexistente_xyz"},
        {"query": "notebook", "profile_id": 42},
        {"query": "notebook", "limit": 0},
        {"query": "notebook", "limit": -1},
        {"query": "notebook", "limit": True},
        {"query": "notebook", "limit": 21},
        {"query": "notebook", "limit": 1.5},
    ],
)
def test_search_request_schema_validation_and_forbid_extra(bad_payload: dict[str, object]) -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post(
            "/v1/search",
            headers={"X-API-Key": _API_KEY},
            json=bad_payload,
        )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


def test_search_request_rejects_raw_nan_and_inf_floats_and_preserves_d8() -> None:
    for bad_float in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError):
            SearchRequest(query="notebook", budget_brl=bad_float)  # type: ignore[arg-type]

    # Confirma preservação da decisão D8 nos schemas internos de intenção.
    intent = UserIntent(category="notebook", query="laptop", unexpected_llm_field="ok")
    price = PriceConstraint(max_price=1000, unexpected_price_field="ok")
    assert not hasattr(intent, "unexpected_llm_field")
    assert not hasattr(price, "unexpected_price_field")
    assert len(load_default_registry().profiles) == 6


def test_openapi_schema_documents_endpoints_and_security_without_secrets() -> None:
    app = _build_app()
    with TestClient(app) as client:
        spec = client.get("/openapi.json")
    assert spec.status_code == 200
    data = spec.json()
    assert "/v1/health" in data["paths"]
    assert "/v1/search" in data["paths"]
    assert "APIKeyHeader" in data["components"]["securitySchemes"]
    assert _API_KEY not in spec.text
