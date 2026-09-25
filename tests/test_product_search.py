"""Busca textual e filtros usando um fornecedor de catálogo em memória."""

from __future__ import annotations

from decimal import Decimal

import pytest

from buscai.catalog import Product, ProductPage
from buscai.catalog.dummyjson import CatalogError
from buscai.tools import search_products
from scripts.demo_catalog_search import main as demo_main
from userintent.schema import UserIntent


class FakeCatalog:
    def __init__(self, products: list[Product]) -> None:
        self.products = products
        self.calls: list[int] = []

    def list_products(self, *, limit: int = 30, skip: int = 0) -> ProductPage:
        self.calls.append(skip)
        return ProductPage(
            products=tuple(self.products[skip : skip + limit]),
            total=len(self.products),
            skip=skip,
            limit=limit,
        )


def product(
    product_id: int,
    *,
    title: str = "Item comum",
    price: str = "10.00",
    currency: str | None = "BRL",
    attributes: dict[str, object] | None = None,
) -> Product:
    return Product(
        id=str(product_id),
        source="fake",
        title=title,
        price=Decimal(price),
        currency=currency,
        category="eletrônicos",
        attributes=attributes or {},
    )


def test_search_scans_all_catalog_pages_before_paginating_matches() -> None:
    products = [product(index) for index in range(35)]
    for index in (0, 30, 34):
        products[index] = product(index, attributes={"description": "Fóne Bluetooth"})
    catalog = FakeCatalog(products)

    result = search_products("fone bluetooth", catalog=catalog, limit=1, offset=1)

    assert catalog.calls == [0, 30]
    assert result.total_matches == 3
    assert (result.limit, result.offset) == (1, 1)
    assert [item.id for item in result.products] == ["30"]


def test_price_filter_is_inclusive_and_requires_matching_currency() -> None:
    catalog = FakeCatalog(
        [product(1, title="Fone", price="19.99"), product(2, title="Fone", price="20.00"),
         product(3, title="Fone", price="20.01")]
    )

    result = search_products("fone", 20, catalog=catalog, budget_currency="brl")

    assert result.total_matches == 2
    assert [item.id for item in result.products] == ["1", "2"]


def test_search_returns_empty_result_for_no_match_or_offset_past_end() -> None:
    catalog = FakeCatalog([product(1, title="Fone")])

    assert search_products("camera", catalog=catalog).total_matches == 0
    result = search_products("fone", catalog=catalog, offset=10)
    assert result.total_matches == 1
    assert result.products == ()


def test_numeric_query_does_not_match_part_of_a_larger_number() -> None:
    catalog = FakeCatalog([product(1, title="Fone 128GB"), product(2, title="Fone 8GB")])

    result = search_products("fone 8 gb", catalog=catalog)

    assert [item.id for item in result.products] == ["2"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": "  "},
        {"query": "!!!"},
        {"query": "fone", "limit": 0},
        {"query": "fone", "limit": 51},
        {"query": "fone", "offset": -1},
        {"query": "fone", "max_price": -1, "budget_currency": "BRL"},
        {"query": "fone", "max_price": 20},
    ],
)
def test_rejects_invalid_search_arguments(kwargs: dict) -> None:
    catalog = FakeCatalog([product(1, title="Fone")])

    with pytest.raises(ValueError):
        search_products(catalog=catalog, **kwargs)
    assert catalog.calls == []


@pytest.mark.parametrize("currency", [None, "USD"])
def test_refuses_price_comparison_with_unknown_or_different_currency(currency: str | None) -> None:
    catalog = FakeCatalog([product(1, title="Fone", currency=currency)])

    with pytest.raises(ValueError, match="moeda do produto"):
        search_products("fone", 20, catalog=catalog, budget_currency="BRL")


def test_rejects_incomplete_catalog_pagination() -> None:
    class IncompleteCatalog:
        def list_products(self, *, limit: int = 30, skip: int = 0) -> ProductPage:
            return ProductPage(products=(), total=3, skip=skip, limit=limit)

    with pytest.raises(CatalogError, match="paginação"):
        search_products("fone", catalog=IncompleteCatalog())


def test_supplier_failure_on_later_page_does_not_return_partial_results() -> None:
    class FailingSecondPage(FakeCatalog):
        def list_products(self, *, limit: int = 30, skip: int = 0) -> ProductPage:
            if skip > 0:
                raise CatalogError("Fornecedor indisponível.")
            return super().list_products(limit=limit, skip=skip)

    catalog = FailingSecondPage([product(index, title="Fone") for index in range(31)])

    with pytest.raises(CatalogError, match="Fornecedor indisponível"):
        search_products("fone", catalog=catalog)


def test_manual_user_intent_can_supply_supported_query_without_gemini() -> None:
    intent = UserIntent(query="fone bluetooth")
    catalog = FakeCatalog([product(1, title="Fone Bluetooth")])

    result = search_products(intent.query, catalog=catalog)

    assert [item.id for item in result.products] == ["1"]


def test_demo_uses_manual_intent_and_reports_empty_page(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    catalog = FakeCatalog([product(1, title="Fone", currency=None)])

    assert demo_main(["fone", "--limit", "1"], catalog=catalog) == 0
    output = capsys.readouterr().out
    assert "Correspondências: 1" in output
    assert "Fone" in output
    assert "moeda não informada" in output

    assert demo_main(["camera"], catalog=catalog) == 0
    assert "Nenhum produto nesta página." in capsys.readouterr().out
