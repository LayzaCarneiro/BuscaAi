"""Busca textual simples sobre produtos de um fornecedor de catálogo."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable

from buscai.catalog import CatalogProvider, Product
from buscai.catalog.dummyjson import CatalogError, DummyJsonCatalog


CATALOG_PAGE_SIZE = 30
MAX_RESULTS_PER_PAGE = 50


@dataclass(frozen=True)
class SearchResultPage:
    products: tuple[Product, ...]
    total_matches: int
    limit: int
    offset: int


def normalize_text(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    separated_units = re.sub(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)", " ", without_accents)
    return " ".join(re.findall(r"[^\W_]+", separated_units))


def _matches(product: Product, terms: list[str]) -> bool:
    fields = [product.title, product.category or ""]
    for name in ("description", "brand", "tags"):
        value = product.attributes.get(name)
        if isinstance(value, str):
            fields.append(value)
        elif isinstance(value, list):
            fields.extend(item for item in value if isinstance(item, str))
    searchable = normalize_text(" ".join(fields))
    words = set(searchable.split())
    return all(term in words if any(char.isdigit() for char in term) else term in searchable for term in terms)


def _price_limit(value: Decimal | int | float | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float)):
        raise ValueError("max_price deve ser um número não negativo.")
    try:
        price = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("max_price deve ser um número não negativo.") from exc
    if not price.is_finite() or price < 0:
        raise ValueError("max_price deve ser um número não negativo.")
    return price


def search_products(
    query: str,
    max_price: Decimal | int | float | None = None,
    *,
    catalog: CatalogProvider | None = None,
    budget_currency: str | None = None,
    limit: int = 10,
    offset: int = 0,
    filter_product: Callable[[Product], bool] | None = None,
) -> SearchResultPage:
    """Busca em todas as páginas do catálogo e pagina as correspondências.

    O teto de preço só é aplicado quando a moeda do orçamento e a do
    produto são conhecidas e iguais. `catalog` permite trocar o fornecedor.
    """
    if not isinstance(query, str):
        raise ValueError("query deve ser um texto não vazio.")
    normalized_query = normalize_text(query)
    if not normalized_query:
        raise ValueError("query deve ser um texto não vazio.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS_PER_PAGE:
        raise ValueError(f"limit deve ser um inteiro entre 1 e {MAX_RESULTS_PER_PAGE}.")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset deve ser um inteiro não negativo.")

    price_limit = _price_limit(max_price)
    if price_limit is not None:
        if (
            not isinstance(budget_currency, str)
            or len(budget_currency) != 3
            or not budget_currency.isascii()
            or not budget_currency.isalpha()
        ):
            raise ValueError("budget_currency deve indicar a moeda do orçamento com três letras.")
        budget_currency = budget_currency.upper()

    provider = catalog if catalog is not None else DummyJsonCatalog()
    matches: list[Product] = []
    terms = normalized_query.split()
    skip = 0
    expected_total: int | None = None
    while True:
        page = provider.list_products(limit=CATALOG_PAGE_SIZE, skip=skip)
        if expected_total is None:
            expected_total = page.total
        if page.total != expected_total or page.skip != skip:
            raise CatalogError("O catálogo mudou durante a busca ou retornou paginação incorreta.")
        if len(page.products) > page.limit or skip + len(page.products) > page.total:
            raise CatalogError("O catálogo retornou uma página de produtos incorreta.")
        if not page.products and skip < page.total:
            raise CatalogError("O catálogo interrompeu a paginação antes do fim.")

        for product in page.products:
            if not _matches(product, terms):
                continue
            if filter_product is not None and not filter_product(product):
                continue
            if price_limit is not None:
                if product.currency is None or product.currency.upper() != budget_currency:
                    raise ValueError("Não é possível filtrar preço: moeda do produto ausente ou diferente do orçamento.")
                if product.price > price_limit:
                    continue
            matches.append(product)

        skip += len(page.products)
        if skip >= page.total:
            break

    return SearchResultPage(
        products=tuple(matches[offset : offset + limit]),
        total_matches=len(matches),
        limit=limit,
        offset=offset,
    )
