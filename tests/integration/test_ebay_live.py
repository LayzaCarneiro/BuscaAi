r"""Teste de integração opt-in contra a API real de produção da eBay (EBAY_US).

Desativado por padrão para evitar consumo involuntário da cota de 5.000 chamadas/dia.
Para executar manualmente:
    $env:RUN_EBAY_INTEGRATION_TESTS="1"
    .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -v -m ebay_live
"""

from __future__ import annotations

import io
import os
from decimal import Decimal
from pathlib import Path

import pytest

from buscai.config import load_settings
from buscai.observability import configure_logging
from buscai.search import EbayLiveBackend, EbayOAuthClient, QuotaGuard, SearchQuery

pytestmark = [
    pytest.mark.ebay_live,
    pytest.mark.skipif(
        os.getenv("RUN_EBAY_INTEGRATION_TESTS") != "1",
        reason=(
            "Teste real da eBay desativado por padrão; "
            "defina RUN_EBAY_INTEGRATION_TESTS=1 para executar (consome cota de produção)."
        ),
    ),
]


def test_ebay_live_oauth_search_and_selective_get_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = load_settings(load_dotenv_file=True)
    assert settings.is_ebay_configured, "EBAY_CLIENT_ID e EBAY_CLIENT_SECRET devem estar configurados no .env."

    log_stream = io.StringIO()
    logger = configure_logging("INFO", stream=log_stream, logger_name="buscai.test.ebay_live")
    guard = QuotaGuard(
        daily_limit=settings.ebay_daily_quota_limit,
        default_max_search_per_request=1,
        default_max_get_item_per_request=1,
        logger=logger,
    )
    budget = guard.new_request_budget()

    oauth = EbayOAuthClient.from_settings(settings, logger=logger)
    backend = EbayLiveBackend(
        oauth_client=oauth,
        quota_guard=guard,
        default_marketplace_id=settings.ebay_marketplace_id,
        logger=logger,
    )

    try:
        token_first = oauth.get_access_token(request_id="live-test-1")
        token_second = oauth.get_access_token(request_id="live-test-1")
        assert isinstance(token_first, str) and len(token_first) > 10
        assert token_first == token_second

        response = backend.search(
            SearchQuery(
                query_text="laptop",
                marketplace_id=settings.ebay_marketplace_id,
                max_price_usd=Decimal("1000.00"),
                limit=3,
            ),
            budget=budget,
            request_id="live-test-1",
        )
        assert response.calls_used == 1
        assert len(response.candidates) <= 3

        checked_titles: list[str] = []
        checked_urls: list[str] = []
        for item in response.candidates:
            assert isinstance(item.price, Decimal)
            assert item.price >= 0
            assert item.price <= Decimal("1000.00")
            assert item.currency == "USD"
            checked_titles.append(item.title)
            if item.item_web_url:
                checked_urls.append(item.item_web_url)

        if response.candidates:
            detail = backend.get_item_details(
                response.candidates[0].item_id,
                budget=budget,
                request_id="live-test-1",
            )
            assert detail is not None
            assert detail.details_loaded is True
            assert isinstance(detail.price, Decimal)

        assert budget.total_calls_used <= 2

        logs_raw = log_stream.getvalue()
        assert token_first not in logs_raw
        for title in checked_titles:
            assert title not in logs_raw
        for url in checked_urls:
            assert url not in logs_raw
        assert list(tmp_path.rglob("*")) == []
    finally:
        backend.close()
        oauth.close()
