"""Fornecedor de catálogo baseado na API pública DummyJSON."""

from __future__ import annotations

import json
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import Product, ProductPage


PRODUCTS_URL = "https://dummyjson.com/products"
SOURCE = "dummyjson"
REQUEST_TIMEOUT_SECONDS = 10


class CatalogError(RuntimeError):
    """Falha de comunicação ou resposta inválida do fornecedor."""


class DummyJsonCatalog:
    def __init__(self, *, currency: str | None = None) -> None:
        # A API não informa a moeda. Sem configuração explícita, ela é desconhecida.
        if currency is not None and (len(currency) != 3 or not currency.isascii() or not currency.isalpha()):
            raise ValueError("currency deve ser um código de moeda com três letras.")
        self.currency = currency.upper() if currency is not None else None

    def list_products(self, *, limit: int = 30, skip: int = 0) -> ProductPage:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit deve ser um inteiro positivo.")
        if isinstance(skip, bool) or not isinstance(skip, int) or skip < 0:
            raise ValueError("skip deve ser um inteiro não negativo.")

        request = Request(
            f"{PRODUCTS_URL}?{urlencode({'limit': limit, 'skip': skip})}",
            headers={"User-Agent": "BuscAI/0.1"},
        )
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                payload = json.load(response, parse_float=Decimal)
        except HTTPError as exc:
            raise CatalogError(f"DummyJSON retornou HTTP {exc.code}.") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise CatalogError(f"Não foi possível conectar ao DummyJSON: {exc}.") from exc
        except (ValueError, UnicodeError) as exc:
            raise CatalogError("DummyJSON retornou um JSON inválido.") from exc

        if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
            raise CatalogError("Resposta inválida do DummyJSON: campo 'products' ausente ou incorreto.")

        total = payload.get("total")
        page_skip = payload.get("skip")
        page_limit = payload.get("limit")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (total, page_skip, page_limit)):
            raise CatalogError("Resposta inválida do DummyJSON: paginação ausente ou incorreta.")
        # O DummyJSON informa a quantidade efetiva na última página.
        if page_skip != skip or page_limit > limit or len(payload["products"]) > page_limit:
            raise CatalogError("Resposta inválida do DummyJSON: paginação diferente da solicitada.")

        return ProductPage(
            products=tuple(self._parse_product(item) for item in payload["products"]),
            total=total,
            skip=page_skip,
            limit=page_limit,
        )

    def _parse_product(self, item: object) -> Product:
        if not isinstance(item, dict):
            raise CatalogError("Resposta inválida do DummyJSON: produto não é um objeto.")

        product_id = item.get("id")
        title = item.get("title")
        price = item.get("price")
        category = item.get("category")
        if isinstance(product_id, bool) or not isinstance(product_id, int) or product_id <= 0:
            raise CatalogError("Resposta inválida do DummyJSON: produto sem ID válido.")
        if not isinstance(title, str) or not title.strip():
            raise CatalogError("Resposta inválida do DummyJSON: produto sem título válido.")
        if isinstance(price, bool) or not isinstance(price, (int, Decimal)):
            raise CatalogError("Resposta inválida do DummyJSON: produto sem preço válido.")
        normalized_price = Decimal(price)
        if not normalized_price.is_finite() or normalized_price < 0:
            raise CatalogError("Resposta inválida do DummyJSON: produto sem preço válido.")
        if category is not None and (not isinstance(category, str) or not category.strip()):
            raise CatalogError("Resposta inválida do DummyJSON: categoria incorreta.")

        core_fields = {"id", "title", "price", "category"}
        return Product(
            id=str(product_id),
            source=SOURCE,
            title=title.strip(),
            price=normalized_price,
            currency=self.currency,
            category=category.strip() if category is not None else None,
            attributes={key: value for key, value in item.items() if key not in core_fields},
        )
