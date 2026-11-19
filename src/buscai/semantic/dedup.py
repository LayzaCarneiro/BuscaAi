"""Serviço híbrido de deduplicação de produtos (regras estruturadas + similaridade semântica)."""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Sequence
from decimal import Decimal
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from buscai.config import Settings
from buscai.observability import log_operational_event
from buscai.search.models import Candidate
from buscai.semantic.embeddings import EmbeddingService, cosine_similarity_matrix
from buscai.semantic.representation import (
    ProductStructuredAttributes,
    _VARIANT_TIER_TOKENS,
    extract_product_attributes,
)

PairClassification = Literal["probable_duplicate", "similar_product", "different_product"]


class DeduplicationConfig(BaseModel):
    """Parâmetros configuráveis da deduplicação híbrida em memória."""

    model_config = ConfigDict(frozen=True)

    probable_duplicate_semantic_threshold: float = Field(default=0.86, ge=0.0, le=1.0)
    rephrased_model_semantic_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    similar_product_semantic_threshold: float = Field(default=0.68, ge=0.0, le=1.0)
    screen_size_tolerance_inches: Decimal = Field(default=Decimal("0.3"), ge=0)

    @field_validator(
        "probable_duplicate_semantic_threshold",
        "rephrased_model_semantic_threshold",
        "similar_product_semantic_threshold",
        mode="before",
    )
    @classmethod
    def _reject_bool_threshold(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("Limiar numérico não pode ser booleano.")
        return value

    @field_validator(
        "probable_duplicate_semantic_threshold",
        "rephrased_model_semantic_threshold",
        "similar_product_semantic_threshold",
        mode="after",
    )
    @classmethod
    def _finite_threshold(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Limiar de similaridade deve ser finito.")
        return value

    @model_validator(mode="after")
    def _check_ordering(self) -> DeduplicationConfig:
        if self.similar_product_semantic_threshold > self.rephrased_model_semantic_threshold:
            raise ValueError(
                "similar_product_semantic_threshold deve ser menor ou igual a rephrased_model_semantic_threshold."
            )
        if self.rephrased_model_semantic_threshold > self.probable_duplicate_semantic_threshold:
            raise ValueError(
                "rephrased_model_semantic_threshold deve ser menor ou igual a probable_duplicate_semantic_threshold."
            )
        return self

    @classmethod
    def from_settings(cls, settings: Settings) -> DeduplicationConfig:
        """Constrói configuração a partir de Settings validado."""
        rephrased = min(
            settings.dedup_probable_threshold,
            max(settings.dedup_similar_threshold, round(settings.dedup_probable_threshold - 0.11, 4)),
        )
        return cls(
            probable_duplicate_semantic_threshold=settings.dedup_probable_threshold,
            rephrased_model_semantic_threshold=rephrased,
            similar_product_semantic_threshold=settings.dedup_similar_threshold,
        )


class PairEvaluation(BaseModel):
    """Resultado auditável da comparação par a par entre dois candidatos."""

    model_config = ConfigDict(frozen=True)

    left_item_id: str
    right_item_id: str
    classification: PairClassification
    semantic_similarity: float
    reasons: tuple[str, ...] = ()


class DuplicateGroup(BaseModel):
    """Grupo consolidado de anúncios identificados como duplicatas prováveis."""

    model_config = ConfigDict(frozen=True)

    canonical: Candidate
    duplicate_item_ids: tuple[str, ...] = ()
    max_similarity: float
    reasons: tuple[str, ...] = ()


class DeduplicationResult(BaseModel):
    """Saída transitória da deduplicação preservando apenas metadados e candidatos únicos."""

    model_config = ConfigDict(frozen=True)

    candidates: tuple[Candidate, ...] = ()
    groups: tuple[DuplicateGroup, ...] = ()
    removed_count: int = Field(default=0, ge=0)
    similar_pairs: tuple[PairEvaluation, ...] = ()
    pair_evaluations: tuple[PairEvaluation, ...] = ()


def _strong_model_codes(tokens: tuple[str, ...]) -> set[str]:
    return {tok for tok in tokens if any(ch.isdigit() for ch in tok)}


def _tier_tokens(tokens: tuple[str, ...]) -> set[str]:
    return {tok for tok in tokens if tok in _VARIANT_TIER_TOKENS}


def _check_model_compatibility(
    left: ProductStructuredAttributes,
    right: ProductStructuredAttributes,
) -> tuple[bool, bool, str | None]:
    """Avalia identidade de modelo retornando `(tem_conflito, modelo_confirmado, motivo)`."""
    if not left.model_tokens or not right.model_tokens:
        return False, False, None

    left_tiers = _tier_tokens(left.model_tokens)
    right_tiers = _tier_tokens(right.model_tokens)
    if left_tiers != right_tiers:
        return True, False, "conflicting_model_variants"

    left_codes = _strong_model_codes(left.model_tokens)
    right_codes = _strong_model_codes(right.model_tokens)
    if left_codes and right_codes:
        if left_codes != right_codes:
            if left_codes.isdisjoint(right_codes):
                return True, False, "distinct_model_codes"
            return True, False, "uncertain_submodel_codes"

    left_set = {tok for tok in left.model_tokens if tok not in _VARIANT_TIER_TOKENS}
    right_set = {tok for tok in right.model_tokens if tok not in _VARIANT_TIER_TOKENS}
    if not left_set or not right_set:
        return False, False, None

    intersection = left_set & right_set
    union = left_set | right_set
    if not intersection:
        return True, False, "distinct_model_lines"

    jaccard = len(intersection) / len(union)
    codes_match = bool(left_codes and left_codes == right_codes)
    has_alnum_sku_match = any(any(c.isalpha() for c in code) and any(c.isdigit() for c in code) for code in left_codes)
    if jaccard >= 0.75 or (codes_match and (len(intersection) >= 2 or has_alnum_sku_match)):
        return False, True, "matching_model_identity"
    if jaccard < 0.34:
        return True, False, "divergent_model_tokens"
    return False, False, "partial_model_overlap"


def _candidate_completeness_score(candidate: Candidate, original_index: int) -> tuple[object, ...]:
    return (
        1 if candidate.details_loaded else 0,
        len(candidate.aspects),
        1 if candidate.brand is not None else 0,
        1 if candidate.model is not None else 0,
        1 if candidate.short_description is not None else 0,
        1 if candidate.seller is not None else 0,
        1 if candidate.shipping is not None else 0,
        len(candidate.title),
        -original_index,
    )


class ProductDeduplicator:
    """Identifica duplicatas prováveis combinando sinais estruturados e cosseno semântico."""

    def __init__(
        self,
        embedding_service: EmbeddingService | None = None,
        *,
        config: DeduplicationConfig | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._embedding_service = embedding_service
        self._config = config or DeduplicationConfig()
        self._logger = logger

    @property
    def config(self) -> DeduplicationConfig:
        return self._config

    def classify_pair(
        self,
        left_candidate: Candidate,
        right_candidate: Candidate,
        *,
        left_attrs: ProductStructuredAttributes | None = None,
        right_attrs: ProductStructuredAttributes | None = None,
        semantic_similarity: float,
        category_hint: str | None = None,
    ) -> PairEvaluation:
        """Classifica dois candidatos em duplicata provável, produto semelhante ou produto diferente."""
        if not math.isfinite(semantic_similarity):
            raise ValueError("semantic_similarity deve ser um valor numérico finito.")

        if left_candidate.item_id == right_candidate.item_id:
            return PairEvaluation(
                left_item_id=left_candidate.item_id,
                right_item_id=right_candidate.item_id,
                classification="probable_duplicate",
                semantic_similarity=round(float(semantic_similarity), 4),
                reasons=("same_item_id",),
            )

        l_attr = left_attrs or extract_product_attributes(left_candidate, category_hint=category_hint)
        r_attr = right_attrs or extract_product_attributes(right_candidate, category_hint=category_hint)

        vetoes: list[str] = []

        if (
            l_attr.canonical_category is not None
            and r_attr.canonical_category is not None
            and l_attr.canonical_category != r_attr.canonical_category
        ):
            vetoes.append("category_mismatch")

        if l_attr.brand is not None and r_attr.brand is not None and l_attr.brand != r_attr.brand:
            vetoes.append("brand_mismatch")

        shared_mfg_ids: set[str] = set()
        if l_attr.manufacturer_ids and r_attr.manufacturer_ids:
            shared_mfg_ids = set(l_attr.manufacturer_ids) & set(r_attr.manufacturer_ids)
            if not shared_mfg_ids:
                vetoes.append("manufacturer_id_mismatch")

        if l_attr.storage_gb is not None and r_attr.storage_gb is not None and l_attr.storage_gb != r_attr.storage_gb:
            vetoes.append("storage_mismatch")

        if l_attr.ram_gb is not None and r_attr.ram_gb is not None and l_attr.ram_gb != r_attr.ram_gb:
            vetoes.append("ram_mismatch")

        if l_attr.screen_size_inches is not None and r_attr.screen_size_inches is not None:
            if abs(l_attr.screen_size_inches - r_attr.screen_size_inches) > self._config.screen_size_tolerance_inches:
                vetoes.append("screen_size_mismatch")

        if l_attr.resolution is not None and r_attr.resolution is not None and l_attr.resolution != r_attr.resolution:
            vetoes.append("resolution_mismatch")

        if (
            l_attr.refresh_rate_hz is not None
            and r_attr.refresh_rate_hz is not None
            and l_attr.refresh_rate_hz != r_attr.refresh_rate_hz
        ):
            vetoes.append("refresh_rate_mismatch")

        if l_attr.panel_type is not None and r_attr.panel_type is not None and l_attr.panel_type != r_attr.panel_type:
            vetoes.append("panel_type_mismatch")

        if l_attr.form_factor is not None and r_attr.form_factor is not None and l_attr.form_factor != r_attr.form_factor:
            vetoes.append("form_factor_mismatch")

        if (l_attr.condition_bucket == "parts") != (r_attr.condition_bucket == "parts"):
            vetoes.append("parts_vs_functional_mismatch")

        model_conflict, model_confirmed, model_reason = _check_model_compatibility(l_attr, r_attr)
        if model_conflict and model_reason is not None:
            vetoes.append(model_reason)

        if vetoes:
            classification = self._fallback_non_duplicate_class(
                l_attr, r_attr, semantic_similarity=semantic_similarity
            )
            return PairEvaluation(
                left_item_id=left_candidate.item_id,
                right_item_id=right_candidate.item_id,
                classification=classification,
                semantic_similarity=round(float(semantic_similarity), 4),
                reasons=tuple(vetoes),
            )

        # Incerteza em atributos críticos diferenciadores quando um anúncio informa e o outro omite.
        uncertainty_reasons: list[str] = []
        if not shared_mfg_ids:
            if (l_attr.storage_gb is None) != (r_attr.storage_gb is None):
                uncertainty_reasons.append("uncertain_storage_variant")
            if (l_attr.ram_gb is None) != (r_attr.ram_gb is None):
                uncertainty_reasons.append("uncertain_ram_variant")
            if (l_attr.resolution is None) != (r_attr.resolution is None):
                uncertainty_reasons.append("uncertain_resolution_variant")

        if shared_mfg_ids:
            return PairEvaluation(
                left_item_id=left_candidate.item_id,
                right_item_id=right_candidate.item_id,
                classification="probable_duplicate",
                semantic_similarity=round(float(semantic_similarity), 4),
                reasons=("shared_manufacturer_id",),
            )

        if uncertainty_reasons:
            classification = self._fallback_non_duplicate_class(
                l_attr, r_attr, semantic_similarity=semantic_similarity
            )
            return PairEvaluation(
                left_item_id=left_candidate.item_id,
                right_item_id=right_candidate.item_id,
                classification=classification,
                semantic_similarity=round(float(semantic_similarity), 4),
                reasons=tuple(uncertainty_reasons),
            )

        if model_confirmed and semantic_similarity >= self._config.rephrased_model_semantic_threshold:
            return PairEvaluation(
                left_item_id=left_candidate.item_id,
                right_item_id=right_candidate.item_id,
                classification="probable_duplicate",
                semantic_similarity=round(float(semantic_similarity), 4),
                reasons=("matching_model_identity", "high_semantic_similarity"),
            )

        if (
            semantic_similarity >= self._config.probable_duplicate_semantic_threshold
            and l_attr.brand is not None
            and l_attr.brand == r_attr.brand
            and model_reason != "partial_model_overlap"
        ):
            return PairEvaluation(
                left_item_id=left_candidate.item_id,
                right_item_id=right_candidate.item_id,
                classification="probable_duplicate",
                semantic_similarity=round(float(semantic_similarity), 4),
                reasons=("consistent_brand_and_specs", "high_semantic_similarity"),
            )

        reasons: list[str] = []
        if model_reason is not None:
            reasons.append(model_reason)
        if semantic_similarity < self._config.probable_duplicate_semantic_threshold:
            reasons.append("below_duplicate_threshold")
        classification = self._fallback_non_duplicate_class(
            l_attr, r_attr, semantic_similarity=semantic_similarity
        )
        return PairEvaluation(
            left_item_id=left_candidate.item_id,
            right_item_id=right_candidate.item_id,
            classification=classification,
            semantic_similarity=round(float(semantic_similarity), 4),
            reasons=tuple(reasons) or ("preserved_on_uncertainty",),
        )

    def _fallback_non_duplicate_class(
        self,
        left: ProductStructuredAttributes,
        right: ProductStructuredAttributes,
        *,
        semantic_similarity: float,
    ) -> PairClassification:
        same_category = (
            left.canonical_category is not None and left.canonical_category == right.canonical_category
        )
        same_brand = left.brand is not None and left.brand == right.brand
        if semantic_similarity >= self._config.similar_product_semantic_threshold or (same_category and same_brand):
            return "similar_product"
        return "different_product"

    def deduplicate(
        self,
        candidates: Sequence[Candidate],
        *,
        vectors: np.ndarray | None = None,
        category_hint: str | None = None,
        request_id: str | None = None,
    ) -> DeduplicationResult:
        """Agrupa duplicatas prováveis preservando variantes distintas e produtos semelhantes."""
        if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
            raise ValueError("candidates deve ser uma sequência válida de Candidate.")
        if not candidates:
            return DeduplicationResult()
        if len(candidates) == 1:
            single = candidates[0]
            return DeduplicationResult(
                candidates=(single,),
                groups=(
                    DuplicateGroup(
                        canonical=single,
                        duplicate_item_ids=(single.item_id,),
                        max_similarity=1.0,
                        reasons=("singleton",),
                    ),
                ),
                removed_count=0,
            )

        started = time.monotonic()
        n = len(candidates)

        if vectors is not None:
            local_vectors = np.asarray(vectors, dtype=np.float32)
            if local_vectors.ndim != 2 or local_vectors.shape[0] != n:
                raise ValueError(
                    f"vectors deve possuir {n} linhas compatíveis com candidates; recebido {local_vectors.shape}."
                )
        elif self._embedding_service is not None:
            local_vectors = self._embedding_service.embed_candidates(candidates, request_id=request_id)
        else:
            raise ValueError("Forneça embedding_service ou vectors para executar deduplicação semântica.")

        sim_matrix = cosine_similarity_matrix(local_vectors, local_vectors)
        # Libera referência local ao array de embeddings após computar similaridades escalares.
        del local_vectors

        attrs = [extract_product_attributes(cand, category_hint=category_hint) for cand in candidates]

        pair_evals: dict[tuple[int, int], PairEvaluation] = {}
        similar_pairs: list[PairEvaluation] = []
        all_evaluations: list[PairEvaluation] = []

        for i in range(n):
            for j in range(i + 1, n):
                ev = self.classify_pair(
                    candidates[i],
                    candidates[j],
                    left_attrs=attrs[i],
                    right_attrs=attrs[j],
                    semantic_similarity=float(sim_matrix[i, j]),
                    category_hint=category_hint,
                )
                pair_evals[(i, j)] = ev
                all_evaluations.append(ev)
                if ev.classification == "similar_product":
                    similar_pairs.append(ev)

        # Agrupamento com guarda de clique completa (evita transitividade indevida entre variantes conflitantes).
        clusters: list[list[int]] = []
        for idx in range(n):
            placed = False
            for cluster in clusters:
                is_compatible_with_all = all(
                    pair_evals[(min(member, idx), max(member, idx))].classification == "probable_duplicate"
                    for member in cluster
                )
                if is_compatible_with_all:
                    cluster.append(idx)
                    placed = True
                    break
            if not placed:
                clusters.append([idx])

        canonical_items: list[Candidate] = []
        groups: list[DuplicateGroup] = []
        for cluster in clusters:
            best_idx = max(cluster, key=lambda idx: _candidate_completeness_score(candidates[idx], idx))
            canonical = candidates[best_idx]
            canonical_items.append(canonical)

            if len(cluster) == 1:
                groups.append(
                    DuplicateGroup(
                        canonical=canonical,
                        duplicate_item_ids=(canonical.item_id,),
                        max_similarity=1.0,
                        reasons=("singleton",),
                    )
                )
            else:
                group_sims: list[float] = []
                group_reasons: list[str] = []
                for p_i in range(len(cluster)):
                    for p_j in range(p_i + 1, len(cluster)):
                        a_idx, b_idx = min(cluster[p_i], cluster[p_j]), max(cluster[p_i], cluster[p_j])
                        ev = pair_evals[(a_idx, b_idx)]
                        group_sims.append(ev.semantic_similarity)
                        group_reasons.extend(ev.reasons)
                groups.append(
                    DuplicateGroup(
                        canonical=canonical,
                        duplicate_item_ids=tuple(candidates[idx].item_id for idx in cluster),
                        max_similarity=max(group_sims) if group_sims else 1.0,
                        reasons=tuple(dict.fromkeys(group_reasons)),
                    )
                )

        removed = n - len(canonical_items)
        if self._logger is not None:
            log_operational_event(
                self._logger,
                logging.INFO,
                "semantic_deduplication_completed",
                stage="semantic_dedup",
                candidate_count=n,
                result_count=len(canonical_items),
                duration_ms=round((time.monotonic() - started) * 1000.0, 2),
                request_id=request_id,
            )

        return DeduplicationResult(
            candidates=tuple(canonical_items),
            groups=tuple(groups),
            removed_count=removed,
            similar_pairs=tuple(similar_pairs),
            pair_evaluations=tuple(all_evaluations),
        )


__all__ = [
    "DeduplicationConfig",
    "DeduplicationResult",
    "DuplicateGroup",
    "PairClassification",
    "PairEvaluation",
    "ProductDeduplicator",
]
