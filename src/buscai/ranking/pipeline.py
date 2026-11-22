"""End-to-end Stage 6 ranking pipeline with sanitized operational logging."""

from __future__ import annotations

import logging
import time

from buscai.catalog_registry import ProfileRegistry, load_default_registry
from buscai.observability.logging import log_operational_event
from buscai.pricing.currency import CurrencyConverter
from buscai.ranking.criteria import default_currency_converter
from buscai.ranking.models import RankingConfig, RankingPipelineResult
from buscai.ranking.reranker import MultiCriteriaReranker
from buscai.ranking.weights import DEFAULT_PROFILE_ID, WeightRegistry
from buscai.semantic.pipeline import SemanticProcessingResult
from buscai.validation.models import ValidationPipelineResult

_LOGGER = logging.getLogger("buscai.ranking")


class RankingPipeline:
    """Orchestrates Stage 6 reranking and explainability over validated candidates."""

    def __init__(
        self,
        *,
        config: RankingConfig | None = None,
        weight_registry: WeightRegistry | None = None,
        converter: CurrencyConverter | None = None,
        profile_registry: ProfileRegistry | None = None,
    ) -> None:
        self.config = config or RankingConfig()
        self.profile_registry = profile_registry or load_default_registry()
        self.weight_registry = weight_registry or WeightRegistry(
            profile_registry=self.profile_registry
        )
        self.converter = converter or default_currency_converter()
        self.reranker = MultiCriteriaReranker(
            config=self.config,
            weight_registry=self.weight_registry,
            converter=self.converter,
        )

    def run(
        self,
        validation_result: ValidationPipelineResult,
        *,
        semantic_result: SemanticProcessingResult | None = None,
        profile_id: str | None = None,
        category_hint: str | None = None,
        request_id: str | None = None,
    ) -> RankingPipelineResult:
        """Execute two-level reranking and return structured groups, offers, and explanations."""
        start = time.perf_counter()

        raw_profile = profile_id or validation_result.active_profile_id
        resolved_profile_obj = self.profile_registry.resolve_profile(raw_profile)
        resolved_profile = (
            getattr(resolved_profile_obj, "profile_id", None)
            or getattr(resolved_profile_obj, "id", None)
            or raw_profile
            or DEFAULT_PROFILE_ID
        )

        resolved_category_obj = self.profile_registry.resolve_category(category_hint)
        resolved_category = (
            getattr(resolved_category_obj, "canonical_id", None)
            or getattr(resolved_category_obj, "category_id", None)
            or getattr(resolved_category_obj, "id", None)
            or category_hint
        )

        ranked_groups, ranked_offers, rejected = self.reranker.rerank(
            validation_result,
            semantic_result=semantic_result,
            profile_id=resolved_profile,
            category_hint=resolved_category,
        )

        detected_cat = resolved_category or (
            ranked_groups[0].detected_category if ranked_groups else None
        )
        duration_ms = round((time.perf_counter() - start) * 1000.0, 2)

        log_operational_event(
            _LOGGER,
            logging.INFO,
            "ranking_completed",
            request_id=request_id or "offline",
            stage="ranking",
            duration_ms=duration_ms,
            candidate_count=validation_result.total_candidates_evaluated,
            result_count=len(ranked_groups),
            relaxation_count=len(validation_result.relaxations),
            provider="local_ranking",
        )

        return RankingPipelineResult(
            ranked_groups=ranked_groups,
            ranked_offers=ranked_offers,
            rejected_candidates=rejected,
            active_profile_id=resolved_profile,
            detected_category=detected_cat,
            relaxed=validation_result.relaxed,
            relaxations=validation_result.relaxations,
            hard_constraints_preserved=validation_result.hard_constraints_preserved,
            semantic_enabled=self.config.enable_semantic_score,
            total_candidates_input=validation_result.total_candidates_evaluated,
            eligible_offers_count=len(ranked_offers),
            unique_products_count=len(ranked_groups),
            rejected_count=len(rejected),
        )
