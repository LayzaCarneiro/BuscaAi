"""Contracts and data models for Stage 6 multi-criteria reranking and explainability."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from buscai.search.models import Candidate
from buscai.validation.models import CandidateValidationResult, RelaxationStep


class CriterionName(str, Enum):
    """Canonical criteria evaluated during multi-criteria reranking."""

    COST_VALUE = "cost_value"
    BUDGET_FIT = "budget_fit"
    SELLER_RELIABILITY = "seller_reliability"
    CONDITION_QUALITY = "condition_quality"
    TECHNICAL_FIT = "technical_fit"
    PORTABILITY = "portability"
    LOGISTICS = "logistics"
    SEMANTIC_RELEVANCE = "semantic_relevance"


class ApprovalState(str, Enum):
    """Validation and approval state reflected in ranking and explanations."""

    STRICTLY_APPROVED = "strictly_approved"
    APPROVED_WITH_RELAXATION = "approved_with_relaxation"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    REJECTED = "rejected"


@dataclass(frozen=True)
class CriterionScore:
    """Normalized score and weight contribution for a single ranking criterion."""

    name: CriterionName
    raw_value: float | str | None
    normalized_score: float | None
    available: bool
    not_applicable: bool = False
    base_weight: float = 0.0
    effective_weight: float = 0.0
    weighted_contribution: float = 0.0
    evidence_sources: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class ScoreBreakdown:
    """Full auditable score decomposition for an offer or product group."""

    criteria: tuple[CriterionScore, ...]
    weighted_sum: float
    evidence_coverage: float
    confidence_factor: float
    contradiction_count: int
    contradiction_penalty: float
    final_score: float
    semantic_enabled: bool
    semantic_weight_applied: float
    total_cost_brl: Decimal | None = None
    shipping_known: bool = False

    def by_name(self, name: CriterionName) -> CriterionScore | None:
        """Return the criterion entry for ``name`` if present."""
        for item in self.criteria:
            if item.name == name:
                return item
        return None


@dataclass(frozen=True)
class Explanation:
    """Deterministic, evidence-grounded explanation in PT-BR for a ranked result."""

    summary: str
    rank: int = 0
    total_score: float = 0.0
    reasons: tuple[str, ...] = ()
    tradeoffs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    missing_data: tuple[str, ...] = ()
    penalties: tuple[str, ...] = ()
    relaxation_notes: tuple[str, ...] = ()
    evidence_citations: tuple[str, ...] = ()
    offer_comparison_note: str | None = None
    profile_id: str | None = None
    approval_state: ApprovalState = ApprovalState.STRICTLY_APPROVED


@dataclass(frozen=True)
class RankedOffer:
    """Level-2 ranked offer preserved inside a product group."""

    rank: int
    item_id: str
    product_group_id: str
    candidate: Candidate
    validation: CandidateValidationResult
    approval_state: ApprovalState
    score_breakdown: ScoreBreakdown
    explanation: Explanation
    semantic_score: float | None = None
    is_best_offer_in_group: bool = False


@dataclass(frozen=True)
class RankedProductGroup:
    """Level-1 ranked product group with all preserved distinct offers."""

    rank: int
    product_group_id: str
    canonical_title: str
    detected_category: str | None
    approval_state: ApprovalState
    representative_offer: RankedOffer
    offers: tuple[RankedOffer, ...]
    offer_count: int
    score_breakdown: ScoreBreakdown
    explanation: Explanation


@dataclass(frozen=True)
class RejectedCandidateSummary:
    """Auditable summary of a candidate rejected before reranking."""

    item_id: str
    title: str
    reasons: tuple[str, ...]
    hard_not_satisfied_count: int
    hard_unknown_count: int


@dataclass(frozen=True)
class RankingConfig:
    """Runtime configuration for Stage 6 reranking."""

    enable_semantic_score: bool = True
    semantic_max_weight: float = 0.15
    min_confidence_floor: float = 0.75
    contradiction_penalty_per_item: float = 0.08
    max_contradiction_penalty: float = 0.25
    relaxation_score_factor: float = 0.95
    insufficient_evidence_threshold: float = 0.55
    strict_unknown_hard_as_rejected: bool = True


@dataclass(frozen=True)
class RankingPipelineResult:
    """Output contract of ``RankingPipeline.run``."""

    ranked_groups: tuple[RankedProductGroup, ...] = ()
    ranked_offers: tuple[RankedOffer, ...] = ()
    rejected_candidates: tuple[RejectedCandidateSummary, ...] = ()
    active_profile_id: str | None = None
    detected_category: str | None = None
    relaxed: bool = False
    relaxations: tuple[RelaxationStep, ...] = field(default_factory=tuple)
    hard_constraints_preserved: bool = True
    semantic_enabled: bool = True
    total_candidates_input: int = 0
    eligible_offers_count: int = 0
    unique_products_count: int = 0
    rejected_count: int = 0
