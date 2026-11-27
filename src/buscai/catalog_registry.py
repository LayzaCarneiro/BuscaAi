"""Registro centralizado e extensível de categorias MVP e perfis de utilização (Etapa 5)."""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_PROFILES_PATH = Path(__file__).resolve().with_name("catalog_profiles.json")


def _norm_key(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", " ".join(text.strip().casefold().split()))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


class CategoryDefinition(BaseModel):
    """Definição declarativa única de uma categoria de produto no BuscaAI."""

    model_config = ConfigDict(frozen=True)

    canonical_id: str
    primary_term_en: str
    aliases: tuple[str, ...] = ()
    subtype_terms_en: dict[str, str] = Field(default_factory=dict)
    standalone_models: tuple[str, ...] = ()
    detection_keywords: tuple[str, ...] = ()
    priority_aspect_keys: tuple[str, ...] = ()
    applicable_attributes: tuple[str, ...] = ()


class ProfileCriterion(BaseModel):
    """Critério declarativo de validação vinculado a um perfil e categoria."""

    model_config = ConfigDict(frozen=True)

    criterion_id: str
    attribute: str
    operator: Literal["gte", "lte", "eq", "in", "contains", "neq"]
    expected_value: Any
    priority: Literal["hard", "soft"] = "soft"
    relaxable: bool = True
    relaxation_order: int = Field(default=0, ge=0)
    missing_behavior: Literal["unknown", "not_applicable"] = "unknown"
    required_evidence_fields: tuple[str, ...] = ("aspects", "title", "short_description")
    description_pt: str = ""

    @model_validator(mode="after")
    def _hard_never_relaxable(self) -> ProfileCriterion:
        if self.priority == "hard" and self.relaxable:
            raise ValueError(
                f"Critério obrigatório '{self.criterion_id}' não pode ter relaxable=True."
            )
        return self


class UsageProfile(BaseModel):
    """Perfil de utilização ou objetivo de recomendação aplicável às categorias."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    display_name_pt: str
    profile_kind: Literal["usage", "objective"] = "usage"
    aliases: tuple[str, ...] = ()
    description_pt: str = ""
    criteria_by_category: dict[str, tuple[ProfileCriterion, ...]] = Field(default_factory=dict)


class ProfileRegistry(BaseModel):
    """Catálogo declarativo e extensível de categorias e perfis do BuscaAI."""

    model_config = ConfigDict(frozen=True)

    version: str = "1.0"
    categories: tuple[CategoryDefinition, ...] = ()
    profiles: tuple[UsageProfile, ...] = ()

    @classmethod
    def from_json_file(cls, path: Path | str = DEFAULT_PROFILES_PATH) -> ProfileRegistry:
        """Carrega e valida o registro declarativo a partir de um arquivo JSON."""
        raw = Path(path).read_text(encoding="utf-8")
        return cls.model_validate_json(raw)

    def with_extra_categories(self, extra: Sequence[CategoryDefinition]) -> ProfileRegistry:
        """Retorna um novo registro incorporando categorias adicionais sem mutação."""
        by_id = {_norm_key(cat.canonical_id): cat for cat in self.categories}
        for cat in extra:
            by_id[_norm_key(cat.canonical_id)] = cat
        return self.model_copy(update={"categories": tuple(by_id.values())})

    def with_extra_profiles(self, extra: Sequence[UsageProfile]) -> ProfileRegistry:
        """Retorna um novo registro incorporando perfis adicionais sem mutação."""
        by_id = {_norm_key(prof.profile_id): prof for prof in self.profiles}
        for prof in extra:
            by_id[_norm_key(prof.profile_id)] = prof
        return self.model_copy(update={"profiles": tuple(by_id.values())})

    def resolve_category(self, raw_category: str | None) -> CategoryDefinition | None:
        if not raw_category or not raw_category.strip():
            return None
        target = _norm_key(raw_category)
        for cat in self.categories:
            if _norm_key(cat.canonical_id) == target:
                return cat
            if any(_norm_key(alias) == target for alias in cat.aliases):
                return cat
        return None

    def resolve_profile(self, raw_profile: str | None) -> UsageProfile | None:
        if not raw_profile or not raw_profile.strip():
            return None
        target = _norm_key(raw_profile)
        for prof in self.profiles:
            if _norm_key(prof.profile_id) == target or _norm_key(prof.display_name_pt) == target:
                return prof
            if any(_norm_key(alias) == target for alias in prof.aliases):
                return prof
        return None

    def resolve_profile_for_intent(
        self,
        intent: object | None,
        *,
        explicit_profile: str | None = None,
    ) -> UsageProfile | None:
        """Resolve o perfil ativo por parâmetro explícito ou a partir de `use_cases`/`profile_context`."""
        if explicit_profile is not None:
            resolved = self.resolve_profile(explicit_profile)
            if resolved is None:
                raise ValueError(f"Perfil de utilização desconhecido: {explicit_profile!r}.")
            return resolved
        if intent is None:
            return None
        for field_name in ("use_cases", "profile_context"):
            values = getattr(intent, field_name, ()) or ()
            for val in values:
                if isinstance(val, str):
                    resolved = self.resolve_profile(val)
                    if resolved is not None:
                        return resolved
        return None

    def criteria_for(
        self,
        profile_id: str,
        category_id: str | None,
    ) -> tuple[ProfileCriterion, ...]:
        prof = self.resolve_profile(profile_id)
        if prof is None or not category_id:
            return ()
        cat = self.resolve_category(category_id)
        target_key = _norm_key(cat.canonical_id if cat is not None else category_id)
        for key, criteria in prof.criteria_by_category.items():
            if _norm_key(key) == target_key:
                return criteria
        return ()

    def is_attribute_applicable(self, attribute: str, category_id: str | None) -> bool:
        if not category_id:
            return True
        cat = self.resolve_category(category_id)
        if cat is None or not cat.applicable_attributes:
            return True
        return attribute in cat.applicable_attributes


@lru_cache(maxsize=1)
def load_default_registry() -> ProfileRegistry:
    """Carrega e valida em cache o registro padrão de categorias MVP e 6 perfis."""
    return ProfileRegistry.from_json_file(DEFAULT_PROFILES_PATH)


def build_category_detection_keywords() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Exporta palavras-chave de detecção para `representation.py` sem duplicar dados."""
    reg = load_default_registry()
    return tuple((cat.canonical_id, cat.detection_keywords) for cat in reg.categories if cat.detection_keywords)


def build_category_priority_aspects() -> dict[str, tuple[str, ...]]:
    """Exporta mapa de atributos prioritários por categoria para `representation.py`."""
    reg = load_default_registry()
    return {cat.canonical_id: cat.priority_aspect_keys for cat in reg.categories if cat.priority_aspect_keys}


__all__ = [
    "CategoryDefinition",
    "DEFAULT_PROFILES_PATH",
    "ProfileCriterion",
    "ProfileRegistry",
    "UsageProfile",
    "build_category_detection_keywords",
    "build_category_priority_aspects",
    "load_default_registry",
]
