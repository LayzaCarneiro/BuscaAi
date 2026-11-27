"""Testes offline de health check (liveness/readiness), lifespan e ausência de downloads/segredos."""

from __future__ import annotations

import io
import json
import sys
from decimal import Decimal

from fastapi.testclient import TestClient

from buscai.api import create_app
from buscai.config import load_settings
from buscai.observability import configure_logging
from buscai.orchestration import OrchestrationDependencies, SearchOrchestrator
from buscai.pricing import CurrencyConverter, FixedEnvExchangeRateProvider
from buscai.ranking.pipeline import RankingPipeline
from buscai.search.models import SearchQuery, SearchResponse
from buscai.search.query_builder import QueryBuilder
from buscai.search.quota import QuotaGuard, RequestCallBudget
from buscai.semantic.embeddings import (
    DeterministicStubEmbeddingProvider,
    EmbeddingService,
    SentenceTransformerEmbeddingProvider,
)
from buscai.semantic.pipeline import SemanticSearchProcessor
from buscai.validation.pipeline import ValidationPipeline
from userintent.schema import UserIntent


class _StubExtractor:
    def extract(self, user_text: str) -> UserIntent:
        return UserIntent(category="notebook", query=user_text)


class _EmptyBackend:
    def search(
        self,
        query: SearchQuery,
        *,
        budget: RequestCallBudget | None = None,
        request_id: str | None = None,
    ) -> SearchResponse:
        return SearchResponse(candidates=(), total_estimated=0, limit=query.limit, offset=0, calls_used=1)

    def get_item_details(self, item_id: str, **kwargs) -> None:
        return None


def _build_orchestrator(*, loaded_embeddings: bool = True) -> SearchOrchestrator:
    converter = CurrencyConverter(FixedEnvExchangeRateProvider(rate=Decimal("5.50")))
    provider = DeterministicStubEmbeddingProvider()
    emb_service = EmbeddingService(provider)
    if loaded_embeddings:
        emb_service.embed_queries(["warmup"])
    deps = OrchestrationDependencies(
        extractor=_StubExtractor(),
        query_builder=QueryBuilder(currency_converter=converter),
        search_backend=_EmptyBackend(),
        semantic_processor=SemanticSearchProcessor(emb_service),
        validation_pipeline=ValidationPipeline(currency_converter=converter),
        ranking_pipeline=RankingPipeline(converter=converter),
        quota_guard=QuotaGuard(),
    )
    return SearchOrchestrator(deps)


def _valid_settings(*, extra: dict[str, str] | None = None):
    env = {
        "BUSCAAI_API_KEYS": "buscai_valid_key_alpha_987",
        "GEMINI_API_KEY": "fake_gemini_secret_token_123",
        "EBAY_CLIENT_ID": "fake_ebay_client_id",
        "EBAY_CLIENT_SECRET": "fake_ebay_client_secret_456",
        "USD_BRL_RATE": "5.50",
        "USD_BRL_RATE_UPDATED_AT": "2026-11-25",
    }
    if extra:
        env.update(extra)
    return load_settings(environ=env)


def test_health_ready_when_all_required_components_are_available() -> None:
    orch = _build_orchestrator(loaded_embeddings=True)
    settings = _valid_settings()
    app = create_app(settings=settings, orchestrator=orch, preload_embeddings=False)

    with TestClient(app) as client:
        resp = client.get("/v1/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["liveness"] == "alive"
    assert body["readiness"] == "ready"
    components = {comp["name"]: comp for comp in body["components"]}
    assert components["api_auth"]["status"] == "ready"
    assert components["api_auth"]["required"] is True
    assert components["search_orchestrator"]["status"] == "ready"
    assert components["embeddings"]["status"] == "ready"
    assert components["tracing_policy"]["status"] == "ready"
    assert components["exchange_rate"]["required"] is False
    assert components["exchange_rate"]["status"] == "ready"


def test_health_returns_not_ready_when_embeddings_not_loaded_and_never_downloads() -> None:
    def _failing_loader(_name: str, _device: str):
        raise AssertionError("Health check ou startup sem preload não deve baixar pesos!")

    unloaded_provider = SentenceTransformerEmbeddingProvider(model_loader=_failing_loader)
    emb_service = EmbeddingService(unloaded_provider)
    orch = _build_orchestrator(loaded_embeddings=False)
    settings = _valid_settings()

    network_calls: list[str] = []
    active = True

    def _audit(event: str, args: tuple) -> None:
        if not active:
            return
        if event == "socket.connect":
            addr = args[1] if len(args) >= 2 else None
            host = addr[0] if isinstance(addr, tuple) and addr else None
            if host in {"127.0.0.1", "::1"}:
                return
            network_calls.append(f"{event}:{host}")
        elif event == "urllib.Request":
            network_calls.append(event)

    sys.addaudithook(_audit)
    try:
        app = create_app(
            settings=settings,
            orchestrator=orch,
            embedding_service=emb_service,
            preload_embeddings=False,
        )
        with TestClient(app) as client:
            resp = client.get("/v1/health")
    finally:
        active = False

    assert network_calls == []
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["liveness"] == "alive"
    assert body["readiness"] == "not_ready"
    components = {comp["name"]: comp for comp in body["components"]}
    assert components["embeddings"]["status"] == "not_ready"


def test_health_not_ready_when_credentials_missing_or_auth_missing() -> None:
    empty_settings = load_settings(environ={})
    app = create_app(settings=empty_settings, preload_embeddings=False)

    with TestClient(app) as client:
        resp = client.get("/v1/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["liveness"] == "alive"
    assert body["readiness"] == "not_ready"
    components = {comp["name"]: comp for comp in body["components"]}
    assert components["api_auth"]["status"] == "not_ready"
    assert components["search_orchestrator"]["status"] == "not_ready"
    assert components["embeddings"]["status"] == "not_ready"


def test_health_never_exposes_secrets_env_or_stack_traces() -> None:
    settings = _valid_settings()
    orch = _build_orchestrator(loaded_embeddings=True)
    stream = io.StringIO()
    logger = configure_logging("INFO", stream=stream, logger_name="buscai.test.health")
    app = create_app(settings=settings, orchestrator=orch, preload_embeddings=False, logger=logger)

    with TestClient(app) as client:
        resp = client.get("/v1/health")

    raw_text = resp.text
    for forbidden in (
        "buscai_valid_key_alpha_987",
        "fake_gemini_secret_token_123",
        "fake_ebay_client_secret_456",
        "fake_ebay_client_id",
        "Traceback",
        "GEMINI_API_KEY",
        "EBAY_CLIENT_SECRET",
    ):
        assert forbidden not in raw_text
        assert forbidden not in stream.getvalue()


def test_health_not_ready_when_tracing_env_active(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    orch = _build_orchestrator(loaded_embeddings=True)
    app = create_app(settings=_valid_settings(), orchestrator=orch, preload_embeddings=False)

    with TestClient(app) as client:
        resp = client.get("/v1/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["liveness"] == "alive"
    assert body["readiness"] == "not_ready"
    components = {comp["name"]: comp for comp in body["components"]}
    assert components["tracing_policy"]["status"] == "not_ready"


def test_lifespan_shares_single_orchestrator_across_requests_and_preloads_stub_when_requested() -> None:
    orch = _build_orchestrator(loaded_embeddings=False)
    app = create_app(settings=_valid_settings(), orchestrator=orch, preload_embeddings=True)

    with TestClient(app) as client:
        health = client.get("/v1/health")
        assert health.status_code == 200
        container_1 = app.state.container
        r1 = client.post(
            "/v1/search",
            headers={"X-API-Key": "buscai_valid_key_alpha_987"},
            json={"query": "notebook 16gb"},
        )
        r2 = client.post(
            "/v1/search",
            headers={"X-API-Key": "buscai_valid_key_alpha_987"},
            json={"query": "monitor 4k"},
        )
        assert app.state.container is container_1
        assert app.state.container.orchestrator is orch
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert json.loads(r1.text)["request_id"] != json.loads(r2.text)["request_id"]
