"""Ordenação semântica multilíngue estritamente em memória sobre candidatos efêmeros."""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from buscai.observability import log_operational_event
from buscai.search.models import Candidate
from buscai.semantic.embeddings import (
    EmbeddingService,
    clamp_unit_scores,
    cosine_similarity_matrix,
)
from buscai.semantic.representation import build_query_embedding_text


class SemanticScoredCandidate(BaseModel):
    """Par candidato + score de similaridade semântica normalizado em `[0.0, 1.0]`."""

    model_config = ConfigDict(frozen=True)

    candidate: Candidate
    semantic_score: float = Field(ge=0.0, le=1.0)


class InMemorySemanticRanker:
    """Computa similaridade semântica PT-BR <-> EN-US exclusivamente em memória."""

    def __init__(
        self,
        embedding_service: EmbeddingService,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._embedding_service = embedding_service
        self._logger = logger

    def rank(
        self,
        query_text: str,
        candidates: Sequence[Candidate],
        *,
        candidate_vectors: np.ndarray | None = None,
        request_id: str | None = None,
    ) -> tuple[SemanticScoredCandidate, ...]:
        """Ordena candidatos por cosseno normalizado `[0.0, 1.0]` sem reter vetores."""
        clean_query = build_query_embedding_text(query_text)
        if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
            raise ValueError("candidates deve ser uma sequência válida de Candidate.")
        if not candidates:
            return ()

        started = time.monotonic()
        query_vec = self._embedding_service.embed_query(clean_query, request_id=request_id)
        if candidate_vectors is not None:
            local_cand_vecs = np.asarray(candidate_vectors, dtype=np.float32)
            if local_cand_vecs.ndim != 2 or local_cand_vecs.shape[0] != len(candidates):
                raise ValueError(
                    "candidate_vectors deve possuir o mesmo número de linhas que candidates."
                )
        else:
            local_cand_vecs = self._embedding_service.embed_candidates(candidates, request_id=request_id)

        raw_sims = cosine_similarity_matrix(query_vec, local_cand_vecs)[0]
        unit_scores = clamp_unit_scores(raw_sims)
        # Libera referências aos arrays transitórios imediatamente após o cálculo escalar.
        del query_vec, local_cand_vecs, raw_sims

        scored = [
            (float(unit_scores[idx]), -idx, cand)
            for idx, cand in enumerate(candidates)
        ]
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)

        result = tuple(
            SemanticScoredCandidate(
                candidate=cand,
                semantic_score=round(score, 6),
            )
            for score, _, cand in scored
        )

        if self._logger is not None:
            log_operational_event(
                self._logger,
                logging.INFO,
                "semantic_ranking_completed",
                stage="semantic_ranking",
                candidate_count=len(candidates),
                result_count=len(result),
                duration_ms=round((time.monotonic() - started) * 1000.0, 2),
                request_id=request_id,
            )

        return result


__all__ = ["InMemorySemanticRanker", "SemanticScoredCandidate"]
