"""Suíte dedicada de auditoria de conformidade da API HTTP e da integração eBay (T9.1)."""

from __future__ import annotations

import ast
import gc
import io
import json
import sys
import tempfile
import weakref
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from buscai.api import create_app
from buscai.config import load_settings
from buscai.observability import ALLOWED_LOG_FIELDS, configure_logging
from buscai.orchestration import (
    TRACING_ENV_VARS,
    OrchestrationDependencies,
    SearchOrchestrator,
)
from buscai.pricing import CurrencyConverter, FixedEnvExchangeRateProvider
from buscai.ranking.pipeline import RankingPipeline
from buscai.search import EbayLiveBackend, EbayOAuthClient, QuotaGuard
from buscai.search.query_builder import QueryBuilder
from buscai.semantic.embeddings import DeterministicStubEmbeddingProvider, EmbeddingService
from buscai.semantic.pipeline import SemanticSearchProcessor
from buscai.validation.pipeline import ValidationPipeline
from userintent import IntentExtractor, UserIntent

ROOT = Path(__file__).resolve().parents[1]
_API_KEY = "buscai_compliance_secret_9101"
_OAUTH_TOKEN = "SENTINEL_OAUTH_ACCESS_TOKEN_9101"
_SENTINEL_TITLE = "SENTINEL_EBAY_TITLE_9101"
_SENTINEL_URL = "https://ebay.sentinel.invalid/itm/9101"
_SENTINEL_SELLER = "sentinel_ebay_seller_9101"
_SENTINEL_DESC = "SENTINEL_EBAY_DESC_9101"
_SENTINEL_GEMINI_SECRET = "AIzaSentinelComplianceSecret9101"


class _StubGenAIClient:
    def __init__(self, intent: UserIntent) -> None:
        self._intent = intent
        self.models = SimpleNamespace(
            generate_content=lambda **_kwargs: SimpleNamespace(parsed=self._intent, text=None)
        )


def _make_compliance_stack(
    *,
    browse_handler,
    oauth_handler=None,
    intent: UserIntent | None = None,
    max_search_calls: int = 2,
    max_get_item_calls: int = 2,
    daily_limit: int = 5000,
    max_retries: int = 2,
):
    log_stream = io.StringIO()
    logger = configure_logging("DEBUG", stream=log_stream, logger_name="buscai.test.compliance")

    default_oauth = oauth_handler or (
        lambda _req: httpx.Response(200, json={"access_token": _OAUTH_TOKEN, "expires_in": 7200})
    )
    oauth = EbayOAuthClient(
        client_id="compliance_client_id",
        client_secret="compliance_client_secret_value",
        transport=httpx.MockTransport(default_oauth),
        logger=logger,
    )
    quota = QuotaGuard(
        daily_limit=daily_limit,
        default_max_search_per_request=max_search_calls,
        default_max_get_item_per_request=max_get_item_calls,
        logger=logger,
    )
    backend = EbayLiveBackend(
        oauth_client=oauth,
        quota_guard=quota,
        default_marketplace_id="EBAY_US",
        max_retries=max_retries,
        base_backoff_seconds=0.0,
        max_backoff_seconds=4.0,
        transport=httpx.MockTransport(browse_handler),
        sleep=lambda _: None,
        logger=logger,
    )

    active_intent = intent or UserIntent(
        category="notebook",
        brands=["Lenovo"],
        required_features=["16GB RAM"],
        query="notebook lenovo 16gb",
    )
    extractor = IntentExtractor(
        model="gemini-3.5-flash-lite",
        client=_StubGenAIClient(active_intent),
        logger=logger,
    )
    converter = CurrencyConverter(
        FixedEnvExchangeRateProvider(rate=Decimal("5.50"), updated_at="2026-11-25", source="env_fixed_rate")
    )
    emb = EmbeddingService(DeterministicStubEmbeddingProvider(), logger=logger)
    emb.embed_queries(["warmup"])

    deps = OrchestrationDependencies(
        extractor=extractor,
        query_builder=QueryBuilder(currency_converter=converter),
        search_backend=backend,
        semantic_processor=SemanticSearchProcessor(emb, logger=logger),
        validation_pipeline=ValidationPipeline(
            currency_converter=converter,
            search_backend=backend,
            max_enrich_items=max_get_item_calls,
            logger=logger,
        ),
        ranking_pipeline=RankingPipeline(converter=converter),
        quota_guard=quota,
        max_search_calls=max_search_calls,
        max_get_item_calls=max_get_item_calls,
        logger=logger,
    )
    orch = SearchOrchestrator(deps)
    settings = load_settings(
        environ={
            "BUSCAAI_API_KEYS": _API_KEY,
            "GEMINI_API_KEY": _SENTINEL_GEMINI_SECRET,
            "EBAY_CLIENT_ID": "compliance_client_id",
            "EBAY_CLIENT_SECRET": "compliance_client_secret_value",
            "USD_BRL_RATE": "5.50",
            "USD_BRL_RATE_UPDATED_AT": "2026-11-25",
        }
    )
    app = create_app(settings=settings, orchestrator=orch, preload_embeddings=False, logger=logger)
    return app, log_stream, quota, backend, oauth, orch, emb


def test_compliance_no_disk_writes_no_external_network_and_no_memory_retention(capsys) -> None:
    prices = ["450.00", "499.00"]
    browse_calls: list[str] = []
    oauth_calls = 0

    def oauth_handler(_req: httpx.Request) -> httpx.Response:
        nonlocal oauth_calls
        oauth_calls += 1
        return httpx.Response(200, json={"access_token": _OAUTH_TOKEN, "expires_in": 7200})

    def browse_handler(req: httpx.Request) -> httpx.Response:
        browse_calls.append(str(req.url))
        if "/item_summary/search" in req.url.path:
            price_val = prices[min(len(browse_calls) - 1, len(prices) - 1)]
            return httpx.Response(
                200,
                json={
                    "total": 1,
                    "limit": 30,
                    "offset": 0,
                    "itemSummaries": [
                        {
                            "itemId": "v1|9101|0",
                            "title": f"Lenovo {_SENTINEL_TITLE} Laptop",
                            "price": {"value": price_val, "currency": "USD"},
                            "condition": "NEW",
                            "categories": [{"categoryName": "Laptops"}],
                            "brand": "Lenovo",
                            "shortDescription": _SENTINEL_DESC,
                            "itemWebUrl": _SENTINEL_URL,
                            "seller": {
                                "username": _SENTINEL_SELLER,
                                "feedbackScore": 400,
                                "feedbackPercentage": "99.1",
                            },
                            "shippingOptions": [
                                {"shippingCostType": "FIXED", "shippingCost": {"value": "0.00", "currency": "USD"}}
                            ],
                        }
                    ],
                },
            )
        if "/buy/browse/v1/item/" in req.url.path:
            return httpx.Response(
                200,
                json={
                    "itemId": "v1|9101|0",
                    "title": f"Lenovo {_SENTINEL_TITLE} Laptop",
                    "price": {"value": prices[-1], "currency": "USD"},
                    "condition": "NEW",
                    "brand": "Lenovo",
                    "shortDescription": _SENTINEL_DESC,
                    "itemWebUrl": _SENTINEL_URL,
                    "localizedAspects": [
                        {"name": "RAM Size", "value": "16 GB"},
                        {"name": "SSD Capacity", "value": "512 GB"},
                    ],
                },
            )
        return httpx.Response(404)

    app, log_stream, quota, backend, oauth, orch, emb = _make_compliance_stack(
        browse_handler=browse_handler,
        oauth_handler=oauth_handler,
        max_search_calls=1,
        max_get_item_calls=1,
    )

    temp_root = Path(tempfile.gettempdir()).resolve()
    temp_before = {p.resolve() for p in temp_root.iterdir()} if temp_root.exists() else set()
    repo_before = {p.resolve() for p in (ROOT / "src").rglob("*")}

    write_events: list[str] = []
    network_events: list[str] = []
    active = True

    def _audit(event: str, args: tuple) -> None:
        if not active:
            return
        if event == "socket.connect":
            addr = args[1] if len(args) >= 2 else None
            host = addr[0] if isinstance(addr, tuple) and addr else None
            if host in {"127.0.0.1", "::1"}:
                return
            network_events.append(f"{event}:{host}")
        elif event == "urllib.Request":
            network_events.append(event)
        elif event == "open" and len(args) >= 2 and any(flag in str(args[1]) for flag in ("w", "a", "x", "+")):
            write_events.append(str(args[0]))

    weak_results: list[weakref.ref[object]] = []
    sys.addaudithook(_audit)
    try:
        with TestClient(app) as client:
            r1 = client.post(
                "/v1/search",
                headers={"X-API-Key": _API_KEY, "X-Request-ID": "comp-1"},
                json={"query": "notebook lenovo 16gb ram", "budget_brl": 4000},
            )
            r2 = client.post(
                "/v1/search",
                headers={"X-API-Key": _API_KEY, "X-Request-ID": "comp-2"},
                json={"query": "notebook lenovo 16gb ram", "budget_brl": 4000},
            )
            unauth = client.post("/v1/search", json={"query": "notebook lenovo"})
            container = app.state.container
            # Nenhum payload de anúncio fica retido no container, rate limiter ou quota guard.
            container_dump = repr(container.rate_limiter._buckets) + repr(quota.snapshot())
            assert _SENTINEL_TITLE not in container_dump
            assert _SENTINEL_URL not in container_dump
            assert _SENTINEL_SELLER not in container_dump
            assert _API_KEY not in container_dump
            assert _OAUTH_TOKEN not in container_dump
            weak_results.append(weakref.ref(r1))
            weak_results.append(weakref.ref(r2))
    finally:
        active = False
        backend.close()
        oauth.close()

    del r1, r2
    gc.collect()
    assert all(ref() is None for ref in weak_results)

    temp_after = {p.resolve() for p in temp_root.iterdir()} if temp_root.exists() else set()
    repo_after = {p.resolve() for p in (ROOT / "src").rglob("*")}
    assert repo_before == repo_after
    for created_tmp in temp_after - temp_before:
        if created_tmp.is_file():
            content = created_tmp.read_text(encoding="utf-8", errors="ignore")
            assert _SENTINEL_TITLE not in content
            assert _SENTINEL_URL not in content

    assert network_events == []
    assert write_events == []

    # OAuth reutilizado em memória (1 emissão) e zero cache de anúncios (2 chamadas search distintas).
    assert oauth_calls == 1
    search_only_calls = [u for u in browse_calls if "/item_summary/search" in u]
    get_item_calls = [u for u in browse_calls if "/buy/browse/v1/item/" in u]
    assert len(search_only_calls) == 2
    assert len(get_item_calls) == 2  # 1 por requisição respeitando max_get_item_calls=1

    # Confirma LangGraph sem checkpointer ou store.
    assert orch.compiled_graph.checkpointer is None
    assert getattr(orch.compiled_graph, "store", None) is None
    assert emb.is_ready() is True

    # Logs estruturados + stdout/stderr livres de sentinelas e segredos.
    captured = capsys.readouterr()
    raw_logs = log_stream.getvalue()
    all_output = raw_logs + captured.out + captured.err
    for secret_or_listing in (
        _API_KEY,
        _OAUTH_TOKEN,
        _SENTINEL_GEMINI_SECRET,
        _SENTINEL_TITLE,
        _SENTINEL_URL,
        _SENTINEL_SELLER,
        _SENTINEL_DESC,
        "compliance_client_secret_value",
    ):
        assert secret_or_listing not in all_output
        assert secret_or_listing not in unauth.text

    for line in raw_logs.strip().splitlines():
        record = json.loads(line)
        extra_keys = set(record.keys()) - {"timestamp", "level", "logger", "event"}
        assert extra_keys.issubset(ALLOWED_LOG_FIELDS)


def test_compliance_zero_cache_reflects_updated_ebay_price_immediately() -> None:
    call_num = 0

    def browse_handler(req: httpx.Request) -> httpx.Response:
        nonlocal call_num
        if "/item_summary/search" in req.url.path:
            call_num += 1
            price = "450.00" if call_num == 1 else "499.00"
            return httpx.Response(
                200,
                json={
                    "total": 1,
                    "limit": 30,
                    "offset": 0,
                    "itemSummaries": [
                        {
                            "itemId": "v1|price-check|0",
                            "title": "Lenovo ThinkPad 16GB RAM",
                            "price": {"value": price, "currency": "USD"},
                            "condition": "NEW",
                            "brand": "Lenovo",
                            "shippingOptions": [
                                {"shippingCostType": "FIXED", "shippingCost": {"value": "0.00", "currency": "USD"}}
                            ],
                        }
                    ],
                },
            )
        return httpx.Response(404)

    app, _logs, _quota, backend, oauth, _orch, _emb = _make_compliance_stack(
        browse_handler=browse_handler,
        max_search_calls=1,
        max_get_item_calls=0,
    )
    try:
        with TestClient(app) as client:
            first = client.post(
                "/v1/search",
                headers={"X-API-Key": _API_KEY},
                json={"query": "notebook lenovo 16gb ram"},
            )
            second = client.post(
                "/v1/search",
                headers={"X-API-Key": _API_KEY},
                json={"query": "notebook lenovo 16gb ram"},
            )
    finally:
        backend.close()
        oauth.close()

    assert first.status_code == 200
    assert second.status_code == 200
    assert call_num == 2
    assert first.json()["products"][0]["offers"][0]["price"] == "450.00"
    assert second.json()["products"][0]["offers"][0]["price"] == "499.00"


def test_compliance_ebay_429_retries_5xx_and_quota_guard() -> None:
    attempts = 0

    def browse_handler(req: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if "/item_summary/search" in req.url.path:
            attempts += 1
            if attempts == 1:
                return httpx.Response(503, json={"errors": [{"message": _SENTINEL_TITLE}]})
            if attempts == 2:
                return httpx.Response(429, headers={"Retry-After": "1"})
            return httpx.Response(
                200,
                json={
                    "total": 1,
                    "limit": 30,
                    "offset": 0,
                    "itemSummaries": [
                        {
                            "itemId": "v1|retry-ok|0",
                            "title": "Lenovo ThinkPad 16GB RAM",
                            "price": {"value": "500.00", "currency": "USD"},
                            "condition": "NEW",
                            "brand": "Lenovo",
                            "shippingOptions": [
                                {"shippingCostType": "FIXED", "shippingCost": {"value": "0.00", "currency": "USD"}}
                            ],
                        }
                    ],
                },
            )
        return httpx.Response(404)

    app, log_stream, quota, backend, oauth, _orch, _emb = _make_compliance_stack(
        browse_handler=browse_handler,
        max_search_calls=1,
        max_get_item_calls=0,
        daily_limit=3,
        max_retries=2,
    )
    try:
        with TestClient(app) as client:
            ok = client.post(
                "/v1/search",
                headers={"X-API-Key": _API_KEY},
                json={"query": "notebook lenovo 16gb ram"},
            )
            exhausted = client.post(
                "/v1/search",
                headers={"X-API-Key": _API_KEY},
                json={"query": "notebook lenovo 16gb ram"},
            )
    finally:
        backend.close()
        oauth.close()

    assert ok.status_code == 200
    assert attempts == 3
    assert quota.snapshot().used_in_window == 3
    assert exhausted.status_code == 503
    assert exhausted.json()["error"]["code"] == "upstream_rate_limit"
    assert _SENTINEL_TITLE not in log_stream.getvalue()
    assert _SENTINEL_TITLE not in exhausted.text


def test_compliance_blocks_tracing_env_and_forbids_persistence_imports_in_api(monkeypatch) -> None:
    app, _logs, _quota, backend, oauth, _orch, _emb = _make_compliance_stack(
        browse_handler=lambda _req: httpx.Response(200, json={"itemSummaries": []}),
    )
    try:
        for var in TRACING_ENV_VARS:
            monkeypatch.setenv(var, "1")
            with TestClient(app) as client:
                health = client.get("/v1/health")
                search = client.post(
                    "/v1/search",
                    headers={"X-API-Key": _API_KEY},
                    json={"query": "notebook lenovo"},
                )
            assert health.status_code == 503
            assert health.json()["readiness"] == "not_ready"
            assert search.status_code == 503
            assert search.json()["error"]["code"] == "policy_violation"
            monkeypatch.delenv(var, raising=False)
    finally:
        backend.close()
        oauth.close()

    api_dir = ROOT / "src" / "buscai" / "api"
    forbidden = {
        "sqlite3",
        "sqlalchemy",
        "redis",
        "diskcache",
        "shelve",
        "pickle",
        "hishel",
        "requests_cache",
        "langsmith",
        "langgraph.checkpoint",
    }
    for py_file in api_dir.glob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not any(alias.name == f or alias.name.startswith(f"{f}.") for f in forbidden)
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not any(node.module == f or node.module.startswith(f"{f}.") for f in forbidden)
