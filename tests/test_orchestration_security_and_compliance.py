"""Security, zero-persistence, zero-tracing, and legacy compatibility tests for Stage 7."""

from __future__ import annotations

import ast
import logging
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from buscai.agent.search_flow import run_search_flow
from buscai.observability.logging import ALLOWED_LOG_FIELDS
from buscai.orchestration import (
    TRACING_ENV_VARS,
    OrchestrationDependencies,
    SearchOrchestrator,
    TracingPolicyViolationError,
    ensure_tracing_disabled,
)
from buscai.pricing import CurrencyConverter, FixedEnvExchangeRateProvider
from buscai.ranking.pipeline import RankingPipeline
from buscai.search.models import Candidate, SearchQuery, SearchResponse, SellerInfo, ShippingInfo
from buscai.search.query_builder import QueryBuilder
from buscai.search.quota import QuotaGuard, RequestCallBudget
from buscai.semantic.embeddings import DeterministicStubEmbeddingProvider, EmbeddingService
from buscai.semantic.pipeline import SemanticSearchProcessor
from buscai.validation.pipeline import ValidationPipeline
from userintent.schema import UserIntent


class _SafeExtractor:
    def extract(self, user_text: str) -> UserIntent:
        return UserIntent(category="monitor", query=user_text)


class _SafeBackend:
    def search(
        self,
        query: SearchQuery,
        *,
        budget: RequestCallBudget | None = None,
        request_id: str | None = None,
    ) -> SearchResponse:
        cand = Candidate(
            item_id="sec-1",
            source="synthetic",
            title="TOP_SECRET_PRODUCT_TITLE_4K",
            price=Decimal("399.00"),
            currency="USD",
            condition="New",
            categories=("monitor",),
            item_web_url="https://ebay.com/itm/secret-item-123",
            seller=SellerInfo(username="secret-pii-seller", feedback_percentage=99.0, feedback_score=500),
            shipping=ShippingInfo(free_shipping=True, estimated_max_days=3),
        )
        return SearchResponse(query=query, candidates=(cand,), total_available=1, limit=query.limit, provider="synthetic")

    def get_item_details(self, item_id: str, **kwargs) -> Candidate | None:
        return None


def _build_orch() -> SearchOrchestrator:
    converter = CurrencyConverter(FixedEnvExchangeRateProvider(rate=Decimal("5.00")))
    deps = OrchestrationDependencies(
        extractor=_SafeExtractor(),
        query_builder=QueryBuilder(currency_converter=converter),
        search_backend=_SafeBackend(),
        semantic_processor=SemanticSearchProcessor(
            EmbeddingService(DeterministicStubEmbeddingProvider())
        ),
        validation_pipeline=ValidationPipeline(currency_converter=converter),
        ranking_pipeline=RankingPipeline(converter=converter),
        quota_guard=QuotaGuard(),
    )
    return SearchOrchestrator(deps)


def test_compiled_graph_has_no_checkpointer_or_store_and_blocks_tracing_env(monkeypatch) -> None:
    orch = _build_orch()
    assert orch.compiled_graph.checkpointer is None
    assert getattr(orch.compiled_graph, "store", None) is None

    for var in TRACING_ENV_VARS:
        monkeypatch.setenv(var, "true")
        with pytest.raises(TracingPolicyViolationError):
            ensure_tracing_disabled()
        with pytest.raises(TracingPolicyViolationError):
            orch.run("monitor 4k")
        monkeypatch.delenv(var, raising=False)


def test_orchestration_ast_forbids_checkpointers_tracing_db_http_or_langgraph_persistence() -> None:
    orch_dir = Path(__file__).resolve().parents[1] / "src" / "buscai" / "orchestration"
    forbidden_modules = {
        "langgraph.checkpoint",
        "langsmith",
        "fastapi",
        "starlette",
        "sqlite3",
        "sqlalchemy",
        "redis",
        "diskcache",
        "shelve",
        "pickle",
        "httpx",
        "requests",
        "urllib.request",
    }
    forbidden_names = {"MemorySaver", "InMemorySaver", "SqliteSaver", "PostgresSaver", "tracing_v2_enabled"}

    for py_file in orch_dir.glob("*.py"):
        src = py_file.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not any(alias.name == f or alias.name.startswith(f"{f}.") for f in forbidden_modules)
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not any(node.module == f or node.module.startswith(f"{f}.") for f in forbidden_modules)
            elif isinstance(node, ast.Name):
                assert node.id not in forbidden_names


def test_orchestration_runtime_has_zero_network_disk_writes_and_sanitized_logs(caplog) -> None:
    orch = _build_orch()
    temp_root = Path(tempfile.gettempdir()).resolve()
    violations: list[str] = []

    def _audit(event: str, args: tuple) -> None:
        if event in ("socket.connect", "urllib.Request"):
            violations.append(f"network:{event}")
        elif event == "open" and len(args) >= 2:
            mode = str(args[1])
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                target = Path(str(args[0])).resolve()
                if not str(target).startswith(str(temp_root)):
                    violations.append(f"write:{target}")

    sys.addaudithook(_audit)
    with caplog.at_level(logging.INFO, logger="buscai.orchestration"):
        res = orch.run("monitor 4k", request_id="req-sec-1")

    assert res.status.value == "success"
    assert violations == []

    for rec in caplog.records:
        msg = rec.getMessage()
        assert "TOP_SECRET_PRODUCT_TITLE_4K" not in msg
        assert "secret-pii-seller" not in msg
        assert "secret-item-123" not in msg
        if hasattr(rec, "operational_fields"):
            assert set(rec.operational_fields.keys()).issubset(ALLOWED_LOG_FIELDS)


def test_legacy_search_flow_remains_compatible() -> None:
    flow_res = run_search_flow(
        "preciso de algo",
        extractor=type(
            "_E",
            (),
            {
                "extract": staticmethod(
                    lambda _: UserIntent(
                        category=None,
                        clarification_needed=True,
                        clarification_question="Qual categoria?",
                        query="preciso de algo",
                    )
                )
            },
        )(),
    )
    assert flow_res.clarification_question == "Qual categoria?"
    assert flow_res.search is None
