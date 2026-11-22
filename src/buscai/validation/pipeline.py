"""Orquestração da Etapa 5: validação por evidências + `getItem` seletivo + relaxamento controlado (T5.6)."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from buscai.catalog_registry import ProfileRegistry, load_default_registry
from buscai.observability import log_operational_event
from buscai.pricing import CurrencyConverter
from buscai.search.backend import SearchBackend
from buscai.search.models import Candidate, SearchResponse
from buscai.search.query_builder import QueryBuilder
from buscai.search.quota import RequestCallBudget
from buscai.semantic.dedup import DuplicateGroup, ProductDeduplicator
from buscai.semantic.pipeline import SemanticProcessingResult, merge_search_responses
from buscai.validation.enrichment import SelectiveEnricher
from buscai.validation.models import CandidateValidationResult, ValidationPipelineResult
from buscai.validation.relaxation import RelaxationConfig, RelaxationEngine
from buscai.validation.validator import EvidenceValidator
from userintent.schema import UserIntent


class ValidationPipeline:
    """Conecta busca/semântica à validação por evidências, enriquecimento seletivo e relaxamento."""

    def __init__(
        self,
        *,
        registry: ProfileRegistry | None = None,
        currency_converter: CurrencyConverter | None = None,
        search_backend: SearchBackend | None = None,
        query_builder: QueryBuilder | None = None,
        deduplicator: ProductDeduplicator | None = None,
        relaxation_config: RelaxationConfig | None = None,
        max_enrich_items: int = 6,
        max_enrich_concurrency: int = 4,
        require_shipping_for_total: bool = True,
        logger: logging.Logger | None = None,
    ) -> None:
        self._registry = registry or load_default_registry()
        self._validator = EvidenceValidator(
            registry=self._registry,
            currency_converter=currency_converter,
            require_shipping_for_total=require_shipping_for_total,
        )
        self._enricher = SelectiveEnricher(
            search_backend,
            max_items=max_enrich_items,
            max_concurrency=max_enrich_concurrency,
            logger=logger,
        )
        self._relaxation = RelaxationEngine(
            config=relaxation_config,
            registry=self._registry,
            logger=logger,
        )
        self._search_backend = search_backend
        self._query_builder = query_builder
        self._deduplicator = deduplicator
        self._logger = logger

    def run(
        self,
        intent: UserIntent,
        candidates_input: SemanticProcessingResult | Sequence[Candidate] | Sequence[SearchResponse],
        *,
        profile_id: str | None = None,
        budget: RequestCallBudget | None = None,
        request_id: str | None = None,
    ) -> ValidationPipelineResult:
        candidates, existing_groups, ambiguous_ids = self._extract_inputs(candidates_input)
        active_profile = self._registry.resolve_profile_for_intent(intent, explicit_profile=profile_id)
        active_profile_id = active_profile.profile_id if active_profile is not None else None

        if not candidates:
            return ValidationPipelineResult(
                active_profile_id=active_profile_id,
                total_candidates_evaluated=0,
            )

        # 1. Validação inicial
        initial_validations = tuple(
            self._validator.validate_candidate(
                cand,
                intent,
                profile_id=active_profile_id,
                ambiguous_item_ids=ambiguous_ids,
            )
            for cand in candidates
        )

        # 2. Enriquecimento seletivo via getItem
        enriched_candidates, enrich_summary = self._enricher.enrich(
            initial_validations,
            ambiguous_item_ids=ambiguous_ids,
            budget=budget,
            request_id=request_id,
        )

        # Se houve enriquecimento em candidatos, revalida (e re-agrupa se deduplicator disponível).
        current_groups = existing_groups
        if enrich_summary.enriched_item_ids:
            if self._deduplicator is not None and len(enriched_candidates) > 1:
                try:
                    dedup_res = self._deduplicator.deduplicate(
                        enriched_candidates,
                        category_hint=intent.category,
                        request_id=request_id,
                    )
                    enriched_candidates = dedup_res.candidates
                    current_groups = dedup_res.groups
                    ambiguous_ids = frozenset(
                        pair.left_item_id for pair in dedup_res.similar_pairs
                    ) | frozenset(pair.right_item_id for pair in dedup_res.similar_pairs)
                except ValueError:
                    pass

            post_enrich_validations = tuple(
                self._validator.validate_candidate(
                    cand,
                    intent,
                    profile_id=active_profile_id,
                    ambiguous_item_ids=ambiguous_ids,
                )
                for cand in enriched_candidates
            )
        else:
            post_enrich_validations = initial_validations

        # 3. Relaxamento controlado de preferências flexíveis (nunca hard constraints)
        final_candidates, final_validations, relaxations = self._relaxation.run(
            candidates=enriched_candidates,
            initial_validations=post_enrich_validations,
            intent=intent,
            validator=self._validator,
            profile_id=active_profile_id,
            ambiguous_item_ids=ambiguous_ids,
            search_backend=self._search_backend,
            query_builder=self._query_builder,
            budget=budget,
            request_id=request_id,
        )

        eligible = tuple(v for v in final_validations if v.eligible)
        rejected = tuple(v for v in final_validations if not v.eligible)

        if self._logger is not None:
            log_operational_event(
                self._logger,
                logging.INFO,
                "validation_pipeline_completed",
                stage="validation",
                candidate_count=len(final_candidates),
                result_count=len(eligible),
                relaxation_count=len(relaxations),
                ebay_calls=enrich_summary.calls_used,
                request_id=request_id,
            )

        return ValidationPipelineResult(
            eligible_candidates=eligible,
            rejected_candidates=rejected,
            all_validations=final_validations,
            product_groups=current_groups,
            active_profile_id=active_profile_id,
            enrichment=enrich_summary,
            relaxations=relaxations,
            relaxed=bool(relaxations),
            hard_constraints_preserved=True,
            total_candidates_evaluated=len(final_candidates),
        )

    @staticmethod
    def _extract_inputs(
        inputs: SemanticProcessingResult | Sequence[Candidate] | Sequence[SearchResponse],
    ) -> tuple[tuple[Candidate, ...], tuple[DuplicateGroup, ...], frozenset[str]]:
        if isinstance(inputs, SemanticProcessingResult):
            ambiguous: set[str] = set()
            for pair in inputs.deduplication.similar_pairs:
                ambiguous.add(pair.left_item_id)
                ambiguous.add(pair.right_item_id)
            return inputs.candidates, inputs.product_groups, frozenset(ambiguous)

        if isinstance(inputs, (str, bytes)) or not isinstance(inputs, Sequence):
            raise ValueError("candidates_input deve ser SemanticProcessingResult, sequência de Candidate ou SearchResponse.")
        if not inputs:
            return (), (), frozenset()
        first = inputs[0]
        if isinstance(first, SearchResponse):
            return merge_search_responses(inputs), (), frozenset()  # type: ignore[arg-type]
        if isinstance(first, Candidate):
            return tuple(inputs), (), frozenset()  # type: ignore[arg-type]
        raise TypeError("candidates_input contém tipo incompatível.")


__all__ = ["ValidationPipeline"]
