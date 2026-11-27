"""Testes integrados ponta a ponta e auditoria de conformidade/privacidade da Etapa 5."""

from __future__ import annotations

import ast
import io
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from buscai.config import load_settings
from buscai.observability import configure_logging
from buscai.pricing import CurrencyConverter, FixedEnvExchangeRateProvider
from buscai.search import (
    EbayLiveBackend,
    EbayOAuthClient,
    QueryBuilder,
    QuotaGuard,
    build_search_plan_from_text,
)
from buscai.semantic import DeterministicStubEmbeddingProvider, SemanticSearchProcessor
from buscai.validation import RelaxationConfig, ValidationPipeline
from userintent import IntentExtractor

ROOT = Path(__file__).resolve().parents[1]
VALIDATION_DIR = ROOT / "src" / "buscai" / "validation"


def test_stage5_end_to_end_with_enrichment_relaxation_and_zero_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    temp_dir = Path(tempfile.gettempdir())
    before_tmp = {p.name for p in temp_dir.iterdir()}

    write_events: list[tuple[str, tuple[object, ...]]] = []

    def _audit(event: str, args: tuple[object, ...]) -> None:
        if event == "open" and len(args) >= 2:
            mode = str(args[1]) if args[1] is not None else ""
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                write_events.append((event, args))
        elif event in {"os.mkdir", "os.makedirs", "os.rename", "os.replace"}:
            write_events.append((event, args))

    sys.addaudithook(_audit)

    settings = load_settings(
        environ={
            "GEMINI_API_KEY": "AIzaFakeGeminiStage5Secret",
            "EBAY_CLIENT_ID": "fake_ebay_cid",
            "EBAY_CLIENT_SECRET": "fake_ebay_csec",
            "USD_BRL_RATE": "5.50",
        }
    )
    llm_payload = {
        "category": "notebook",
        "price": {
            "min_price": None,
            "max_price": 5500,
            "min_inclusive": True,
            "max_inclusive": True,
            "is_hard_constraint": True,
            "currency": "BRL",
        },
        "brands": ["Lenovo"],
        "models": ["ThinkPad"],
        "excluded_brands": [],
        "use_cases": ["uso profissional"],
        "profile_context": [],
        "required_features": ["16 GB de RAM"],
        "preferred_features": ["teclado retroiluminado"],
        "excluded_features": [],
        "condition": "novo",
        "query": "notebook Lenovo ThinkPad 16GB RAM",
        "clarification_needed": False,
        "clarification_question": None,
    }
    stub_llm = SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda **_k: SimpleNamespace(parsed=None, text=json.dumps(llm_payload)))
    )
    extractor = IntentExtractor.from_settings(settings, client=stub_llm)
    qb = QueryBuilder.from_settings(settings)

    def ebay_handler(request: httpx.Request) -> httpx.Response:
        if "/item_summary/search" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "total": 2,
                    "itemSummaries": [
                        {
                            "itemId": "v1|tp-seller-a|0",
                            "title": "SENTINEL_TITLE_A Lenovo ThinkPad X1 Laptop",
                            "price": {"value": "850.00", "currency": "USD"},
                            "condition": "NEW",
                            "brand": "Lenovo",
                            "model": "ThinkPad X1",
                            "itemWebUrl": "https://ebay.example/itm/SENTINEL_URL_A",
                            "seller": {"username": "SENTINEL_SELLER_A"},
                            "shippingOptions": [{"shippingCost": {"value": "0.00", "currency": "USD"}}],
                        },
                        {
                            "itemId": "v1|tp-seller-b|0",
                            "title": "SENTINEL_TITLE_B Lenovo ThinkPad X1 Laptop",
                            "price": {"value": "820.00", "currency": "USD"},
                            "condition": "NEW",
                            "brand": "Lenovo",
                            "model": "ThinkPad X1",
                            "itemWebUrl": "https://ebay.example/itm/SENTINEL_URL_B",
                            "seller": {"username": "SENTINEL_SELLER_B"},
                            "shippingOptions": [{"shippingCost": {"value": "15.00", "currency": "USD"}}],
                        },
                    ],
                },
            )
        # getItem devolve RAM Size = 16 GB para resolver unknown
        item_id = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={
                "itemId": "v1|tp-seller-a|0" if "seller-a" in item_id else "v1|tp-seller-b|0",
                "title": "Lenovo ThinkPad X1 Laptop",
                "price": {"value": "850.00", "currency": "USD"},
                "localizedAspects": [
                    {"name": "RAM Size", "value": "16 GB"},
                    {"name": "SSD Capacity", "value": "512 GB"},
                ],
            },
        )

    log_stream = io.StringIO()
    logger = configure_logging("INFO", stream=log_stream, logger_name="buscai.test.stage5.e2e")
    oauth = EbayOAuthClient.from_settings(
        settings,
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})),
        logger=logger,
    )
    guard = QuotaGuard(daily_limit=5000)
    backend = EbayLiveBackend(
        oauth_client=oauth,
        quota_guard=guard,
        transport=httpx.MockTransport(ebay_handler),
        logger=logger,
    )

    try:
        plan_res = build_search_plan_from_text(
            "Quero notebook Lenovo ThinkPad com 16GB RAM até R$ 5.500",
            extractor=extractor,
            query_builder=qb,
            request_id="req-s5-1",
        )
        budget = guard.new_request_budget(max_search_calls=4, max_get_item_calls=4)
        responses = [backend.search(q, budget=budget, request_id="req-s5-1") for q in plan_res.search_queries]

        sem_proc = SemanticSearchProcessor.from_settings(
            settings,
            provider=DeterministicStubEmbeddingProvider(),
            logger=logger,
        )
        sem_res = sem_proc.process(
            "notebook Lenovo ThinkPad",
            responses,
            intent=plan_res.intent,
            category_hint="notebook",
            request_id="req-s5-1",
        )

        val_pipe = ValidationPipeline(
            currency_converter=CurrencyConverter(FixedEnvExchangeRateProvider.from_settings(settings)),
            search_backend=backend,
            query_builder=qb,
            relaxation_config=RelaxationConfig(min_eligible_results=3, max_relaxation_steps=1, max_extra_search_calls=1),
            logger=logger,
        )

        before_writes = len(write_events)
        val_res = val_pipe.run(plan_res.intent, sem_res, budget=budget, request_id="req-s5-1")
        after_writes = len(write_events)

        assert after_writes == before_writes, f"Escrita inesperada em disco: {write_events[before_writes:]}"
        assert val_res.active_profile_id == "professional_use"
        assert len(val_res.enrichment.enriched_item_ids) == 2
        assert len(val_res.eligible_candidates) == 2
        assert all(v.strictly_verified for v in val_res.eligible_candidates)
        assert val_res.relaxed is True
        assert val_res.hard_constraints_preserved is True

        # Logs sanitizados e sem sentinelas de anúncio ou credenciais
        logs_raw = log_stream.getvalue()
        assert "validation_pipeline_completed" in logs_raw
        for secret in (
            "SENTINEL_TITLE_A",
            "SENTINEL_TITLE_B",
            "SENTINEL_URL_A",
            "SENTINEL_SELLER_A",
            "AIzaFakeGeminiStage5Secret",
            "fake_ebay_csec",
        ):
            assert secret not in logs_raw

        assert list(tmp_path.rglob("*")) == []
        assert {p.name for p in temp_dir.iterdir()} == before_tmp
    finally:
        backend.close()
        oauth.close()


def test_validation_modules_have_no_persistence_or_stage6_to_10_dependencies() -> None:
    forbidden_roots = {
        "sqlite3",
        "pickle",
        "shelve",
        "tempfile",
        "fastapi",
        "starlette",
        "langgraph",
        "langchain",
        "chromadb",
        "faiss",
        "sqlalchemy",
    }
    forbidden_calls = {"open", "write_text", "write_bytes", "save", "savez", "memmap"}

    offenders: list[str] = []
    for py_file in sorted(VALIDATION_DIR.glob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in forbidden_roots:
                        offenders.append(f"{py_file.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in forbidden_roots:
                    offenders.append(f"{py_file.name}: from {node.module}")
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in forbidden_calls:
                    offenders.append(f"{py_file.name}: call {func.id}()")
                elif isinstance(func, ast.Attribute) and func.attr in forbidden_calls:
                    offenders.append(f"{py_file.name}: call .{func.attr}()")

    assert offenders == [], f"Violações em src/buscai/validation/: {offenders}"
