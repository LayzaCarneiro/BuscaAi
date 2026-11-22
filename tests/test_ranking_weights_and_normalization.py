"""Tests for Stage 6 weight matrices, normalization, missing data, and confidence scaling."""

from __future__ import annotations

from decimal import Decimal

import pytest

from buscai.ranking.criteria import (
    compute_candidate_cost_brl,
    evaluate_budget_fit,
    evaluate_condition_quality,
    evaluate_logistics,
    evaluate_portability,
    evaluate_seller_reliability,
    evaluate_semantic_relevance,
    evaluate_technical_fit,
)
from buscai.ranking.models import ApprovalState, CriterionName, RankingConfig
from buscai.ranking.normalization import (
    CandidateRawEvaluationBundle,
    build_score_breakdown,
    normalize_cost_scores,
)
from buscai.ranking.weights import (
    MAX_SEMANTIC_WEIGHT,
    PROFILE_WEIGHTS,
    WEIGHT_RATIONALE,
    WeightRegistry,
    WeightVector,
)
from buscai.search.models import Candidate, SellerInfo, ShippingInfo
from buscai.validation.models import CandidateValidationResult


def test_all_six_profiles_sum_to_one_cap_semantic_and_have_rationale() -> None:
    expected = {
        "best_value",
        "best_performance",
        "lowest_price",
        "professional_use",
        "study",
        "everyday_use",
    }
    assert set(PROFILE_WEIGHTS.keys()) == expected
    assert set(WEIGHT_RATIONALE.keys()) == expected
    for profile_id, vec in PROFILE_WEIGHTS.items():
        assert sum(vec.weights.values()) == pytest.approx(1.0, abs=1e-6), profile_id
        assert vec.get(CriterionName.SEMANTIC_RELEVANCE) <= MAX_SEMANTIC_WEIGHT


def test_weight_vector_rejects_excessive_semantic_weight() -> None:
    with pytest.raises(ValueError, match="exceeds cap"):
        WeightVector(
            weights={
                CriterionName.COST_VALUE: 0.80,
                CriterionName.BUDGET_FIT: 0.0,
                CriterionName.SELLER_RELIABILITY: 0.0,
                CriterionName.CONDITION_QUALITY: 0.0,
                CriterionName.TECHNICAL_FIT: 0.0,
                CriterionName.PORTABILITY: 0.0,
                CriterionName.LOGISTICS: 0.0,
                CriterionName.SEMANTIC_RELEVANCE: 0.20,
            }
        )


def test_weight_registry_disables_semantic_and_renormalizes() -> None:
    registry = WeightRegistry()
    vec_on = registry.get_group_weights("best_performance", "notebook", enable_semantic_score=True)
    vec_off = registry.get_group_weights("best_performance", "notebook", enable_semantic_score=False)

    assert vec_on.get(CriterionName.SEMANTIC_RELEVANCE) > 0.0
    assert vec_off.get(CriterionName.SEMANTIC_RELEVANCE) == 0.0
    assert sum(vec_off.weights.values()) == pytest.approx(1.0, abs=1e-6)


def test_weight_registry_resolves_aliases_and_supports_dynamic_extension() -> None:
    registry = WeightRegistry()
    # "laptop" resolves to "notebook" and "Estudos" resolves to "study".
    vec_alias = registry.get_group_weights("Estudos", "laptop")
    vec_canonical = registry.get_group_weights("study", "notebook")
    assert vec_alias.weights == vec_canonical.weights

    custom_vec = WeightVector(
        weights={
            CriterionName.COST_VALUE: 0.30,
            CriterionName.BUDGET_FIT: 0.10,
            CriterionName.SELLER_RELIABILITY: 0.10,
            CriterionName.CONDITION_QUALITY: 0.10,
            CriterionName.TECHNICAL_FIT: 0.20,
            CriterionName.PORTABILITY: 0.10,
            CriterionName.LOGISTICS: 0.05,
            CriterionName.SEMANTIC_RELEVANCE: 0.05,
        }
    )
    extended = registry.with_profile_weights("gaming_custom", custom_vec).with_category_profile_weights(
        "tablet", "gaming_custom", custom_vec
    )
    assert extended.get_group_weights("gaming_custom", "tablet").weights == custom_vec.weights


def test_missing_criteria_redistribute_weights_and_lower_confidence() -> None:
    registry = WeightRegistry()
    weights = registry.get_group_weights("best_value", "notebook", enable_semantic_score=False)
    config = RankingConfig(enable_semantic_score=False, min_confidence_floor=0.75)

    cand_rich = Candidate(
        item_id="rich",
        source="dummy",
        title="Notebook Rich",
        price=Decimal("500.00"),
        currency="USD",
        condition="New",
        seller=SellerInfo(username="s1", feedback_percentage=99.0, feedback_score=1000),
        shipping=ShippingInfo(free_shipping=True, estimated_max_days=3),
    )
    cand_sparse = Candidate(
        item_id="sparse",
        source="dummy",
        title="Notebook Sparse",
        price=Decimal("500.00"),
        currency="USD",
    )

    def _make_bundle(cand: Candidate, contradictions: int = 0) -> CandidateRawEvaluationBundle:
        val = CandidateValidationResult(
            candidate=cand,
            eligible=True,
            strictly_verified=True,
            needs_enrichment=False,
        )
        return CandidateRawEvaluationBundle(
            item_id=cand.item_id,
            cost_info=compute_candidate_cost_brl(cand),
            raw_criteria={
                CriterionName.BUDGET_FIT: evaluate_budget_fit(val),
                CriterionName.SELLER_RELIABILITY: evaluate_seller_reliability(cand),
                CriterionName.CONDITION_QUALITY: evaluate_condition_quality(cand),
                CriterionName.TECHNICAL_FIT: evaluate_technical_fit(val),
                CriterionName.PORTABILITY: evaluate_portability(cand, category_hint="notebook"),
                CriterionName.LOGISTICS: evaluate_logistics(cand),
                CriterionName.SEMANTIC_RELEVANCE: evaluate_semantic_relevance(
                    None, enable_semantic_score=False
                ),
            },
            contradiction_count=contradictions,
            approval_state=ApprovalState.STRICTLY_APPROVED,
        )

    b_rich = _make_bundle(cand_rich)
    b_sparse = _make_bundle(cand_sparse)
    b_contradict = _make_bundle(cand_rich, contradictions=2)
    costs = normalize_cost_scores([b_rich, b_sparse, b_contradict])

    bd_rich = build_score_breakdown(b_rich, costs["rich"], weights, config)
    bd_sparse = build_score_breakdown(b_sparse, costs["sparse"], weights, config)
    bd_contra = build_score_breakdown(b_contradict, costs["rich"], weights, config)

    # Mathematical invariants: weighted_sum == sum(contributions), bounds in [0.0, 1.0]
    assert bd_rich.weighted_sum == pytest.approx(
        sum(c.weighted_contribution for c in bd_rich.criteria), abs=1e-9
    )
    assert 0.0 <= bd_rich.final_score <= 1.0
    assert 0.0 <= bd_sparse.final_score <= 1.0
    assert 0.0 <= bd_contra.final_score <= 1.0

    # Sparse candidate must NOT have seller/condition coerced to 0.0, and must have lower coverage/confidence.
    seller_sparse = bd_sparse.by_name(CriterionName.SELLER_RELIABILITY)
    assert seller_sparse is not None
    assert seller_sparse.available is False
    assert seller_sparse.normalized_score is None
    assert bd_sparse.evidence_coverage < bd_rich.evidence_coverage
    assert bd_sparse.confidence_factor < bd_rich.confidence_factor
    assert bd_rich.final_score > bd_sparse.final_score

    # Contradiction penalty strictly reduces score.
    assert bd_contra.contradiction_penalty == pytest.approx(0.16, abs=1e-9)
    assert bd_contra.final_score < bd_rich.final_score


def test_portability_is_not_applicable_for_monitors() -> None:
    monitor = Candidate(
        item_id="m1",
        source="dummy",
        title='Monitor 27" 4K',
        price=Decimal("300"),
        currency="USD",
    )
    ev = evaluate_portability(monitor, category_hint="monitor")
    assert ev.not_applicable is True
    assert ev.available is False
