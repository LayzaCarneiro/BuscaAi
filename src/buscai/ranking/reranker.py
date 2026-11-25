"""Two-level multi-criteria reranker with deterministic tie-breaking."""

from __future__ import annotations

from decimal import Decimal

from buscai.pricing.currency import CurrencyConverter
from buscai.ranking.criteria import (
    compute_candidate_cost_brl,
    default_currency_converter,
    evaluate_budget_fit,
    evaluate_condition_quality,
    evaluate_logistics,
    evaluate_portability,
    evaluate_seller_reliability,
    evaluate_semantic_relevance,
    evaluate_technical_fit,
)
from buscai.ranking.explainer import build_group_explanation, build_offer_explanation
from buscai.ranking.models import (
    ApprovalState,
    CriterionName,
    RankedOffer,
    RankedProductGroup,
    RankingConfig,
    RejectedCandidateSummary,
    ScoreBreakdown,
)
from buscai.ranking.normalization import (
    CandidateRawEvaluationBundle,
    build_score_breakdown,
    normalize_cost_scores,
)
from buscai.ranking.weights import WeightRegistry, WeightVector
from buscai.semantic.pipeline import SemanticProcessingResult
from buscai.validation.evidence import extract_candidate_evidences
from buscai.validation.models import (
    CandidateValidationResult,
    RelaxationStep,
    ValidationPipelineResult,
)

from buscai.search.models import Candidate

_INF_COST = Decimal("999999999999")


_APPROVAL_ORDER: dict[ApprovalState, int] = {
    ApprovalState.STRICTLY_APPROVED: 0,
    ApprovalState.APPROVED_WITH_RELAXATION: 1,
    ApprovalState.INSUFFICIENT_EVIDENCE: 2,
    ApprovalState.REJECTED: 3,
}


def _resolve_offer_approval_state(
    validation: CandidateValidationResult,
    breakdown: ScoreBreakdown,
    *,
    relaxed: bool,
    config: RankingConfig,
) -> ApprovalState:
    if relaxed:
        return ApprovalState.APPROVED_WITH_RELAXATION
    if (
        not validation.strictly_verified
        or breakdown.evidence_coverage < config.insufficient_evidence_threshold
        or (validation.soft_unknown_count > 0 and validation.soft_satisfied_count == 0)
    ):
        return ApprovalState.INSUFFICIENT_EVIDENCE
    return ApprovalState.STRICTLY_APPROVED


def _offer_sort_key(
    item_id: str,
    approval_state: ApprovalState,
    breakdown: ScoreBreakdown,
    seller_feedback_pct: float | None,
) -> tuple[float, int, float, int, Decimal, float, str]:
    """Total-order permutation-invariant sort key (never uses arrival order or title length)."""
    return (
        -round(breakdown.final_score, 9),
        _APPROVAL_ORDER.get(approval_state, 3),
        -round(breakdown.evidence_coverage, 9),
        0 if breakdown.shipping_known else 1,
        breakdown.total_cost_brl if breakdown.total_cost_brl is not None else _INF_COST,
        -(float(seller_feedback_pct) if seller_feedback_pct is not None else -1.0),
        item_id,
    )


def _seller_feedback_pct(candidate: Candidate) -> float | None:
    if candidate.seller is None or candidate.seller.feedback_percentage is None:
        return None
    return float(candidate.seller.feedback_percentage)


def _is_rejected(
    validation: CandidateValidationResult,
    config: RankingConfig,
) -> bool:
    if not validation.eligible:
        return True
    if validation.hard_not_satisfied_count > 0:
        return True
    if config.strict_unknown_hard_as_rejected and validation.hard_unknown_count > 0:
        return True
    if (
        validation.budget_evaluation is not None
        and validation.budget_evaluation.priority == "hard"
        and validation.budget_evaluation.status in ("not_satisfied", "unknown")
    ):
        return True
    return False


def _rejection_reasons(validation: CandidateValidationResult) -> tuple[str, ...]:
    reasons: list[str] = []
    for ev in validation.evaluations:
        if ev.priority == "hard" and ev.status in ("not_satisfied", "unknown"):
            reasons.append(f"{ev.criterion_id}: {ev.status}")
    if validation.budget_evaluation and validation.budget_evaluation.status == "not_satisfied":
        reasons.append("budget: exceeded")
    if not reasons and not validation.eligible:
        reasons.append("ineligible")
    return tuple(reasons)


def _build_group_lookup(
    validation_result: ValidationPipelineResult,
    semantic_result: SemanticProcessingResult | None,
) -> tuple[dict[str, str], dict[str, str]]:
    item_to_group: dict[str, str] = {}
    group_titles: dict[str, str] = {}

    for group in validation_result.product_groups:
        group_titles[group.group_id] = group.canonical.title
        item_to_group[group.canonical.item_id] = group.group_id
        for offer in group.offers:
            item_to_group[offer.item_id] = group.group_id

    if semantic_result is not None:
        for sc in semantic_result.scored_candidates:
            if sc.product_group_id and sc.candidate.item_id not in item_to_group:
                item_to_group[sc.candidate.item_id] = sc.product_group_id
                group_titles.setdefault(sc.product_group_id, sc.candidate.title)

    return item_to_group, group_titles


def _build_semantic_lookup(
    semantic_result: SemanticProcessingResult | None,
) -> dict[str, float]:
    if semantic_result is None:
        return {}
    return {
        sc.candidate.item_id: sc.semantic_score
        for sc in semantic_result.scored_candidates
    }


def _evaluate_bundle(
    validation: CandidateValidationResult,
    *,
    category_hint: str | None,
    semantic_score: float | None,
    approval_state: ApprovalState,
    config: RankingConfig,
    converter: CurrencyConverter,
) -> CandidateRawEvaluationBundle:
    cand = validation.candidate
    resolved_cat = validation.detected_category or category_hint
    ev_map = extract_candidate_evidences(cand, category_hint=resolved_cat)
    cost_info = compute_candidate_cost_brl(cand, converter=converter)

    raw_criteria = {
        CriterionName.BUDGET_FIT: evaluate_budget_fit(validation),
        CriterionName.SELLER_RELIABILITY: evaluate_seller_reliability(cand),
        CriterionName.CONDITION_QUALITY: evaluate_condition_quality(cand),
        CriterionName.TECHNICAL_FIT: evaluate_technical_fit(
            validation, category_hint=resolved_cat, evidences=ev_map
        ),
        CriterionName.PORTABILITY: evaluate_portability(
            cand, category_hint=resolved_cat, evidences=ev_map
        ),
        CriterionName.LOGISTICS: evaluate_logistics(cand),
        CriterionName.SEMANTIC_RELEVANCE: evaluate_semantic_relevance(
            semantic_score, enable_semantic_score=config.enable_semantic_score
        ),
    }

    contradiction_count = len(validation.contradictions) or len(ev_map.contradictions)
    return CandidateRawEvaluationBundle(
        item_id=cand.item_id,
        cost_info=cost_info,
        raw_criteria=raw_criteria,
        contradiction_count=contradiction_count,
        approval_state=approval_state,
    )


def _score_cohort(
    bundles: list[CandidateRawEvaluationBundle],
    weights: WeightVector,
    config: RankingConfig,
) -> dict[str, ScoreBreakdown]:
    cost_evals = normalize_cost_scores(bundles)
    return {
        b.item_id: build_score_breakdown(b, cost_evals[b.item_id], weights, config)
        for b in bundles
    }


class MultiCriteriaReranker:
    """Two-level deterministic reranker for validated products and offers."""

    def __init__(
        self,
        *,
        config: RankingConfig | None = None,
        weight_registry: WeightRegistry | None = None,
        converter: CurrencyConverter | None = None,
    ) -> None:
        self.config = config or RankingConfig()
        self.weight_registry = weight_registry or WeightRegistry()
        self.converter = converter or default_currency_converter()

    def rerank(
        self,
        validation_result: ValidationPipelineResult,
        *,
        semantic_result: SemanticProcessingResult | None = None,
        profile_id: str | None = None,
        category_hint: str | None = None,
    ) -> tuple[
        tuple[RankedProductGroup, ...],
        tuple[RankedOffer, ...],
        tuple[RejectedCandidateSummary, ...],
    ]:
        """Rank eligible offers inside product groups (Level 2) and groups (Level 1)."""
        resolved_profile = profile_id or validation_result.active_profile_id
        approval_state = (
            ApprovalState.APPROVED_WITH_RELAXATION
            if validation_result.relaxed
            else ApprovalState.STRICTLY_APPROVED
        )
        relaxations: tuple[RelaxationStep, ...] = validation_result.relaxations

        item_to_group, group_titles = _build_group_lookup(
            validation_result, semantic_result
        )
        semantic_scores = _build_semantic_lookup(semantic_result)

        all_validations = validation_result.all_validations or (
            validation_result.eligible_candidates + validation_result.rejected_candidates
        )

        eligible_validations: list[CandidateValidationResult] = []
        rejected_summaries: list[RejectedCandidateSummary] = []
        seen_ids: set[str] = set()

        for v in all_validations:
            iid = v.candidate.item_id
            if iid in seen_ids:
                continue
            seen_ids.add(iid)
            if _is_rejected(v, self.config):
                rejected_summaries.append(
                    RejectedCandidateSummary(
                        item_id=iid,
                        title=v.candidate.title,
                        reasons=_rejection_reasons(v),
                        hard_not_satisfied_count=v.hard_not_satisfied_count,
                        hard_unknown_count=v.hard_unknown_count,
                    )
                )
            else:
                eligible_validations.append(v)

        if not eligible_validations:
            rejected_sorted = tuple(sorted(rejected_summaries, key=lambda r: r.item_id))
            return (), (), rejected_sorted

        # Evaluate raw criteria once per eligible candidate.
        bundles_by_id: dict[str, CandidateRawEvaluationBundle] = {}
        validations_by_id: dict[str, CandidateValidationResult] = {}
        grouped_ids: dict[str, list[str]] = {}

        for v in eligible_validations:
            iid = v.candidate.item_id
            validations_by_id[iid] = v
            gid = item_to_group.get(iid, f"group:{iid}")
            group_titles.setdefault(gid, v.candidate.title)
            grouped_ids.setdefault(gid, []).append(iid)
            bundles_by_id[iid] = _evaluate_bundle(
                v,
                category_hint=category_hint,
                semantic_score=semantic_scores.get(iid),
                approval_state=approval_state,
                config=self.config,
                converter=self.converter,
            )

        # Level 2: rank offers within each product group.
        offer_weights = self.weight_registry.get_offer_weights()
        group_offers_ranked: dict[str, tuple[RankedOffer, ...]] = {}

        for gid, item_ids in grouped_ids.items():
            cohort_bundles = [bundles_by_id[iid] for iid in item_ids]
            offer_breakdowns = _score_cohort(cohort_bundles, offer_weights, self.config)
            offer_states = {
                iid: _resolve_offer_approval_state(
                    validations_by_id[iid],
                    offer_breakdowns[iid],
                    relaxed=validation_result.relaxed,
                    config=self.config,
                )
                for iid in item_ids
            }

            sorted_ids = sorted(
                item_ids,
                key=lambda iid: _offer_sort_key(
                    iid,
                    offer_states[iid],
                    offer_breakdowns[iid],
                    _seller_feedback_pct(validations_by_id[iid].candidate),
                ),
            )

            ranked_list: list[RankedOffer] = []
            for idx, iid in enumerate(sorted_ids, start=1):
                v = validations_by_id[iid]
                bd = offer_breakdowns[iid]
                st = offer_states[iid]
                expl = build_offer_explanation(
                    candidate=v.candidate,
                    validation=v,
                    breakdown=bd,
                    approval_state=st,
                    profile_id=resolved_profile,
                    rank=idx,
                    relaxations=relaxations,
                    sibling_breakdowns=offer_breakdowns,
                )
                ranked_list.append(
                    RankedOffer(
                        rank=idx,
                        item_id=iid,
                        product_group_id=gid,
                        candidate=v.candidate,
                        validation=v,
                        approval_state=st,
                        score_breakdown=bd,
                        explanation=expl,
                        semantic_score=semantic_scores.get(iid),
                        is_best_offer_in_group=(idx == 1),
                    )
                )
            group_offers_ranked[gid] = tuple(ranked_list)

        # Level 1: score and rank product groups across representatives.
        rep_bundles = [
            bundles_by_id[offers[0].item_id] for offers in group_offers_ranked.values()
        ]
        resolved_category = category_hint or next(
            (
                validations_by_id[b.item_id].detected_category
                for b in rep_bundles
                if validations_by_id[b.item_id].detected_category
            ),
            None,
        )
        group_weights = self.weight_registry.get_group_weights(
            resolved_profile,
            resolved_category,
            enable_semantic_score=self.config.enable_semantic_score,
            semantic_max_weight=self.config.semantic_max_weight,
        )
        group_breakdowns = _score_cohort(rep_bundles, group_weights, self.config)
        group_states = {
            gid: _resolve_offer_approval_state(
                group_offers_ranked[gid][0].validation,
                group_breakdowns[group_offers_ranked[gid][0].item_id],
                relaxed=validation_result.relaxed,
                config=self.config,
            )
            for gid in group_offers_ranked
        }

        sorted_gids = sorted(
            group_offers_ranked.keys(),
            key=lambda gid: _offer_sort_key(
                group_offers_ranked[gid][0].item_id,
                group_states[gid],
                group_breakdowns[group_offers_ranked[gid][0].item_id],
                _seller_feedback_pct(group_offers_ranked[gid][0].candidate),
            ),
        )

        ranked_groups: list[RankedProductGroup] = []
        all_ranked_offers: list[RankedOffer] = []

        for g_rank, gid in enumerate(sorted_gids, start=1):
            offers = group_offers_ranked[gid]
            rep = offers[0]
            v = rep.validation
            rep_group_bd = group_breakdowns[rep.item_id]
            g_state = group_states[gid]
            rep_group_expl = build_offer_explanation(
                candidate=rep.candidate,
                validation=v,
                breakdown=rep_group_bd,
                approval_state=g_state,
                profile_id=resolved_profile,
                rank=g_rank,
                relaxations=relaxations,
            )
            updated_rep = RankedOffer(
                rank=rep.rank,
                item_id=rep.item_id,
                product_group_id=rep.product_group_id,
                candidate=rep.candidate,
                validation=rep.validation,
                approval_state=g_state,
                score_breakdown=rep_group_bd,
                explanation=rep_group_expl,
                semantic_score=rep.semantic_score,
                is_best_offer_in_group=True,
            )
            group_expl = build_group_explanation(
                canonical_title=group_titles.get(gid, rep.candidate.title),
                representative_offer=updated_rep,
                offer_count=len(offers),
                rank=g_rank,
            )
            ranked_groups.append(
                RankedProductGroup(
                    rank=g_rank,
                    product_group_id=gid,
                    canonical_title=group_titles.get(gid, rep.candidate.title),
                    detected_category=v.detected_category or resolved_category,
                    approval_state=g_state,
                    representative_offer=updated_rep,
                    offers=offers,
                    offer_count=len(offers),
                    score_breakdown=rep_group_bd,
                    explanation=group_expl,
                )
            )
            all_ranked_offers.extend(offers)

        rejected_sorted = tuple(sorted(rejected_summaries, key=lambda r: r.item_id))
        return tuple(ranked_groups), tuple(all_ranked_offers), rejected_sorted
