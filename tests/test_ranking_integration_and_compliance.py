"""End-to-end integration, offline benchmark, and architectural compliance tests for Stage 6."""

from __future__ import annotations

import ast
import logging
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

from buscai.observability.logging import ALLOWED_LOG_FIELDS
from buscai.pricing import CurrencyConverter, FixedEnvExchangeRateProvider
from buscai.ranking.pipeline import RankingPipeline
from buscai.search.models import Candidate, SearchResponse, SellerInfo, ShippingInfo
from buscai.search.query_builder import QueryBuilder
from buscai.semantic.embeddings import DeterministicStubEmbeddingProvider, EmbeddingService
from buscai.semantic.pipeline import SemanticSearchProcessor
from buscai.validation.models import (
    BudgetEvaluation,
    CandidateValidationResult,
    CriterionEvaluation,
    EnrichmentSummary,
    ValidationPipelineResult,
)
from buscai.validation.pipeline import ValidationPipeline
from scripts.benchmark_ranking import run_benchmark
from userintent.schema import PriceConstraint, UserIntent


def test_full_offline_pipeline_intent_to_ranking(caplog) -> None:
    intent = UserIntent(
        category="notebook",
        profile="study",
        price=PriceConstraint(max_price=5000.0, currency="BRL", is_hard_constraint=True),
        requirements=["16GB RAM"],
        query="notebook leve para estudos até 5000 reais com 16gb ram",
    )

    converter = CurrencyConverter(FixedEnvExchangeRateProvider(rate=Decimal("5.00")))
    plan = QueryBuilder(currency_converter=converter).build(intent)
    assert plan.canonical_category == "notebook"
    assert len(plan.queries) >= 1

    c_best = Candidate(
        item_id="e2e-1",
        source="dummyjson",
        title="Notebook Lenovo Ultrabook 13.3 16GB RAM SSD 512GB",
        price=Decimal("700.00"),
        currency="USD",
        condition="New",
        categories=("notebook",),
        aspects={"RAM Size": ("16 GB",), "SSD Capacity": ("512 GB",), "Weight": ("1.25 kg",)},
        seller=SellerInfo(username="pii-seller-name", feedback_percentage=99.1, feedback_score=1500),
        shipping=ShippingInfo(free_shipping=True, estimated_max_days=4),
    )
    c_rejected = Candidate(
        item_id="e2e-2",
        source="dummyjson",
        title="Notebook Caro 16GB RAM SSD 512GB",
        price=Decimal("1500.00"),
        currency="USD",
        condition="New",
        categories=("notebook",),
        aspects={"RAM Size": ("16 GB",), "SSD Capacity": ("512 GB",)},
        seller=SellerInfo(username="seller-expensive", feedback_percentage=98.0, feedback_score=800),
        shipping=ShippingInfo(free_shipping=True, estimated_max_days=4),
    )

    search_resp = SearchResponse(
        query=plan.primary_query,
        candidates=(c_best, c_rejected),
        total_available=2,
        limit=plan.primary_query.limit if plan.primary_query is not None else 30,
        provider="dummyjson",
    )

    emb_service = EmbeddingService(DeterministicStubEmbeddingProvider())
    sem_res = SemanticSearchProcessor(emb_service).process(
        intent.query,
        (search_resp,),
        intent=intent,
        category_hint="notebook",
        request_id="req-e2e-stage6",
    )

    val_res = ValidationPipeline(currency_converter=converter).run(
        intent,
        sem_res,
        profile_id="study",
        request_id="req-e2e-stage6",
    )

    pipeline = RankingPipeline(converter=converter)
    with caplog.at_level(logging.INFO, logger="buscai.ranking"):
        rank_res = pipeline.run(
            val_res,
            semantic_result=sem_res,
            profile_id="study",
            category_hint="notebook",
            request_id="req-e2e-stage6",
        )

    assert rank_res.unique_products_count == 1
    assert rank_res.rejected_count == 1
    assert rank_res.ranked_groups[0].representative_offer.item_id == "e2e-1"
    assert rank_res.rejected_candidates[0].item_id == "e2e-2"

    for rec in caplog.records:
        msg = rec.getMessage()
        assert "pii-seller-name" not in msg
        assert "Notebook Lenovo" not in msg
        if hasattr(rec, "operational_fields"):
            assert set(rec.operational_fields.keys()).issubset(ALLOWED_LOG_FIELDS)


def test_ranking_pipeline_end_to_end_and_sanitized_logging(caplog) -> None:
    cand = Candidate(
        item_id="int-1",
        source="dummy",
        title="Monitor LG 27 4K IPS",
        price=Decimal("400.00"),
        currency="USD",
        condition="New",
        categories=("monitor",),
        seller=SellerInfo(username="secret-seller-pii", feedback_percentage=99.0, feedback_score=1200),
        shipping=ShippingInfo(free_shipping=True, estimated_max_days=4),
    )
    val = CandidateValidationResult(
        candidate=cand,
        eligible=True,
        strictly_verified=True,
        needs_enrichment=False,
        detected_category="monitor",
        evaluations=(
            CriterionEvaluation(
                criterion_id="resolution",
                criterion_label="resolution",
                priority="soft",
                status="satisfied",
                evidence="4K",
                source_field="title",
            ),
        ),
        budget_evaluation=BudgetEvaluation(
            status="satisfied",
            priority="hard",
            budget_currency="BRL",
            max_price=Decimal("3000.00"),
            total_price_in_budget_currency=Decimal("2000.00"),
            shipping_known=True,
        ),
        soft_satisfied_count=1,
    )
    val_res = ValidationPipelineResult(
        eligible_candidates=(val,),
        all_validations=(val,),
        enrichment=EnrichmentSummary(),
        total_candidates_evaluated=1,
    )

    pipeline = RankingPipeline()
    with caplog.at_level(logging.INFO, logger="buscai.ranking"):
        result = pipeline.run(
            val_res,
            profile_id="professional_use",
            category_hint="monitor",
            request_id="req-stage6-test",
        )

    assert result.unique_products_count == 1
    assert result.eligible_offers_count == 1
    assert result.active_profile_id == "professional_use"
    assert result.detected_category == "monitor"

    assert len(caplog.records) >= 1
    for rec in caplog.records:
        msg = rec.getMessage()
        assert "secret-seller-pii" not in msg
        assert "Monitor LG" not in msg
        if hasattr(rec, "operational_fields"):
            assert set(rec.operational_fields.keys()).issubset(ALLOWED_LOG_FIELDS)


def test_offline_synthetic_benchmark_runs_cleanly() -> None:
    metrics_on = run_benchmark(enable_semantic_score=True)
    metrics_off = run_benchmark(enable_semantic_score=False)

    assert metrics_on["dataset_origin"] == "synthetic_offline_stage6"
    assert metrics_on["scenarios_evaluated"] == 6
    assert metrics_on["hard_constraint_compliance_rate"] == 1.0
    assert metrics_on["offer_preservation_rate"] == 1.0
    assert metrics_on["ndcg_at_3"] >= 0.9
    assert metrics_off["hard_constraint_compliance_rate"] == 1.0
    assert metrics_off["offer_preservation_rate"] == 1.0


def test_stage6_ranking_has_zero_forbidden_imports_or_network_or_file_writes() -> None:
    ranking_dir = Path(__file__).resolve().parents[1] / "src" / "buscai" / "ranking"
    forbidden_modules = {
        "sqlite3",
        "sqlalchemy",
        "redis",
        "diskcache",
        "shelve",
        "pickle",
        "fastapi",
        "starlette",
        "langgraph",
        "langchain",
        "google.genai",
        "httpx",
        "requests",
        "urllib.request",
    }

    for py_file in ranking_dir.glob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not any(
                        alias.name == f or alias.name.startswith(f"{f}.")
                        for f in forbidden_modules
                    ), f"Forbidden import {alias.name} in {py_file.name}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not any(
                    node.module == f or node.module.startswith(f"{f}.")
                    for f in forbidden_modules
                ), f"Forbidden import-from {node.module} in {py_file.name}"

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
    metrics = run_benchmark(enable_semantic_score=True)
    assert metrics["scenarios_evaluated"] == 6
    assert violations == []
