"""Testes integrados com mocks da Etapa 4 (PT-BR -> Intent -> QueryBuilder -> eBay -> Semântica)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx

from buscai.config import load_settings
from buscai.search import (
    EbayLiveBackend,
    EbayOAuthClient,
    QueryBuilder,
    QuotaGuard,
    build_search_plan_from_text,
)
from buscai.semantic import (
    DeterministicStubEmbeddingProvider,
    SemanticSearchProcessor,
    merge_search_responses,
)
from scripts.benchmark_embeddings import evaluate_model as benchmark_evaluate_model
from scripts.benchmark_embeddings import main as benchmark_main
from userintent import IntentExtractor

ROOT = Path(__file__).resolve().parents[1]


class _StubModels:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def generate_content(self, **_kwargs):
        return SimpleNamespace(parsed=None, text=json.dumps(self._payload))


class _StubGenAI:
    def __init__(self, payload: dict[str, object]) -> None:
        self.models = _StubModels(payload)


def test_stage4_integrated_mocked_flow_dedup_and_preserves_variants() -> None:
    settings = load_settings(
        environ={
            "GEMINI_API_KEY": "fake_gem_key",
            "EBAY_CLIENT_ID": "fake_ebay_cid",
            "EBAY_CLIENT_SECRET": "fake_ebay_csec",
            "USD_BRL_RATE": "5.50",
        }
    )

    llm_payload = {
        "category": "smartphone",
        "price": {
            "min_price": None,
            "max_price": None,
            "min_inclusive": True,
            "max_inclusive": True,
            "is_hard_constraint": False,
            "currency": None,
        },
        "brands": ["Apple"],
        "models": ["iPhone"],
        "excluded_brands": [],
        "use_cases": [],
        "profile_context": [],
        "required_features": ["pelo menos 256 GB de armazenamento"],
        "preferred_features": [],
        "excluded_features": [],
        "condition": None,
        "query": "iPhone com pelo menos 256 GB de armazenamento",
        "clarification_needed": False,
        "clarification_question": None,
    }
    extractor = IntentExtractor.from_settings(settings, client=_StubGenAI(llm_payload))
    query_builder = QueryBuilder.from_settings(settings)

    def browse_handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("q", "")
        if "256GB" in q:
            items = [
                {
                    "itemId": "v1|ip256-a|0",
                    "title": "Apple iPhone 15 256GB Unlocked 5G Smartphone",
                    "price": {"value": "799.00", "currency": "USD"},
                    "brand": "Apple",
                    "model": "iPhone 15",
                },
                {
                    "itemId": "v1|ip256-b|0",
                    "title": "iPhone 15 Apple 256GB 5G Smartphone Unlocked Black",
                    "price": {"value": "789.00", "currency": "USD"},
                    "brand": "Apple",
                    "model": "iPhone 15",
                },
            ]
        else:
            items = [
                # Mesmo itemId retornado também pela query alternativa + variante de 128 GB e Pro
                {
                    "itemId": "v1|ip256-a|0",
                    "title": "Apple iPhone 15 256GB Unlocked 5G Smartphone",
                    "price": {"value": "799.00", "currency": "USD"},
                    "brand": "Apple",
                    "model": "iPhone 15",
                },
                {
                    "itemId": "v1|ip128|0",
                    "title": "Apple iPhone 15 128GB Unlocked 5G Smartphone",
                    "price": {"value": "699.00", "currency": "USD"},
                    "brand": "Apple",
                    "model": "iPhone 15",
                },
                {
                    "itemId": "v1|ip256-pro|0",
                    "title": "Apple iPhone 15 Pro 256GB Unlocked 5G Smartphone",
                    "price": {"value": "949.00", "currency": "USD"},
                    "brand": "Apple",
                    "model": "iPhone 15 Pro",
                },
            ]
        return httpx.Response(200, json={"total": len(items), "itemSummaries": items})

    oauth = EbayOAuthClient.from_settings(
        settings,
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})),
    )
    guard = QuotaGuard(daily_limit=settings.ebay_daily_quota_limit, default_max_search_per_request=2)
    backend = EbayLiveBackend(
        oauth_client=oauth,
        quota_guard=guard,
        transport=httpx.MockTransport(browse_handler),
    )

    try:
        user_text = "Procuro um iPhone com pelo menos 256 GB de armazenamento."
        plan_res = build_search_plan_from_text(
            user_text,
            extractor=extractor,
            query_builder=query_builder,
            request_id="req-s4-int",
        )
        assert len(plan_res.search_queries) == 2

        budget = guard.new_request_budget(max_search_calls=2, max_get_item_calls=0)
        responses = [
            backend.search(query, budget=budget, request_id="req-s4-int")
            for query in plan_res.search_queries
        ]
        merged = merge_search_responses(responses)
        assert len(merged) == 5

        processor = SemanticSearchProcessor.from_settings(
            settings,
            provider=DeterministicStubEmbeddingProvider(),
        )
        sem_res = processor.process(
            user_text,
            responses,
            category_hint=plan_res.plan.canonical_category,
            request_id="req-s4-int",
        )

        # Duplicata exata do mesmo item_id v1|ip256-a|0 (vinda da query alternativa) é colapsada (1 removido),
        # mas as 2 ofertas distintas do iPhone 15 256GB (ip256-a e ip256-b) permanecem disponíveis (A1)
        # agrupadas em um único DuplicateGroup, junto com iPhone 15 128GB e iPhone 15 Pro 256GB.
        assert sem_res.input_count == 5
        assert sem_res.removed_duplicates_count == 1
        assert sem_res.output_count == 4
        assert sem_res.unique_products_count == 3
        kept_ids = {c.item_id for c in sem_res.candidates}
        assert kept_ids == {"v1|ip256-a|0", "v1|ip256-b|0", "v1|ip128|0", "v1|ip256-pro|0"}
        assert len(sem_res.product_groups) == 3
        assert any(set(g.duplicate_item_ids) == {"v1|ip256-a|0", "v1|ip256-b|0"} for g in sem_res.product_groups)
        assert sem_res.scored_candidates[0].semantic_score >= sem_res.scored_candidates[-1].semantic_score
    finally:
        backend.close()
        oauth.close()


def test_benchmark_embeddings_script_runs_offline_and_prints_only_aggregates(capsys) -> None:
    code = benchmark_main(["--backend", "stub"])
    assert code == 0
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.strip().splitlines()]
    assert len(lines) == 2
    for row in lines:
        assert row["backend"] == "stub"
        assert row["dimension"] == 384
        assert row["false_dedup_rate"] == 0.0
        assert row["mrr"] == 1.0
    assert "ThinkPad" not in out
    assert "iPhone" not in out


def test_semantic_v2_dataset_integrity_coverage_and_split_separation() -> None:
    cal_path = ROOT / "data" / "eval_semantic_v2_calibration.jsonl"
    eval_path = ROOT / "data" / "eval_semantic_v2_evaluation.jsonl"
    assert cal_path.exists()
    assert eval_path.exists()

    cal_rows = [json.loads(line) for line in cal_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    eval_rows = [json.loads(line) for line in eval_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    assert all(row["synthetic"] is True and row["split"] == "calibration" for row in cal_rows)
    assert all(row["synthetic"] is True and row["split"] == "evaluation" for row in eval_rows)

    cal_ret = [r for r in cal_rows if r["task"] == "retrieval"]
    eval_ret = [r for r in eval_rows if r["task"] == "retrieval"]
    cal_ded = [r for r in cal_rows if r["task"] == "deduplication"]
    eval_ded = [r for r in eval_rows if r["task"] == "deduplication"]

    assert len(cal_ret) + len(eval_ret) >= 30
    assert len(cal_ded) + len(eval_ded) >= 100

    assert {r["query_pt"].casefold() for r in cal_ret}.isdisjoint({r["query_pt"].casefold() for r in eval_ret})
    cal_pairs = {(r["left"]["title"].casefold(), r["right"]["title"].casefold()) for r in cal_ded}
    eval_pairs = {(r["left"]["title"].casefold(), r["right"]["title"].casefold()) for r in eval_ded}
    assert cal_pairs.isdisjoint(eval_pairs)

    all_rows = cal_rows + eval_rows
    raw_text = cal_path.read_text(encoding="utf-8") + eval_path.read_text(encoding="utf-8")
    assert "ebay.com/itm" not in raw_text.casefold()
    assert "v1|" not in raw_text

    all_langs = {lang for r in all_rows for lang in r["languages"]}
    assert {"pt-BR", "en-US"}.issubset(all_langs)

    all_dedup = cal_ded + eval_ded
    labels = {r["expected_label"] for r in all_dedup}
    assert {"probable_duplicate", "similar_product", "different_product"} == labels

    tags = " ".join(r["case_tag"] for r in all_dedup)
    for required_variant in ("mini", "plus", "pro", "max", "ultra", "se", "fe"):
        assert f"variant_{required_variant}" in tags

    conditions_seen = {
        side.get("condition")
        for r in all_dedup
        for side in (r["left"], r["right"])
        if side.get("condition") is not None
    }
    assert {"New", "Used", "Refurbished", "Open Box"}.issubset(conditions_seen)
    assert any("diff_ram" in r["case_tag"] for r in all_dedup)
    assert any("diff_storage" in r["case_tag"] for r in all_dedup)
    assert any("incompat_" in r["case_tag"] for r in all_dedup)
    assert any("incomplete_info" in r["case_tag"] for r in all_dedup)
    assert any("same_brand_diff_model" in r["case_tag"] for r in all_dedup)
    assert any("equiv_" in r["case_tag"] for r in all_dedup)

    cal_summary = benchmark_evaluate_model(cal_rows, backend="stub", model_name="intfloat/multilingual-e5-small")
    eval_summary = benchmark_evaluate_model(eval_rows, backend="stub", model_name="intfloat/multilingual-e5-small")
    assert cal_summary["retrieval_cases"] == len(cal_ret)
    assert eval_summary["dedup_cases"] == len(eval_ded)
