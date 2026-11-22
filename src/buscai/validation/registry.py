"""Reexportação do registro declarativo de categorias MVP e perfis (T5.1)."""

from buscai.catalog_registry import (
    DEFAULT_PROFILES_PATH,
    CategoryDefinition,
    ProfileCriterion,
    ProfileRegistry,
    UsageProfile,
    build_category_detection_keywords,
    build_category_priority_aspects,
    load_default_registry,
)

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
