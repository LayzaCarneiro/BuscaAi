"""Testes integrados com mocks da Etapa 4 (PT-BR -> Intent -> QueryBuilder -> eBay -> Semântica)."""

from __future__ import annotations

import json
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
from scripts.benchmark_embeddings import main as benchmark_main
from userintent import IntentExtractor


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

        # Duplicatas do iPhone 15 256GB agrupadas (2 removidos: ip256-b e ip256-a repetido),
        # mas iPhone 15 128GB e iPhone 15 Pro 256GB preservados como produtos distintos/semelhantes.
        assert sem_res.input_count == 5
        assert sem_res.removed_duplicates_count == 2
        assert sem_res.output_count == 3
        kept_ids = {c.item_id for c in sem_res.candidates}
        assert "v1|ip128|0" in kept_ids
        assert "v1|ip256-pro|0" in kept_ids
        assert ("v1|ip256-a|0" in kept_ids) or ("v1|ip256-b|0" in kept_ids)
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
