"""Tests for deterministic, evidence-grounded PT-BR explainability."""

from __future__ import annotations

from decimal import Decimal

import pytest

from buscai.ranking.models import ApprovalState
from buscai.ranking.reranker import MultiCriteriaReranker
from buscai.search.models import Candidate, SellerInfo
from buscai.validation.models import (
    CandidateValidationResult,
    CriterionEvaluation,
    EnrichmentSummary,
    ValidationPipelineResult,
)


def test_explanation_never_claims_lowest_total_cost_when_shipping_unknown() -> None:
    cand = Candidate(
        item_id="no-ship-1",
        source="dummy",
        title="Smartphone Test 128GB",
        price=Decimal("200.00"),
        currency="USD",
        condition="Used",
        seller=SellerInfo(username="seller1", feedback_percentage=95.0, feedback_score=80),
        shipping=None,
    )
    val = CandidateValidationResult(
        candidate=cand,
        eligible=True,
        strictly_verified=True,
        needs_enrichment=False,
        detected_category="smartphone",
        evaluations=(
            CriterionEvaluation(
                criterion_id="storage_gb",
                criterion_label="storage_gb",
                priority="soft",
                status="satisfied",
                evidence="128 GB",
                source_field="title",
            ),
        ),
        soft_satisfied_count=1,
    )
    val_res = ValidationPipelineResult(
        eligible_candidates=(val,),
        all_validations=(val,),
        enrichment=EnrichmentSummary(),
        total_candidates_evaluated=1,
    )

    reranker = MultiCriteriaReranker()
    groups, _, _ = reranker.rerank(val_res, profile_id="lowest_price")
    expl = groups[0].explanation

    assert groups[0].score_breakdown.shipping_known is False
    combined_text = " ".join([expl.summary, *expl.reasons, *expl.warnings, *expl.missing_data]).lower()
    assert "custo total verificado" not in combined_text
    assert "frete não informado" in combined_text
    assert any("usado" in t.lower() for t in expl.tradeoffs)
    assert expl.approval_state is ApprovalState.STRICTLY_APPROVED


def test_explanation_traces_missing_data_contradictions_and_score_match() -> None:
    cand = Candidate(
        item_id="trace-1",
        source="dummy",
        title="Monitor 27 4K",
        price=Decimal("350.00"),
        currency="USD",
        condition="New",
        aspects={"Maximum Resolution": ("1080p",)},
        seller=None,
        shipping=None,
    )
    val = CandidateValidationResult(
        candidate=cand,
        eligible=True,
        strictly_verified=False,
        needs_enrichment=True,
        detected_category="monitor",
        evaluations=(
            CriterionEvaluation(
                criterion_id="refresh_rate_hz",
                criterion_label="frequência do monitor",
                priority="soft",
                status="unknown",
                insufficient_information=True,
            ),
        ),
        soft_unknown_count=1,
        contradictions={"resolution": ("aspects=1080p", "title=4K")},
    )
    val_res = ValidationPipelineResult(
        eligible_candidates=(val,),
        all_validations=(val,),
        enrichment=EnrichmentSummary(),
        total_candidates_evaluated=1,
    )

    reranker = MultiCriteriaReranker()
    groups, _, _ = reranker.rerank(val_res, profile_id="professional_use", category_hint="monitor")
    group = groups[0]
    expl = group.explanation

    assert expl.rank == 1
    assert expl.total_score == pytest.approx(group.score_breakdown.final_score, abs=1e-9)
    assert any("frequência do monitor" in m.lower() for m in expl.missing_data)
    assert any("reputação do vendedor" in m.lower() for m in expl.missing_data)
    assert any("contradição" in w.lower() for w in expl.warnings)
    assert any("penalidade" in p.lower() for p in expl.penalties)
