"""Fluxo integrado da Etapa 4: deduplicação híbrida + similaridade semântica em memória."""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from buscai.config import Settings
from buscai.observability import log_operational_event
from buscai.search.models import Candidate, SearchResponse
from buscai.semantic.dedup import DeduplicationConfig, DeduplicationResult, ProductDeduplicator
from buscai.semantic.embeddings import EmbeddingProvider, EmbeddingService
from buscai.semantic.ranker import InMemorySemanticRanker, SemanticScoredCandidate
from buscai.semantic.representation import build_query_embedding_text


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
    """Resultado transitório da Etapa 4 sem retenção de vetores de embeddings."""

    model_config = ConfigDict(frozen=True)

    candidates: tuple[Candidate, ...] = ()
    scored_candidates: tuple[SemanticScoredCandidate, ...] = ()
    deduplication: DeduplicationResult = Field(default_factory=DeduplicationResult)
    input_count: int = Field(default=0, ge=0)
    output_count: int = Field(default=0, ge=0)
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
        category_hint: str | None = None,
        request_id: str | None = None,
    ) -> SemanticProcessingResult:
        """Executa embedding transitório, deduplicação híbrida e ordenação semântica em memória."""
        clean_query = build_query_embedding_text(query_text)
        raw_candidates = self._normalize_inputs(candidates_or_responses)
        model_name = self._embedding_service.model_info().model_name

        if not raw_candidates:
            return SemanticProcessingResult(embedding_model=model_name)

        started = time.monotonic()

        # Gera embeddings dos candidatos uma única vez em lote e mantém o array somente
        # durante esta chamada.
        candidate_vectors = self._embedding_service.embed_candidates(raw_candidates, request_id=request_id)
        dedup_result = self._deduplicator.deduplicate(
            raw_candidates,
            vectors=candidate_vectors,
            category_hint=category_hint,
            request_id=request_id,
        )

        # Reaproveita os vetores dos candidatos canônicos escolhidos para evitar segunda inferência.
        idx_by_identity = {id(cand): idx for idx, cand in enumerate(raw_candidates)}
        idx_by_item_id: dict[str, int] = {}
        for idx, cand in enumerate(raw_candidates):
            idx_by_item_id.setdefault(cand.item_id, idx)

        canonical_indices: list[int] = []
        can_slice = True
        for cand in dedup_result.candidates:
            mapped_idx = idx_by_identity.get(id(cand), idx_by_item_id.get(cand.item_id))
            if mapped_idx is None:
                can_slice = False
                break
            canonical_indices.append(mapped_idx)

        canonical_vectors = candidate_vectors[canonical_indices] if can_slice else None
        del candidate_vectors

        scored = self._ranker.rank(
            clean_query,
            dedup_result.candidates,
            candidate_vectors=canonical_vectors,
            request_id=request_id,
        )
        del canonical_vectors

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
            deduplication=dedup_result,
            input_count=len(raw_candidates),
            output_count=len(ordered_candidates),
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
