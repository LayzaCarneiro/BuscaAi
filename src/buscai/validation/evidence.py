"""Extração determinística de evidências concretas e detecção de contradições em `Candidate`."""

from __future__ import annotations

import re
import unicodedata

from buscai.search.models import Candidate
from buscai.semantic.representation import extract_product_attributes
from buscai.validation.models import CandidateEvidenceMap, ExtractedEvidence


def _norm_text(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", " ".join(text.strip().casefold().split()))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _first_matching_aspect_key(candidate: Candidate, target_keys: tuple[str, ...]) -> str | None:
    targets = {k.casefold() for k in target_keys}
    for key in candidate.aspects:
        if key.strip().casefold() in targets:
            return f"aspects.{key}"
    return None


def _resolve_source_field(
    candidate: Candidate,
    *,
    preferred_aspect_keys: tuple[str, ...] = (),
    structured_field: str | None = None,
    structured_value: object = None,
    text_probe_token: str | None = None,
) -> tuple[str, str | None]:
    if preferred_aspect_keys:
        aspect_key = _first_matching_aspect_key(candidate, preferred_aspect_keys)
        if aspect_key is not None:
            raw_vals = candidate.aspects.get(aspect_key.removeprefix("aspects."), ())
            return aspect_key, ", ".join(raw_vals) if raw_vals else None
    if structured_field is not None and structured_value not in (None, "", ()):
        return structured_field, str(structured_value)
    if text_probe_token:
        title_norm = _norm_text(candidate.title)
        if text_probe_token in title_norm:
            return "title", candidate.title
    if candidate.short_description:
        return "short_description", candidate.short_description
    return "title", candidate.title


def extract_candidate_evidences(
    candidate: Candidate,
    *,
    category_hint: str | None = None,
) -> CandidateEvidenceMap:
    """Extrai mapa auditável de evidências sem inventar atributos ausentes.

    Compara atributos extraídos apenas de `aspects` contra os extraídos apenas de `title`
    para detectar contradições internas (ex.: título diz 16GB RAM, aspect diz 8GB RAM).
    """
    attrs_full = extract_product_attributes(candidate, category_hint=category_hint)
    attrs_title_only = extract_product_attributes(
        candidate.model_copy(update={"aspects": {}, "short_description": None}),
        category_hint=category_hint,
    )
    attrs_aspects_only = (
        extract_product_attributes(
            candidate.model_copy(
                update={"title": "item", "short_description": None, "brand": None, "model": None, "condition": None}
            ),
            category_hint=None,
        )
        if candidate.aspects
        else None
    )

    contradictions: dict[str, tuple[str, ...]] = {}
    if attrs_aspects_only is not None:
        for attr_name in ("ram_gb", "storage_gb", "resolution", "refresh_rate_hz", "panel_type"):
            asp_val = getattr(attrs_aspects_only, attr_name)
            tit_val = getattr(attrs_title_only, attr_name)
            if asp_val is not None and tit_val is not None and asp_val != tit_val:
                contradictions[attr_name] = (
                    f"aspects={asp_val}",
                    f"title={tit_val}",
                )

    evidences: dict[str, ExtractedEvidence] = {}
    from_get_item = candidate.details_loaded

    if attrs_full.canonical_category is not None:
        src = "categories" if candidate.categories else ("title" if not category_hint else "category_hint")
        evidences["canonical_category"] = ExtractedEvidence(
            attribute="canonical_category",
            value=attrs_full.canonical_category,
            source_field=src,
            raw_text=", ".join(candidate.categories) if candidate.categories else candidate.title,
            from_get_item=from_get_item,
        )

    if attrs_full.brand is not None:
        src, raw = _resolve_source_field(
            candidate,
            preferred_aspect_keys=("brand", "marca", "manufacturer"),
            structured_field="brand",
            structured_value=candidate.brand,
            text_probe_token=_norm_text(attrs_full.brand),
        )
        evidences["brand"] = ExtractedEvidence(
            attribute="brand",
            value=attrs_full.brand,
            source_field=src,
            raw_text=raw,
            from_get_item=from_get_item,
        )

    if attrs_full.model_tokens:
        src, raw = _resolve_source_field(
            candidate,
            preferred_aspect_keys=("model", "modelo", "series"),
            structured_field="model",
            structured_value=candidate.model,
        )
        evidences["model_tokens"] = ExtractedEvidence(
            attribute="model_tokens",
            value=attrs_full.model_tokens,
            source_field=src,
            raw_text=raw,
            from_get_item=from_get_item,
        )

    if attrs_full.condition_bucket is not None:
        src = "condition" if candidate.condition else "title"
        raw = candidate.condition or candidate.title
        evidences["condition_bucket"] = ExtractedEvidence(
            attribute="condition_bucket",
            value=attrs_full.condition_bucket,
            source_field=src,
            raw_text=raw,
            from_get_item=from_get_item,
        )

    if attrs_full.ram_gb is not None and "ram_gb" not in contradictions:
        src, raw = _resolve_source_field(
            candidate,
            preferred_aspect_keys=("ram size", "ram", "memory", "system memory", "memória ram"),
            text_probe_token=f"{attrs_full.ram_gb}gb",
        )
        evidences["ram_gb"] = ExtractedEvidence(
            attribute="ram_gb",
            value=attrs_full.ram_gb,
            source_field=src,
            raw_text=raw,
            from_get_item=from_get_item,
        )

    if attrs_full.storage_gb is not None and "storage_gb" not in contradictions:
        src, raw = _resolve_source_field(
            candidate,
            preferred_aspect_keys=(
                "ssd capacity",
                "storage capacity",
                "hard drive capacity",
                "capacity",
                "internal storage",
                "storage",
            ),
        )
        evidences["storage_gb"] = ExtractedEvidence(
            attribute="storage_gb",
            value=attrs_full.storage_gb,
            source_field=src,
            raw_text=raw,
            from_get_item=from_get_item,
        )

    if attrs_full.screen_size_inches is not None:
        src, raw = _resolve_source_field(
            candidate,
            preferred_aspect_keys=("screen size", "display size", "tamanho da tela"),
        )
        evidences["screen_size_inches"] = ExtractedEvidence(
            attribute="screen_size_inches",
            value=attrs_full.screen_size_inches,
            source_field=src,
            raw_text=raw,
            from_get_item=from_get_item,
        )

    if attrs_full.resolution is not None and "resolution" not in contradictions:
        src, raw = _resolve_source_field(
            candidate,
            preferred_aspect_keys=("maximum resolution", "resolution", "display resolution"),
        )
        evidences["resolution"] = ExtractedEvidence(
            attribute="resolution",
            value=attrs_full.resolution,
            source_field=src,
            raw_text=raw,
            from_get_item=from_get_item,
        )

    if attrs_full.refresh_rate_hz is not None and "refresh_rate_hz" not in contradictions:
        src, raw = _resolve_source_field(
            candidate,
            preferred_aspect_keys=("refresh rate", "taxa de atualização"),
        )
        evidences["refresh_rate_hz"] = ExtractedEvidence(
            attribute="refresh_rate_hz",
            value=attrs_full.refresh_rate_hz,
            source_field=src,
            raw_text=raw,
            from_get_item=from_get_item,
        )

    if attrs_full.panel_type is not None and "panel_type" not in contradictions:
        src, raw = _resolve_source_field(
            candidate,
            preferred_aspect_keys=("display type", "panel type", "screen type"),
        )
        evidences["panel_type"] = ExtractedEvidence(
            attribute="panel_type",
            value=attrs_full.panel_type,
            source_field=src,
            raw_text=raw,
            from_get_item=from_get_item,
        )

    if attrs_full.connectivity_features:
        src, raw = _resolve_source_field(
            candidate,
            preferred_aspect_keys=("connectivity", "features", "wireless technology"),
        )
        evidences["connectivity_features"] = ExtractedEvidence(
            attribute="connectivity_features",
            value=attrs_full.connectivity_features,
            source_field=src,
            raw_text=raw,
            from_get_item=from_get_item,
        )

    if attrs_full.form_factor is not None:
        src, raw = _resolve_source_field(
            candidate,
            preferred_aspect_keys=("form factor", "type", "fit design"),
        )
        evidences["form_factor"] = ExtractedEvidence(
            attribute="form_factor",
            value=attrs_full.form_factor,
            source_field=src,
            raw_text=raw,
            from_get_item=from_get_item,
        )

    searchable = _norm_text(
        " ".join(
            [
                candidate.title,
                candidate.short_description or "",
                *(f"{k} {' '.join(v)}" for k, v in candidate.aspects.items()),
            ]
        )
    )
    evidences["searchable_norm"] = ExtractedEvidence(
        attribute="searchable_norm",
        value=searchable,
        source_field="title+aspects+short_description",
        raw_text=None,
        from_get_item=from_get_item,
    )

    return CandidateEvidenceMap(
        item_id=candidate.item_id,
        canonical_category=attrs_full.canonical_category,
        evidences=evidences,
        contradictions=contradictions,
        details_loaded=candidate.details_loaded,
    )


def match_model_tokens_in_candidate(requested_model: str, candidate: Candidate) -> tuple[bool, str | None]:
    """Verifica se os tokens de um modelo exigido aparecem nas evidências do candidato."""
    dummy = candidate.model_copy(
        update={"title": requested_model, "model": requested_model, "aspects": {}, "short_description": None}
    )
    req_attrs = extract_product_attributes(dummy)
    cand_attrs = extract_product_attributes(candidate)
    if not req_attrs.model_tokens:
        return False, None
    if not cand_attrs.model_tokens:
        return False, None
    req_set = set(req_attrs.model_tokens)
    cand_set = set(cand_attrs.model_tokens)
    if req_set.issubset(cand_set):
        return True, ", ".join(cand_attrs.model_tokens)
    return False, None


__all__ = ["extract_candidate_evidences", "match_model_tokens_in_candidate"]
