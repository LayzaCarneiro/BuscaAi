"""Integração seletiva com `getItem` / `enrich_candidates` respeitando cota e não persistência (T5.4)."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from buscai.catalog.errors import (
    CatalogError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from buscai.observability import log_operational_event
from buscai.search.backend import SearchBackend
from buscai.search.ebay_mapper import merge_candidate_details
from buscai.search.models import Candidate
from buscai.search.quota import RequestCallBudget
from buscai.validation.models import CandidateValidationResult, EnrichmentSummary


class SelectiveEnricher:
    """Enriquece seletivamente via `getItem` apenas candidatos que realmente precisam de evidência."""

    def __init__(
        self,
        backend: SearchBackend | None,
        *,
        max_items: int = 6,
        max_concurrency: int = 4,
        logger: logging.Logger | None = None,
    ) -> None:
        if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 0:
            raise ValueError("max_items deve ser um inteiro não negativo.")
        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or max_concurrency < 1:
            raise ValueError("max_concurrency deve ser um inteiro positivo.")
        self._backend = backend
        self._max_items = max_items
        self._max_concurrency = max_concurrency
        self._logger = logger

    def enrich(
        self,
        validations: Sequence[CandidateValidationResult],
        *,
        ambiguous_item_ids: frozenset[str] = frozenset(),
        budget: RequestCallBudget | None = None,
        request_id: str | None = None,
    ) -> tuple[tuple[Candidate, ...], EnrichmentSummary]:
        candidates = [v.candidate for v in validations]
        if self._backend is None or self._max_items == 0 or not candidates:
            return tuple(candidates), EnrichmentSummary()

        # Prioridade de enriquecimento:
        # 1) Elegíveis com hard_unknown > 0
        # 2) Envolvidos em ambiguidade de variante/modelo/condição
        # 3) Elegíveis com soft_unknown > 0 ou contradições internas
        scored_targets: list[tuple[tuple[int, int, int, int], int]] = []
        for idx, val in enumerate(validations):
            cand = val.candidate
            if cand.details_loaded or not val.eligible or not val.needs_enrichment:
                continue
            is_ambig = 1 if cand.item_id in ambiguous_item_ids else 0
            priority_key = (
                1 if val.hard_unknown_count > 0 else 0,
                is_ambig,
                1 if val.contradictions else 0,
                val.soft_unknown_count,
            )
            scored_targets.append((priority_key, idx))

        if not scored_targets:
            return tuple(candidates), EnrichmentSummary()

        scored_targets.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        cap = self._max_items
        skipped_budget = 0
        if budget is not None:
            rem = budget.remaining_get_item_calls
            if len(scored_targets) > rem:
                skipped_budget = min(len(scored_targets), cap) - rem
            cap = min(cap, rem)

        selected_indices = [idx for _, idx in scored_targets[: max(0, cap)]]
        selected_ids = tuple(candidates[idx].item_id for idx in selected_indices)
        if not selected_indices:
            return tuple(candidates), EnrichmentSummary(skipped_due_to_budget=max(0, skipped_budget))

        calls_before = budget.get_item_calls_used if budget is not None else 0
        enriched_ids: list[str] = []
        unchanged_ids: list[str] = []
        partial_failures = 0

        # Reutiliza `enrich_candidates` quando o backend expõe o método oficial do EbayLiveBackend.
        enrich_fn = getattr(self._backend, "enrich_candidates", None)
        if callable(enrich_fn):
            selected_cands = [candidates[idx] for idx in selected_indices]
            try:
                updated = enrich_fn(
                    selected_cands,
                    max_items=len(selected_cands),
                    max_concurrency=self._max_concurrency,
                    budget=budget,
                    request_id=request_id,
                )
            except (ProviderRateLimitError, ProviderTimeoutError, ProviderUnavailableError, CatalogError) as exc:
                partial_failures += len(selected_cands)
                self._log_warning(exc_type=type(exc).__name__, request_id=request_id)
                updated = tuple(selected_cands)

            for idx, new_cand in zip(selected_indices, updated):
                if new_cand.details_loaded and new_cand is not candidates[idx]:
                    candidates[idx] = new_cand
                    enriched_ids.append(new_cand.item_id)
                else:
                    unchanged_ids.append(candidates[idx].item_id)
        else:
            for idx in selected_indices:
                summary = candidates[idx]
                try:
                    detail = self._backend.get_item_details(
                        summary.item_id,
                        budget=budget,
                        request_id=request_id,
                    )
                except (ProviderRateLimitError, ProviderTimeoutError, ProviderUnavailableError, CatalogError) as exc:
                    partial_failures += 1
                    unchanged_ids.append(summary.item_id)
                    self._log_warning(exc_type=type(exc).__name__, request_id=request_id)
                    continue
                if detail is not None:
                    candidates[idx] = merge_candidate_details(summary, detail)
                    enriched_ids.append(summary.item_id)
                else:
                    unchanged_ids.append(summary.item_id)

        calls_used = (
            (budget.get_item_calls_used - calls_before)
            if budget is not None
            else len(selected_indices)
        )
        return tuple(candidates), EnrichmentSummary(
            selected_item_ids=selected_ids,
            enriched_item_ids=tuple(enriched_ids),
            unchanged_item_ids=tuple(unchanged_ids),
            skipped_due_to_budget=max(0, skipped_budget),
            partial_failures=partial_failures,
            calls_used=max(0, calls_used),
        )

    def _log_warning(self, *, exc_type: str, request_id: str | None) -> None:
        if self._logger is not None:
            log_operational_event(
                self._logger,
                logging.WARNING,
                "selective_enrichment_partial_failure",
                stage="validation_enrichment",
                provider="ebay",
                error_type=exc_type,
                request_id=request_id,
            )


__all__ = ["SelectiveEnricher"]
