"""Testes unitários do registro centralizado de categorias MVP e 6 perfis (T5.1)."""

from __future__ import annotations

import pytest

from buscai.search.query_builder import DEFAULT_MVP_CATEGORIES
from buscai.validation import (
    CategoryDefinition,
    EvidenceValidator,
    ProfileCriterion,
    ProfileRegistry,
    load_default_registry,
)
from buscai.search.models import Candidate
from decimal import Decimal
from userintent.schema import UserIntent


EXPECTED_PROFILES = (
    "best_value",
    "best_performance",
    "lowest_price",
    "professional_use",
    "study",
    "everyday_use",
)
EXPECTED_CATEGORIES = ("notebook", "smartphone", "monitor", "fone de ouvido")


@pytest.mark.parametrize("profile_id", EXPECTED_PROFILES)
@pytest.mark.parametrize("category_id", EXPECTED_CATEGORIES)
def test_all_six_profiles_cover_all_four_mvp_categories(profile_id: str, category_id: str) -> None:
    registry = load_default_registry()
    prof = registry.resolve_profile(profile_id)
    cat = registry.resolve_category(category_id)
    assert prof is not None
    assert cat is not None
    criteria = registry.criteria_for(prof.profile_id, cat.canonical_id)
    assert len(criteria) >= 1
    assert any(c.priority == "hard" and not c.relaxable for c in criteria)


def test_profile_aliases_and_intent_resolution() -> None:
    registry = load_default_registry()
    assert registry.resolve_profile("melhor custo-benefício").profile_id == "best_value"  # type: ignore[union-attr]
    assert registry.resolve_profile("melhor desempenho").profile_id == "best_performance"  # type: ignore[union-attr]
    assert registry.resolve_profile("menor preço").profile_id == "lowest_price"  # type: ignore[union-attr]
    assert registry.resolve_profile("uso profissional").profile_id == "professional_use"  # type: ignore[union-attr]
    assert registry.resolve_profile("estudos").profile_id == "study"  # type: ignore[union-attr]
    assert registry.resolve_profile("uso cotidiano").profile_id == "everyday_use"  # type: ignore[union-attr]

    intent = UserIntent(category="notebook", use_cases=["programação"], query="notebook para programar")
    resolved = registry.resolve_profile_for_intent(intent)
    assert resolved is not None and resolved.profile_id == "professional_use"

    with pytest.raises(ValueError, match="Perfil de utilização desconhecido"):
        registry.resolve_profile_for_intent(intent, explicit_profile="perfil_inexistente")


def test_hard_criterion_cannot_be_marked_relaxable() -> None:
    with pytest.raises(ValueError, match="relaxable=True"):
        ProfileCriterion(
            criterion_id="bad_hard",
            attribute="ram_gb",
            operator="gte",
            expected_value=16,
            priority="hard",
            relaxable=True,
        )


def test_registry_extensibility_for_fifth_category_and_query_builder_sync() -> None:
    base = load_default_registry()
    qb_ids = {c.canonical_id for c in DEFAULT_MVP_CATEGORIES}
    assert qb_ids == set(EXPECTED_CATEGORIES)

    extended = base.with_extra_categories(
        [
            CategoryDefinition(
                canonical_id="tablet",
                primary_term_en="tablet",
                aliases=("tablet", "ipad"),
                applicable_attributes=("storage_gb", "condition_bucket"),
            )
        ]
    )
    assert extended.resolve_category("ipad").canonical_id == "tablet"  # type: ignore[union-attr]

    # Validador aceita o registro estendido sem alterar código de validação.
    validator = EvidenceValidator(registry=extended)
    cand = Candidate(
        item_id="tab-1",
        source="ebay",
        title="Apple iPad Air 256GB Tablet",
        price=Decimal("599.00"),
        currency="USD",
        categories=("Tablets",),
        condition="NEW",
    )
    res = validator.validate_candidate(cand, UserIntent(category="tablet", query="tablet"))
    assert res.eligible is True
