"""Cohort normalization, weight redistribution, confidence scaling, and breakdown scoring."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from buscai.ranking.criteria import CandidateCostInfo, RawCriterionEvaluation
from buscai.ranking.models import (
    ApprovalState,
    CriterionName,
    CriterionScore,
    RankingConfig,
    ScoreBreakdown,
)
from buscai.ranking.weights import WeightVector


@dataclass(frozen=True)
class CandidateRawEvaluationBundle:
    """All raw evaluations and metadata for one candidate prior to cohort normalization."""

    item_id: str
    cost_info: CandidateCostInfo
    raw_criteria: dict[CriterionName, RawCriterionEvaluation]
    contradiction_count: int
    approval_state: ApprovalState


def normalize_cost_scores(
    bundles: list[CandidateRawEvaluationBundle],
) -> dict[str, RawCriterionEvaluation]:
    """Normalize total BRL costs across a cohort via inverse min-max scaling."""
    costs: dict[str, Decimal] = {
        b.item_id: b.cost_info.total_cost_brl
        for b in bundles
        if b.cost_info.total_cost_brl is not None
    }
    if not costs:
        return {
            b.item_id: RawCriterionEvaluation(
                name=CriterionName.COST_VALUE,
                raw_value=None,
                normalized_score=None,
                available=False,
                note="Preço indisponível.",
            )
            for b in bundles
        }

    min_cost = min(costs.values())
    max_cost = max(costs.values())
    span = max_cost - min_cost

    results: dict[str, RawCriterionEvaluation] = {}
    for b in bundles:
        cost = b.cost_info.total_cost_brl
        if cost is None:
            results[b.item_id] = RawCriterionEvaluation(
                name=CriterionName.COST_VALUE,
                raw_value=None,
                normalized_score=None,
                available=False,
                note="Preço indisponível.",
            )
            continue

        if span <= Decimal("0"):
            norm = 1.0
        else:
            norm = float((max_cost - cost) / span)
        norm = max(0.0, min(1.0, norm))

        shipping_note = (
            "frete conhecido" if b.cost_info.shipping_known else "frete desconhecido"
        )
        sources = ("price", "shipping") if b.cost_info.shipping_known else ("price",)
        results[b.item_id] = RawCriterionEvaluation(
            name=CriterionName.COST_VALUE,
            raw_value=float(cost),
            normalized_score=norm,
            available=True,
            evidence_sources=sources,
            note=f"Custo estimado R$ {cost:.2f} ({shipping_note}).",
        )
    return results


def build_score_breakdown(
    bundle: CandidateRawEvaluationBundle,
    cost_eval: RawCriterionEvaluation,
    weights: WeightVector,
    config: RankingConfig,
) -> ScoreBreakdown:
    """Compute proportional weight redistribution, confidence factor, and final score."""
    all_raw = dict(bundle.raw_criteria)
    all_raw[CriterionName.COST_VALUE] = cost_eval

    applicable_weight_sum = 0.0
    available_weight_sum = 0.0

    for criterion in CriterionName:
        base_w = weights.get(criterion)
        raw = all_raw[criterion]
        if base_w <= 0.0 or raw.not_applicable:
            continue
        applicable_weight_sum += base_w
        if raw.available and raw.normalized_score is not None:
            available_weight_sum += base_w

    evidence_coverage = (
        available_weight_sum / applicable_weight_sum
        if applicable_weight_sum > 0.0
        else 1.0
    )
    evidence_coverage = max(0.0, min(1.0, evidence_coverage))

    scored_criteria: list[CriterionScore] = []
    weighted_sum = 0.0

    for criterion in CriterionName:
        base_w = weights.get(criterion)
        raw = all_raw[criterion]
        if (
            base_w > 0.0
            and not raw.not_applicable
            and raw.available
            and raw.normalized_score is not None
            and available_weight_sum > 0.0
        ):
            eff_w = base_w / available_weight_sum
            contribution = eff_w * raw.normalized_score
            weighted_sum += contribution
        else:
            eff_w = 0.0
            contribution = 0.0

        scored_criteria.append(
            CriterionScore(
                name=criterion,
                raw_value=raw.raw_value,
                normalized_score=raw.normalized_score,
                available=raw.available,
                not_applicable=raw.not_applicable,
                base_weight=base_w,
                effective_weight=eff_w,
                weighted_contribution=contribution,
                evidence_sources=raw.evidence_sources,
                note=raw.note,
            )
        )

    floor = max(0.0, min(1.0, config.min_confidence_floor))
    confidence_factor = floor + (1.0 - floor) * evidence_coverage

    contradiction_penalty = min(
        config.max_contradiction_penalty,
        bundle.contradiction_count * config.contradiction_penalty_per_item,
    )
    approval_factor = (
        config.relaxation_score_factor
        if bundle.approval_state is ApprovalState.APPROVED_WITH_RELAXATION
        else 1.0
    )

    final_score = max(
        0.0,
        min(1.0, weighted_sum * confidence_factor * approval_factor - contradiction_penalty),
    )

    sem_item = next(
        (c for c in scored_criteria if c.name is CriterionName.SEMANTIC_RELEVANCE),
        None,
    )
    semantic_weight_applied = sem_item.effective_weight if sem_item is not None else 0.0

    return ScoreBreakdown(
        criteria=tuple(scored_criteria),
        weighted_sum=weighted_sum,
        evidence_coverage=evidence_coverage,
        confidence_factor=confidence_factor,
        contradiction_count=bundle.contradiction_count,
        contradiction_penalty=contradiction_penalty,
        final_score=final_score,
        semantic_enabled=config.enable_semantic_score,
        semantic_weight_applied=semantic_weight_applied,
        total_cost_brl=bundle.cost_info.total_cost_brl,
        shipping_known=bundle.cost_info.shipping_known,
    )
