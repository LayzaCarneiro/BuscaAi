"""Cliente produtivo da eBay Browse API (item_summary/search e getItem seletivo)."""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Literal
from urllib.parse import quote

import httpx

from buscai.catalog.errors import (
    CatalogError,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from buscai.observability import log_operational_event
from buscai.search.ebay_auth import EbayOAuthClient, parse_retry_after_header
from buscai.search.ebay_mapper import (
    map_item_detail,
    merge_candidate_details,
    parse_json_decimal,
    parse_search_response,
)
from buscai.search.models import Candidate, SearchQuery, SearchResponse
from buscai.search.quota import QuotaGuard, RequestCallBudget

EBAY_BROWSE_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EBAY_BROWSE_ITEM_URL = "https://api.ebay.com/buy/browse/v1/item/{item_id}"
DEFAULT_BROWSE_TIMEOUT = httpx.Timeout(connect=5.0, read=12.0, write=5.0, pool=5.0)


def _format_decimal_param(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def build_ebay_search_params(query: SearchQuery) -> dict[str, str]:
    """Constrói os query parameters oficiais para `GET /buy/browse/v1/item_summary/search`."""
    params: dict[str, str] = {
        "q": query.query_text,
        "limit": str(query.limit),
        "offset": str(query.offset),
    }

    filters: list[str] = []
    if query.min_price_usd is not None or query.max_price_usd is not None:
        lo = _format_decimal_param(query.min_price_usd) if query.min_price_usd is not None else ""
        hi = _format_decimal_param(query.max_price_usd) if query.max_price_usd is not None else ""
        filters.append(f"price:[{lo}..{hi}]")
        filters.append(f"priceCurrency:{query.currency}")

    if query.fixed_price_only:
        filters.append("buyingOptions:{FIXED_PRICE}")

    if query.conditions:
        conds = "|".join(cond.upper() for cond in query.conditions)
        filters.append(f"conditions:{{{conds}}}")

    if query.condition_ids:
        cond_ids = "|".join(query.condition_ids)
        filters.append(f"conditionIds:{{{cond_ids}}}")

    if filters:
        params["filter"] = ",".join(filters)

    if query.category_ids:
        params["category_ids"] = ",".join(query.category_ids)

    return params


class EbayLiveBackend:
    """Cliente somente leitura da eBay Browse API sem cache ou persistência de anúncios."""

    def __init__(
        self,
        *,
        oauth_client: EbayOAuthClient,
        quota_guard: QuotaGuard | None = None,
        default_marketplace_id: str = "EBAY_US",
        search_url: str = EBAY_BROWSE_SEARCH_URL,
        item_url_template: str = EBAY_BROWSE_ITEM_URL,
        max_retries: int = 2,
        base_backoff_seconds: float = 0.5,
        max_backoff_seconds: float = 4.0,
        timeout: httpx.Timeout | float = DEFAULT_BROWSE_TIMEOUT,
        http_client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries deve ser um inteiro não negativo.")
        if (
            isinstance(base_backoff_seconds, bool)
            or not isinstance(base_backoff_seconds, (int, float))
            or not math.isfinite(base_backoff_seconds)
            or base_backoff_seconds < 0
        ):
            raise ValueError("base_backoff_seconds deve ser numérico finito não negativo.")
        if (
            isinstance(max_backoff_seconds, bool)
            or not isinstance(max_backoff_seconds, (int, float))
            or not math.isfinite(max_backoff_seconds)
            or max_backoff_seconds < base_backoff_seconds
        ):
            raise ValueError("max_backoff_seconds deve ser maior ou igual a base_backoff_seconds.")
        if not isinstance(default_marketplace_id, str) or not default_marketplace_id.strip():
            raise ValueError("default_marketplace_id não pode ser vazio.")

        self._oauth = oauth_client
        self._quota_guard = quota_guard or QuotaGuard(logger=logger)
        self._default_marketplace_id = default_marketplace_id.strip().upper()
        self._search_url = search_url
        self._item_url_template = item_url_template
        self._max_retries = max_retries
        self._base_backoff = float(base_backoff_seconds)
        self._max_backoff = float(max_backoff_seconds)
        self._sleep = sleep
        self._logger = logger

        if http_client is not None:
            self._client = http_client
            self._owns_client = False
        else:
            self._client = httpx.Client(timeout=timeout, transport=transport)
            self._owns_client = True

    def close(self) -> None:
        """Libera o cliente HTTP interno quando gerenciado pela própria instância."""
        if self._owns_client:
            self._client.close()

    def search(
        self,
        query: SearchQuery,
        *,
        budget: RequestCallBudget | None = None,
        request_id: str | None = None,
    ) -> SearchResponse:
        """Executa busca efêmera na eBay Browse API (`item_summary/search`)."""
        params = build_ebay_search_params(query)
        marketplace_id = query.marketplace_id or self._default_marketplace_id
        payload, calls_used = self._request_json(
            url=self._search_url,
            params=params,
            marketplace_id=marketplace_id,
            operation="search",
            budget=budget,
            request_id=request_id,
            allow_not_found=False,
        )
        response = parse_search_response(
            payload,
            limit=query.limit,
            offset=query.offset,
            calls_used=calls_used,
        )
        self._log_event(
            logging.INFO,
            "ebay_search_completed",
            operation="search",
            marketplace_id=marketplace_id,
            status_code=200,
            item_count=len(response.candidates),
            ebay_calls=calls_used,
            request_id=request_id,
        )
        return response

    def get_item_details(
        self,
        item_id: str,
        *,
        budget: RequestCallBudget | None = None,
        request_id: str | None = None,
        marketplace_id: str | None = None,
    ) -> Candidate | None:
        """Recupera detalhes completos e `localizedAspects` de um item via `getItem`."""
        if not isinstance(item_id, str) or not item_id.strip():
            raise ValueError("item_id deve ser um texto não vazio.")

        encoded_id = quote(item_id.strip(), safe="")
        url = self._item_url_template.format(item_id=encoded_id)
        mkt = (marketplace_id or self._default_marketplace_id).strip().upper()
        payload, _ = self._request_json(
            url=url,
            params=None,
            marketplace_id=mkt,
            operation="get_item",
            budget=budget,
            request_id=request_id,
            allow_not_found=True,
        )
        if payload is None:
            return None
        candidate = map_item_detail(payload)
        if candidate is None:
            raise CatalogError("Resposta inválida da eBay Browse API ao detalhar item.")
        return candidate

    def enrich_candidates(
        self,
        candidates: Sequence[Candidate],
        *,
        max_items: int = 6,
        max_concurrency: int = 4,
        budget: RequestCallBudget | None = None,
        request_id: str | None = None,
        marketplace_id: str | None = None,
    ) -> tuple[Candidate, ...]:
        """Enriquece seletivamente até `max_items` candidatos via `getItem` em paralelo."""
        if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 0:
            raise ValueError("max_items deve ser um inteiro não negativo.")
        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or max_concurrency < 1:
            raise ValueError("max_concurrency deve ser um inteiro positivo.")

        result = list(candidates)
        if not result or max_items == 0:
            return tuple(result)

        cap = max_items
        if budget is not None:
            cap = min(cap, budget.remaining_get_item_calls)
        if cap <= 0:
            return tuple(result)

        target_indices = [
            idx for idx, cand in enumerate(result) if not cand.details_loaded
        ][:cap]
        if not target_indices:
            return tuple(result)

        def _fetch_one(idx: int) -> Candidate:
            summary = result[idx]
            try:
                detail = self.get_item_details(
                    summary.item_id,
                    budget=budget,
                    request_id=request_id,
                    marketplace_id=marketplace_id,
                )
            except ProviderAuthError:
                raise
            except (ProviderTimeoutError, ProviderUnavailableError, ProviderRateLimitError, CatalogError) as exc:
                self._log_event(
                    logging.WARNING,
                    "ebay_get_item_partial_failure",
                    operation="get_item",
                    marketplace_id=(marketplace_id or self._default_marketplace_id),
                    error_type=type(exc).__name__,
                    request_id=request_id,
                )
                return summary
            return merge_candidate_details(summary, detail) if detail is not None else summary

        workers = min(max_concurrency, len(target_indices))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            enriched = list(pool.map(_fetch_one, target_indices))

        for idx, cand in zip(target_indices, enriched):
            result[idx] = cand
        return tuple(result)

    def _request_json(
        self,
        *,
        url: str,
        params: dict[str, str] | None,
        marketplace_id: str,
        operation: Literal["search", "get_item"],
        budget: RequestCallBudget | None,
        request_id: str | None,
        allow_not_found: bool,
    ) -> tuple[object | None, int]:
        calls_used = 0
        retry_attempt = 0
        refreshed_auth = False

        while True:
            token = self._oauth.get_access_token(
                force_refresh=refreshed_auth,
                request_id=request_id,
            )
            # Consome do orçamento por requisição somente na primeira tentativa; retries
            # ainda contabilizam na cota diária global sem violar a política de retry.
            self._quota_guard.consume(
                operation,
                budget=budget if calls_used == 0 else None,
                request_id=request_id,
            )
            calls_used += 1

            started = time.monotonic()
            try:
                response = self._client.get(
                    url,
                    params=params,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-EBAY-C-MARKETPLACE-ID": marketplace_id,
                        "Accept": "application/json",
                        "User-Agent": "BuscAI/0.2",
                    },
                )
            except httpx.TimeoutException as exc:
                duration_ms = self._elapsed_ms(started)
                if retry_attempt < self._max_retries:
                    delay = self._compute_backoff(retry_attempt)
                    retry_attempt += 1
                    self._log_event(
                        logging.WARNING,
                        "ebay_request_retry",
                        operation=operation,
                        marketplace_id=marketplace_id,
                        duration_ms=duration_ms,
                        retry_attempt=retry_attempt,
                        error_type="ProviderTimeoutError",
                        request_id=request_id,
                    )
                    self._sleep(delay)
                    continue
                self._log_event(
                    logging.ERROR,
                    "ebay_request_timeout",
                    operation=operation,
                    marketplace_id=marketplace_id,
                    duration_ms=duration_ms,
                    error_type="ProviderTimeoutError",
                    request_id=request_id,
                )
                raise ProviderTimeoutError("Timeout ao consultar a eBay Browse API.") from exc
            except httpx.HTTPError as exc:
                duration_ms = self._elapsed_ms(started)
                if retry_attempt < self._max_retries:
                    delay = self._compute_backoff(retry_attempt)
                    retry_attempt += 1
                    self._log_event(
                        logging.WARNING,
                        "ebay_request_retry",
                        operation=operation,
                        marketplace_id=marketplace_id,
                        duration_ms=duration_ms,
                        retry_attempt=retry_attempt,
                        error_type="ProviderUnavailableError",
                        request_id=request_id,
                    )
                    self._sleep(delay)
                    continue
                self._log_event(
                    logging.ERROR,
                    "ebay_request_network_error",
                    operation=operation,
                    marketplace_id=marketplace_id,
                    duration_ms=duration_ms,
                    error_type="ProviderUnavailableError",
                    request_id=request_id,
                )
                raise ProviderUnavailableError("Falha de rede ao consultar a eBay Browse API.") from exc

            duration_ms = self._elapsed_ms(started)
            status = response.status_code

            if status == 200:
                return parse_json_decimal(response.text), calls_used

            if status == 404 and allow_not_found:
                self._log_event(
                    logging.INFO,
                    "ebay_item_not_found",
                    operation=operation,
                    marketplace_id=marketplace_id,
                    status_code=status,
                    duration_ms=duration_ms,
                    request_id=request_id,
                )
                return None, calls_used

            if status == 401:
                self._oauth.invalidate()
                if not refreshed_auth:
                    refreshed_auth = True
                    self._log_event(
                        logging.WARNING,
                        "ebay_token_refresh_on_401",
                        operation=operation,
                        marketplace_id=marketplace_id,
                        status_code=status,
                        duration_ms=duration_ms,
                        request_id=request_id,
                    )
                    continue
                self._log_event(
                    logging.ERROR,
                    "ebay_auth_failed",
                    operation=operation,
                    marketplace_id=marketplace_id,
                    status_code=status,
                    duration_ms=duration_ms,
                    error_type="ProviderAuthError",
                    request_id=request_id,
                )
                raise ProviderAuthError("A eBay recusou o token de acesso mesmo após renovação (HTTP 401).")

            if status == 403:
                self._log_event(
                    logging.ERROR,
                    "ebay_auth_forbidden",
                    operation=operation,
                    marketplace_id=marketplace_id,
                    status_code=status,
                    duration_ms=duration_ms,
                    error_type="ProviderAuthError",
                    request_id=request_id,
                )
                raise ProviderAuthError("A eBay recusou a permissão de acesso (HTTP 403).")

            if status == 429:
                retry_after = parse_retry_after_header(response.headers)
                delay = retry_after if retry_after is not None else self._compute_backoff(retry_attempt)
                if retry_attempt < self._max_retries and delay <= self._max_backoff:
                    retry_attempt += 1
                    self._log_event(
                        logging.WARNING,
                        "ebay_rate_limit_retry",
                        operation=operation,
                        marketplace_id=marketplace_id,
                        status_code=status,
                        duration_ms=duration_ms,
                        retry_attempt=retry_attempt,
                        error_type="ProviderRateLimitError",
                        request_id=request_id,
                    )
                    self._sleep(delay)
                    continue
                self._log_event(
                    logging.ERROR,
                    "ebay_rate_limited",
                    operation=operation,
                    marketplace_id=marketplace_id,
                    status_code=status,
                    duration_ms=duration_ms,
                    error_type="ProviderRateLimitError",
                    request_id=request_id,
                )
                raise ProviderRateLimitError(
                    "A eBay limitou as requisições (HTTP 429).",
                    retry_after_seconds=retry_after,
                )

            if 500 <= status < 600:
                if retry_attempt < self._max_retries:
                    delay = self._compute_backoff(retry_attempt)
                    retry_attempt += 1
                    self._log_event(
                        logging.WARNING,
                        "ebay_server_error_retry",
                        operation=operation,
                        marketplace_id=marketplace_id,
                        status_code=status,
                        duration_ms=duration_ms,
                        retry_attempt=retry_attempt,
                        error_type="ProviderUnavailableError",
                        request_id=request_id,
                    )
                    self._sleep(delay)
                    continue
                self._log_event(
                    logging.ERROR,
                    "ebay_server_unavailable",
                    operation=operation,
                    marketplace_id=marketplace_id,
                    status_code=status,
                    duration_ms=duration_ms,
                    error_type="ProviderUnavailableError",
                    request_id=request_id,
                )
                raise ProviderUnavailableError(f"eBay Browse API indisponível (HTTP {status}).")

            self._log_event(
                logging.ERROR,
                "ebay_http_error",
                operation=operation,
                marketplace_id=marketplace_id,
                status_code=status,
                duration_ms=duration_ms,
                error_type="CatalogError",
                request_id=request_id,
            )
            raise CatalogError(f"eBay Browse API retornou HTTP {status}.")

    def _compute_backoff(self, retry_attempt: int) -> float:
        return min(self._max_backoff, self._base_backoff * (2 ** retry_attempt))

    def _log_event(
        self,
        level: int,
        event: str,
        *,
        operation: Literal["search", "get_item"],
        **fields: object,
    ) -> None:
        if self._logger is not None:
            log_operational_event(
                self._logger,
                level,
                event,
                stage=f"ebay_{operation}",
                provider="ebay",
                **fields,
            )

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return round((time.monotonic() - started) * 1000.0, 2)
