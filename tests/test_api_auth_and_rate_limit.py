"""Testes offline de autenticação X-API-Key e rate limiting em memória."""

from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from buscai.api import InMemoryRateLimiter, create_app
from buscai.config import load_settings
from buscai.observability import configure_logging
from buscai.orchestration import OrchestrationDependencies, SearchOrchestrator
from buscai.pricing import CurrencyConverter, FixedEnvExchangeRateProvider
from buscai.ranking.pipeline import RankingPipeline
from buscai.search.models import SearchQuery, SearchResponse
from buscai.search.query_builder import QueryBuilder
from buscai.search.quota import QuotaGuard, RequestCallBudget
from buscai.semantic.embeddings import DeterministicStubEmbeddingProvider, EmbeddingService
from buscai.semantic.pipeline import SemanticSearchProcessor
from buscai.validation.pipeline import ValidationPipeline
from userintent.schema import UserIntent


class _FakeExtractor:
    def extract(self, user_text: str) -> UserIntent:
        return UserIntent(category="notebook", query=user_text)


class _FakeBackend:
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


def _make_orch() -> SearchOrchestrator:
    converter = CurrencyConverter(FixedEnvExchangeRateProvider(rate=Decimal("5.50")))
    emb = EmbeddingService(DeterministicStubEmbeddingProvider())
    emb.embed_queries(["warmup"])
    deps = OrchestrationDependencies(
        extractor=_FakeExtractor(),
        query_builder=QueryBuilder(currency_converter=converter),
        search_backend=_FakeBackend(),
        semantic_processor=SemanticSearchProcessor(emb),
        validation_pipeline=ValidationPipeline(currency_converter=converter),
        ranking_pipeline=RankingPipeline(converter=converter),
        quota_guard=QuotaGuard(),
    )
    return SearchOrchestrator(deps)


def test_auth_accepts_valid_key_and_rejects_missing_or_wrong_keys() -> None:
    valid_key = "prod_client_secret_token_9988"
    wrong_key = "wrong_client_token_1122"
    stream = io.StringIO()
    logger = configure_logging("INFO", stream=stream, logger_name="buscai.test.auth")
    settings = load_settings(environ={"BUSCAAI_API_KEYS": valid_key})
    app = create_app(settings=settings, orchestrator=_make_orch(), logger=logger)

    with TestClient(app) as client:
        missing = client.post("/v1/search", json={"query": "notebook"})
        assert missing.status_code == 401
        assert missing.json()["error"]["code"] == "missing_api_key"

        wrong = client.post(
            "/v1/search",
            headers={"X-API-Key": wrong_key},
            json={"query": "notebook"},
        )
        assert wrong.status_code == 401
        assert wrong.json()["error"]["code"] == "invalid_api_key"

        ok = client.post(
            "/v1/search",
            headers={"X-API-Key": valid_key},
            json={"query": "notebook"},
        )
        assert ok.status_code == 200

    logged = stream.getvalue()
    assert valid_key not in logged
    assert wrong_key not in logged
    assert valid_key not in ok.text
    assert wrong_key not in wrong.text


@pytest.mark.parametrize(
    "placeholder",
    [
        "chave_cliente_1",
        "coloque_sua_chave_aqui",
        "YOUR_API_KEY",
        "placeholder",
        "insert_token_here",
        "<api-key>",
    ],
)
def test_auth_rejects_placeholders_and_fails_closed_when_only_placeholders_configured(placeholder: str) -> None:
    settings = load_settings(environ={"BUSCAAI_API_KEYS": placeholder})
    app = create_app(settings=settings, orchestrator=_make_orch())

    with TestClient(app) as client:
        resp = client.post(
            "/v1/search",
            headers={"X-API-Key": placeholder},
            json={"query": "notebook"},
        )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "auth_not_configured"


def test_auth_rejects_placeholder_even_when_real_key_also_configured() -> None:
    settings = load_settings(environ={"BUSCAAI_API_KEYS": "chave_cliente_1,real_secret_key_abc"})
    app = create_app(settings=settings, orchestrator=_make_orch())

    with TestClient(app) as client:
        bad = client.post(
            "/v1/search",
            headers={"X-API-Key": "chave_cliente_1"},
            json={"query": "notebook"},
        )
        good = client.post(
            "/v1/search",
            headers={"X-API-Key": "real_secret_key_abc"},
            json={"query": "notebook"},
        )

    assert bad.status_code == 401
    assert good.status_code == 200


def test_auth_fails_closed_when_no_keys_configured() -> None:
    settings = load_settings(environ={})
    app = create_app(settings=settings, orchestrator=_make_orch())

    with TestClient(app) as client:
        resp = client.post(
            "/v1/search",
            headers={"X-API-Key": "qualquer_chave"},
            json={"query": "notebook"},
        )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "auth_not_configured"


def test_rate_limiting_http_429_retry_after_and_no_plaintext_keys_in_memory() -> None:
    key_one = "secret_api_key_one_abc"
    key_two = "secret_api_key_two_xyz"
    now = [100.0]
    limiter = InMemoryRateLimiter(max_requests=2, window_seconds=30.0, clock=lambda: now[0])
    settings = load_settings(environ={"BUSCAAI_API_KEYS": f"{key_one},{key_two}"})
    app = create_app(settings=settings, orchestrator=_make_orch(), rate_limiter=limiter)

    with TestClient(app) as client:
        r1 = client.post("/v1/search", headers={"X-API-Key": key_one}, json={"query": "notebook"})
        r2 = client.post("/v1/search", headers={"X-API-Key": key_one}, json={"query": "notebook"})
        r3 = client.post("/v1/search", headers={"X-API-Key": key_one}, json={"query": "notebook"})
        other_client = client.post("/v1/search", headers={"X-API-Key": key_two}, json={"query": "notebook"})

        assert r1.status_code == 200
        assert r1.headers["X-RateLimit-Limit"] == "2"
        assert r1.headers["X-RateLimit-Remaining"] == "1"
        assert r2.status_code == 200
        assert r2.headers["X-RateLimit-Remaining"] == "0"

        assert r3.status_code == 429
        assert r3.json()["error"]["code"] == "rate_limit_exceeded"
        assert int(r3.headers["Retry-After"]) >= 1
        assert other_client.status_code == 200

        # Garante que nenhuma chave em texto puro foi armazenada no rate limiter.
        stored_keys = list(limiter._buckets.keys())
        assert key_one not in stored_keys
        assert key_two not in stored_keys

        # Avança o relógio além da janela e confirma recuperação do cliente.
        now[0] += 31.0
        recovered = client.post("/v1/search", headers={"X-API-Key": key_one}, json={"query": "notebook"})
        assert recovered.status_code == 200


def test_rate_limiter_cleanup_expired_and_memory_cap() -> None:
    now = [0.0]
    limiter = InMemoryRateLimiter(
        max_requests=5,
        window_seconds=10.0,
        max_clients=3,
        cleanup_interval=50,
        clock=lambda: now[0],
    )

    for idx in range(5):
        assert limiter.check_and_consume(f"client-{idx}").allowed is True
    assert limiter.tracked_clients_count() <= 3

    now[0] = 25.0
    removed = limiter.cleanup_expired()
    assert removed >= 1
    assert limiter.tracked_clients_count() == 0


def test_rate_limiter_thread_safety_under_concurrency() -> None:
    limiter = InMemoryRateLimiter(max_requests=10, window_seconds=60.0)

    def _hit(_i: int) -> bool:
        return limiter.check_and_consume("concurrent-client-hash").allowed

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(_hit, range(30)))

    assert sum(1 for ok in outcomes if ok) == 10
    assert sum(1 for ok in outcomes if not ok) == 20
