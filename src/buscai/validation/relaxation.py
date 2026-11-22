"""Mecanismo determinístico e transparente de relaxamento controlado de preferências (T5.5)."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from buscai.catalog.errors import CatalogError
from buscai.catalog_registry import ProfileRegistry, load_default_registry
from buscai.observability import log_operational_event
from buscai.search.backend import SearchBackend
from buscai.search.models import Candidate
from buscai.search.query_builder import QueryBuilder
from buscai.search.quota import RequestCallBudget
from buscai.validation.models import CandidateValidationResult, RelaxationStep
from buscai.validation.validator import EvidenceValidator
from userintent.schema import UserIntent


class RelaxationConfig(BaseModel):
    """Limites configuráveis do relaxamento controlado de preferências flexíveis."""

    model_config = ConfigDict(frozen=True)

    min_eligible_results: int = Field(default=3, ge=1, le=50)
    max_relaxation_steps: int = Field(default=3, ge=0, le=10)
    max_extra_search_calls: int = Field(default=2, ge=0, le=5)


class RelaxationEngine:
    """Relaxa apenas preferências flexíveis em ordem determinística, preservando hard constraints."""

    def __init__(
        self,
        *,
        config: RelaxationConfig | None = None,
        registry: ProfileRegistry | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config or RelaxationConfig()
        self._registry = registry or load_default_registry()
        self._logger = logger

    def run(
        self,
        *,
        candidates: Sequence[Candidate],
        initial_validations: Sequence[CandidateValidationResult],
        intent: UserIntent,
        validator: EvidenceValidator,
        profile_id: str | None = None,
        ambiguous_item_ids: frozenset[str] = frozenset(),
        search_backend: SearchBackend | None = None,
        query_builder: QueryBuilder | None = None,
        budget: RequestCallBudget | None = None,
        seen_query_fingerprints: set[tuple[object, ...]] | None = None,
        request_id: str | None = None,
    ) -> tuple[tuple[Candidate, ...], tuple[CandidateValidationResult, ...], tuple[RelaxationStep, ...]]:
        current_candidates = list(candidates)
        current_validations = tuple(initial_validations)
        eligible_count = sum(1 for v in current_validations if v.eligible)

        if (
            eligible_count >= self._config.min_eligible_results
            or self._config.max_relaxation_steps == 0
        ):
            return tuple(current_candidates), current_validations, ()

        # Monta fila ordenada de preferências estritamente relaxáveis (nunca hard constraints).
        relax_queue: list[tuple[str, str, str, str]] = []
        for pref in intent.preferred_features:
            relax_queue.append(
                (
                    "preferred_feature",
                    pref,
                    "omitido",
                    "Preferência opcional da consulta relaxada por insuficiência de resultados elegíveis.",
                )
            )

        active_profile = self._registry.resolve_profile_for_intent(intent, explicit_profile=profile_id)
        cat_def = self._registry.resolve_category(intent.category)
        canonical_cat = cat_def.canonical_id if cat_def is not None else intent.category
        if active_profile is not None and canonical_cat is not None:
            soft_profile_criteria = sorted(
                (
                    c
                    for c in self._registry.criteria_for(active_profile.profile_id, canonical_cat)
                    if c.priority == "soft" and c.relaxable
                ),
                key=lambda c: (c.relaxation_order, c.criterion_id),
            )
            for crit in soft_profile_criteria:
                relax_queue.append(
                    (
                        "profile_criterion",
                        crit.criterion_id,
                        "ignorado",
                        f"Critério flexível do perfil ({crit.description_pt or crit.attribute}) relaxado.",
                    )
                )

        if (
            not intent.price.is_hard_constraint
            and (intent.price.min_price is not None or intent.price.max_price is not None)
        ):
            relax_queue.append(
                (
                    "soft_budget",
                    f"{intent.price.max_price} {intent.price.currency or ''}".strip(),
                    "sem teto flexível",
                    "Orçamento flexível (is_hard_constraint=False) relaxado por baixa cobertura.",
                )
            )

        relaxed_preferred: set[str] = set()
        relaxed_criteria: set[str] = set()
        ignore_soft_budget = False
        steps: list[RelaxationStep] = []
        extra_calls_used = 0
        known_fps = set(seen_query_fingerprints or ())
        known_item_ids = {c.item_id for c in current_candidates}

        for kind, target_val, relaxed_to, reason in relax_queue:
            if len(steps) >= self._config.max_relaxation_steps:
                break
            if eligible_count >= self._config.min_eligible_results:
                break

            if kind == "preferred_feature":
                relaxed_preferred.add(" ".join(target_val.strip().casefold().split()))
            elif kind == "profile_criterion":
                relaxed_criteria.add(target_val)
            elif kind == "soft_budget":
                if intent.price.is_hard_constraint:
                    continue
                ignore_soft_budget = True

            triggered_search = False
            if (
                search_backend is not None
                and query_builder is not None
                and extra_calls_used < self._config.max_extra_search_calls
                and (budget is None or budget.remaining_search_calls > 0)
            ):
                relaxed_intent = self._build_relaxed_intent(
                    intent,
                    relaxed_preferred=relaxed_preferred,
                    ignore_soft_budget=ignore_soft_budget,
                )
                plan = query_builder.build(relaxed_intent)
                for planned in plan.queries:
                    if extra_calls_used >= self._config.max_extra_search_calls:
                        break
                    if budget is not None and budget.remaining_search_calls <= 0:
                        break
                    fp = (
                        planned.query.query_text.casefold(),
                        planned.query.min_price_usd,
                        planned.query.max_price_usd,
                        planned.query.conditions,
                        planned.query.condition_ids,
                    )
                    if fp in known_fps:
                        continue
                    known_fps.add(fp)
                    try:
                        resp = search_backend.search(planned.query, budget=budget, request_id=request_id)
                        triggered_search = True
                        extra_calls_used += 1
                        for cand in resp.candidates:
                            if cand.item_id not in known_item_ids:
                                known_item_ids.add(cand.item_id)
                                current_candidates.append(cand)
                    except CatalogError as exc:
                        if self._logger is not None:
                            log_operational_event(
                                self._logger,
                                logging.WARNING,
                                "relaxation_search_failed",
                                stage="validation_relaxation",
                                provider="ebay",
                                error_type=type(exc).__name__,
                                request_id=request_id,
                            )
                        break

            # Revalida mantendo todas as hard constraints intactas.
            current_validations = tuple(
                validator.validate_candidate(
                    cand,
                    intent,
                    profile_id=profile_id,
                    relaxed_criteria_ids=frozenset(relaxed_criteria),
                    relaxed_preferred_features=frozenset(relaxed_preferred),
                    ignore_soft_budget=ignore_soft_budget,
                    ambiguous_item_ids=ambiguous_item_ids,
                )
                for cand in current_candidates
            )
            eligible_count = sum(1 for v in current_validations if v.eligible)

            steps.append(
                RelaxationStep(
                    step_index=len(steps) + 1,
                    field=kind,
                    original_value=target_val,
                    relaxed_to=relaxed_to,
                    reason=reason,
                    triggered_search=triggered_search,
                    eligible_count_after=eligible_count,
                )
            )

        return tuple(current_candidates), current_validations, tuple(steps)

    @staticmethod
    def _build_relaxed_intent(
        intent: UserIntent,
        *,
        relaxed_preferred: set[str],
        ignore_soft_budget: bool,
    ) -> UserIntent:
        new_prefs = [
            p for p in intent.preferred_features if " ".join(p.strip().casefold().split()) not in relaxed_preferred
        ]
        updates: dict[str, object] = {"preferred_features": new_prefs}
        if ignore_soft_budget and not intent.price.is_hard_constraint:
            updates["price"] = intent.price.model_copy(update={"min_price": None, "max_price": None})
        return intent.model_copy(update=updates)


__all__ = ["RelaxationConfig", "RelaxationEngine"]
