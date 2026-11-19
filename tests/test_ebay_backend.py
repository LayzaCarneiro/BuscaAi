"""Testes do cliente EbayLiveBackend, normalização defensiva, retries e conformidade."""

from __future__ import annotations

import io
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from buscai.catalog.errors import (
    CatalogError,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from buscai.config import load_settings
from buscai.observability import configure_logging
from buscai.pricing import CurrencyConverter, FixedEnvExchangeRateProvider
from buscai.search import EbayLiveBackend, EbayOAuthClient, QuotaGuard, SearchQuery, build_ebay_search_params
from scripts.check_ebay_connection import main as check_ebay_main


def _make_oauth(handler=None, *, logger=None) -> EbayOAuthClient:
    default_handler = handler or (
        lambda _req: httpx.Response(200, json={"access_token": "SENTINEL_OAUTH_TOKEN", "expires_in": 7200})
    )
    return EbayOAuthClient(
        client_id="fake_cid",
        client_secret="fake_csec",
        transport=httpx.MockTransport(default_handler),
        logger=logger,
    )


def test_search_sends_official_params_normalizes_candidates_and_preserves_missing_fields() -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        payload = {
            "total": 42,
            "limit": 10,
            "offset": 0,
            "itemSummaries": [
                {
                    "itemId": "v1|111|0",
                    "title": "Synthetic Lenovo Laptop 16GB",
                    "price": {"value": "799.95", "currency": "USD"},
                    "condition": "NEW",
                    "categories": [{"categoryName": "Laptops & Netbooks"}],
                    "brand": "Lenovo",
                    "shortDescription": "Lightweight synthetic laptop",
                    "image": {"imageUrl": "https://img.example.com/laptop.jpg"},
                    "itemWebUrl": "https://www.ebay.example.com/itm/111",
                    "seller": {
                        "username": "trusted_store",
                        "feedbackScore": 2500,
                        "feedbackPercentage": "99.6",
                    },
                    "shippingOptions": [
                        {"shippingCostType": "FIXED", "shippingCost": {"value": "0.00", "currency": "USD"}}
                    ],
                    "buyingOptions": ["FIXED_PRICE"],
                },
                {
                    "itemId": "v1|222|0",
                    "title": "Synthetic Minimal Item",
                    "price": {"value": "350.00", "currency": "USD"},
                },
                {
                    "itemId": "v1|bad|0",
                    "title": "Broken Price Item",
                    "price": {"value": "-10", "currency": "USD"},
                },
            ],
        }
        return httpx.Response(200, json=payload)

    oauth = _make_oauth()
    backend = EbayLiveBackend(oauth_client=oauth, transport=httpx.MockTransport(handler))
    try:
        query = SearchQuery(
            query_text="lenovo laptop",
            marketplace_id="EBAY_US",
            category_ids=("177",),
            min_price_usd=Decimal("300.00"),
            max_price_usd=Decimal("900.50"),
            conditions=("NEW", "OPEN_BOX"),
            limit=10,
            offset=0,
        )
        response = backend.search(query, request_id="req-search-1")

        assert len(captured_requests) == 1
        req = captured_requests[0]
        assert req.headers["Authorization"] == "Bearer SENTINEL_OAUTH_TOKEN"
        assert req.headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US"
        assert req.url.params["q"] == "lenovo laptop"
        assert req.url.params["limit"] == "10"
        assert req.url.params["offset"] == "0"
        assert req.url.params["category_ids"] == "177"
        assert req.url.params["filter"] == (
            "price:[300..900.5],priceCurrency:USD,buyingOptions:{FIXED_PRICE},conditions:{NEW|OPEN_BOX}"
        )

        assert response.total_estimated == 42
        assert response.calls_used == 1
        assert len(response.candidates) == 2

        full, minimal = response.candidates
        assert full.item_id == "v1|111|0"
        assert full.price == Decimal("799.95")
        assert isinstance(full.price, Decimal)
        assert full.currency == "USD"
        assert full.seller is not None and full.seller.feedback_percentage == Decimal("99.6")
        assert full.shipping is not None and full.shipping.free_shipping is True
        assert full.details_loaded is False

        assert minimal.item_id == "v1|222|0"
        assert minimal.brand is None
        assert minimal.condition is None
        assert minimal.seller is None
        assert minimal.shipping is None
        assert minimal.image_url is None
        assert minimal.item_web_url is None
    finally:
        backend.close()
        oauth.close()



def test_search_handles_empty_results() -> None:
    responses = [
        httpx.Response(200, json={"total": 0, "limit": 10, "offset": 0}),
        httpx.Response(200, json={"total": 0, "limit": 10, "offset": 0, "itemSummaries": []}),
    ]
    idx = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal idx
        resp = responses[idx]
        idx += 1
        return resp

    oauth = _make_oauth()
    backend = EbayLiveBackend(oauth_client=oauth, transport=httpx.MockTransport(handler))
    try:
        for _ in range(2):
            res = backend.search(SearchQuery(query_text="nonexistent", limit=10))
            assert res.candidates == ()
            assert res.total_estimated == 0
    finally:
        backend.close()
        oauth.close()



def test_retries_429_respecting_retry_after_and_5xx_then_fails_cleanly() -> None:
    sleeps: list[float] = []
    attempts = 0

    def handler_429_then_ok(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "1.5"})
        return httpx.Response(200, json={"total": 1, "itemSummaries": []})

    oauth = _make_oauth()
    backend = EbayLiveBackend(
        oauth_client=oauth,
        max_retries=2,
        base_backoff_seconds=0.5,
        max_backoff_seconds=4.0,
        transport=httpx.MockTransport(handler_429_then_ok),
        sleep=sleeps.append,
    )
    try:
        res = backend.search(SearchQuery(query_text="laptop"))
        assert res.calls_used == 2
        assert sleeps == [1.5]
    finally:
        backend.close()
        oauth.close()


    # 429 persistente esgota retries e levanta ProviderRateLimitError
    oauth2 = _make_oauth()
    backend_always_429 = EbayLiveBackend(
        oauth_client=oauth2,
        max_retries=1,
        base_backoff_seconds=0.2,
        max_backoff_seconds=2.0,
        transport=httpx.MockTransport(lambda _r: httpx.Response(429, headers={"Retry-After": "0.5"})),
        sleep=lambda _s: None,
    )
    try:
        with pytest.raises(ProviderRateLimitError) as exc_info:
            backend_always_429.search(SearchQuery(query_text="laptop"))
        assert exc_info.value.retry_after_seconds == 0.5
    finally:
        backend_always_429.close()
        oauth2.close()

    # 503 persistente levanta ProviderUnavailableError
    oauth3 = _make_oauth()
    backend_503 = EbayLiveBackend(
        oauth_client=oauth3,
        max_retries=1,
        base_backoff_seconds=0.1,
        max_backoff_seconds=1.0,
        transport=httpx.MockTransport(lambda _r: httpx.Response(503)),
        sleep=lambda _s: None,
    )
    try:
        with pytest.raises(ProviderUnavailableError, match="503"):
            backend_503.search(SearchQuery(query_text="laptop"))
    finally:
        backend_503.close()
        oauth3.close()


def test_refreshes_token_once_on_401_and_handles_403_and_timeout() -> None:
    token_num = 0

    def oauth_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal token_num
        token_num += 1
        return httpx.Response(200, json={"access_token": f"tok_{token_num}", "expires_in": 3600})

    def browse_handler(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == "Bearer tok_1":
            return httpx.Response(401)
        return httpx.Response(200, json={"total": 0, "itemSummaries": []})

    oauth = _make_oauth(oauth_handler)
    backend = EbayLiveBackend(oauth_client=oauth, transport=httpx.MockTransport(browse_handler))
    try:
        res = backend.search(SearchQuery(query_text="monitor"))
        assert res.calls_used == 2
        assert token_num == 2
    finally:
        backend.close()
        oauth.close()


    # 403 -> ProviderAuthError sem retry
    oauth403 = _make_oauth()
    backend403 = EbayLiveBackend(
        oauth_client=oauth403,
        max_retries=2,
        transport=httpx.MockTransport(lambda _r: httpx.Response(403)),
    )
    try:
        with pytest.raises(ProviderAuthError, match="403"):
            backend403.search(SearchQuery(query_text="monitor"))
    finally:
        backend403.close()
        oauth403.close()

    # Timeout -> ProviderTimeoutError após retries
    def raise_timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout")

    oauth_to = _make_oauth()
    backend_to = EbayLiveBackend(
        oauth_client=oauth_to,
        max_retries=1,
        base_backoff_seconds=0.05,
        max_backoff_seconds=1.0,
        transport=httpx.MockTransport(raise_timeout),
        sleep=lambda _s: None,
    )
    try:
        with pytest.raises(ProviderTimeoutError):
            backend_to.search(SearchQuery(query_text="monitor"))
    finally:
        backend_to.close()
        oauth_to.close()


def test_get_item_details_and_selective_enrich_candidates() -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.raw_path.decode("ascii"))
        if "v1%7C1%7C0" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "itemId": "v1|1|0",
                    "title": "Synthetic Laptop 1",
                    "price": {"value": "800.00", "currency": "USD"},
                    "categoryPath": "Computers|Laptops",
                    "localizedAspects": [
                        {"type": "STRING", "name": "Brand", "value": "Lenovo"},
                        {"type": "STRING", "name": "RAM Size", "value": "16 GB"},
                        {"type": "STRING_ARRAY", "name": "Features", "value": ["Backlit Keyboard", "SSD"]},
                    ],
                },
            )
        if "v1%7C2%7C0" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(500)

    oauth = _make_oauth()
    guard = QuotaGuard(daily_limit=100, default_max_get_item_per_request=2)
    budget = guard.new_request_budget()
    backend = EbayLiveBackend(
        oauth_client=oauth,
        quota_guard=guard,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )

    try:
        from buscai.search.models import Candidate

        candidates = [
            Candidate(item_id="v1|1|0", source="ebay", title="Synthetic Laptop 1", price=Decimal("800.00"), currency="USD"),
            Candidate(item_id="v1|2|0", source="ebay", title="Synthetic Laptop 2", price=Decimal("700.00"), currency="USD"),
            Candidate(item_id="v1|3|0", source="ebay", title="Synthetic Laptop 3", price=Decimal("600.00"), currency="USD"),
        ]

        enriched = backend.enrich_candidates(candidates, max_items=2, max_concurrency=2, budget=budget)
        assert len(enriched) == 3
        assert enriched[0].details_loaded is True
        assert enriched[0].brand == "Lenovo"
        assert enriched[0].aspects["RAM Size"] == ("16 GB",)
        assert enriched[0].aspects["Features"] == ("Backlit Keyboard", "SSD")
        assert enriched[0].categories == ("Computers", "Laptops")
        # 404 parcial preserva candidato original sem derrubar os demais
        assert enriched[1].details_loaded is False
        assert enriched[1].item_id == "v1|2|0"
        # Item 3 além de max_items=2 não foi consultado
        assert enriched[2].details_loaded is False
        assert budget.get_item_calls_used == 2
        assert all("%7C" in p for p in requested_paths)

        # Chamada direta get_item_details em 404 retorna None; erro 400 levanta CatalogError
        assert backend.get_item_details("v1|2|0") is None
        with pytest.raises(CatalogError):
            backend.get_item_details("v1|3|0")
    finally:
        backend.close()
        oauth.close()



def test_ebay_backend_has_no_cache_no_disk_persistence_and_clean_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    call_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        price_val = "500.00" if call_count == 1 else "450.00"
        return httpx.Response(
            200,
            json={
                "total": 1,
                "itemSummaries": [
                    {
                        "itemId": "v1|900|0",
                        "title": "SENTINEL_EBAY_TITLE_XYZ",
                        "price": {"value": price_val, "currency": "USD"},
                        "itemWebUrl": "https://ebay.example/SENTINEL_URL_XYZ",
                        "seller": {"username": "SENTINEL_SELLER_XYZ"},
                    }
                ],
            },
        )

    log_stream = io.StringIO()
    logger = configure_logging("DEBUG", stream=log_stream, logger_name="buscai.test.ebay.compliance")
    oauth = _make_oauth(logger=logger)
    backend = EbayLiveBackend(oauth_client=oauth, transport=httpx.MockTransport(handler), logger=logger)

    try:
        q = SearchQuery(query_text="gaming laptop")
        first = backend.search(q, request_id="req-c1")
        second = backend.search(q, request_id="req-c2")

        # Sem cache de anúncios entre requisições: duas buscas idênticas fazem 2 chamadas HTTP
        assert call_count == 2
        assert first.candidates[0].price == Decimal("500.00")
        assert second.candidates[0].price == Decimal("450.00")

        logs_raw = log_stream.getvalue()
        assert "SENTINEL_EBAY_TITLE_XYZ" not in logs_raw
        assert "SENTINEL_URL_XYZ" not in logs_raw
        assert "SENTINEL_SELLER_XYZ" not in logs_raw
        assert "SENTINEL_OAUTH_TOKEN" not in logs_raw
        assert "500.00" not in logs_raw
        assert "450.00" not in logs_raw

        # Zero gravação em disco
        assert list(tmp_path.rglob("*")) == []
    finally:
        backend.close()
        oauth.close()



def test_check_ebay_connection_script_prints_only_metadata_and_hides_listings_and_tokens(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def oauth_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "SENTINEL_SCRIPT_TOKEN", "expires_in": 3600})

    def browse_handler(request: httpx.Request) -> httpx.Response:
        if "item_summary/search" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "total": 1,
                    "itemSummaries": [
                        {
                            "itemId": "v1|777|0",
                            "title": "SENTINEL_SCRIPT_TITLE",
                            "price": {"value": "649.99", "currency": "USD"},
                            "itemWebUrl": "https://ebay.example/SENTINEL_SCRIPT_URL",
                            "seller": {"username": "SENTINEL_SCRIPT_SELLER"},
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "itemId": "v1|777|0",
                "title": "SENTINEL_SCRIPT_TITLE",
                "price": {"value": "649.99", "currency": "USD"},
                "localizedAspects": [{"name": "Brand", "value": "SENTINEL_BRAND"}],
            },
        )

    settings = load_settings(
        environ={
            "EBAY_CLIENT_ID": "fake_id",
            "EBAY_CLIENT_SECRET": "SENTINEL_SCRIPT_SECRET",
            "USD_BRL_RATE": "5.50",
            "USD_BRL_RATE_UPDATED_AT": "2026-11-19",
            "USD_BRL_RATE_SOURCE": "env_fixed_rate",
        }
    )
    oauth = EbayOAuthClient(
        client_id="fake_id",
        client_secret="SENTINEL_SCRIPT_SECRET",
        transport=httpx.MockTransport(oauth_handler),
    )
    backend = EbayLiveBackend(oauth_client=oauth, transport=httpx.MockTransport(browse_handler))
    converter = CurrencyConverter(FixedEnvExchangeRateProvider.from_settings(settings))

    try:
        code = check_ebay_main(
            ["--query", "laptop", "--limit", "3", "--with-details"],
            settings=settings,
            oauth_client=oauth,
            backend=backend,
            converter=converter,
        )
        assert code == 0
        out = capsys.readouterr().out
        assert "OAuth eBay: OK" in out
        assert "Busca eBay: OK" in out
        assert "items_returned=1" in out
        assert "Detalhes getItem: OK | details_loaded=True | aspect_count=1" in out
        assert "Câmbio USD/BRL: rate=5.50" in out
        assert "is_estimate=True" in out

        for forbidden in (
            "SENTINEL_SCRIPT_TOKEN",
            "SENTINEL_SCRIPT_SECRET",
            "SENTINEL_SCRIPT_TITLE",
            "SENTINEL_SCRIPT_URL",
            "SENTINEL_SCRIPT_SELLER",
            "SENTINEL_BRAND",
            "649.99",
            "v1|777|0",
        ):
            assert forbidden not in out
    finally:
        backend.close()
        oauth.close()


def test_build_ebay_search_params_supports_official_condition_ids() -> None:
    query = SearchQuery(
        query_text="laptop",
        max_price_usd=Decimal("909.09"),
        condition_ids=("2000", "2010", "2020", "2030"),
    )
    params = build_ebay_search_params(query)
    assert params["filter"] == (
        "price:[..909.09],priceCurrency:USD,buyingOptions:{FIXED_PRICE},conditionIds:{2000|2010|2020|2030}"
    )

