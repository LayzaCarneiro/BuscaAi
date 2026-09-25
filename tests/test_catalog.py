"""Contrato de produto e falhas do fornecedor DummyJSON, sem rede."""

from __future__ import annotations

from io import BytesIO
from urllib.error import HTTPError

import pytest

from buscai.catalog import CatalogProvider
from buscai.catalog.dummyjson import CatalogError, DummyJsonCatalog


def test_normalizes_products_and_preserves_page_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    response = BytesIO(
        b'{"products":[{"id":7,"title":"  Example  ","price":19.99,'
        b'"category":"beauty","brand":"ACME","stock":4},'
        b'{"id":8,"title":"Other","price":0}],"total":42,"skip":10,"limit":2}'
    )

    def fake_urlopen(request, *, timeout):
        assert request.full_url.endswith("?limit=2&skip=10")
        assert request.get_header("User-agent") == "BuscAI/0.1"
        assert timeout == 10
        return response

    monkeypatch.setattr("buscai.catalog.dummyjson.urlopen", fake_urlopen)
    provider: CatalogProvider = DummyJsonCatalog(currency="brl")
    page = provider.list_products(limit=2, skip=10)

    assert (page.total, page.skip, page.limit) == (42, 10, 2)
    first, second = page.products
    assert (first.id, first.source, first.title, str(first.price), first.currency) == (
        "7", "dummyjson", "Example", "19.99", "BRL"
    )
    assert first.category == "beauty"
    assert first.attributes == {"brand": "ACME", "stock": 4}
    assert second.category is None
    assert second.attributes == {}


@pytest.mark.parametrize(
    "payload",
    [
        b'not-json',
        b'{"products":{},"total":1,"skip":0,"limit":1}',
        b'{"products":[{"id":1,"title":"X"}],"total":1,"skip":0,"limit":1}',
        b'{"products":[],"total":1,"skip":3,"limit":1}',
    ],
)
def test_rejects_malformed_catalog_responses(
    monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    monkeypatch.setattr("buscai.catalog.dummyjson.urlopen", lambda *_args, **_kwargs: BytesIO(payload))

    with pytest.raises(CatalogError):
        DummyJsonCatalog().list_products(limit=1)


def test_reports_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_request(*_args, **_kwargs):
        raise HTTPError("https://dummyjson.com/products", 403, "Forbidden", {}, None)

    monkeypatch.setattr("buscai.catalog.dummyjson.urlopen", fail_request)

    with pytest.raises(CatalogError, match="HTTP 403"):
        DummyJsonCatalog().list_products()


def test_accepts_short_final_page(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b'{"products":[{"id":34,"title":"Last","price":5}],"total":34,"skip":33,"limit":1}'
    monkeypatch.setattr("buscai.catalog.dummyjson.urlopen", lambda *_args, **_kwargs: BytesIO(payload))

    page = DummyJsonCatalog().list_products(limit=30, skip=33)

    assert page.limit == 1
    assert [product.id for product in page.products] == ["34"]


def test_currency_is_unknown_until_explicitly_configured() -> None:
    assert DummyJsonCatalog().currency is None
    with pytest.raises(ValueError):
        DummyJsonCatalog(currency="R$")
