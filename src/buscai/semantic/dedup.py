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
    VARIANT_TIER_TOKENS,
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
    """Grupo consolidado de ofertas equivalentes de um mesmo produto."""

    model_config = ConfigDict(frozen=True)

    group_id: str = ""
    canonical: Candidate
    offers: tuple[Candidate, ...] = ()
    duplicate_item_ids: tuple[str, ...] = ()
    max_similarity: float
    reasons: tuple[str, ...] = ()

    @property
    def offer_count(self) -> int:
        return len(self.offers) if self.offers else len(self.duplicate_item_ids)


class DeduplicationResult(BaseModel):
    """Saída transitória da deduplicação preservando ofertas distintas e grupos de produto."""

    model_config = ConfigDict(frozen=True)

    candidates: tuple[Candidate, ...] = ()
    canonical_candidates: tuple[Candidate, ...] = ()
    groups: tuple[DuplicateGroup, ...] = ()
    removed_count: int = Field(default=0, ge=0)
    grouped_offers_count: int = Field(default=0, ge=0)
    similar_pairs: tuple[PairEvaluation, ...] = ()
    pair_evaluations: tuple[PairEvaluation, ...] = ()


def _strong_model_codes(tokens: tuple[str, ...]) -> set[str]:
    return {tok for tok in tokens if any(ch.isdigit() for ch in tok)}


def _tier_tokens(tokens: tuple[str, ...]) -> set[str]:
    return {tok for tok in tokens if tok in VARIANT_TIER_TOKENS}


def _check_model_compatibility(
    left: ProductStructuredAttributes,
    right: ProductStructuredAttributes,
) -> tuple[bool, bool, str | None]:
    """Avalia identidade de modelo retornando `(tem_conflito, modelo_confirmado, motivo)`."""
    if not left.model_tokens and not right.model_tokens:
        return False, False, "missing_model_identity"
    if bool(left.model_tokens) != bool(right.model_tokens):
        return False, False, "one_sided_model_identity"

    left_tiers = _tier_tokens(left.model_tokens)
    right_tiers = _tier_tokens(right.model_tokens)
    if left_tiers != right_tiers:
        return True, False, "conflicting_model_variants"

    left_codes = _strong_model_codes(left.model_tokens)
    right_codes = _strong_model_codes(right.model_tokens)
    if bool(left_codes) != bool(right_codes):
        return False, False, "one_sided_model_code"
    if left_codes and right_codes:
        if left_codes != right_codes:
            if left_codes.isdisjoint(right_codes):
                return True, False, "distinct_model_codes"
            return True, False, "uncertain_submodel_codes"

    left_set = {tok for tok in left.model_tokens if tok not in VARIANT_TIER_TOKENS}
    right_set = {tok for tok in right.model_tokens if tok not in VARIANT_TIER_TOKENS}
    if not left_set or not right_set:
        return False, False, "tier_only_model_tokens"

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
    """Pontua qualidade estruturada e menor preço para eleger o representante do grupo (sem tamanho de título)."""
    return (
        1 if candidate.details_loaded else 0,
        len(candidate.aspects),
        1 if candidate.brand is not None else 0,
        1 if candidate.model is not None else 0,
        1 if candidate.short_description is not None else 0,
        1 if candidate.seller is not None else 0,
        1 if candidate.shipping is not None else 0,
        -candidate.price,
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
        elif (
            l_attr.condition_bucket is not None
            and r_attr.condition_bucket is not None
            and l_attr.condition_bucket != r_attr.condition_bucket
        ):
            vetoes.append("condition_mismatch")

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
            if (l_attr.condition_bucket is None) != (r_attr.condition_bucket is None):
                uncertainty_reasons.append("uncertain_condition_variant")
            if model_reason in {
                "one_sided_model_identity",
                "missing_model_identity",
                "one_sided_model_code",
                "tier_only_model_tokens",
            }:
                uncertainty_reasons.append(model_reason)

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
            and model_reason not in {
                "partial_model_overlap",
                "one_sided_model_identity",
                "missing_model_identity",
                "one_sided_model_code",
                "tier_only_model_tokens",
            }
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
        include_pair_evaluations: bool = False,
    ) -> DeduplicationResult:
        """Agrupa ofertas equivalentes preservando todos os anúncios distintos e suas ofertas.

        - Duplicatas exatas de anúncio (mesmo `item_id` vindo de múltiplas queries) são colapsadas.
        - Anúncios distintos (`item_id` diferentes) do mesmo produto são agrupados em `DuplicateGroup`,
          mas todas as ofertas permanecem preservadas em `result.candidates` e `group.offers`.
        """
        if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
            raise ValueError("candidates deve ser uma sequência válida de Candidate.")
        if not candidates:
            return DeduplicationResult()

        started = time.monotonic()

        # Passo 1: remove apenas duplicatas exatas pelo identificador do anúncio (`item_id`),
        # mantendo a versão mais completa para cada anúncio.
        best_by_item_id: dict[str, tuple[int, Candidate]] = {}
        for orig_idx, cand in enumerate(candidates):
            existing = best_by_item_id.get(cand.item_id)
            if existing is None or _candidate_completeness_score(cand, orig_idx) > _candidate_completeness_score(
                existing[1], existing[0]
            ):
                best_by_item_id[cand.item_id] = (orig_idx, cand)

        unique_offer_entries = sorted(best_by_item_id.values(), key=lambda entry: entry[0])
        unique_indices = [entry[0] for entry in unique_offer_entries]
        unique_offers = tuple(entry[1] for entry in unique_offer_entries)
        exact_removed = len(candidates) - len(unique_offers)

        n = len(unique_offers)
        if n == 1:
            single = unique_offers[0]
            return DeduplicationResult(
                candidates=(single,),
                canonical_candidates=(single,),
                groups=(
                    DuplicateGroup(
                        group_id=f"product:{single.item_id}",
                        canonical=single,
                        offers=(single,),
                        duplicate_item_ids=(single.item_id,),
                        max_similarity=1.0,
                        reasons=("singleton",),
                    ),
                ),
                removed_count=exact_removed,
                grouped_offers_count=0,
            )

        if vectors is not None:
            raw_vectors = np.asarray(vectors, dtype=np.float32)
            if raw_vectors.ndim != 2 or raw_vectors.shape[0] != len(candidates):
                raise ValueError(
                    f"vectors deve possuir {len(candidates)} linhas compatíveis com candidates; recebido {raw_vectors.shape}."
                )
            local_vectors = raw_vectors[unique_indices]
        elif self._embedding_service is not None:
            local_vectors = self._embedding_service.embed_candidates(
                unique_offers,
                mode="symmetric_similarity",
                category_hint=category_hint,
                request_id=request_id,
            )
        else:
            raise ValueError("Forneça embedding_service ou vectors para executar deduplicação semântica.")

        sim_matrix = cosine_similarity_matrix(local_vectors, local_vectors)
        # Libera referência local ao array de embeddings após computar similaridades escalares.
        del local_vectors

        attrs = [extract_product_attributes(cand, category_hint=category_hint) for cand in unique_offers]
        mfg_sets = [set(attr.manufacturer_ids) for attr in attrs]
        sim_threshold = self._config.similar_product_semantic_threshold

        probable_pairs: set[tuple[int, int]] = set()
        probable_pair_meta: dict[tuple[int, int], tuple[float, tuple[str, ...]]] = {}
        similar_pairs: list[PairEvaluation] = []
        all_evaluations: list[PairEvaluation] = []

        for i in range(n):
            a_i = attrs[i]
            m_i = mfg_sets[i]
            for j in range(i + 1, n):
                sim_val = float(sim_matrix[i, j])
                if not include_pair_evaluations:
                    # Filtro O(1) exato (A10): se não há MPN compartilhado, cosseno abaixo do limiar de
                    # semelhança e não compartilha (categoria, marca), o par é comprovadamente `different_product`.
                    has_shared_mfg = bool(m_i and mfg_sets[j] and (m_i & mfg_sets[j]))
                    same_cat_brand = (
                        a_i.canonical_category is not None
                        and a_i.canonical_category == attrs[j].canonical_category
                        and a_i.brand is not None
                        and a_i.brand == attrs[j].brand
                    )
                    if not has_shared_mfg and sim_val < sim_threshold and not same_cat_brand:
                        continue

                ev = self.classify_pair(
                    unique_offers[i],
                    unique_offers[j],
                    left_attrs=a_i,
                    right_attrs=attrs[j],
                    semantic_similarity=sim_val,
                    category_hint=category_hint,
                )
                if include_pair_evaluations:
                    all_evaluations.append(ev)
                if ev.classification == "probable_duplicate":
                    probable_pairs.add((i, j))
                    probable_pair_meta[(i, j)] = (ev.semantic_similarity, ev.reasons)
                elif ev.classification == "similar_product":
                    similar_pairs.append(ev)

        # Agrupamento com guarda de clique completa (evita transitividade indevida entre variantes conflitantes).
        clusters: list[list[int]] = []
        for idx in range(n):
            placed = False
            for cluster in clusters:
                if all((min(member, idx), max(member, idx)) in probable_pairs for member in cluster):
                    cluster.append(idx)
                    placed = True
                    break
            if not placed:
                clusters.append([idx])

        canonical_items: list[Candidate] = []
        groups: list[DuplicateGroup] = []
        for cluster in clusters:
            best_idx = max(cluster, key=lambda idx: _candidate_completeness_score(unique_offers[idx], idx))
            canonical = unique_offers[best_idx]
            canonical_items.append(canonical)
            group_offers = tuple(unique_offers[idx] for idx in cluster)

            if len(cluster) == 1:
                groups.append(
                    DuplicateGroup(
                        group_id=f"product:{canonical.item_id}",
                        canonical=canonical,
                        offers=group_offers,
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
                        sim_pair, reasons_pair = probable_pair_meta[
                            (min(cluster[p_i], cluster[p_j]), max(cluster[p_i], cluster[p_j]))
                        ]
                        group_sims.append(sim_pair)
                        group_reasons.extend(reasons_pair)
                groups.append(
                    DuplicateGroup(
                        group_id=f"product:{canonical.item_id}",
                        canonical=canonical,
                        offers=group_offers,
                        duplicate_item_ids=tuple(offer.item_id for offer in group_offers),
                        max_similarity=max(group_sims) if group_sims else 1.0,
                        reasons=tuple(dict.fromkeys(group_reasons)),
                    )
                )

        grouped_offers_count = n - len(groups)
        if self._logger is not None:
            log_operational_event(
                self._logger,
                logging.INFO,
                "semantic_deduplication_completed",
                stage="semantic_dedup",
                candidate_count=len(candidates),
                result_count=len(unique_offers),
                duration_ms=round((time.monotonic() - started) * 1000.0, 2),
                request_id=request_id,
            )

        return DeduplicationResult(
            candidates=unique_offers,
            canonical_candidates=tuple(canonical_items),
            groups=tuple(groups),
            removed_count=exact_removed,
            grouped_offers_count=grouped_offers_count,
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
