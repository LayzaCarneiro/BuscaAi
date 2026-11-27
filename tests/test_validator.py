"""Testes unitários do registro de perfis/categorias, validador por evidências e orçamento."""

from __future__ import annotations

from decimal import Decimal

import pytest

from buscai.pricing import CurrencyConverter, FixedEnvExchangeRateProvider
from buscai.search.models import Candidate, ShippingInfo
from buscai.search.query_builder import DEFAULT_MVP_CATEGORIES
from buscai.validation import (
    CategoryDefinition,
    EvidenceValidator,
    ProfileCriterion,
    evaluate_budget,
    load_default_registry,
)
from userintent.schema import PriceConstraint, UserIntent


def _converter(rate: str = "5.50") -> CurrencyConverter:
    return CurrencyConverter(FixedEnvExchangeRateProvider(rate=rate, updated_at="2026-11-22", source="env_fixed_rate"))


def test_validator_satisfies_all_hard_and_soft_criteria_with_concrete_evidence() -> None:
    validator = EvidenceValidator(currency_converter=_converter())
    cand = Candidate(
        item_id="nb-ok",
        source="ebay",
        title="Lenovo ThinkPad X1 Carbon Gen 11 14 inch Laptop",
        price=Decimal("700.00"),
        currency="USD",
        brand="Lenovo",
        model="ThinkPad X1 Carbon Gen 11",
        condition="NEW",
        categories=("Laptops",),
        aspects={"RAM Size": ("16 GB",), "SSD Capacity": ("512 GB",)},
        shipping=ShippingInfo(cost=Decimal("20.00"), currency="USD", free_shipping=False),
        details_loaded=True,
    )
    intent = UserIntent(
        category="notebook",
        brands=["Lenovo"],
        models=["ThinkPad X1"],
        condition="novo",
        required_features=["16 GB de RAM", "512 GB SSD"],
        preferred_features=["14 polegadas"],
        use_cases=["uso profissional"],
        price=PriceConstraint(max_price=4500, is_hard_constraint=True, currency="BRL"),
        query="notebook Lenovo ThinkPad X1 16GB RAM",
    )

    res = validator.validate_candidate(cand, intent)
    assert res.eligible is True
    assert res.strictly_verified is True
    assert res.needs_enrichment is False
    assert res.hard_not_satisfied_count == 0
    assert res.hard_unknown_count == 0
    assert res.budget_evaluation is not None
    assert res.budget_evaluation.status == "satisfied"
    assert res.budget_evaluation.is_estimated_conversion is True
    assert res.budget_evaluation.total_price_in_budget_currency == Decimal("3960.00")


def test_validator_rejects_violated_hard_constraints_and_excluded_brands() -> None:
    validator = EvidenceValidator(currency_converter=_converter())
    wrong_brand = Candidate(
        item_id="nb-wrong",
        source="ebay",
        title="HP EliteBook 840 G9 14 inch Laptop 8GB RAM 256GB SSD",
        price=Decimal("500.00"),
        currency="USD",
        brand="HP",
        condition="USED",
        categories=("Laptops",),
    )
    intent = UserIntent(
        category="notebook",
        brands=["Lenovo"],
        excluded_brands=["HP"],
        condition="novo",
        required_features=["16 GB de RAM"],
        query="notebook Lenovo novo 16GB RAM",
    )
    res = validator.validate_candidate(wrong_brand, intent)
    assert res.eligible is False
    assert res.hard_not_satisfied_count >= 4  # brand, excluded_brand, condition, ram_gb
    by_id = {e.criterion_id: e for e in res.evaluations}
    assert by_id["hard:brand"].status == "not_satisfied"
    assert by_id["hard:condition"].status == "not_satisfied"
    assert by_id["hard:excluded_brand:hp"].status == "not_satisfied"


def test_validator_treats_missing_data_as_unknown_and_contradictions_safely() -> None:
    validator = EvidenceValidator()

    # Informações incompletas -> unknown sem inventar evidência
    sparse = Candidate(
        item_id="nb-sparse",
        source="ebay",
        title="Lenovo ThinkPad Laptop",
        price=Decimal("400.00"),
        currency="USD",
        brand="Lenovo",
        categories=("Laptops",),
    )
    intent = UserIntent(
        category="notebook",
        required_features=["16 GB de RAM"],
        query="notebook Lenovo 16GB RAM",
    )
    sparse_res = validator.validate_candidate(sparse, intent)
    assert sparse_res.eligible is True
    assert sparse_res.strictly_verified is False
    assert sparse_res.needs_enrichment is True
    ram_ev = next(e for e in sparse_res.evaluations if "ram" in e.criterion_id)
    assert ram_ev.status == "unknown"
    assert ram_ev.evidence is None
    assert ram_ev.source_field is None
    assert ram_ev.insufficient_information is True

    # Evidência contraditória entre título (16GB) e aspects (8GB) -> marcado em contradictions e unknown
    contradictory = Candidate(
        item_id="nb-contra",
        source="ebay",
        title="Lenovo ThinkPad 16GB RAM Laptop",
        price=Decimal("400.00"),
        currency="USD",
        brand="Lenovo",
        aspects={"RAM Size": ("8 GB",)},
        categories=("Laptops",),
    )
    contra_res = validator.validate_candidate(contradictory, intent)
    assert "ram_gb" in contra_res.contradictions
    ram_contra = next(e for e in contra_res.evaluations if "ram" in e.criterion_id)
    assert ram_contra.status == "unknown"
    assert "contradit" in (ram_contra.note or "").casefold()


def test_validator_marks_not_applicable_criteria_across_categories() -> None:
    validator = EvidenceValidator()
    # Monitor avaliado para perfil de desempenho (usa Hz e resolução) vs fone avaliado para perfil desempenho (ANC)
    headphones = Candidate(
        item_id="hp-1",
        source="ebay",
        title="Sony WH-1000XM5 Wireless ANC Bluetooth Headphones",
        price=Decimal("299.00"),
        currency="USD",
        brand="Sony",
        condition="NEW",
        categories=("Headphones",),
    )
    res_hp = validator.validate_candidate(
        headphones,
        UserIntent(category="fone de ouvido", use_cases=["melhor desempenho"], query="fone ANC"),
    )
    assert res_hp.eligible is True
    assert any(e.criterion_id == "bp_hp_anc" and e.status == "satisfied" for e in res_hp.evaluations)

    monitor = Candidate(
        item_id="mn-1",
        source="ebay",
        title="Dell 27 inch QHD 144Hz IPS Monitor",
        price=Decimal("320.00"),
        currency="USD",
        brand="Dell",
        condition="NEW",
        categories=("Monitors",),
    )
    res_mn = validator.validate_candidate(
        monitor,
        UserIntent(category="monitor", use_cases=["melhor desempenho"], query="monitor 144Hz"),
    )
    assert any(e.criterion_id == "bp_mn_hz_120" and e.status == "satisfied" for e in res_mn.evaluations)


@pytest.mark.parametrize(
    ("price_usd", "ship_cost", "free_ship", "max_price", "max_inc", "currency", "expected_status"),
    [
        # USD dentro do teto com frete conhecido
        ("450.00", "20.00", False, 500.0, True, "USD", "satisfied"),
        # Limite exclusivo: 500.00 < 500.00 é falso -> not_satisfied
        ("480.00", "20.00", False, 500.0, False, "USD", "not_satisfied"),
        # Limite inclusivo: 500.00 <= 500.00 -> satisfied
        ("480.00", "20.00", False, 500.0, True, "USD", "satisfied"),
        # Frete grátis explícito
        ("500.00", None, True, 500.0, True, "USD", "satisfied"),
        # Frete desconhecido com item dentro do teto -> unknown (nunca afirma orçamento total!)
        ("450.00", None, None, 500.0, True, "USD", "unknown"),
        # Item acima do teto rígido mesmo sem frete -> not_satisfied
        ("550.00", None, None, 500.0, True, "USD", "not_satisfied"),
        # Conversão USD -> BRL (500 USD * 5.50 = 2750 BRL <= 3000 BRL)
        ("480.00", "20.00", False, 3000.0, True, "BRL", "satisfied"),
        # Conversão USD -> BRL acima do teto (600 USD * 5.50 = 3300 BRL > 3000 BRL)
        ("580.00", "20.00", False, 3000.0, True, "BRL", "not_satisfied"),
    ],
)
def test_budget_evaluation_brl_usd_inclusivity_and_shipping(
    price_usd: str,
    ship_cost: str | None,
    free_ship: bool | None,
    max_price: float,
    max_inc: bool,
    currency: str,
    expected_status: str,
) -> None:
    shipping = (
        None
        if (ship_cost is None and free_ship is None)
        else ShippingInfo(
            cost=Decimal(ship_cost) if ship_cost is not None else None,
            currency="USD" if ship_cost is not None else None,
            free_shipping=free_ship,
        )
    )
    cand = Candidate(
        item_id="b-1",
        source="ebay",
        title="Test Laptop",
        price=Decimal(price_usd),
        currency="USD",
        shipping=shipping,
    )
    constraint = PriceConstraint(
        max_price=max_price,
        max_inclusive=max_inc,
        is_hard_constraint=True,
        currency=currency,
    )
    ev = evaluate_budget(cand, constraint, currency_converter=_converter("5.50"))
    assert ev.status == expected_status


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
