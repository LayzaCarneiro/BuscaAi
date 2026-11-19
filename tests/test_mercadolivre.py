"""Testes do MercadoLivreCatalog com a API simulada (sem rede).

Ajuste o import abaixo se o módulo ficar em outro pacote.
"""
from __future__ import annotations

import io
import json
import unittest
from decimal import Decimal
from unittest.mock import patch
from urllib.error import HTTPError

from buscai.catalog.dummyjson import CatalogError
from buscai.catalog.mercadolivre import MercadoLivreCatalog, SOURCE
from buscai.catalog.models import ProductPage


def search_payload(*items, total=None):
    return {
        "keywords": "q",
        "paging": {"total": total if total is not None else len(items), "limit": 10, "offset": 0},
        "results": list(items),
    }


def hit(pid, name="Produto", children=None, status="active"):
    return {"id": pid, "name": name, "status": status, "children_ids": children or []}


def detail(pid, *, price=Decimal("4499.00"), currency="BRL", category="MLB1055", name="Samsung Galaxy S25 256 GB"):
    winner = None
    if price is not ...:
        winner = {
            "item_id": "MLB999", "category_id": category, "seller_id": 123, "price": price,
            "currency_id": currency, "shipping": {"free_shipping": True},
            "seller": {"reputation_level_id": "GREEN"},
        }
    return {
        "id": pid, "name": name, "domain_id": "MLB-CELLPHONES", "family_name": "Galaxy S25",
        "permalink": f"https://ml.example/{pid}",
        "pictures": [{"url": "http://x/a.jpg", "secure_url": "https://x/a.jpg"}],
        "short_description": {"type": "text", "content": "Smartphone top."},
        "main_features": [{"text": "256 GB"}, {"text": "12 GB RAM"}],
        "attributes": [
            {"id": "BRAND", "name": "Marca", "value_name": "Samsung"},
            {"id": "MODEL", "name": "Modelo", "value_name": "Galaxy S25"},
            {"id": "COLOR", "name": "Cor", "value_name": "Preto"},
            {"id": "EMPTY", "name": "Vazio", "value_name": None},
        ],
        "buy_box_winner": winner,
    }


class FakeApi:
    """Roteia chamadas de `_get_json` para respostas prontas e registra o que foi pedido."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, path, params=None):
        self.calls.append((path, params))
        response = self.routes[path]
        if isinstance(response, Exception):
            raise response
        return response


def make(routes, **kwargs):
    catalog = MercadoLivreCatalog(access_token="tok", **kwargs)
    api = FakeApi(routes)
    return catalog, api


class MappingTests(unittest.TestCase):
    def test_maps_product_to_internal_dto(self):
        catalog, api = make({
            "/products/search": search_payload(hit("MLB1")),
            "/products/MLB1": detail("MLB1"),
            "/categories/MLB1055": {"name": "Celulares e Smartphones"},
        }, currency="brl")
        with patch.object(MercadoLivreCatalog, "_get_json", side_effect=api):
            page = catalog.search_products("Galaxy S25 256GB", limit=10)

        self.assertIsInstance(page, ProductPage)
        self.assertEqual((page.total, page.skip, page.limit), (1, 0, 10))
        product = page.products[0]
        self.assertEqual(product.id, "MLB1")
        self.assertEqual(product.source, SOURCE)
        self.assertEqual(product.title, "Samsung Galaxy S25 256 GB")
        self.assertEqual(product.price, Decimal("4499.00"))
        self.assertIsInstance(product.price, Decimal)
        self.assertEqual(product.currency, "BRL")
        self.assertEqual(product.category, "Celulares e Smartphones")
        a = product.attributes
        self.assertEqual(a["brand"], "Samsung")
        self.assertEqual(a["model"], "Galaxy S25")
        self.assertEqual(a["thumbnail"], "https://x/a.jpg")
        self.assertEqual(a["description"], "Smartphone top.")
        self.assertIs(a["free_shipping"], True)
        self.assertEqual(a["seller_reputation"], "GREEN")
        self.assertEqual(a["main_features"], ["256 GB", "12 GB RAM"])
        self.assertEqual(a["specs"], {"Marca": "Samsung", "Modelo": "Galaxy S25", "Cor": "Preto"})
        self.assertEqual((a["item_id"], a["seller_id"]), ("MLB999", 123))
        self.assertNotIn("Vazio", a["specs"])

    def test_search_request_parameters(self):
        catalog, api = make({"/products/search": search_payload()}, domain_id="MLB-CELLPHONES")
        with patch.object(MercadoLivreCatalog, "_get_json", side_effect=api):
            catalog.search_products("  notebook 16GB  ", limit=5, skip=10)
        path, params = api.calls[0]
        self.assertEqual(path, "/products/search")
        self.assertEqual(params, {
            "site_id": "MLB", "status": "active", "q": "notebook 16GB",
            "limit": 5, "offset": 10, "domain_id": "MLB-CELLPHONES",
        })

    def test_results_keep_search_order(self):
        ids = [f"MLB{i}" for i in range(1, 8)]
        routes = {"/products/search": search_payload(*[hit(i) for i in ids]), "/categories/MLB1055": {"name": "C"}}
        routes.update({f"/products/{i}": detail(i, price=Decimal(100 + n)) for n, i in enumerate(ids)})
        catalog, api = make(routes, max_workers=3)
        with patch.object(MercadoLivreCatalog, "_get_json", side_effect=api):
            page = catalog.search_products("x")
        self.assertEqual([p.id for p in page.products], ids)
        self.assertEqual(sum(1 for path, _ in api.calls if path == "/categories/MLB1055"), 1)  # cache


class FilteringTests(unittest.TestCase):
    def test_skips_parents_without_offer_and_missing_products(self):
        catalog, api = make({
            "/products/search": search_payload(
                hit("PAI", children=["MLB1"]), hit("MLB2"), hit("MLB3"), hit("MLB4"), hit("MLB5", status="inactive"),
                total=50,
            ),
            "/products/MLB2": detail("MLB2", price=...),               # buy_box_winner = null
            "/products/MLB3": _http_404(),                            # sumiu do catálogo
            "/products/MLB4": detail("MLB4"),
            "/categories/MLB1055": {"name": "Cat"},
        })
        with patch.object(MercadoLivreCatalog, "_get_json", side_effect=api):
            page = catalog.search_products("x")
        self.assertEqual([p.id for p in page.products], ["MLB4"])
        self.assertEqual(page.total, 50)  # total do catálogo, não só os com preço
        requested = [path for path, _ in api.calls]
        self.assertNotIn("/products/PAI", requested)   # pais não geram consulta
        self.assertNotIn("/products/MLB5", requested)  # inativos também não

    def test_drops_offers_in_other_currency_only_when_currency_is_set(self):
        routes = {
            "/products/search": search_payload(hit("MLB1"), hit("MLB2")),
            "/products/MLB1": detail("MLB1", currency="BRL"),
            "/products/MLB2": detail("MLB2", currency="USD"),
            "/categories/MLB1055": {"name": "Cat"},
        }
        catalog, api = make(routes, currency="BRL")
        with patch.object(MercadoLivreCatalog, "_get_json", side_effect=api):
            self.assertEqual([p.id for p in catalog.search_products("x").products], ["MLB1"])
        catalog, api = make(routes)
        with patch.object(MercadoLivreCatalog, "_get_json", side_effect=api):
            page = catalog.search_products("x")
        self.assertEqual({p.currency for p in page.products}, {"BRL", "USD"})

    def test_category_falls_back_to_domain_when_lookup_fails(self):
        catalog, api = make({
            "/products/search": search_payload(hit("MLB1")),
            "/products/MLB1": detail("MLB1"),
            "/categories/MLB1055": CatalogError("falhou"),
        })
        with patch.object(MercadoLivreCatalog, "_get_json", side_effect=api):
            page = catalog.search_products("x")
        self.assertEqual(page.products[0].category, "MLB-CELLPHONES")


class ErrorTests(unittest.TestCase):
    def test_invalid_price_raises(self):
        for bad in ("4499", True, Decimal("-1")):
            catalog, api = make({
                "/products/search": search_payload(hit("MLB1")),
                "/products/MLB1": detail("MLB1", price=bad),
            })
            with patch.object(MercadoLivreCatalog, "_get_json", side_effect=api):
                with self.assertRaises(CatalogError, msg=repr(bad)):
                    catalog.search_products("x")

    def test_invalid_search_payloads_raise(self):
        for bad in ([], {"results": []}, {"results": [], "paging": {"total": "x"}}):
            catalog, api = make({"/products/search": bad})
            with patch.object(MercadoLivreCatalog, "_get_json", side_effect=api):
                with self.assertRaises(CatalogError):
                    catalog.search_products("x")

    def test_list_products_requires_query(self):
        with self.assertRaises(ValueError):
            MercadoLivreCatalog(access_token="t").list_products()

    def test_list_products_uses_constructor_query(self):
        catalog, api = make({"/products/search": search_payload()}, query="  tv 55  ")
        with patch.object(MercadoLivreCatalog, "_get_json", side_effect=api):
            catalog.list_products(limit=3, skip=6)
        self.assertEqual(api.calls[0][1]["q"], "tv 55")
        self.assertEqual((api.calls[0][1]["limit"], api.calls[0][1]["offset"]), (3, 6))

    def test_argument_validation(self):
        catalog = MercadoLivreCatalog(access_token="t")
        for kwargs in ({"limit": 0}, {"limit": True}, {"skip": -1}):
            with self.assertRaises(ValueError):
                catalog.search_products("x", **kwargs)
        with self.assertRaises(ValueError):
            catalog.search_products("   ")
        for kwargs in ({"currency": "REAIS"}, {"site_id": "M1"}, {"query": " "}, {"max_workers": 0}):
            with self.assertRaises(ValueError):
                MercadoLivreCatalog(**kwargs)


class HttpTests(unittest.TestCase):
    def test_sends_bearer_token_and_encoded_query(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"], captured["auth"], captured["timeout"] = request.full_url, request.get_header("Authorization"), timeout
            return io.BytesIO(json.dumps({"results": [], "paging": {"total": 0, "limit": 1, "offset": 0}}).encode())

        with patch("buscai.catalog.mercadolivre.urlopen", side_effect=fake_urlopen):
            MercadoLivreCatalog(access_token=lambda: "abc", timeout=3).search_products("Galaxy S25 ção", limit=1)
        self.assertEqual(captured["auth"], "Bearer abc")
        self.assertEqual(captured["timeout"], 3)
        self.assertIn("q=Galaxy%20S25%20%C3%A7%C3%A3o", captured["url"])
        self.assertTrue(captured["url"].startswith("https://api.mercadolibre.com/products/search?"))

    def test_http_errors_become_catalog_errors(self):
        for code, fragment in ((401, "token"), (403, "token"), (429, "limitou"), (500, "HTTP 500")):
            err = HTTPError("u", code, "m", {}, None)
            with patch("buscai.catalog.mercadolivre.urlopen", side_effect=err):
                with self.assertRaisesRegex(CatalogError, fragment):
                    MercadoLivreCatalog(access_token="t").search_products("x")

    def test_missing_token_and_env_fallback(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(CatalogError, "MERCADOLIVRE_ACCESS_TOKEN"):
                MercadoLivreCatalog().search_products("x")
        with patch.dict("os.environ", {"MERCADOLIVRE_ACCESS_TOKEN": "envtok"}):
            self.assertEqual(MercadoLivreCatalog()._headers()["Authorization"], "Bearer envtok")

    def test_invalid_json_and_network_errors(self):
        with patch("buscai.catalog.mercadolivre.urlopen", return_value=io.BytesIO(b"<html>")):
            with self.assertRaisesRegex(CatalogError, "JSON"):
                MercadoLivreCatalog(access_token="t").search_products("x")
        with patch("buscai.catalog.mercadolivre.urlopen", side_effect=TimeoutError()):
            with self.assertRaisesRegex(CatalogError, "conectar"):
                MercadoLivreCatalog(access_token="t").search_products("x")

    def test_prices_are_parsed_as_decimal_not_float(self):
        body = json.dumps({"x": 1}).replace("1", "4499.90").encode()
        with patch("buscai.catalog.mercadolivre.urlopen", return_value=io.BytesIO(body)):
            data = MercadoLivreCatalog(access_token="t")._get_json("/x")
        self.assertEqual(data["x"], Decimal("4499.90"))


def _http_404():
    from buscai.catalog.mercadolivre import _NotFoundError
    return _NotFoundError("404")


if __name__ == "__main__":
    unittest.main()
