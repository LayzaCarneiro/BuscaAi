"""Fornecedor de catálogo baseado na API do Mercado Livre.

Como funciona
-------------
1. `GET /products/search?q=...` procura **produtos de catálogo** (PRODUCT_ID) a partir
   do texto processado pelo Agente (normalmente `UserIntent.query`).
2. Esse endpoint não traz preço. Para cada produto "filho" encontrado, um
   `GET /products/{PRODUCT_ID}` recupera o `buy_box_winner`, que é a oferta vencedora
   (preço, moeda, vendedor, frete). As consultas de detalhe rodam em paralelo.
3. Cada produto vira um `Product` interno. O `id` é o PRODUCT_ID; os dados do anúncio
   vencedor (item_id, vendedor, frete) ficam em `attributes`.

Limitações conhecidas
---------------------
* Só entram em `ProductPage.products` produtos que têm oferta com preço. Produtos pai
  (agrupadores, `children_ids` preenchido) e produtos sem `buy_box_winner` são descartados,
  então `len(products)` pode ser menor que `limit`. `total` é o total do catálogo para a
  busca e inclui esses descartados.
* Uma busca de `limit` produtos faz até `1 + limit` requisições (mais a consulta de nomes
  de categoria, que é cacheada). Use `limit` baixo e `max_workers` coerente com o seu
  limite de requisições.
* A busca por `q` é por palavra-chave, não semântica.
* O token OAuth 2.0 é informado por quem usa a classe (parâmetro ou variável de ambiente);
  a obtenção e a renovação do token ficam fora daqui.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from . import CatalogProvider
from .dummyjson import CatalogError
from .models import Product, ProductPage


API_URL = "https://api.mercadolibre.com"
SOURCE = "mercadolivre"
TOKEN_ENV_VAR = "MERCADOLIVRE_ACCESS_TOKEN"
REQUEST_TIMEOUT_SECONDS = 10
DEFAULT_MAX_WORKERS = 5


class _NotFoundError(CatalogError):
    """HTTP 404: o recurso não existe (tratado caso a caso, não derruba a busca)."""


def _clean(value: object) -> str | None:
    """Texto não vazio ou None."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _valid_currency(code: str) -> bool:
    return len(code) == 3 and code.isascii() and code.isalpha()


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class MercadoLivreCatalog(CatalogProvider):
    """Catálogo do Mercado Livre.

    Uso com o pipeline atual (que chama `list_products`): crie o catálogo já com o texto
    do Agente.

        catalog = MercadoLivreCatalog(query=intent.query, currency="BRL")

    Também é possível chamar `search_products(texto)` diretamente.
    """

    def __init__(
        self,
        *,
        query: str | None = None,
        access_token: str | Callable[[], str] | None = None,
        currency: str | None = None,
        site_id: str = "MLB",
        domain_id: str | None = None,
        max_workers: int = DEFAULT_MAX_WORKERS,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        # Moeda esperada. Ofertas em outra moeda são descartadas, para não misturar valores
        # que o filtro de orçamento não consegue comparar. Sem valor, aceita a que a API informar.
        if currency is not None and not _valid_currency(currency):
            raise ValueError("currency deve ser um código de moeda com três letras.")
        if not (len(site_id) == 3 and site_id.isascii() and site_id.isalpha()):
            raise ValueError("site_id deve ter três letras, como 'MLB'.")
        if query is not None and not query.strip():
            raise ValueError("query não pode ser vazia.")
        if not _is_int(max_workers) or max_workers <= 0:
            raise ValueError("max_workers deve ser um inteiro positivo.")

        self.query = query.strip() if query is not None else None
        self.currency = currency.upper() if currency is not None else None
        self.site_id = site_id.upper()
        self.domain_id = domain_id
        self.max_workers = max_workers
        self.timeout = timeout
        self._access_token = access_token
        self._category_names: dict[str, str | None] = {}

    # ------------------------------------------------------------------ API pública

    def list_products(self, *, limit: int = 30, skip: int = 0) -> ProductPage:
        """Contrato de `CatalogProvider`: pesquisa usando o `query` informado no construtor."""
        if self.query is None:
            raise ValueError(
                "MercadoLivreCatalog precisa de um texto de busca: informe `query` no construtor "
                "ou use search_products()."
            )
        return self.search_products(self.query, limit=limit, skip=skip)

    def search_products(
        self,
        query: str,
        *,
        limit: int = 30,
        skip: int = 0,
        domain_id: str | None = None,
    ) -> ProductPage:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query deve ser um texto não vazio.")
        if not _is_int(limit) or limit <= 0:
            raise ValueError("limit deve ser um inteiro positivo.")
        if not _is_int(skip) or skip < 0:
            raise ValueError("skip deve ser um inteiro não negativo.")

        params: dict[str, object] = {
            "site_id": self.site_id,
            "status": "active",
            "q": query.strip(),
            "limit": limit,
            "offset": skip,
        }
        domain = domain_id or self.domain_id
        if domain:
            params["domain_id"] = domain

        payload = self._get_json("/products/search", params)
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise CatalogError("Resposta inválida do Mercado Livre: campo 'results' ausente ou incorreto.")
        paging = payload.get("paging")
        if not isinstance(paging, dict):
            raise CatalogError("Resposta inválida do Mercado Livre: campo 'paging' ausente ou incorreto.")
        total = paging.get("total")
        page_limit = paging.get("limit", limit)
        page_offset = paging.get("offset", skip)
        if not all(_is_int(v) and v >= 0 for v in (total, page_limit, page_offset)):
            raise CatalogError("Resposta inválida do Mercado Livre: paginação ausente ou incorreta.")

        candidates = [item for item in payload["results"] if self._is_buyable_candidate(item)]
        details = self._fetch_details([item["id"].strip() for item in candidates])

        products = []
        for candidate, detail in zip(candidates, details):
            if detail is None:
                continue
            product = self._to_product(candidate, detail)
            if product is not None:
                products.append(product)

        return ProductPage(products=tuple(products), total=int(total), skip=page_offset, limit=page_limit)

    # ------------------------------------------------------------------ busca e detalhes

    @staticmethod
    def _is_buyable_candidate(item: object) -> bool:
        """Produto ativo e "filho". Pais agrupam variações e não têm oferta própria."""
        if not isinstance(item, dict) or _clean(item.get("id")) is None:
            return False
        if item.get("status") not in (None, "active"):
            return False
        children = item.get("children_ids")
        return not (isinstance(children, list) and children)

    def _fetch_details(self, product_ids: list[str]) -> list[dict | None]:
        if not product_ids:
            return []
        workers = min(self.max_workers, len(product_ids))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(self._fetch_detail, product_ids))  # mantém a ordem da busca

    def _fetch_detail(self, product_id: str) -> dict | None:
        try:
            payload = self._get_json(f"/products/{quote(product_id, safe='')}")
        except _NotFoundError:
            return None
        if not isinstance(payload, dict):
            raise CatalogError("Resposta inválida do Mercado Livre: detalhe do produto não é um objeto.")
        return payload

    def _category_name(self, category_id: str) -> str | None:
        """Nome da categoria (cacheado). Falha aqui é cosmética e não derruba a busca."""
        if category_id not in self._category_names:
            try:
                payload = self._get_json(f"/categories/{quote(category_id, safe='')}")
                name = _clean(payload.get("name")) if isinstance(payload, dict) else None
            except CatalogError:
                name = None
            self._category_names[category_id] = name
        return self._category_names[category_id]

    # ------------------------------------------------------------------ mapeamento para o DTO

    def _to_product(self, candidate: dict, detail: dict) -> Product | None:
        winner = detail.get("buy_box_winner")
        if winner is None:
            return None  # sem anúncio competindo: não há preço
        if not isinstance(winner, dict):
            raise CatalogError("Resposta inválida do Mercado Livre: buy_box_winner incorreto.")

        price = winner.get("price")
        if price is None:
            return None
        if isinstance(price, bool) or not isinstance(price, (int, Decimal)):
            raise CatalogError("Resposta inválida do Mercado Livre: oferta sem preço válido.")
        normalized_price = Decimal(price)
        if not normalized_price.is_finite() or normalized_price < 0:
            raise CatalogError("Resposta inválida do Mercado Livre: oferta sem preço válido.")

        currency = (_clean(winner.get("currency_id")) or self.currency)
        currency = currency.upper() if currency else None
        if self.currency is not None and currency != self.currency:
            return None

        title = _clean(detail.get("name")) or _clean(candidate.get("name"))
        if title is None:
            raise CatalogError("Resposta inválida do Mercado Livre: produto sem nome válido.")

        category_id = _clean(winner.get("category_id"))
        category = (self._category_name(category_id) if category_id else None) or _clean(detail.get("domain_id"))

        return Product(
            id=candidate["id"].strip(),
            source=SOURCE,
            title=title,
            price=normalized_price,
            currency=currency,
            category=category,
            attributes=self._build_attributes(detail, winner),
        )

    @staticmethod
    def _build_attributes(detail: dict, winner: dict) -> dict[str, object]:
        """Atributos do produto (catálogo) + dados do anúncio vencedor, sem valores vazios."""
        specs: dict[str, str] = {}
        brand = model = None
        for attr in detail.get("attributes") or []:
            if not isinstance(attr, dict):
                continue
            value = _clean(attr.get("value_name"))
            if value is None:
                continue
            if attr.get("id") == "BRAND":
                brand = value
            elif attr.get("id") == "MODEL":
                model = value
            name = _clean(attr.get("name"))
            if name:
                specs[name] = value

        features: list[str] = []
        for feature in detail.get("main_features") or []:
            feature_text = _clean(feature.get("text")) if isinstance(feature, dict) else None
            if feature_text:
                features.append(feature_text)

        description = detail.get("short_description")
        if isinstance(description, dict):
            description = description.get("content")

        thumbnail = None
        for picture in detail.get("pictures") or []:
            url = _clean(picture.get("secure_url") or picture.get("url")) if isinstance(picture, dict) else None
            if url and url.startswith(("http://", "https://")):
                thumbnail = url
                break

        shipping = winner.get("shipping")
        free_shipping = shipping.get("free_shipping") if isinstance(shipping, dict) else None
        seller = winner.get("seller")
        reputation = _clean(seller.get("reputation_level_id")) if isinstance(seller, dict) else None

        attributes: dict[str, object] = {
            "brand": brand,
            "model": model,
            "description": _clean(description),
            "thumbnail": thumbnail,
            "free_shipping": free_shipping if isinstance(free_shipping, bool) else None,
            "seller_reputation": reputation,
            "main_features": features,
            "specs": specs,
            "family_name": _clean(detail.get("family_name")),
            "domain_id": _clean(detail.get("domain_id")),
            "permalink": _clean(detail.get("permalink")),
            "item_id": _clean(winner.get("item_id")),
            "seller_id": winner.get("seller_id") if _is_int(winner.get("seller_id")) else None,
        }
        return {key: value for key, value in attributes.items() if value not in (None, "", [], {})}

    # ------------------------------------------------------------------ HTTP

    def _headers(self) -> dict[str, str]:
        token = self._access_token() if callable(self._access_token) else self._access_token
        token = token or os.getenv(TOKEN_ENV_VAR)
        if not token or not token.strip():
            raise CatalogError(
                f"Token de acesso do Mercado Livre ausente: informe access_token ou defina {TOKEN_ENV_VAR}."
            )
        return {
            "Authorization": f"Bearer {token.strip()}",
            "Accept": "application/json",
            "User-Agent": "BuscAI/0.1",
        }

    def _get_json(self, path: str, params: dict[str, object] | None = None) -> object:
        url = f"{API_URL}{path}"
        if params:
            url += "?" + urlencode(params, quote_via=quote)
        request = Request(url, headers=self._headers())
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.load(response, parse_float=Decimal)
        except HTTPError as exc:
            raise self._http_error(exc) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise CatalogError(f"Não foi possível conectar ao Mercado Livre: {exc}.") from exc
        except (ValueError, UnicodeError) as exc:
            raise CatalogError("O Mercado Livre retornou um JSON inválido.") from exc

    @staticmethod
    def _http_error(exc: HTTPError) -> CatalogError:
        if exc.code == 404:
            return _NotFoundError("Mercado Livre retornou HTTP 404.")
        if exc.code in (401, 403):
            return CatalogError(f"O Mercado Livre recusou o token de acesso (HTTP {exc.code}).")
        if exc.code == 429:
            return CatalogError("O Mercado Livre limitou as requisições (HTTP 429). Tente novamente em instantes.")
        return CatalogError(f"Mercado Livre retornou HTTP {exc.code}.")
