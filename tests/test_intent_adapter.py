"""Contrato entre UserIntent e a busca, sem chamadas ao Gemini ou à rede."""

from __future__ import annotations

from decimal import Decimal

import pytest

from buscai.adapters import (
    ClarificationRequired,
    ConstraintUnavailableError,
    adapt_intent,
    search_intent,
)
from buscai.catalog import Product, ProductPage
from userintent.schema import UserIntent


class InMemoryCatalog:
    def __init__(self, products: list[Product]) -> None:
        self.products = products
        self.calls = 0

    def list_products(self, *, limit: int = 30, skip: int = 0) -> ProductPage:
        self.calls += 1
        return ProductPage(
            products=tuple(self.products[skip : skip + limit]),
            total=len(self.products),
            skip=skip,
            limit=limit,
        )


def product(
    product_id: int,
    *,
    price: str = "150",
    currency: str | None = "BRL",
    category: str | None = "laptops",
    attributes: dict[str, object] | None = None,
) -> Product:
    return Product(
        id=str(product_id),
        source="test",
        title="notebook de teste",
        price=Decimal(price),
        currency=currency,
        category=category,
        attributes=attributes or {},
    )


def test_adapter_preserves_intent_and_maps_category() -> None:
    intent = UserIntent(
        category="Notebook",
        price={"min_price": 100, "max_price": 200, "min_inclusive": False,
               "max_inclusive": False, "is_hard_constraint": True},
        brands=["Acme"],
        excluded_brands=["Other"],
        required_features=["16 GB RAM"],
        preferred_features=["SSD"],
        excluded_features=["usado"],
        use_cases=["programação"],
        profile_context=["estudante"],
        query="notebook",
    )

    criteria = adapt_intent(intent, category_map={"notebook": "laptops"})

    assert criteria.category == "laptops"
    assert (criteria.min_price, criteria.max_price) == (Decimal("100"), Decimal("200"))
    assert (criteria.min_inclusive, criteria.max_inclusive, criteria.price_is_hard) == (False, False, True)
    assert criteria.brands == ("Acme",)
    assert criteria.excluded_brands == ("Other",)
    assert criteria.required_features == ("16 GB RAM",)
    assert criteria.preferred_features == ("SSD",)
    assert criteria.excluded_features == ("usado",)
    assert criteria.use_cases == ("programação",)
    assert criteria.profile_context == ("estudante",)


@pytest.mark.parametrize(
    ("inclusive", "expected_ids"),
    [(False, ["2"]), (True, ["1", "2", "3"])],
)
def test_hard_price_bounds_and_category_are_applied_before_pagination(
    inclusive: bool, expected_ids: list[str]
) -> None:
    intent = UserIntent(
        category="notebook",
        query="notebook",
        price={"min_price": 100, "max_price": 200, "min_inclusive": inclusive,
               "max_inclusive": inclusive, "is_hard_constraint": True},
    )
    catalog = InMemoryCatalog(
        [product(1, price="100"), product(2), product(3, price="200"),
         product(4, price="150", category="smartphones")]
    )

    result = search_intent(intent, catalog=catalog, category_map={"notebook": "laptops"}, budget_currency="brl")

    assert [item.id for item in result.results.products] == expected_ids
    assert result.results.total_matches == len(expected_ids)


def test_soft_preferences_and_context_are_kept_without_excluding_products() -> None:
    intent = UserIntent(
        query="notebook",
        price={"max_price": 100, "is_hard_constraint": False},
        brands=["Acme"],
        preferred_features=["SSD"],
        use_cases=["programação"],
        profile_context=["estudante"],
    )
    catalog = InMemoryCatalog([product(1, price="500", attributes={"brand": "Other"})])

    result = search_intent(intent, catalog=catalog)

    assert [item.id for item in result.results.products] == ["1"]
    assert result.criteria.max_price == Decimal("100")
    assert result.unapplied_preferences == (
        "brands", "preferred_features", "use_cases", "profile_context", "price"
    )


def test_excluded_brand_and_required_feature_filter_products_with_evidence() -> None:
    intent = UserIntent(
        query="notebook",
        excluded_brands=["Other"],
        required_features=["16 GB RAM"],
    )
    catalog = InMemoryCatalog(
        [
            product(1, attributes={"brand": "Acme", "description": "Notebook com 16GB de RAM"}),
            product(2, attributes={"brand": "Other", "description": "Notebook com 16GB de RAM"}),
            product(3, attributes={"brand": "Acme", "description": "Notebook com 8GB de RAM"}),
            product(4, attributes={"brand": "Acme", "description": "Notebook com 128GB de RAM"}),
        ]
    )

    result = search_intent(intent, catalog=catalog)

    assert [item.id for item in result.results.products] == ["1"]


@pytest.mark.parametrize(
    ("intent", "candidate", "message"),
    [
        (UserIntent(query="notebook", excluded_brands=["Other"]), product(1), "marca"),
        (UserIntent(query="notebook", required_features=["16 GB"]), product(1), "descrição"),
        (UserIntent(query="notebook", category="notebook"), product(1, category=None), "categoria"),
        (UserIntent(query="notebook", price={"max_price": 200, "is_hard_constraint": True}),
         product(1, currency=None), "Moeda"),
    ],
)
def test_missing_data_for_hard_constraints_is_reported(
    intent: UserIntent, candidate: Product, message: str
) -> None:
    with pytest.raises(ConstraintUnavailableError, match=message):
        search_intent(intent, catalog=InMemoryCatalog([candidate]), budget_currency="BRL")


def test_excluded_features_fail_explicitly_without_absence_data() -> None:
    intent = UserIntent(query="notebook", excluded_features=["usado"])
    catalog = InMemoryCatalog([product(1, attributes={"description": "Notebook novo"})])

    with pytest.raises(ConstraintUnavailableError, match="excluded_features"):
        search_intent(intent, catalog=catalog)
    assert catalog.calls == 1


def test_excluded_features_use_only_a_declared_complete_feature_list() -> None:
    intent = UserIntent(query="notebook", excluded_features=["usado"])
    catalog = InMemoryCatalog(
        [
            product(1, attributes={"features": ["novo", "SSD"], "features_complete": True}),
            product(2, attributes={"features": ["usado", "SSD"], "features_complete": True}),
        ]
    )

    result = search_intent(intent, catalog=catalog)

    assert [item.id for item in result.results.products] == ["1"]


def test_hard_budget_requires_currency_before_catalog_access() -> None:
    intent = UserIntent(query="notebook", price={"max_price": 200, "is_hard_constraint": True})
    catalog = InMemoryCatalog([product(1)])

    with pytest.raises(ConstraintUnavailableError, match="moeda do orçamento"):
        search_intent(intent, catalog=catalog)
    assert catalog.calls == 0


def test_clarification_is_forwarded_without_searching() -> None:
    intent = UserIntent(
        query="notebook", clarification_needed=True,
        clarification_question="Qual é o seu orçamento?"
    )
    catalog = InMemoryCatalog([product(1)])

    with pytest.raises(ClarificationRequired, match="Qual é o seu orçamento"):
        search_intent(intent, catalog=catalog)
    assert catalog.calls == 0
