"""Unit tests for Stage 7 orchestration nodes, error classification, and conditional routes."""

from __future__ import annotations

from decimal import Decimal

from buscai.catalog.errors import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from buscai.orchestration.nodes import (
    OrchestrationDependencies,
    _classify_error,
    prepare_intent_and_plan_node,
    search_products_node,
)
from buscai.orchestration.routing import (
    route_after_prepare,
    route_after_search,
    route_after_semantics,
    route_after_validation,
)
from buscai.orchestration.state import ExecutionStatus, OrchestrationError, SearchGraphState
from buscai.pricing import CurrencyConverter, FixedEnvExchangeRateProvider
from buscai.ranking.pipeline import RankingPipeline
from buscai.search.models import Candidate, SearchQuery, SearchResponse
from buscai.search.query_builder import QueryBuilder, SearchPlan
from buscai.search.quota import QuotaGuard, RequestCallBudget
from buscai.semantic.embeddings import DeterministicStubEmbeddingProvider, EmbeddingService
from buscai.semantic.pipeline import SemanticProcessingResult, SemanticSearchProcessor
from buscai.validation.models import ValidationPipelineResult
from buscai.validation.pipeline import ValidationPipeline
from userintent.schema import UserIntent


class _CountingExtractor:
    def __init__(self, intent: UserIntent) -> None:
        self._intent = intent
        self.calls = 0

    def extract(self, user_text: str) -> UserIntent:
        self.calls += 1
        return self._intent


class _FakeBackend:
    def __init__(self, candidates_by_call: list[tuple[Candidate, ...]]) -> None:
        self._batches = list(candidates_by_call)
        self.calls = 0

    def search(
        self,
        query: SearchQuery,
        *,
        budget: RequestCallBudget | None = None,
        request_id: str | None = None,
    ) -> SearchResponse:
        if budget is not None:
            budget.search_calls_used += 1
        batch = self._batches[min(self.calls, len(self._batches) - 1)]
        self.calls += 1
        return SearchResponse(
            query=query,
            candidates=batch,
            total_available=len(batch),
            limit=query.limit,
            provider="synthetic",
        )

    def get_item_details(self, item_id: str, **kwargs) -> Candidate | None:
        return None


def _make_deps(
    extractor: _CountingExtractor,
    backend: _FakeBackend,
) -> OrchestrationDependencies:
    converter = CurrencyConverter(FixedEnvExchangeRateProvider(rate=Decimal("5.00")))
    return OrchestrationDependencies(
        extractor=extractor,
        query_builder=QueryBuilder(currency_converter=converter),
        search_backend=backend,
        semantic_processor=SemanticSearchProcessor(
            EmbeddingService(DeterministicStubEmbeddingProvider())
        ),
        validation_pipeline=ValidationPipeline(currency_converter=converter),
        ranking_pipeline=RankingPipeline(converter=converter),
        quota_guard=QuotaGuard(),
    )


def test_prepare_node_calls_extractor_exactly_once_and_handles_clarification() -> None:
    clarify_intent = UserIntent(
        category=None,
        clarification_needed=True,
        clarification_question="Qual categoria de produto você procura?",
        query="quero um aparelho bom",
    )
    extractor = _CountingExtractor(clarify_intent)
    deps = _make_deps(extractor, _FakeBackend([()]))

    update = prepare_intent_and_plan_node(
        {"user_text": "quero um aparelho bom", "request_id": "req-clarify", "limit": 10},
        deps,
    )
    assert extractor.calls == 1
    assert update["llm_calls_used"] == 1
    assert update["status"] is ExecutionStatus.CLARIFICATION_NEEDED
    assert update["clarification_needed"] is True
    assert route_after_prepare(update) == "finalize"


def test_routing_functions_short_circuit_on_errors_empty_or_rejected() -> None:
    err = OrchestrationError(
        node="search_products",
        error_type="ProviderTimeoutError",
        code="provider_timeout",
        safe_message="Timeout",
    )
    assert route_after_prepare({"error": err}) == "finalize"
    assert route_after_prepare({"status": ExecutionStatus.INVALID_PLAN}) == "finalize"
    assert route_after_search({"error": err}) == "finalize"
    assert route_after_search({"status": ExecutionStatus.NO_RESULTS, "search_responses": ()}) == "finalize"
    assert route_after_semantics({"semantic_result": SemanticProcessingResult()}) == "finalize"
    assert route_after_validation({"validation_result": ValidationPipelineResult()}) == "finalize"

    valid_plan = SearchPlan(
        canonical_category="notebook",
        is_supported_mvp_category=True,
        queries=(),
    )
    assert route_after_prepare({"search_plan": valid_plan}) == "finalize"


def test_search_node_preserves_request_budget_and_handles_empty_search() -> None:
    intent = UserIntent(category="notebook", query="notebook para estudos")
    extractor = _CountingExtractor(intent)
    backend = _FakeBackend([()])
    deps = _make_deps(extractor, backend)

    prep = prepare_intent_and_plan_node(
        {"user_text": "notebook para estudos", "request_id": "req-empty", "limit": 10},
        deps,
    )
    assert route_after_prepare(prep) == "search_products"

    search_out = search_products_node(prep, deps)
    assert search_out["status"] is ExecutionStatus.NO_RESULTS
    assert search_out["call_budget"].search_calls_used >= 1
    assert route_after_search(search_out) == "finalize"


def test_classify_error_never_leaks_listing_payloads() -> None:
    secret_title = "SECRET_TITLE_IPHONE_15_URL_https://ebay.com/itm/999"
    for exc, expected_code, expected_retryable in (
        (ProviderRateLimitError(secret_title, retry_after_seconds=12.0), "provider_rate_limit", True),
        (ProviderAuthError(secret_title), "provider_auth_error", False),
        (ProviderTimeoutError(secret_title), "provider_timeout", True),
        (ProviderUnavailableError(secret_title), "provider_unavailable", True),
        (ValueError(secret_title), "invalid_input_or_contract", False),
        (RuntimeError(secret_title), "internal_orchestration_error", False),
    ):
        err = _classify_error("search_products", exc)
        assert err.code == expected_code
        assert err.retryable is expected_retryable
        assert "SECRET_TITLE" not in err.safe_message
        assert "https://ebay.com" not in err.safe_message
