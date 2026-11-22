"""Stage 6 multi-criteria reranking and deterministic explainability."""

from buscai.ranking.explainer import build_group_explanation, build_offer_explanation
from buscai.ranking.models import (
    ApprovalState,
    CriterionName,
    CriterionScore,
    Explanation,
    RankedOffer,
    RankedProductGroup,
    RankingConfig,
    RankingPipelineResult,
    RejectedCandidateSummary,
    ScoreBreakdown,
)
from buscai.ranking.pipeline import RankingPipeline
from buscai.ranking.reranker import MultiCriteriaReranker
from buscai.ranking.weights import (
    DEFAULT_PROFILE_ID,
    MAX_SEMANTIC_WEIGHT,
    OFFER_RANKING_WEIGHTS,
    PROFILE_WEIGHTS,
    WEIGHT_RATIONALE,
    WeightRegistry,
    WeightVector,
)

__all__ = [
    "ApprovalState",
    "CriterionName",
    "CriterionScore",
    "DEFAULT_PROFILE_ID",
    "Explanation",
    "MAX_SEMANTIC_WEIGHT",
    "MultiCriteriaReranker",
    "OFFER_RANKING_WEIGHTS",
    "PROFILE_WEIGHTS",
    "RankedOffer",
    "RankedProductGroup",
    "RankingConfig",
    "RankingPipeline",
    "RankingPipelineResult",
    "RejectedCandidateSummary",
    "ScoreBreakdown",
    "WEIGHT_RATIONALE",
    "WeightRegistry",
    "WeightVector",
    "build_group_explanation",
    "build_offer_explanation",
]
