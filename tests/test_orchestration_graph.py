"""Tests for Stage 7 compiled StateGraph, conditional paths, concurrency, and memory isolation."""

from __future__ import annotations

import gc
import weakref
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from buscai.catalog.errors import ProviderTimeoutError
from buscai.orchestration import (
    ExecutionStatus,
    OrchestrationDependencies,
    SearchOrchestrator,
)
from buscai.pricing import CurrencyConverter, FixedEnvExchangeRateProvider
from buscai.ranking.pipeline import RankingPipeline
from buscai.search.models import Candidate, SearchQuery, SearchResponse, SellerInfo, ShippingInfo
from buscai.search.query_builder import QueryBuilder
from buscai.search.quota import QuotaGuard, RequestCallBudget
from buscai.semantic.embeddings import DeterministicStubEmbeddingProvider, EmbeddingService
from buscai.semantic.pipeline import SemanticSearchProcessor
from buscai.validation.pipeline import ValidationPipeline
from userintent.schema import PriceConstraint, UserIntent


class _StaticExtractor:
    def __init__(self, intent: UserIntent | Exception) -> None:
        self._intent = intent
        self.calls = 0

    def extract(self, user_text: str) -> UserIntent:
        self.calls += 1
        if isinstance(self._intent, Exception):
            raise self._intent
        return self._intent


class _MappingBackend:
    def __init__(self, fn) -> None:
        self._fn = fn
        self.calls = 0

    def search(
        self,
        query: SearchQuery,
        *,
        budget: RequestCallBudget | None = None,
        request_id: str | None = None,
    ) -> SearchResponse:
        self.calls += 1
        if budget is not None:
            budget.search_calls_used += 1
        return self._fn(query, request_id)

    def get_item_details(self, item_id: str, **kwargs) -> Candidate | None:
        return None


def _build_orchestrator(extractor, backend, *, validation=None, semantic=None, ranking=None) -> SearchOrchestrator:
    converter = CurrencyConverter(FixedEnvExchangeRateProvider(rate=Decimal("5.00")))
    deps = OrchestrationDependencies(
        extractor=extractor,
        query_builder=QueryBuilder(currency_converter=converter),
        search_backend=backend,
        semantic_processor=semantic
        or SemanticSearchProcessor(EmbeddingService(DeterministicStubEmbeddingProvider())),
        validation_pipeline=validation or ValidationPipeline(currency_converter=converter),
        ranking_pipeline=ranking or RankingPipeline(converter=converter),
        quota_guard=QuotaGuard(),
    )
    return SearchOrchestrator(deps)


def test_full_valid_graph_flow_and_single_extraction() -> None:
    intent = UserIntent(
        category="notebook",
        profile="study",
        price=PriceConstraint(max_price=5000.0, currency="BRL", is_hard_constraint=True),
        requirements=["16GB RAM"],
        query="notebook 16gb ram para estudos até 5000 reais",
    )
    extractor = _StaticExtractor(intent)

    def _search(query: SearchQuery, request_id: str | None) -> SearchResponse:
        cand = Candidate(
            item_id="nb-1",
            source="synthetic",
            title="Notebook Lenovo 13.3 16GB RAM SSD 512GB",
            price=Decimal("700.00"),
            currency="USD",
            condition="New",
            categories=("notebook",),
            aspects={"RAM Size": ("16 GB",), "SSD Capacity": ("512 GB",), "Weight": ("1.25 kg",)},
            seller=SellerInfo(username="seller1", feedback_percentage=99.0, feedback_score=900),
            shipping=ShippingInfo(free_shipping=True, estimated_max_days=4),
        )
        return SearchResponse(query=query, candidates=(cand,), total_available=1, limit=query.limit, provider="synthetic")

    orch = _build_orchestrator(extractor, _MappingBackend(_search))
    res = orch.run("notebook 16gb ram para estudos até 5000 reais", profile_id="study", request_id="req-ok")

    assert res.status is ExecutionStatus.SUCCESS
    assert res.error is None
    assert extractor.calls == 1
    assert res.metrics is not None
    assert res.metrics.executed_nodes == (
        "prepare_intent_and_plan",
        "search_products",
        "process_semantics",
        "validate_candidates",
        "rank_candidates",
        "finalize",
    )
    assert res.ranking_result is not None
    assert res.ranking_result.ranked_groups[0].representative_offer.item_id == "nb-1"


def test_early_exits_and_node_error_isolation() -> None:
    # 1. Clarification never calls backend
    backend_spy = _MappingBackend(lambda q, r: SearchResponse(query=q, candidates=(), total_available=0, limit=q.limit, provider="synthetic"))
    orch_clarify = _build_orchestrator(
        _StaticExtractor(UserIntent(category=None, clarification_needed=True, clarification_question="Qual produto?", query="algo")),
        backend_spy,
    )
    res_c = orch_clarify.run("algo")
    assert res_c.status is ExecutionStatus.CLARIFICATION_NEEDED
    assert backend_spy.calls == 0
    assert res_c.metrics is not None
    assert res_c.metrics.executed_nodes == ("prepare_intent_and_plan", "finalize")

    # 2. Search error is NOT masked as NO_RESULTS
    def _failing_search(q: SearchQuery, r: str | None) -> SearchResponse:
        raise ProviderTimeoutError("timeout")

    orch_err = _build_orchestrator(
        _StaticExtractor(UserIntent(category="monitor", query="monitor 4k")),
        _MappingBackend(_failing_search),
    )
    res_e = orch_err.run("monitor 4k")
    assert res_e.status is ExecutionStatus.ERROR
    assert res_e.error is not None
    assert res_e.error.node == "search_products"
    assert res_e.error.code == "provider_timeout"
    assert res_e.metrics is not None
    assert res_e.metrics.executed_nodes == ("prepare_intent_and_plan", "search_products", "finalize")

    # 3. All rejected by validation never executes ranking
    def _over_budget_search(q: SearchQuery, r: str | None) -> SearchResponse:
        cand = Candidate(
            item_id="exp-1",
            source="synthetic",
            title="Monitor 4K Ultra",
            price=Decimal("3000.00"),
            currency="USD",
            condition="New",
            categories=("monitor",),
            shipping=ShippingInfo(free_shipping=True),
        )
        return SearchResponse(query=q, candidates=(cand,), total_available=1, limit=q.limit, provider="synthetic")

    orch_rej = _build_orchestrator(
        _StaticExtractor(
            UserIntent(
                category="monitor",
                price=PriceConstraint(max_price=1000.0, currency="BRL", is_hard_constraint=True),
                query="monitor até 1000 reais",
            )
        ),
        _MappingBackend(_over_budget_search),
    )
    res_r = orch_rej.run("monitor até 1000 reais")
    assert res_r.status is ExecutionStatus.ALL_REJECTED
    assert res_r.ranking_result is None
    assert res_r.metrics is not None
    assert "rank_candidates" not in res_r.metrics.executed_nodes


def test_concurrent_executions_are_isolated_and_retain_no_state() -> None:
    class _MultiExtractor:
        def extract(self, user_text: str) -> UserIntent:
            if "FAIL" in user_text:
                raise RuntimeError("boom")
            cat = "smartphone" if "smartphone" in user_text else "monitor"
            return UserIntent(category=cat, query=user_text)

    def _per_req_search(q: SearchQuery, req_id: str | None) -> SearchResponse:
        if req_id == "req-phone":
            cand = Candidate(
                item_id="phone-only",
                source="synthetic",
                title="Smartphone Galaxy 128GB",
                price=Decimal("300.00"),
                currency="USD",
                condition="New",
                categories=("smartphone",),
                shipping=ShippingInfo(free_shipping=True),
            )
        else:
            cand = Candidate(
                item_id="monitor-only",
                source="synthetic",
                title="Monitor Dell 27 4K IPS",
                price=Decimal("400.00"),
                currency="USD",
                condition="New",
                categories=("monitor",),
                shipping=ShippingInfo(free_shipping=True),
            )
        return SearchResponse(query=q, candidates=(cand,), total_available=1, limit=q.limit, provider="synthetic")

    orch = _build_orchestrator(_MultiExtractor(), _MappingBackend(_per_req_search))

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_phone = pool.submit(orch.run, "smartphone 128gb", request_id="req-phone")
        f_mon = pool.submit(orch.run, "monitor 4k", request_id="req-mon")
        f_fail = pool.submit(orch.run, "FAIL", request_id="req-fail")

        r_phone = f_phone.result()
        r_mon = f_mon.result()
        r_fail = f_fail.result()

    assert r_phone.status is ExecutionStatus.SUCCESS
    assert r_phone.ranking_result is not None
    assert [o.item_id for o in r_phone.ranking_result.ranked_offers] == ["phone-only"]

    assert r_mon.status is ExecutionStatus.SUCCESS
    assert r_mon.ranking_result is not None
    assert [o.item_id for o in r_mon.ranking_result.ranked_offers] == ["monitor-only"]

    assert r_fail.status is ExecutionStatus.ERROR
    assert r_fail.error is not None

    # Verify orchestrator does not retain returned result references after caller drops them.
    ref = weakref.ref(r_phone)
    del f_phone, r_phone
    gc.collect()
    assert ref() is None
