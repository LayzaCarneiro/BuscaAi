"""Testes offline da Etapa 10 para contratos, fluxo, segurança, sanitização e concorrência de POST /v1/chat."""

from __future__ import annotations

import io
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from buscai.api import create_app
from buscai.catalog.errors import (
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from buscai.config import load_settings
from buscai.observability import ALLOWED_LOG_FIELDS, configure_logging
from buscai.orchestration import (
    TRACING_ENV_VARS,
    OrchestrationDependencies,
    SearchOrchestrator,
)
from buscai.pricing import CurrencyConverter, FixedEnvExchangeRateProvider
from buscai.ranking.pipeline import RankingPipeline
from buscai.search.models import Candidate, SearchQuery, SearchResponse, SellerInfo, ShippingInfo
from buscai.search.query_builder import QueryBuilder, SearchPlan
from buscai.search.quota import QuotaGuard, RequestCallBudget
from buscai.semantic.embeddings import DeterministicStubEmbeddingProvider, EmbeddingService
from buscai.semantic.pipeline import SemanticSearchProcessor
from buscai.validation.pipeline import ValidationPipeline
from userintent.llm import IntentExtractionError, LLMUnavailableError
from userintent.schema import PriceConstraint, UserIntent

_API_KEY = "buscai_chat_test_key_001"
_SENTINEL_TITLE = "SENTINEL_CHAT_SECRET_LAPTOP_XYZ"
_SENTINEL_URL = "https://www.ebay.com/itm/9988776655"
_SENTINEL_ITEM_ID = "v1|9988776655|0"
_SENTINEL_SELLER = "sentinel_chat_seller_pii"


class _SpyChatExtractor:
    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.calls: list[dict[str, object]] = []
        self._lock = threading.Lock()

    def extract(
        self,
        user_text: str,
        *,
        conversation_history: list[dict[str, str]] | tuple[dict[str, str], ...] | None = None,
        request_id: str | None = None,
    ) -> UserIntent:
        history_copy = list(conversation_history) if conversation_history is not None else []
        with self._lock:
            self.calls.append(
                {
                    "user_text": user_text,
                    "conversation_history": history_copy,
                    "request_id": request_id,
                }
            )
        if self.mode == "clarify":
            return UserIntent(
                category=None,
                clarification_needed=True,
                clarification_question="Qual tipo de produto e orçamento você procura?",
                query=user_text,
            )
        if self.mode == "invalid_plan":
            return UserIntent(category=None, query="indefinido")
        if self.mode == "llm_unavailable":
            raise LLMUnavailableError("Gemini offline com segredo GEMINI_API_KEY=segredo_chat_123")
        if self.mode == "llm_error":
            raise IntentExtractionError("Erro bruto LLM interno")
        if self.mode == "soft_relax":
            return UserIntent(
                category="notebook",
                preferred_features=["touchscreen", "teclado retroiluminado"],
                query=user_text,
            )
        if self.mode == "hard_impossible":
            return UserIntent(
                category="notebook",
                brands=["Lenovo"],
                required_features=["16GB RAM"],
                price=PriceConstraint(max_price=500, is_hard_constraint=True, currency="BRL"),
                query=user_text,
            )
        if "monitor" in user_text.casefold():
            return UserIntent(category="monitor", required_features=["4K"], query=user_text)
        return UserIntent(
            category="notebook",
            brands=["Lenovo"],
            required_features=["16GB RAM"],
            query=user_text,
        )


class _SpyChatBackend:
    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
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
        if budget is not None:
            budget.search_calls_used += 1
        if self.mode == "empty":
            return SearchResponse(candidates=(), total_estimated=0, limit=query.limit, offset=0, calls_used=1)
        if self.mode == "timeout":
            raise ProviderTimeoutError("Timeout para https://api.ebay.com")
        if self.mode == "unavailable":
            raise ProviderUnavailableError("503 eBay indisponível")
        if self.mode == "all_rejected":
            cand = Candidate(
                item_id="rej-chat-1",
                source="synthetic",
                title="Lenovo ThinkPad X1 16GB RAM 512GB SSD",
                price=Decimal("2200.00"),
                currency="USD",
                condition="New",
                categories=("notebook",),
                brand="Lenovo",
                aspects={"RAM Size": ("16 GB",)},
            )
            return SearchResponse(candidates=(cand,), total_estimated=1, limit=query.limit, offset=0, calls_used=1)

        if self.mode == "slow":
            time.sleep(0.01)

        is_monitor = "monitor" in query.query_text.casefold()
        if is_monitor:
            c_mon = Candidate(
                item_id="chat-mon-1",
                source="synthetic",
                title="Dell UltraSharp 27 4K Monitor",
                price=Decimal("320.00"),
                currency="USD",
                condition="New",
                categories=("monitor",),
                brand="Dell",
                model="UltraSharp 27",
                aspects={"Resolution": ("4K",)},
                shipping=ShippingInfo(cost=Decimal("0"), currency="USD", free_shipping=True),
                seller=SellerInfo(username="mon_seller", feedback_score=400, feedback_percentage=Decimal("99.0")),
                details_loaded=True,
            )
            return SearchResponse(candidates=(c_mon,), total_estimated=1, limit=query.limit, offset=0, calls_used=1)

        c1 = Candidate(
            item_id=_SENTINEL_ITEM_ID,
            source="synthetic",
            title=f"Lenovo ThinkPad T14 16GB RAM 512GB SSD {_SENTINEL_TITLE}",
            price=Decimal("500.00"),
            currency="USD",
            condition="New",
            categories=("notebook",),
            brand="Lenovo",
            model="ThinkPad T14",
            aspects={"RAM Size": ("16 GB",), "SSD Capacity": ("512 GB",)},
            item_web_url=_SENTINEL_URL,
            seller=SellerInfo(
                username=_SENTINEL_SELLER,
                feedback_score=650,
                feedback_percentage=Decimal("99.2"),
            ),
            shipping=ShippingInfo(cost=Decimal("0"), currency="USD", free_shipping=True, estimated_max_days=5),
            details_loaded=True,
        )
        c2 = Candidate(
            item_id="chat-nb-2",
            source="synthetic",
            title="Acer Aspire 5 16GB RAM 512GB SSD Laptop",
            price=Decimal("460.00"),
            currency="USD",
            condition="New",
            categories=("notebook",),
            brand="Acer",
            model="Aspire 5",
            aspects={"RAM Size": ("16 GB",), "SSD Capacity": ("512 GB",)},
            shipping=ShippingInfo(cost=Decimal("0"), currency="USD", free_shipping=True, estimated_max_days=5),
            seller=SellerInfo(username="acer_seller", feedback_score=500, feedback_percentage=Decimal("98.5")),
            details_loaded=True,
        )
        candidates = (c2,) if self.mode == "no_lenovo" else (c1, c2)
        return SearchResponse(candidates=candidates, total_estimated=len(candidates), limit=query.limit, offset=0, calls_used=1)

    def get_item_details(self, item_id: str, **kwargs) -> Candidate | None:
        return None


def _build_chat_app(
    *,
    extractor_mode: str = "ok",
    backend_mode: str = "ok",
    env_overrides: dict[str, str] | None = None,
    with_orchestrator: bool = True,
):
    extractor = _SpyChatExtractor(extractor_mode)
    backend = _SpyChatBackend(backend_mode)
    log_stream = io.StringIO()
    logger = configure_logging("INFO", stream=log_stream, logger_name="buscai.test.chat")

    env: dict[str, str] = {
        "BUSCAAI_API_KEYS": _API_KEY,
        "USD_BRL_RATE": "5.50",
        "USD_BRL_RATE_UPDATED_AT": "2026-11-25",
        "BUSCAAI_RATE_LIMIT_PER_MINUTE": "100",
        "BUSCAAI_SEARCH_MAX_LIMIT": "20",
        "BUSCAAI_CHAT_MAX_HISTORY_MESSAGES": "6",
        "BUSCAAI_CHAT_MAX_MESSAGE_CHARS": "200",
        "BUSCAAI_CHAT_MAX_HISTORY_TOTAL_CHARS": "500",
    }
    if env_overrides:
        env.update(env_overrides)
    settings = load_settings(environ=env)

    orch: SearchOrchestrator | None = None
    if with_orchestrator:
        converter = CurrencyConverter(
            FixedEnvExchangeRateProvider(rate=Decimal("5.50"), updated_at="2026-11-25", source="env_fixed_rate")
        )
        emb = EmbeddingService(DeterministicStubEmbeddingProvider(), logger=logger)
        emb.embed_queries(["warmup"])
        qb = QueryBuilder(currency_converter=converter)
        if extractor_mode == "invalid_plan":
            class _InvalidPlanBuilder(QueryBuilder):
                def build(self, intent: UserIntent, *, limit: int = 30) -> SearchPlan:
                    return SearchPlan(canonical_category=None, is_supported_mvp_category=False, queries=())

            qb = _InvalidPlanBuilder(currency_converter=converter)
        deps = OrchestrationDependencies(
            extractor=extractor,
            query_builder=qb,
            search_backend=backend,
            semantic_processor=SemanticSearchProcessor(emb, logger=logger),
            validation_pipeline=ValidationPipeline(currency_converter=converter),
            ranking_pipeline=RankingPipeline(converter=converter),
            quota_guard=QuotaGuard(),
            logger=logger,
        )
        orch = SearchOrchestrator(deps)

    app = create_app(
        settings=settings,
        orchestrator=orch,
        preload_embeddings=False,
        logger=logger,
    )
    return app, extractor, backend, log_stream


def test_chat_settings_env_parsing_and_validation() -> None:
    defaults = load_settings(environ={})
    assert defaults.chat_max_history_messages == 12
    assert defaults.chat_max_message_chars == 500
    assert defaults.chat_max_history_total_chars == 4000
    summary = defaults.safe_summary()
    assert summary["chat_max_history_messages"] == 12
    assert summary["chat_max_message_chars"] == 500
    assert summary["chat_max_history_total_chars"] == 4000

    with pytest.raises(ValueError, match="chat_max_message_chars"):
        load_settings(
            environ={
                "BUSCAAI_CHAT_MAX_MESSAGE_CHARS": "600",
                "BUSCAAI_CHAT_MAX_HISTORY_TOTAL_CHARS": "400",
            }
        )


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"message": ""},
        {"message": "   \t\n  "},
        {"message": "a" * 201},
        {"message": "notebook", "extra_field": 1},
        {"message": "notebook", "conversation_history": "not-a-list"},
        {"message": "notebook", "conversation_history": None},
        {"message": "notebook", "conversation_history": [{"role": "system", "content": "hack"}]},
        {"message": "notebook", "conversation_history": [{"role": "tool", "content": "hack"}]},
        {"message": "notebook", "conversation_history": [{"role": "user", "content": "   "}]},
        {"message": "notebook", "conversation_history": [{"role": "user", "content": "oi", "extra": "x"}]},
        {"message": "notebook", "conversation_history": [{"role": "user"}]},
        {"message": "notebook", "conversation_history": [{"role": "user", "content": "x" * 201}]},
        {
            "message": "notebook",
            "conversation_history": [{"role": "user", "content": f"turn {i}"} for i in range(7)],
        },
        {
            "message": "notebook",
            "conversation_history": [
                {"role": "user", "content": "a" * 180},
                {"role": "assistant", "content": "b" * 180},
                {"role": "user", "content": "c" * 180},
            ],
        },
        {"message": "notebook", "budget_brl": 0},
        {"message": "notebook", "profile_id": "invalido_xyz"},
        {"message": "notebook", "limit": 21},
    ],
)
def test_chat_request_contracts_and_configured_limits(bad_payload: dict[str, object]) -> None:
    app, _, _, _ = _build_chat_app()
    with TestClient(app) as client:
        resp = client.post("/v1/chat", headers={"X-API-Key": _API_KEY}, json=bad_payload)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["request_id"]


def test_chat_success_clarification_no_results_all_rejected_and_relaxed() -> None:
    # 1. Busca bem-sucedida com resposta determinística grounded e SearchResponseSchema reutilizado
    app_ok, extractor_ok, backend_ok, _ = _build_chat_app()
    with TestClient(app_ok) as client:
        ok_resp = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY, "X-Request-ID": "req-chat-ok"},
            json={
                "message": "Quero um notebook Lenovo com 16 GB de RAM",
                "budget_brl": 4000,
                "profile_id": "best_value",
                "limit": 5,
            },
        )
    assert ok_resp.status_code == 200
    assert ok_resp.headers["X-Request-ID"] == "req-chat-ok"
    data = ok_resp.json()
    assert data["status"] == "ok"
    assert data["clarification_needed"] is False
    assert data["clarification_question"] is None
    assert data["search_results"]["status"] == "ok"
    assert data["search_results"]["profile_id"] == "best_value"
    assert data["search_results"]["detected_category"] == "notebook"
    assert data["conversation_context"]["applied_budget_brl"] == "4000"
    assert "notebook" in data["assistant_message"]
    assert "best_value" in data["assistant_message"]
    assert "Destaque #1:" in data["assistant_message"]
    assert backend_ok.calls >= 1
    assert extractor_ok.calls[0]["conversation_history"] == []

    # 2. Clarification sem chamada à eBay
    app_clarify, _, backend_clarify, _ = _build_chat_app(extractor_mode="clarify")
    with TestClient(app_clarify) as client:
        clarify_resp = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY},
            json={"message": "Quero um presente legal"},
        )
    assert clarify_resp.status_code == 200
    clarify_body = clarify_resp.json()
    assert clarify_body["status"] == "clarification_needed"
    assert clarify_body["clarification_needed"] is True
    assert clarify_body["clarification_question"] == "Qual tipo de produto e orçamento você procura?"
    assert clarify_body["search_results"] is None
    assert backend_clarify.calls == 0

    # 3. Nenhum anúncio encontrado (no_results)
    app_empty, _, _, _ = _build_chat_app(backend_mode="empty")
    with TestClient(app_empty) as client:
        empty_resp = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY},
            json={"message": "Quero um notebook Lenovo"},
        )
    assert empty_resp.status_code == 200
    empty_body = empty_resp.json()
    assert empty_body["status"] == "no_results"
    assert "Nenhum anúncio foi encontrado" in empty_body["assistant_message"]

    # 4. Todos os candidatos rejeitados por hard constraint (diferenciado de no_results)
    app_rej, _, _, _ = _build_chat_app(extractor_mode="hard_impossible", backend_mode="all_rejected")
    with TestClient(app_rej) as client:
        rej_resp = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY},
            json={"message": "Notebook Lenovo 16GB RAM até R$ 500"},
        )
    assert rej_resp.status_code == 200
    rej_body = rej_resp.json()
    assert rej_body["status"] == "all_rejected"
    assert rej_body["search_results"]["rejected_count"] >= 1
    assert "rejeitados pelos requisitos obrigatórios" in rej_body["assistant_message"]
    assert "não são relaxados automaticamente" in rej_body["assistant_message"]

    # 5. Preferência flexível relaxada
    app_rel, _, _, _ = _build_chat_app(extractor_mode="soft_relax", backend_mode="no_lenovo")
    with TestClient(app_rel) as client:
        rel_resp = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY},
            json={"message": "Notebook Lenovo com touchscreen"},
        )
    assert rel_resp.status_code == 200
    rel_body = rel_resp.json()
    assert rel_body["status"] == "ok"
    assert rel_body["search_results"]["relaxed"] is True
    assert rel_body["conversation_context"]["relaxed"] is True
    assert "Preferências flexíveis relaxadas" in rel_body["assistant_message"]


@pytest.mark.parametrize(
    ("extractor_mode", "backend_mode", "with_orch", "expected_status", "expected_code"),
    [
        ("llm_unavailable", "ok", True, 503, "llm_unavailable"),
        ("llm_error", "ok", True, 502, "llm_error"),
        ("ok", "timeout", True, 503, "upstream_timeout"),
        ("ok", "unavailable", True, 503, "upstream_unavailable"),
        ("invalid_plan", "ok", True, 400, "invalid_search_plan"),
        ("ok", "ok", False, 503, "service_not_ready"),
    ],
)
def test_chat_error_handling_never_leaks_secrets_or_turns_failures_into_empty_results(
    extractor_mode: str,
    backend_mode: str,
    with_orch: bool,
    expected_status: int,
    expected_code: str,
) -> None:
    app, _, _, _ = _build_chat_app(
        extractor_mode=extractor_mode,
        backend_mode=backend_mode,
        with_orchestrator=with_orch,
    )
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY},
            json={"message": "Quero um notebook Lenovo"},
        )
    assert resp.status_code == expected_status
    body = resp.json()
    assert body["error"]["code"] == expected_code
    assert body["error"]["request_id"]
    assert "segredo_chat_123" not in resp.text
    assert _API_KEY not in resp.text


def test_chat_history_sanitization_never_forwards_ebay_listings_or_duplicates_current_message() -> None:
    app, extractor, _, _ = _build_chat_app(env_overrides={"BUSCAAI_CHAT_MAX_MESSAGE_CHARS": "400"})
    current_msg = "Prefiro com 16 GB de RAM."
    history = [
        {"role": "user", "content": "Quero um notebook para estudo até R$ 4.000."},
        {
            "role": "assistant",
            "content": (
                f"Qual modelo prefere? "
                f"Destaque #1: {_SENTINEL_TITLE} por US$ 500.00 em {_SENTINEL_URL} "
                f"(item_id: {_SENTINEL_ITEM_ID}, vendedor: {_SENTINEL_SELLER}). "
                "Busca concluída na categoria notebook."
            ),
        },
        # Cliente repete acidentalmente a mensagem atual no final do histórico:
        {"role": "user", "content": "  Prefiro com   16 GB de RAM. "},
    ]

    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY},
            json={"message": current_msg, "conversation_history": history},
        )
    assert resp.status_code == 200

    forwarded = extractor.calls[0]["conversation_history"]
    assert isinstance(forwarded, list)
    # Último turno duplicado foi removido; apenas turno 0 (user) e turno 1 sanitizado (assistant) seguem.
    assert len(forwarded) == 2
    assert forwarded[0] == {"role": "user", "content": "Quero um notebook para estudo até R$ 4.000."}
    assert forwarded[1]["role"] == "assistant"
    assert "Qual modelo prefere?" in forwarded[1]["content"]
    assert "Busca concluída na categoria notebook." in forwarded[1]["content"]

    serialized_forwarded = json.dumps(forwarded, ensure_ascii=False)
    for forbidden in (_SENTINEL_TITLE, _SENTINEL_URL, _SENTINEL_ITEM_ID, _SENTINEL_SELLER, "US$ 500"):
        assert forbidden not in serialized_forwarded


def test_chat_security_no_persistence_no_disk_writes_no_tracing_auth_and_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, extractor, _, log_stream = _build_chat_app(
        env_overrides={"BUSCAAI_RATE_LIMIT_PER_MINUTE": "2"}
    )

    disk_writes: list[str] = []
    external_connects: list[str] = []

    def _audit(event: str, args: tuple[object, ...]) -> None:
        if event == "open" and len(args) >= 2:
            mode = str(args[1])
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                disk_writes.append(f"{args[0]} ({mode})")
        elif event == "socket.connect" and len(args) >= 2:
            addr = str(args[1])
            if "127.0.0.1" not in addr and "::1" not in addr:
                external_connects.append(addr)

    sys.addaudithook(_audit)  # type: ignore[arg-type]

    # Auth ausente ou inválida -> 401
    with TestClient(app) as client:
        missing = client.post("/v1/chat", json={"message": "notebook"})
        assert missing.status_code == 401

        invalid = client.post("/v1/chat", headers={"X-API-Key": "wrong_key"}, json={"message": "notebook"})
        assert invalid.status_code == 401

        # Duas requisições válidas sem histórico -> estateless (servidor não persiste conversa)
        r1 = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY},
            json={"message": "notebook_privado_turno_1"},
        )
        r2 = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY},
            json={"message": "notebook_privado_turno_2"},
        )
        # Terceira estoura rate limit (RPM=2) -> 429
        r3 = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY},
            json={"message": "notebook_privado_turno_3"},
        )

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429
    assert int(r3.headers["Retry-After"]) >= 1
    assert r3.json()["error"]["code"] == "rate_limit_exceeded"
    assert extractor.calls[1]["conversation_history"] == []
    assert disk_writes == []
    assert external_connects == []

    logs_raw = log_stream.getvalue()
    for line in logs_raw.strip().splitlines():
        payload = json.loads(line)
        extra_keys = set(payload.keys()) - {"timestamp", "level", "logger", "event"}
        assert extra_keys.issubset(ALLOWED_LOG_FIELDS)
    for secret_or_pii in (
        _API_KEY,
        _SENTINEL_TITLE,
        _SENTINEL_URL,
        _SENTINEL_SELLER,
        "notebook_privado_turno_1",
        "notebook_privado_turno_2",
    ):
        assert secret_or_pii not in logs_raw

    # Tracing externo proibido -> 503
    for var in TRACING_ENV_VARS[:1]:
        monkeypatch.setenv(var, "true")
    with TestClient(app) as client:
        traced = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY},
            json={"message": "notebook"},
        )
    assert traced.status_code == 503
    assert traced.json()["error"]["code"] == "policy_violation"


def test_chat_concurrency_isolation_and_openapi_contract() -> None:
    app, _, _, _ = _build_chat_app(backend_mode="slow")
    with TestClient(app) as client:
        spec_resp = client.get("/openapi.json")
        assert spec_resp.status_code == 200
        spec = spec_resp.json()
        assert "/v1/chat" in spec["paths"]
        assert "ChatRequest" in spec["components"]["schemas"]
        assert "ChatResponse" in spec["components"]["schemas"]
        assert "APIKeyHeader" in spec["components"]["securitySchemes"]
        assert _API_KEY not in spec_resp.text

        def _worker(idx: int) -> tuple[str, str]:
            is_monitor = idx % 2 == 0
            category = "monitor" if is_monitor else "notebook"
            msg = f"Quero um {category} cliente_{idx}"
            resp = client.post(
                "/v1/chat",
                headers={"X-API-Key": _API_KEY, "X-Request-ID": f"req-conc-{idx}"},
                json={
                    "message": msg,
                    "conversation_history": [{"role": "user", "content": f"Busco {category}"}],
                },
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["request_id"] == f"req-conc-{idx}"
            return category, body["search_results"]["detected_category"]

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(_worker, range(12)))

    assert all(expected == actual for expected, actual in results)
