"""Six synthetic end-to-end orchestration scenarios for Stage 7 (T7.8)."""

from __future__ import annotations

from decimal import Decimal

import pytest

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


class _ScenarioExtractor:
    def __init__(self, intent: UserIntent) -> None:
        self._intent = intent

    def extract(self, user_text: str) -> UserIntent:
        return self._intent


class _ScenarioBackend:
    def __init__(self, candidates: tuple[Candidate, ...]) -> None:
        self._candidates = candidates

    def search(
        self,
        query: SearchQuery,
        *,
        budget: RequestCallBudget | None = None,
        request_id: str | None = None,
    ) -> SearchResponse:
        if budget is not None:
            budget.search_calls_used += 1
        return SearchResponse(
            query=query,
            candidates=self._candidates,
            total_available=len(self._candidates),
            limit=query.limit,
            provider="synthetic",
        )

    def get_item_details(self, item_id: str, **kwargs) -> Candidate | None:
        return None


def _run_scenario(
    intent: UserIntent,
    candidates: tuple[Candidate, ...],
    *,
    profile_id: str | None = None,
):
    converter = CurrencyConverter(FixedEnvExchangeRateProvider(rate=Decimal("5.00")))
    deps = OrchestrationDependencies(
        extractor=_ScenarioExtractor(intent),
        query_builder=QueryBuilder(currency_converter=converter),
        search_backend=_ScenarioBackend(candidates),
        semantic_processor=SemanticSearchProcessor(
            EmbeddingService(DeterministicStubEmbeddingProvider())
        ),
        validation_pipeline=ValidationPipeline(currency_converter=converter),
        ranking_pipeline=RankingPipeline(converter=converter),
        quota_guard=QuotaGuard(),
    )
    return SearchOrchestrator(deps).run(
        intent.query,
        profile_id=profile_id,
        request_id=f"scen-{intent.category or 'none'}",
    )


@pytest.mark.parametrize(
    ("scenario_id", "intent", "profile_id", "candidates", "expected_status", "expected_top_id"),
    [
        (
            "1_notebook_study_budget",
            UserIntent(
                category="notebook",
                profile="study",
                price=PriceConstraint(max_price=4500.0, currency="BRL", is_hard_constraint=True),
                requirements=["16GB RAM"],
                query="notebook para estudos até 4500 reais",
            ),
            "study",
            (
                Candidate(
                    item_id="nb-st-1",
                    source="synthetic",
                    title="Notebook Ultrabook 13.3 16GB RAM SSD 512GB",
                    price=Decimal("650.00"),
                    currency="USD",
                    condition="New",
                    categories=("notebook",),
                    aspects={"RAM Size": ("16 GB",), "SSD Capacity": ("512 GB",), "Weight": ("1.2 kg",)},
                    seller=SellerInfo(username="s1", feedback_percentage=99.0, feedback_score=1000),
                    shipping=ShippingInfo(free_shipping=True, estimated_max_days=4),
                ),
            ),
            ExecutionStatus.SUCCESS,
            "nb-st-1",
        ),
        (
            "2_smartphone_min_storage",
            UserIntent(
                category="smartphone",
                requirements=["256GB"],
                query="smartphone com 256gb de armazenamento",
            ),
            "best_value",
            (
                Candidate(
                    item_id="sp-256",
                    source="synthetic",
                    title="Smartphone Galaxy S24 256GB 8GB RAM",
                    price=Decimal("450.00"),
                    currency="USD",
                    condition="New",
                    categories=("smartphone",),
                    aspects={"Storage Capacity": ("256 GB",), "RAM": ("8 GB",)},
                    seller=SellerInfo(username="s2", feedback_percentage=98.5, feedback_score=800),
                    shipping=ShippingInfo(free_shipping=True, estimated_max_days=5),
                ),
            ),
            ExecutionStatus.SUCCESS,
            "sp-256",
        ),
        (
            "3_monitor_4k_resolution",
            UserIntent(
                category="monitor",
                profile="professional_use",
                requirements=["4K"],
                query="monitor 4k uso profissional",
            ),
            "professional_use",
            (
                Candidate(
                    item_id="mon-4k",
                    source="synthetic",
                    title="Monitor Dell UltraSharp 27 4K IPS",
                    price=Decimal("520.00"),
                    currency="USD",
                    condition="New",
                    categories=("monitor",),
                    aspects={"Maximum Resolution": ("4K 3840x2160",), "Display Type": ("IPS",)},
                    seller=SellerInfo(username="s3", feedback_percentage=99.5, feedback_score=2500),
                    shipping=ShippingInfo(free_shipping=True, estimated_max_days=3),
                ),
            ),
            ExecutionStatus.SUCCESS,
            "mon-4k",
        ),
        (
            "4_headphones_bt_anc",
            UserIntent(
                category="fone de ouvido",
                requirements=["Bluetooth", "ANC"],
                query="fone bluetooth com cancelamento de ruido",
            ),
            "everyday_use",
            (
                Candidate(
                    item_id="hp-anc",
                    source="synthetic",
                    title="Fone Bluetooth Sony WH-1000XM4 ANC",
                    price=Decimal("220.00"),
                    currency="USD",
                    condition="New",
                    categories=("fone de ouvido",),
                    aspects={"Connectivity": ("Bluetooth",), "Features": ("Active Noise Cancellation",)},
                    seller=SellerInfo(username="s4", feedback_percentage=99.0, feedback_score=1400),
                    shipping=ShippingInfo(free_shipping=True, estimated_max_days=4),
                ),
            ),
            ExecutionStatus.SUCCESS,
            "hp-anc",
        ),
        (
            "5_soft_preference_relaxed",
            UserIntent(
                category="notebook",
                profile="best_performance",
                query="notebook para alto desempenho",
            ),
            "best_performance",
            (
                Candidate(
                    item_id="nb-rel",
                    source="synthetic",
                    title="Notebook 16GB RAM SSD 512GB",
                    price=Decimal("550.00"),
                    currency="USD",
                    condition="New",
                    categories=("notebook",),
                    aspects={"RAM Size": ("16 GB",), "SSD Capacity": ("512 GB",)},
                    seller=SellerInfo(username="s5", feedback_percentage=98.0, feedback_score=600),
                    shipping=ShippingInfo(free_shipping=True, estimated_max_days=5),
                ),
            ),
            ExecutionStatus.SUCCESS,
            "nb-rel",
        ),
        (
            "6_no_candidates_skips_ranking",
            UserIntent(
                category="monitor",
                query="monitor 8k microled",
            ),
            "best_value",
            (),
            ExecutionStatus.NO_RESULTS,
            None,
        ),
    ],
)
def test_six_synthetic_end_to_end_scenarios(
    scenario_id: str,
    intent: UserIntent,
    profile_id: str | None,
    candidates: tuple[Candidate, ...],
    expected_status: ExecutionStatus,
    expected_top_id: str | None,
) -> None:
    res = _run_scenario(intent, candidates, profile_id=profile_id)
    assert res.status is expected_status, scenario_id
    if expected_status is ExecutionStatus.SUCCESS:
        assert res.ranking_result is not None
        assert res.ranking_result.ranked_groups[0].representative_offer.item_id == expected_top_id
        assert res.metrics is not None
        assert res.metrics.executed_nodes == (
            "prepare_intent_and_plan",
            "search_products",
            "process_semantics",
            "validate_candidates",
            "rank_candidates",
            "finalize",
        )
    else:
        assert res.ranking_result is None
        assert res.metrics is not None
        assert "rank_candidates" not in res.metrics.executed_nodes
