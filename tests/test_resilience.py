"""Suíte integrada de resiliência HTTP, falhas eBay/Gemini/FastAPI e concorrência (T9.2)."""

from __future__ import annotations

import asyncio
import io
import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from google.genai import errors as genai_errors

from buscai.api import InMemoryRateLimiter, create_app
from buscai.config import load_settings
from buscai.observability import configure_logging
from buscai.orchestration import OrchestrationDependencies, SearchOrchestrator
from buscai.pricing import CurrencyConverter, FixedEnvExchangeRateProvider
from buscai.ranking.pipeline import RankingPipeline
from buscai.search import EbayLiveBackend, EbayOAuthClient, QuotaGuard
from buscai.search.query_builder import QueryBuilder
from buscai.semantic.embeddings import DeterministicStubEmbeddingProvider, EmbeddingService
from buscai.semantic.pipeline import SemanticSearchProcessor
from buscai.validation.pipeline import ValidationPipeline
from userintent import IntentExtractor, PriceConstraint, UserIntent

_API_KEY_A = "buscai_resilience_key_alpha"
_API_KEY_B = "buscai_resilience_key_beta"


class _GenAIStubClient:
    def __init__(self, handler) -> None:
        self.calls = 0

        def _generate(**kwargs):
            self.calls += 1
            return handler(**kwargs)

        self.models = SimpleNamespace(generate_content=_generate)


def _build_resilient_app(
    *,
    llm_handler=None,
    browse_handler=None,
    oauth_handler=None,
    daily_limit: int = 5000,
    max_rpm: int = 100,
    rate_limiter: InMemoryRateLimiter | None = None,
    orchestrator_override: SearchOrchestrator | None = None,
):
    log_stream = io.StringIO()
    logger = configure_logging("INFO", stream=log_stream, logger_name="buscai.test.resilience")

    default_llm = llm_handler or (
        lambda **_k: SimpleNamespace(
            parsed=UserIntent(
                category="notebook",
                brands=["Lenovo"],
                required_features=["16GB RAM"],
                query="notebook lenovo 16gb",
            ),
            text=None,
        )
    )
    llm_stub = _GenAIStubClient(default_llm)
    extractor = IntentExtractor(
        model="gemini-3.5-flash-lite",
        client=llm_stub,
        max_retries=2,
        base_backoff_seconds=0.0,
        max_backoff_seconds=1.0,
        sleep=lambda _: None,
        logger=logger,
    )

    default_oauth = oauth_handler or (
        lambda _req: httpx.Response(200, json={"access_token": "tok_resilience", "expires_in": 7200})
    )
    oauth = EbayOAuthClient(
        client_id="res_cid",
        client_secret="res_secret_value",
        transport=httpx.MockTransport(default_oauth),
        logger=logger,
    )
    quota = QuotaGuard(
        daily_limit=daily_limit,
        default_max_search_per_request=2,
        default_max_get_item_per_request=4,
        logger=logger,
    )
    default_browse = browse_handler or (
        lambda _req: httpx.Response(200, json={"total": 0, "limit": 30, "offset": 0, "itemSummaries": []})
    )
    backend = EbayLiveBackend(
        oauth_client=oauth,
        quota_guard=quota,
        default_marketplace_id="EBAY_US",
        max_retries=1,
        base_backoff_seconds=0.0,
        max_backoff_seconds=4.0,
        transport=httpx.MockTransport(default_browse),
        sleep=lambda _: None,
        logger=logger,
    )

    converter = CurrencyConverter(
        FixedEnvExchangeRateProvider(rate=Decimal("5.50"), updated_at="2026-11-25", source="env_fixed_rate")
    )
    emb = EmbeddingService(DeterministicStubEmbeddingProvider(), logger=logger)
    emb.embed_queries(["warmup"])

    if orchestrator_override is None:
        deps = OrchestrationDependencies(
            extractor=extractor,
            query_builder=QueryBuilder(currency_converter=converter),
            search_backend=backend,
            semantic_processor=SemanticSearchProcessor(emb, logger=logger),
            validation_pipeline=ValidationPipeline(
                currency_converter=converter,
                search_backend=backend,
                max_enrich_items=4,
                logger=logger,
            ),
            ranking_pipeline=RankingPipeline(converter=converter),
            quota_guard=quota,
            logger=logger,
        )
        orch = SearchOrchestrator(deps)
    else:
        orch = orchestrator_override

    settings = load_settings(
        environ={
            "BUSCAAI_API_KEYS": f"{_API_KEY_A},{_API_KEY_B}",
            "BUSCAAI_RATE_LIMIT_PER_MINUTE": str(max_rpm),
            "USD_BRL_RATE": "5.50",
            "USD_BRL_RATE_UPDATED_AT": "2026-11-25",
        }
    )
    app = create_app(
        settings=settings,
        orchestrator=orch,
        embedding_service=emb,
        rate_limiter=rate_limiter,
        preload_embeddings=False,
        logger=logger,
    )
    return app, log_stream, llm_stub, backend, oauth


@pytest.mark.parametrize(
    ("status_code", "headers", "expected_http", "expected_code"),
    [
        (400, {}, 502, "upstream_bad_response"),
        (401, {}, 502, "upstream_auth_error"),
        (403, {}, 502, "upstream_auth_error"),
        (429, {"Retry-After": "9"}, 503, "upstream_rate_limit"),
        (500, {}, 503, "upstream_unavailable"),
        (502, {}, 503, "upstream_unavailable"),
        (503, {}, 503, "upstream_unavailable"),
        (504, {}, 503, "upstream_unavailable"),
    ],
)
def test_resilience_ebay_http_error_statuses(
    status_code: int,
    headers: dict[str, str],
    expected_http: int,
    expected_code: str,
) -> None:
    app, logs, _llm, backend, oauth = _build_resilient_app(
        browse_handler=lambda _req: httpx.Response(status_code, headers=headers, json={"error": "SENTINEL_UPSTREAM"})
    )
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/v1/search",
                headers={"X-API-Key": _API_KEY_A},
                json={"query": "notebook lenovo 16gb"},
            )
    finally:
        backend.close()
        oauth.close()

    assert resp.status_code == expected_http
    body = resp.json()
    assert body["error"]["code"] == expected_code
    assert body["error"]["request_id"]
    assert "SENTINEL_UPSTREAM" not in resp.text
    assert "Traceback" not in resp.text
    if expected_code == "upstream_rate_limit":
        assert resp.headers["Retry-After"] == "9"
    assert "SENTINEL_UPSTREAM" not in logs.getvalue()


def test_resilience_ebay_timeout_invalid_json_incomplete_and_missing_optional_fields() -> None:
    # 1) Timeout
    def _timeout(_req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    app, _logs, _llm, backend, oauth = _build_resilient_app(browse_handler=_timeout)
    try:
        with TestClient(app) as client:
            timeout_resp = client.post("/v1/search", headers={"X-API-Key": _API_KEY_A}, json={"query": "notebook"})
    finally:
        backend.close()
        oauth.close()
    assert timeout_resp.status_code == 503
    assert timeout_resp.json()["error"]["code"] == "upstream_timeout"

    # 2) JSON inválido
    app, _logs, _llm, backend, oauth = _build_resilient_app(
        browse_handler=lambda _req: httpx.Response(200, content=b"{invalid json")
    )
    try:
        with TestClient(app) as client:
            bad_json_resp = client.post("/v1/search", headers={"X-API-Key": _API_KEY_A}, json={"query": "notebook"})
    finally:
        backend.close()
        oauth.close()
    assert bad_json_resp.status_code == 502
    assert bad_json_resp.json()["error"]["code"] == "upstream_bad_response"

    # 3) Resposta vazia / sem itemSummaries -> 200 no_results
    app, _logs, _llm, backend, oauth = _build_resilient_app(
        browse_handler=lambda _req: httpx.Response(200, json={})
    )
    try:
        with TestClient(app) as client:
            empty_resp = client.post("/v1/search", headers={"X-API-Key": _API_KEY_A}, json={"query": "notebook"})
    finally:
        backend.close()
        oauth.close()
    assert empty_resp.status_code == 200
    assert empty_resp.json()["status"] == "no_results"

    # 4) Item corrompido ignorado + item mínimo sem campos opcionais + falha em getItem
    def _partial_and_getitem_fail(req: httpx.Request) -> httpx.Response:
        if "/item_summary/search" in req.url.path:
            return httpx.Response(
                200,
                json={
                    "total": 2,
                    "limit": 30,
                    "offset": 0,
                    "itemSummaries": [
                        {"itemId": "v1|broken|0", "title": "Broken", "price": {"value": "-10", "currency": "USD"}},
                        {
                            "itemId": "v1|minimal|0",
                            "title": "Lenovo ThinkPad 16GB RAM Laptop",
                            "price": {"value": "550.00", "currency": "USD"},
                        },
                    ],
                },
            )
        return httpx.Response(500, json={"error": "getItem failed"})

    app, _logs, _llm, backend, oauth = _build_resilient_app(browse_handler=_partial_and_getitem_fail)
    try:
        with TestClient(app) as client:
            ok_resp = client.post("/v1/search", headers={"X-API-Key": _API_KEY_A}, json={"query": "notebook lenovo"})
    finally:
        backend.close()
        oauth.close()
    assert ok_resp.status_code == 200
    body = ok_resp.json()
    assert body["status"] == "ok"
    offer = body["products"][0]["offers"][0]
    assert offer["seller"]["known"] is False
    assert offer["shipping"]["known"] is False
    assert offer["condition"] is None
    assert offer["image_url"] is None
    assert len(body["warnings"]) >= 1


def test_resilience_quota_exhausted_returns_503_not_no_results() -> None:
    app, _logs, _llm, backend, oauth = _build_resilient_app(
        browse_handler=lambda _req: httpx.Response(
            200,
            json={
                "total": 1,
                "limit": 30,
                "offset": 0,
                "itemSummaries": [
                    {
                        "itemId": "v1|q1|0",
                        "title": "Lenovo ThinkPad 16GB",
                        "price": {"value": "450.00", "currency": "USD"},
                    }
                ],
            },
        ),
        daily_limit=1,
    )
    try:
        with TestClient(app) as client:
            first = client.post("/v1/search", headers={"X-API-Key": _API_KEY_A}, json={"query": "notebook lenovo"})
            second = client.post("/v1/search", headers={"X-API-Key": _API_KEY_A}, json={"query": "notebook lenovo"})
    finally:
        backend.close()
        oauth.close()

    assert first.status_code == 200
    assert second.status_code == 503
    assert second.json()["error"]["code"] == "upstream_rate_limit"


def test_resilience_gemini_failures_retries_and_clarification() -> None:
    # 1) Timeout esgotando retries -> 503 llm_unavailable
    app, _logs, llm_stub, backend, oauth = _build_resilient_app(
        llm_handler=lambda **_k: (_ for _ in ()).throw(TimeoutError("gemini timeout"))
    )
    try:
        with TestClient(app) as client:
            resp = client.post("/v1/search", headers={"X-API-Key": _API_KEY_A}, json={"query": "notebook"})
    finally:
        backend.close()
        oauth.close()
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "llm_unavailable"
    assert llm_stub.calls == 3

    # 2) Indisponibilidade 503 -> 503 llm_unavailable
    app, _logs, llm_stub, backend, oauth = _build_resilient_app(
        llm_handler=lambda **_k: (_ for _ in ()).throw(
            genai_errors.APIError(503, {"error": {"message": "unavailable", "status": "UNAVAILABLE"}})
        )
    )
    try:
        with TestClient(app) as client:
            resp = client.post("/v1/search", headers={"X-API-Key": _API_KEY_A}, json={"query": "notebook"})
    finally:
        backend.close()
        oauth.close()
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "llm_unavailable"
    assert llm_stub.calls == 3

    # 3) Resposta vazia -> 502 llm_error
    app, _logs, llm_stub, backend, oauth = _build_resilient_app(
        llm_handler=lambda **_k: SimpleNamespace(parsed=None, text="   ")
    )
    try:
        with TestClient(app) as client:
            resp = client.post("/v1/search", headers={"X-API-Key": _API_KEY_A}, json={"query": "notebook"})
    finally:
        backend.close()
        oauth.close()
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "llm_error"
    assert llm_stub.calls == 1

    # 4) JSON malformado -> 502 llm_error
    app, _logs, llm_stub, backend, oauth = _build_resilient_app(
        llm_handler=lambda **_k: SimpleNamespace(parsed=None, text="{bad json")
    )
    try:
        with TestClient(app) as client:
            resp = client.post("/v1/search", headers={"X-API-Key": _API_KEY_A}, json={"query": "notebook"})
    finally:
        backend.close()
        oauth.close()
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "llm_error"
    assert llm_stub.calls == 1

    # 5) Erro de autenticação Gemini (401/403) -> 502 sem retry
    app, _logs, llm_stub, backend, oauth = _build_resilient_app(
        llm_handler=lambda **_k: (_ for _ in ()).throw(
            genai_errors.APIError(403, {"error": {"message": "API key invalid", "status": "PERMISSION_DENIED"}})
        )
    )
    try:
        with TestClient(app) as client:
            resp = client.post("/v1/search", headers={"X-API-Key": _API_KEY_A}, json={"query": "notebook"})
    finally:
        backend.close()
        oauth.close()
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "llm_error"
    assert llm_stub.calls == 1

    # 6) Necessidade de esclarecimento -> 200 e 0 chamadas à eBay
    ebay_calls = 0

    def _fail_if_called(_req: httpx.Request) -> httpx.Response:
        nonlocal ebay_calls
        ebay_calls += 1
        raise AssertionError("eBay não deveria ser chamada em clarification_needed!")

    app, _logs, _llm_stub, backend, oauth = _build_resilient_app(
        llm_handler=lambda **_k: SimpleNamespace(
            parsed=UserIntent(
                category=None,
                clarification_needed=True,
                clarification_question="Qual categoria de produto você deseja?",
                query="presente",
            ),
            text=None,
        ),
        browse_handler=_fail_if_called,
    )
    try:
        with TestClient(app) as client:
            resp = client.post("/v1/search", headers={"X-API-Key": _API_KEY_A}, json={"query": "quero um presente"})
    finally:
        backend.close()
        oauth.close()
    assert resp.status_code == 200
    assert resp.json()["status"] == "clarification_needed"
    assert ebay_calls == 0


def test_resilience_fastapi_malformed_not_ready_unexpected_error_and_cancelled_client() -> None:
    # 1) Malformed JSON, auth missing/invalid, rate limit
    limiter = InMemoryRateLimiter(max_requests=2, window_seconds=60.0)
    app, _logs, _llm, backend, oauth = _build_resilient_app(rate_limiter=limiter)
    try:
        with TestClient(app) as client:
            malformed = client.post(
                "/v1/search",
                headers={"X-API-Key": _API_KEY_A, "Content-Type": "application/json"},
                content=b"{not-json",
            )
            assert malformed.status_code == 422

            no_auth = client.post("/v1/search", json={"query": "notebook"})
            assert no_auth.status_code == 401

            bad_auth = client.post("/v1/search", headers={"X-API-Key": "wrong_key"}, json={"query": "notebook"})
            assert bad_auth.status_code == 401

            assert client.post("/v1/search", headers={"X-API-Key": _API_KEY_A}, json={"query": "notebook"}).status_code == 200
            assert client.post("/v1/search", headers={"X-API-Key": _API_KEY_A}, json={"query": "notebook"}).status_code == 200
            limited = client.post("/v1/search", headers={"X-API-Key": _API_KEY_A}, json={"query": "notebook"})
            assert limited.status_code == 429
            assert int(limited.headers["Retry-After"]) >= 1
    finally:
        backend.close()
        oauth.close()

    # 2) Dependência não pronta (orchestrator=None) -> 503
    unready_app = create_app(
        settings=load_settings(environ={"BUSCAAI_API_KEYS": _API_KEY_A}),
        preload_embeddings=False,
    )
    with TestClient(unready_app) as client:
        resp_503 = client.post("/v1/search", headers={"X-API-Key": _API_KEY_A}, json={"query": "notebook"})
    assert resp_503.status_code == 503
    assert resp_503.json()["error"]["code"] == "service_not_ready"

    # 3) Erro inesperado no orquestrador + simulação de desconexão/cancelamento sem derrubar próxima chamada
    class _FlakyOrch:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, user_text: str, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("SENTINEL_UNEXPECTED_STACK_SECRET")
            if self.calls == 2:
                raise asyncio.CancelledError("client disconnected")
            from buscai.orchestration import ExecutionStatus, OrchestrationResult

            return OrchestrationResult(
                request_id="ok-after-cancel",
                status=ExecutionStatus.NO_RESULTS,
            )

    app_err, _logs, _llm, backend, oauth = _build_resilient_app(orchestrator_override=_FlakyOrch())  # type: ignore[arg-type]
    try:
        with TestClient(app_err, raise_server_exceptions=False) as client:
            r_err = client.post("/v1/search", headers={"X-API-Key": _API_KEY_A}, json={"query": "notebook"})
            assert r_err.status_code == 500
            assert "SENTINEL_UNEXPECTED_STACK_SECRET" not in r_err.text
            assert "Traceback" not in r_err.text

            # Simula desconexão/cancelamento (CancelledError) e garante recuperação na chamada seguinte.
            r_cancel = client.post("/v1/search", headers={"X-API-Key": _API_KEY_A}, json={"query": "notebook"})
            assert r_cancel.status_code == 500

            recovered = client.post("/v1/search", headers={"X-API-Key": _API_KEY_A}, json={"query": "notebook"})
            assert recovered.status_code == 200
            assert recovered.json()["status"] == "no_results"
    finally:
        backend.close()
        oauth.close()


def test_resilience_concurrent_mixed_profiles_budgets_errors_and_rate_limits() -> None:
    def _llm_dynamic(*, contents: str, **_kwargs):
        low = contents.casefold()
        if "monitor" in low:
            return SimpleNamespace(
                parsed=UserIntent(category="monitor", required_features=["4K"], query="monitor 4k"),
                text=None,
            )
        if "falha" in low:
            raise genai_errors.APIError(503, {"error": {"message": "down", "status": "UNAVAILABLE"}})
        return SimpleNamespace(
            parsed=UserIntent(
                category="notebook",
                brands=["Lenovo"],
                price=PriceConstraint(max_price=4500, is_hard_constraint=True, currency="BRL"),
                query="notebook lenovo",
            ),
            text=None,
        )

    def _browse_dynamic(req: httpx.Request) -> httpx.Response:
        if "/item_summary/search" in req.url.path:
            q_val = req.url.params.get("q", "").casefold()
            if "monitor" in q_val:
                item = {
                    "itemId": "v1|mon-conc|0",
                    "title": "Monitor Dell 27 4K UHD IPS",
                    "price": {"value": "380.00", "currency": "USD"},
                    "condition": "NEW",
                    "shippingOptions": [{"shippingCostType": "FIXED", "shippingCost": {"value": "0.00", "currency": "USD"}}],
                }
            else:
                item = {
                    "itemId": "v1|nb-conc|0",
                    "title": "Lenovo ThinkPad 16GB RAM 512GB SSD",
                    "price": {"value": "520.00", "currency": "USD"},
                    "condition": "NEW",
                    "brand": "Lenovo",
                    "shippingOptions": [{"shippingCostType": "FIXED", "shippingCost": {"value": "0.00", "currency": "USD"}}],
                }
            return httpx.Response(200, json={"total": 1, "limit": 30, "offset": 0, "itemSummaries": [item]})
        return httpx.Response(404)

    limiter = InMemoryRateLimiter(max_requests=6, window_seconds=60.0)
    app, _logs, _llm, backend, oauth = _build_resilient_app(
        llm_handler=_llm_dynamic,
        browse_handler=_browse_dynamic,
        rate_limiter=limiter,
    )

    tasks = [
        (_API_KEY_A, "req-c0", {"query": "notebook lenovo", "profile_id": "best_value", "budget_brl": 4000}),
        (_API_KEY_A, "req-c1", {"query": "monitor 4k", "profile_id": "professional_use", "budget_brl": 3500}),
        (_API_KEY_A, "req-c2", {"query": "notebook que falha", "profile_id": "study"}),
        (_API_KEY_B, "req-c3", {"query": "notebook lenovo", "profile_id": "lowest_price", "budget_brl": 5000}),
        (_API_KEY_B, "req-c4", {"query": "monitor 4k", "profile_id": "best_performance", "budget_brl": 3000}),
        (_API_KEY_B, "req-c5", {"query": "notebook lenovo", "profile_id": "study", "budget_brl": 3800}),
    ]

    try:
        with TestClient(app) as client:
            def _run_one(item):
                key, req_id, body = item
                resp = client.post(
                    "/v1/search",
                    headers={"X-API-Key": key, "X-Request-ID": req_id},
                    json=body,
                )
                return req_id, resp.status_code, resp.headers.get("X-Request-ID"), resp.json()

            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(_run_one, tasks))

            # Dispara mais 4 chamadas com key A (já usou 3 de 6) -> 3 passam e a 7ª dá 429.
            extra_statuses = [
                client.post(
                    "/v1/search",
                    headers={"X-API-Key": _API_KEY_A},
                    json={"query": "notebook lenovo"},
                ).status_code
                for _ in range(4)
            ]
    finally:
        backend.close()
        oauth.close()

    by_id = {req_id: (status, hdr_id, payload) for req_id, status, hdr_id, payload in results}
    assert by_id["req-c0"][0] == 200 and by_id["req-c0"][2]["profile_id"] == "best_value"
    assert by_id["req-c1"][0] == 200 and by_id["req-c1"][2]["detected_category"] == "monitor"
    assert by_id["req-c2"][0] == 503 and by_id["req-c2"][2]["error"]["code"] == "llm_unavailable"
    assert by_id["req-c3"][0] == 200 and by_id["req-c3"][2]["profile_id"] == "lowest_price"
    assert by_id["req-c4"][0] == 200 and by_id["req-c4"][2]["profile_id"] == "best_performance"
    assert by_id["req-c5"][0] == 200 and by_id["req-c5"][2]["profile_id"] == "study"
    for req_id, (_status, hdr_id, _payload) in by_id.items():
        assert hdr_id == req_id

    assert extra_statuses == [200, 200, 200, 429]


def test_benchmark_api_e2e_smoke() -> None:
    from scripts.benchmark_api_e2e import run_benchmark

    report = run_benchmark(iterations=1, concurrency=1)
    assert report["http_metrics"]["total_requests"] == 10
    assert report["http_metrics"]["scenario_status_compliance_rate"] == 1.0
    assert report["ranking_metrics"]["hard_constraint_compliance_rate"] == 1.0
    assert report["ranking_metrics"]["offer_preservation_rate"] == 1.0
    assert json.dumps(report)
