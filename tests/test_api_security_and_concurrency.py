"""Testes offline de segurança, sanitização, Request ID, não persistência e concorrência."""

from __future__ import annotations

import asyncio
import io
import json
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from buscai.api import create_app
from buscai.api.app import RequestContextMiddleware
from buscai.config import load_settings
from buscai.observability import ALLOWED_LOG_FIELDS, configure_logging
from buscai.orchestration import OrchestrationDependencies, SearchOrchestrator
from buscai.pricing import CurrencyConverter, FixedEnvExchangeRateProvider
from buscai.ranking.pipeline import RankingPipeline
from buscai.search.models import Candidate, SearchQuery, SearchResponse, SellerInfo, ShippingInfo
from buscai.search.query_builder import QueryBuilder
from buscai.search.quota import QuotaGuard, RequestCallBudget
from buscai.semantic.embeddings import DeterministicStubEmbeddingProvider, EmbeddingService
from buscai.semantic.pipeline import SemanticSearchProcessor
from buscai.validation.pipeline import ValidationPipeline
from userintent.schema import UserIntent

_API_KEY = "buscai_sec_test_key_999"
_SENTINEL_TITLE = "SENTINEL_TITLE_ULTRA_SECRET_LAPTOP"
_SENTINEL_URL = "https://sentinel.invalid/item/999888"
_SENTINEL_SELLER = "sentinel_seller_pii"


class _SentinelExtractor:
    def extract(
        self,
        user_text: str,
        *,
        conversation_history=None,
        request_id: str | None = None,
    ) -> UserIntent:
        del conversation_history, request_id
        if "monitor" in user_text.casefold():
            return UserIntent(category="monitor", query=user_text)
        return UserIntent(category="notebook", query=user_text)


class _SentinelBackend:
    def __init__(self, *, fail_on_keyword: str | None = None) -> None:
        self._fail_on_keyword = fail_on_keyword
        self.calls = 0
        self._lock = threading.Lock()

    def search(
        self,
        query: SearchQuery,
        *,
        budget: RequestCallBudget | None = None,
        request_id: str | None = None,
    ) -> SearchResponse:
        with self._lock:
            self.calls += 1
        if self._fail_on_keyword and self._fail_on_keyword in query.query_text.casefold():
            raise RuntimeError(
                f"Falha com {_SENTINEL_TITLE} {_SENTINEL_URL} {_SENTINEL_SELLER}"
            )
        time.sleep(0.01)
        is_monitor = "monitor" in query.query_text.casefold()
        cand = Candidate(
            item_id=f"item-{'mon' if is_monitor else 'nb'}-{request_id or 'x'}",
            source="synthetic",
            title=f"{'Monitor 4K' if is_monitor else _SENTINEL_TITLE}",
            price=Decimal("400.00"),
            currency="USD",
            condition="New",
            categories=("monitor" if is_monitor else "notebook",),
            item_web_url=_SENTINEL_URL,
            seller=SellerInfo(
                username=_SENTINEL_SELLER,
                feedback_score=100,
                feedback_percentage=Decimal("98.5"),
            ),
            shipping=ShippingInfo(cost=Decimal("0"), currency="USD", free_shipping=True),
            details_loaded=True,
        )
        return SearchResponse(candidates=(cand,), total_estimated=1, limit=query.limit, offset=0, calls_used=1)

    def get_item_details(self, item_id: str, **kwargs) -> None:
        return None


def _build_secure_app(
    *,
    fail_on_keyword: str | None = None,
    max_rpm: int = 100,
    max_body_bytes: int | None = None,
):
    converter = CurrencyConverter(FixedEnvExchangeRateProvider(rate=Decimal("5.50")))
    emb = EmbeddingService(DeterministicStubEmbeddingProvider())
    emb.embed_queries(["warmup"])
    backend = _SentinelBackend(fail_on_keyword=fail_on_keyword)
    deps = OrchestrationDependencies(
        extractor=_SentinelExtractor(),
        query_builder=QueryBuilder(currency_converter=converter),
        search_backend=backend,
        semantic_processor=SemanticSearchProcessor(emb),
        validation_pipeline=ValidationPipeline(currency_converter=converter),
        ranking_pipeline=RankingPipeline(converter=converter),
        quota_guard=QuotaGuard(),
    )
    orch = SearchOrchestrator(deps)
    stream = io.StringIO()
    logger = configure_logging("INFO", stream=stream, logger_name="buscai.test.security")
    env: dict[str, str] = {
        "BUSCAAI_API_KEYS": _API_KEY,
        "BUSCAAI_RATE_LIMIT_PER_MINUTE": str(max_rpm),
    }
    if max_body_bytes is not None:
        env["BUSCAAI_MAX_REQUEST_BODY_BYTES"] = str(max_body_bytes)
    settings = load_settings(environ=env)
    app = create_app(settings=settings, orchestrator=orch, preload_embeddings=False, logger=logger)
    return app, stream, backend


def test_request_id_sanitization_and_rejection_of_control_chars() -> None:
    app, stream, _ = _build_secure_app()
    with TestClient(app) as client:
        valid = client.get("/v1/health", headers={"X-Request-ID": "trace-client_01.ok"})
        assert valid.headers["X-Request-ID"] == "trace-client_01.ok"

        for bad_id in ("bad\r\nInjected: 1", "bad\x00id", "has spaces", "x" * 65):
            # httpx rejeita \r\n em header no cliente, então testamos os aceitos pelo cliente HTTP
            if "\r" in bad_id or "\n" in bad_id or "\x00" in bad_id:
                continue
            resp = client.get("/v1/health", headers={"X-Request-ID": bad_id})
            returned = resp.headers["X-Request-ID"]
            assert returned != bad_id
            assert len(returned) == 32

    # Validação direta da função sanitizadora para quebras de linha e caracteres de controle.
    from buscai.api.errors import validate_or_generate_request_id

    for ctrl_id in ("abc\r\ndef", "abc\ndef", "abc\x00def", ""):
        generated = validate_or_generate_request_id(ctrl_id)
        assert generated != ctrl_id
        assert len(generated) == 32
    assert stream.getvalue()


def test_logs_and_errors_never_leak_listings_credentials_or_queries() -> None:
    app, stream, _ = _build_secure_app(fail_on_keyword="laptop")
    secret_query = "notebook_secreto_para_nao_logar"
    with TestClient(app) as client:
        ok_resp = client.post(
            "/v1/search",
            headers={"X-API-Key": _API_KEY, "X-Request-ID": "req-no-leak-ok"},
            json={"query": "monitor 4k"},
        )
        err_resp = client.post(
            "/v1/search",
            headers={"X-API-Key": _API_KEY, "X-Request-ID": "req-no-leak-err"},
            json={"query": secret_query},
        )

    assert ok_resp.status_code == 200
    assert err_resp.status_code == 500
    for forbidden in (_API_KEY, _SENTINEL_TITLE, _SENTINEL_URL, _SENTINEL_SELLER, secret_query):
        assert forbidden not in err_resp.text
        assert forbidden not in stream.getvalue()

    for line in stream.getvalue().strip().splitlines():
        payload = json.loads(line)
        extra_keys = set(payload.keys()) - {"timestamp", "level", "logger", "event"}
        assert extra_keys.issubset(ALLOWED_LOG_FIELDS)


def test_api_search_has_zero_disk_writes_zero_network_and_zero_cache_between_requests() -> None:
    app, _stream, backend = _build_secure_app()
    temp_root = Path(tempfile.gettempdir()).resolve()
    violations: list[str] = []
    active = True

    def _audit(event: str, args: tuple) -> None:
        if not active:
            return
        if event == "socket.connect":
            addr = args[1] if len(args) >= 2 else None
            host = addr[0] if isinstance(addr, tuple) and addr else None
            if host in {"127.0.0.1", "::1"}:
                return
            violations.append(f"network:{event}:{host}")
        elif event == "urllib.Request":
            violations.append(f"network:{event}")
        elif event == "open" and len(args) >= 2 and any(flag in str(args[1]) for flag in ("w", "a", "x", "+")):
            target = Path(str(args[0])).resolve()
            if not str(target).startswith(str(temp_root)):
                violations.append(f"write:{target}")

    sys.addaudithook(_audit)
    try:
        with TestClient(app) as client:
            r1 = client.post(
                "/v1/search",
                headers={"X-API-Key": _API_KEY},
                json={"query": "notebook lenovo"},
            )
            r2 = client.post(
                "/v1/search",
                headers={"X-API-Key": _API_KEY},
                json={"query": "notebook lenovo"},
            )
    finally:
        active = False

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert backend.calls == 2
    assert violations == []


def test_concurrent_requests_remain_isolated_with_shared_orchestrator_and_independent_failures() -> None:
    app, _stream, _backend = _build_secure_app(fail_on_keyword="laptop", max_rpm=50)
    with TestClient(app) as client:

        def _worker(idx: int) -> tuple[int, str, str | None]:
            req_id = f"conc-req-{idx}"
            if idx % 3 == 0:
                resp = client.post(
                    "/v1/search",
                    headers={"X-API-Key": _API_KEY, "X-Request-ID": req_id},
                    json={"query": "laptop que falha"},
                )
                return resp.status_code, resp.headers["X-Request-ID"], resp.json()["error"]["code"]
            resp = client.post(
                "/v1/search",
                headers={"X-API-Key": _API_KEY, "X-Request-ID": req_id},
                json={"query": "monitor 4k", "budget_brl": 3000 + idx},
            )
            return resp.status_code, resp.headers["X-Request-ID"], resp.json().get("detected_category")

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(_worker, range(12)))

    for idx, (status_code, returned_req_id, tag) in enumerate(results):
        assert returned_req_id == f"conc-req-{idx}"
        if idx % 3 == 0:
            assert status_code == 500
            assert tag == "internal_error"
        else:
            assert status_code == 200
            assert tag == "monitor"


def test_api_rejects_oversized_body_with_413_by_content_length_and_streaming() -> None:
    log_stream = io.StringIO()
    app, _stream, _backend = _build_secure_app(max_body_bytes=2048)
    del log_stream
    giant_text = "x" * 3000

    with TestClient(app) as client:
        ok_resp = client.post(
            "/v1/search",
            headers={"X-API-Key": _API_KEY, "X-Request-ID": "req-ok-small"},
            json={"query": "notebook lenovo 16gb"},
        )
        assert ok_resp.status_code == 200

        search_413 = client.post(
            "/v1/search",
            headers={"X-API-Key": _API_KEY, "X-Request-ID": "req-413-search"},
            json={"query": giant_text},
        )
        chat_413 = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY, "X-Request-ID": "req-413-chat"},
            json={"message": giant_text},
        )

    for resp, expected_id in ((search_413, "req-413-search"), (chat_413, "req-413-chat")):
        assert resp.status_code == 413
        assert resp.headers["X-Request-ID"] == expected_id
        body = resp.json()
        assert body["error"]["code"] == "payload_too_large"
        assert body["error"]["request_id"] == expected_id
        assert giant_text not in resp.text
        assert "Traceback" not in resp.text

    async def _run_chunked_middleware_case() -> tuple[int, dict[str, str], dict[str, object]]:
        async def _inner_app(scope, receive, send) -> None:
            del scope, send
            while True:
                msg = await receive()
                if not msg.get("more_body", False):
                    break

        mw_logger = configure_logging("INFO", stream=io.StringIO(), logger_name="buscai.test.mw413")
        middleware = RequestContextMiddleware(_inner_app, logger=mw_logger, max_body_bytes=1024)
        chunks = [
            {"type": "http.request", "body": b"a" * 600, "more_body": True},
            {"type": "http.request", "body": b"b" * 600, "more_body": False},
        ]

        async def _receive():
            return chunks.pop(0) if chunks else {"type": "http.request", "body": b"", "more_body": False}

        sent: list[dict[str, object]] = []

        async def _send(message: dict[str, object]) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/search",
            "headers": [(b"x-request-id", b"req-stream-413")],
        }
        await middleware(scope, _receive, _send)  # type: ignore[arg-type]
        start_msg = sent[0]
        body_msg = sent[1]
        hdrs = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in start_msg["headers"]  # type: ignore[attr-defined]
        }
        payload = json.loads(bytes(body_msg["body"]).decode("utf-8"))  # type: ignore[arg-type]
        return int(start_msg["status"]), hdrs, payload  # type: ignore[arg-type]

    status_chunked, hdrs_chunked, payload_chunked = asyncio.run(_run_chunked_middleware_case())
    assert status_chunked == 413
    assert hdrs_chunked["x-request-id"] == "req-stream-413"
    assert hdrs_chunked["cache-control"] == "no-store"
    assert payload_chunked["error"]["code"] == "payload_too_large"  # type: ignore[index]


def test_api_sets_defensive_security_headers_and_no_store_on_v1_routes() -> None:
    app, _stream, _backend = _build_secure_app(max_body_bytes=1024)
    with TestClient(app) as client:
        responses = [
            client.get("/v1/health"),
            client.post("/v1/search", headers={"X-API-Key": _API_KEY}, json={"query": "notebook"}),
            client.post("/v1/chat", headers={"X-API-Key": _API_KEY}, json={"message": "notebook"}),
            client.post("/v1/search", json={"query": "notebook"}),  # 401
            client.post("/v1/search", headers={"X-API-Key": _API_KEY}, json={"query": ""}),  # 422
            client.post("/v1/search", headers={"X-API-Key": _API_KEY}, json={"query": "z" * 1500}),  # 413
            client.get("/v1/inexistente"),  # 404
        ]

    expected_codes = [200, 200, 200, 401, 422, 413, 404]
    assert [r.status_code for r in responses] == expected_codes
    for resp in responses:
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["Referrer-Policy"] == "no-referrer"
        assert resp.headers["Cache-Control"] == "no-store"


def test_api_malformed_inputs_headers_and_zero_stack_or_secret_exposure() -> None:
    app, stream, _backend = _build_secure_app()
    leaked_secret_pattern = "GEMINI_API_KEY=segredo_super_999"

    with TestClient(app) as client:
        bad_json = client.post(
            "/v1/search",
            headers={"X-API-Key": _API_KEY, "Content-Type": "application/json"},
            content=b"{malformed",
        )
        empty_body = client.post(
            "/v1/search",
            headers={"X-API-Key": _API_KEY, "Content-Type": "application/json"},
            content=b"",
        )
        wrong_ct = client.post(
            "/v1/search",
            headers={"X-API-Key": _API_KEY, "Content-Type": "text/plain"},
            content=b"query=notebook",
        )
        method_405 = client.put("/v1/search", headers={"X-API-Key": _API_KEY}, json={"query": "notebook"})
        not_found_404 = client.get("/v1/nao-existe")
        space_key = client.post("/v1/search", headers={"X-API-Key": "   "}, json={"query": "notebook"})
        placeholder_key = client.post(
            "/v1/search",
            headers={"X-API-Key": "chave_cliente_1"},
            json={"query": "notebook"},
        )
        secret_in_validation = client.post(
            "/v1/search",
            headers={"X-API-Key": _API_KEY},
            json={"query": "notebook", "profile_id": leaked_secret_pattern},
        )

    assert bad_json.status_code == 422
    assert empty_body.status_code == 422
    assert wrong_ct.status_code == 422
    assert method_405.status_code == 405
    assert method_405.json()["error"]["code"] == "method_not_allowed"
    assert not_found_404.status_code == 404
    assert not_found_404.json()["error"]["code"] == "not_found"
    assert space_key.status_code == 401
    assert placeholder_key.status_code == 401
    assert secret_in_validation.status_code == 422
    assert "segredo_super_999" not in secret_in_validation.text
    assert "[REDACTED]" in secret_in_validation.text

    for resp in (
        bad_json,
        empty_body,
        wrong_ct,
        method_405,
        not_found_404,
        space_key,
        placeholder_key,
        secret_in_validation,
    ):
        assert "Traceback" not in resp.text
        assert ".py" not in resp.text
        assert _API_KEY not in resp.text

    assert _API_KEY not in stream.getvalue()
    assert "segredo_super_999" not in stream.getvalue()
