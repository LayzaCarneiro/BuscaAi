"""Fluxo integrado da Etapa 4: deduplicação híbrida + similaridade semântica em memória."""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from buscai.config import Settings
from buscai.observability import log_operational_event
from buscai.search.models import Candidate, SearchResponse
from buscai.semantic.dedup import DeduplicationConfig, DeduplicationResult, DuplicateGroup, ProductDeduplicator
from buscai.semantic.embeddings import EmbeddingProvider, EmbeddingService
from buscai.semantic.ranker import InMemorySemanticRanker, SemanticScoredCandidate
from buscai.semantic.representation import build_semantic_query_text


def merge_search_responses(responses: Sequence[SearchResponse]) -> tuple[Candidate, ...]:
    """Combina candidatos retornados por uma ou mais respostas `SearchResponse`."""
    if isinstance(responses, (str, bytes)) or not isinstance(responses, Sequence):
        raise ValueError("responses deve ser uma sequência de SearchResponse.")
    merged: list[Candidate] = []
    for resp in responses:
        if not isinstance(resp, SearchResponse):
            raise TypeError("Todos os itens de responses devem ser instâncias de SearchResponse.")
        merged.extend(resp.candidates)
    return tuple(merged)


class SemanticProcessingResult(BaseModel):
    """Resultado transitório da Etapa 4 preservando todas as ofertas distintas e grupos de produto."""

    model_config = ConfigDict(frozen=True)

    candidates: tuple[Candidate, ...] = ()
    scored_candidates: tuple[SemanticScoredCandidate, ...] = ()
    product_groups: tuple[DuplicateGroup, ...] = ()
    deduplication: DeduplicationResult = Field(default_factory=DeduplicationResult)
    input_count: int = Field(default=0, ge=0)
    output_count: int = Field(default=0, ge=0)
    unique_products_count: int = Field(default=0, ge=0)
    removed_duplicates_count: int = Field(default=0, ge=0)
    embedding_model: str = ""


class SemanticSearchProcessor:
    """Conecta candidatos da eBay ao processamento semântico + deduplicação em memória."""

    def __init__(
        self,
        embedding_service: EmbeddingService,
        *,
        deduplicator: ProductDeduplicator | None = None,
        ranker: InMemorySemanticRanker | None = None,
        dedup_config: DeduplicationConfig | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._embedding_service = embedding_service
        self._deduplicator = deduplicator or ProductDeduplicator(
            embedding_service=embedding_service,
            config=dedup_config,
            logger=logger,
        )
        self._ranker = ranker or InMemorySemanticRanker(embedding_service, logger=logger)
        self._logger = logger

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        provider: EmbeddingProvider | None = None,
        logger: logging.Logger | None = None,
    ) -> SemanticSearchProcessor:
        """Cria `SemanticSearchProcessor` completo a partir de `Settings`."""
        service = EmbeddingService.from_settings(settings, provider=provider, logger=logger)
        dedup_cfg = DeduplicationConfig.from_settings(settings)
        return cls(embedding_service=service, dedup_config=dedup_cfg, logger=logger)

    def process(
        self,
        query_text: str,
        candidates_or_responses: Sequence[Candidate] | Sequence[SearchResponse],
        *,
        intent: object | None = None,
        category_hint: str | None = None,
        request_id: str | None = None,
    ) -> SemanticProcessingResult:
        """Executa deduplicação de ofertas/produtos e ordenação semântica em memória."""
        clean_query = build_semantic_query_text(query_text, intent=intent)
        raw_candidates = self._normalize_inputs(candidates_or_responses)
        model_info = self._embedding_service.model_info()
        model_name = model_info.model_name

        if not raw_candidates:
            return SemanticProcessingResult(embedding_model=model_name)

        started = time.monotonic()

        # Quando o modelo exige prefixos assimétricos (ex.: E5 `query:` vs `passage:`),
        # a deduplicação usa similaridade simétrica (`query:`) e o ranking usa `passage:`,
        # conforme model card oficial. Caso contrário, reaproveita os vetores em um único lote.
        if model_info.requires_prefixes:
            dedup_result = self._deduplicator.deduplicate(
                raw_candidates,
                category_hint=category_hint,
                request_id=request_id,
            )
            canonical_vectors = None
        else:
            candidate_vectors = self._embedding_service.embed_candidates(
                raw_candidates,
                mode="retrieval_document",
                category_hint=category_hint,
                request_id=request_id,
            )
            dedup_result = self._deduplicator.deduplicate(
                raw_candidates,
                vectors=candidate_vectors,
                category_hint=category_hint,
                request_id=request_id,
            )

            idx_by_identity = {id(cand): idx for idx, cand in enumerate(raw_candidates)}
            idx_by_item_id: dict[str, int] = {}
            for idx, cand in enumerate(raw_candidates):
                idx_by_item_id.setdefault(cand.item_id, idx)

            preserved_indices: list[int] = []
            can_slice = True
            for cand in dedup_result.candidates:
                mapped_idx = idx_by_identity.get(id(cand), idx_by_item_id.get(cand.item_id))
                if mapped_idx is None:
                    can_slice = False
                    break
                preserved_indices.append(mapped_idx)

            canonical_vectors = candidate_vectors[preserved_indices] if can_slice else None
            del candidate_vectors

        scored_raw = self._ranker.rank(
            clean_query,
            dedup_result.candidates,
            candidate_vectors=canonical_vectors,
            request_id=request_id,
        )
        del canonical_vectors

        group_by_item_id: dict[str, str] = {}
        for group in dedup_result.groups:
            gid = group.group_id or f"product:{group.canonical.item_id}"
            for offer in group.offers or (group.canonical,):
                group_by_item_id[offer.item_id] = gid

        scored = tuple(
            entry.model_copy(update={"product_group_id": group_by_item_id.get(entry.candidate.item_id)})
            for entry in scored_raw
        )
        ordered_candidates = tuple(entry.candidate for entry in scored)

        if self._logger is not None:
            log_operational_event(
                self._logger,
                logging.INFO,
                "semantic_pipeline_completed",
                stage="semantic_pipeline",
                candidate_count=len(raw_candidates),
                result_count=len(ordered_candidates),
                duration_ms=round((time.monotonic() - started) * 1000.0, 2),
                request_id=request_id,
            )

        return SemanticProcessingResult(
            candidates=ordered_candidates,
            scored_candidates=scored,
            product_groups=dedup_result.groups,
            deduplication=dedup_result,
            input_count=len(raw_candidates),
            output_count=len(ordered_candidates),
            unique_products_count=len(dedup_result.groups),
            removed_duplicates_count=dedup_result.removed_count,
            embedding_model=model_name,
        )

    @staticmethod
    def _normalize_inputs(
        items: Sequence[Candidate] | Sequence[SearchResponse],
    ) -> tuple[Candidate, ...]:
        if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
            raise ValueError("Entrada de candidatos deve ser uma sequência de Candidate ou SearchResponse.")
        if not items:
            return ()
        first = items[0]
        if isinstance(first, SearchResponse):
            return merge_search_responses(items)  # type: ignore[arg-type]
        if isinstance(first, Candidate):
            for entry in items:
                if not isinstance(entry, Candidate):
                    raise TypeError("Todos os itens da sequência devem ser Candidate.")
            return tuple(items)  # type: ignore[arg-type]
        raise TypeError("Entrada deve conter apenas instâncias de Candidate ou SearchResponse.")


__all__ = [
    "SemanticProcessingResult",
    "SemanticSearchProcessor",
    "merge_search_responses",
]
