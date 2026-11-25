"""Benchmark sintético End-to-End HTTP da Etapa 9 (T9.3).

Percorre a pilha real:
HTTP Request -> FastAPI -> X-API-Key/RateLimit -> SearchOrchestrator -> IntentExtractor(stub)
-> QueryBuilder -> EbayLiveBackend(MockTransport) -> SemanticSearchProcessor(Stub)
-> ValidationPipeline -> RankingPipeline -> HTTP Response.

Nota: usa exclusivamente dados e fronteiras sintéticas offline, sem chamadas reais
à eBay ou ao Gemini e sem representar desempenho de produção.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from buscai.api import InMemoryRateLimiter, create_app  # noqa: E402
from buscai.config import load_settings  # noqa: E402
from buscai.orchestration import OrchestrationDependencies, SearchOrchestrator  # noqa: E402
from buscai.pricing import CurrencyConverter, FixedEnvExchangeRateProvider  # noqa: E402
from buscai.ranking.pipeline import RankingPipeline  # noqa: E402
from buscai.search import EbayLiveBackend, EbayOAuthClient, QuotaGuard  # noqa: E402
from buscai.search.query_builder import QueryBuilder  # noqa: E402
from buscai.semantic.embeddings import DeterministicStubEmbeddingProvider, EmbeddingService  # noqa: E402
from buscai.semantic.pipeline import SemanticSearchProcessor  # noqa: E402
from buscai.validation.pipeline import ValidationPipeline  # noqa: E402
from userintent import IntentExtractor, PriceConstraint, UserIntent  # noqa: E402

_BENCH_API_KEY = "buscai_bench_synthetic_key_01"


@dataclass(frozen=True)
class BenchmarkScenario:
    scenario_id: str
    payload: dict[str, Any]
    expected_http_status: int
    expected_api_status: str | None = None
    send_auth: bool = True
    rate_limit_probe: bool = False
    is_ranking_case: bool = False
    relevant_item_ids: tuple[str, ...] = ()
    relevance_grades: dict[str, int] | None = None
    expected_min_offers: int = 0
    expect_relaxed: bool = False


SCENARIOS: tuple[BenchmarkScenario, ...] = (
    BenchmarkScenario(
        scenario_id="1_notebook_estudo_orcamento",
        payload={"query": "notebook estudo 16gb ram", "budget_brl": 4000, "profile_id": "study", "limit": 5},
        expected_http_status=200,
        expected_api_status="ok",
        is_ranking_case=True,
        relevant_item_ids=("nb-1a", "nb-1b", "nb-2"),
        relevance_grades={"nb-1a": 3, "nb-2": 2},
        expected_min_offers=2,
    ),
    BenchmarkScenario(
        scenario_id="2_smartphone_armazenamento_minimo",
        payload={"query": "smartphone 256gb", "budget_brl": 4500, "profile_id": "best_value", "limit": 5},
        expected_http_status=200,
        expected_api_status="ok",
        is_ranking_case=True,
        relevant_item_ids=("sp-256",),
        relevance_grades={"sp-256": 3, "sp-128": 0},
        expected_min_offers=1,
    ),
    BenchmarkScenario(
        scenario_id="3_monitor_4k",
        payload={"query": "monitor 4k ips", "budget_brl": 4000, "profile_id": "professional_use", "limit": 5},
        expected_http_status=200,
        expected_api_status="ok",
        is_ranking_case=True,
        relevant_item_ids=("mon-4k",),
        relevance_grades={"mon-4k": 3, "mon-fhd": 1},
        expected_min_offers=1,
    ),
    BenchmarkScenario(
        scenario_id="4_headphones_bluetooth_anc",
        payload={"query": "fone bluetooth anc", "budget_brl": 2000, "profile_id": "everyday_use", "limit": 5},
        expected_http_status=200,
        expected_api_status="ok",
        is_ranking_case=True,
        relevant_item_ids=("hp-anc",),
        relevance_grades={"hp-anc": 3, "hp-wired": 1},
        expected_min_offers=1,
    ),
    BenchmarkScenario(
        scenario_id="5_preferencia_flexivel_relaxada",
        payload={"query": "notebook relaxado dell", "budget_brl": 4000, "profile_id": "best_value", "limit": 5},
        expected_http_status=200,
        expected_api_status="ok",
        is_ranking_case=True,
        relevant_item_ids=("nb-rel-1",),
        relevance_grades={"nb-rel-1": 3},
        expected_min_offers=1,
        expect_relaxed=True,
    ),
    BenchmarkScenario(
        scenario_id="6_busca_sem_resultados",
        payload={"query": "monitor inexistente vazio", "limit": 5},
        expected_http_status=200,
        expected_api_status="no_results",
    ),
    BenchmarkScenario(
        scenario_id="7_rejeitado_por_hard_constraint",
        payload={"query": "notebook estourado caro", "budget_brl": 1000, "limit": 5},
        expected_http_status=200,
        expected_api_status="all_rejected",
    ),
    BenchmarkScenario(
        scenario_id="8_falha_provedor",
        payload={"query": "notebook provedor fora", "limit": 5},
        expected_http_status=503,
    ),
    BenchmarkScenario(
        scenario_id="9_sem_autenticacao",
        payload={"query": "notebook estudo 16gb ram"},
        expected_http_status=401,
        send_auth=False,
    ),
    BenchmarkScenario(
        scenario_id="10_rate_limit_excedido",
        payload={"query": "notebook estudo 16gb ram"},
        expected_http_status=429,
        rate_limit_probe=True,
    ),
)


def _dcg(grades: list[int], k: int) -> float:
    return sum((2**g - 1) / math.log2(idx + 1) for idx, g in enumerate(grades[:k], start=1))


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * pct
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _synthetic_llm(*, contents: str, **_kwargs) -> object:
    low = contents.casefold()
    if "provedor fora" in low:
        intent = UserIntent(category="notebook", models=["ProviderDownModel"], query="notebook ProviderDownModel")
    elif "estourado" in low:
        intent = UserIntent(
            category="notebook",
            models=["ExpensiveModel"],
            price=PriceConstraint(max_price=1000, is_hard_constraint=True, currency="BRL"),
            query="notebook ExpensiveModel",
        )
    elif "relaxado" in low:
        intent = UserIntent(
            category="notebook",
            brands=["Dell"],
            preferred_features=["OLED", "16GB RAM"],
            query="notebook dell oled",
        )
    elif "inexistente" in low:
        intent = UserIntent(category="monitor", models=["NonExistentModel"], query="monitor NonExistentModel")
    elif "smartphone" in low:
        intent = UserIntent(
            category="smartphone",
            required_features=["256GB"],
            query="smartphone 256GB",
        )
    elif "monitor" in low:
        intent = UserIntent(
            category="monitor",
            required_features=["4K"],
            preferred_features=["IPS"],
            query="monitor 4K IPS",
        )
    elif "fone" in low:
        intent = UserIntent(
            category="fone de ouvido",
            required_features=["Bluetooth"],
            preferred_features=["ANC"],
            query="headphones Bluetooth ANC",
        )
    else:
        intent = UserIntent(
            category="notebook",
            required_features=["16GB RAM"],
            query="notebook 16GB RAM",
        )
    return SimpleNamespace(parsed=intent, text=None)


def _synthetic_browse(req: httpx.Request) -> httpx.Response:
    if "/item_summary/search" not in req.url.path:
        return httpx.Response(404)
    q = req.url.params.get("q", "").casefold()

    if "providerdownmodel" in q:
        return httpx.Response(503, json={"error": "synthetic_unavailable"})
    if "nonexistentmodel" in q:
        return httpx.Response(200, json={"total": 0, "limit": 30, "offset": 0, "itemSummaries": []})
    if "expensivemodel" in q:
        return httpx.Response(
            200,
            json={
                "total": 1,
                "limit": 30,
                "offset": 0,
                "itemSummaries": [
                    {
                        "itemId": "nb-exp",
                        "title": "ExpensiveModel High-End Laptop 32GB",
                        "price": {"value": "1900.00", "currency": "USD"},
                        "condition": "NEW",
                        "shippingOptions": [{"shippingCostType": "FIXED", "shippingCost": {"value": "0.00", "currency": "USD"}}],
                    }
                ],
            },
        )
    if "dell" in q or "oled" in q:
        if "oled" in q:
            return httpx.Response(200, json={"total": 0, "limit": 30, "offset": 0, "itemSummaries": []})
        return httpx.Response(
            200,
            json={
                "total": 1,
                "limit": 30,
                "offset": 0,
                "itemSummaries": [
                    {
                        "itemId": "nb-rel-1",
                        "title": "Dell Latitude Laptop 16GB RAM 512GB SSD",
                        "price": {"value": "520.00", "currency": "USD"},
                        "condition": "NEW",
                        "brand": "Dell",
                        "shippingOptions": [{"shippingCostType": "FIXED", "shippingCost": {"value": "0.00", "currency": "USD"}}],
                    }
                ],
            },
        )
    if "smartphone" in q:
        items = [
            {
                "itemId": "sp-256",
                "title": "Galaxy Smartphone 256GB 5G Unlocked",
                "price": {"value": "480.00", "currency": "USD"},
                "condition": "NEW",
                "shippingOptions": [{"shippingCostType": "FIXED", "shippingCost": {"value": "0.00", "currency": "USD"}}],
            },
            {
                "itemId": "sp-128",
                "title": "Galaxy Smartphone 128GB 5G Unlocked",
                "price": {"value": "350.00", "currency": "USD"},
                "condition": "NEW",
                "shippingOptions": [{"shippingCostType": "FIXED", "shippingCost": {"value": "0.00", "currency": "USD"}}],
            },
        ]
        return httpx.Response(200, json={"total": 2, "limit": 30, "offset": 0, "itemSummaries": items})
    if "monitor" in q:
        items = [
            {
                "itemId": "mon-4k",
                "title": "Dell UltraSharp 27 Monitor 4K UHD IPS",
                "price": {"value": "450.00", "currency": "USD"},
                "condition": "NEW",
                "shippingOptions": [{"shippingCostType": "FIXED", "shippingCost": {"value": "0.00", "currency": "USD"}}],
            },
            {
                "itemId": "mon-fhd",
                "title": "Budget 24 Monitor 1080p TN",
                "price": {"value": "160.00", "currency": "USD"},
                "condition": "USED",
                "shippingOptions": [{"shippingCostType": "FIXED", "shippingCost": {"value": "20.00", "currency": "USD"}}],
            },
        ]
        return httpx.Response(200, json={"total": 2, "limit": 30, "offset": 0, "itemSummaries": items})
    if "headphones" in q:
        items = [
            {
                "itemId": "hp-anc",
                "title": "Sony Wireless Bluetooth Headphones ANC",
                "price": {"value": "220.00", "currency": "USD"},
                "condition": "NEW",
                "shippingOptions": [{"shippingCostType": "FIXED", "shippingCost": {"value": "0.00", "currency": "USD"}}],
            },
            {
                "itemId": "hp-wired",
                "title": "Basic Wired Headphones 3.5mm",
                "price": {"value": "40.00", "currency": "USD"},
                "condition": "USED",
                "shippingOptions": [{"shippingCostType": "FIXED", "shippingCost": {"value": "10.00", "currency": "USD"}}],
            },
        ]
        return httpx.Response(200, json={"total": 2, "limit": 30, "offset": 0, "itemSummaries": items})

    items = [
        {
            "itemId": "nb-1a",
            "title": "Lenovo ThinkPad T14 Laptop 16GB RAM 512GB SSD",
            "price": {"value": "490.00", "currency": "USD"},
            "condition": "NEW",
            "brand": "Lenovo",
            "model": "ThinkPad T14",
            "shippingOptions": [{"shippingCostType": "FIXED", "shippingCost": {"value": "0.00", "currency": "USD"}}],
        },
        {
            "itemId": "nb-1b",
            "title": "Lenovo ThinkPad T14 Laptop 16GB RAM 512GB SSD",
            "price": {"value": "540.00", "currency": "USD"},
            "condition": "NEW",
            "brand": "Lenovo",
            "model": "ThinkPad T14",
            "shippingOptions": [{"shippingCostType": "FIXED", "shippingCost": {"value": "15.00", "currency": "USD"}}],
        },
        {
            "itemId": "nb-2",
            "title": "HP EliteBook Laptop 16GB RAM 256GB SSD",
            "price": {"value": "610.00", "currency": "USD"},
            "condition": "NEW",
            "brand": "HP",
            "shippingOptions": [{"shippingCostType": "FIXED", "shippingCost": {"value": "0.00", "currency": "USD"}}],
        },
    ]
    return httpx.Response(200, json={"total": 3, "limit": 30, "offset": 0, "itemSummaries": items})


def _build_benchmark_app(*, max_rpm: int = 10_000):
    oauth = EbayOAuthClient(
        client_id="bench_cid",
        client_secret="bench_csecret",
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(200, json={"access_token": "bench_tok", "expires_in": 7200})
        ),
    )
    quota = QuotaGuard(daily_limit=100_000)
    backend = EbayLiveBackend(
        oauth_client=oauth,
        quota_guard=quota,
        max_retries=0,
        transport=httpx.MockTransport(_synthetic_browse),
        sleep=lambda _: None,
    )
    extractor = IntentExtractor(
        model="gemini-3.5-flash-lite",
        client=SimpleNamespace(models=SimpleNamespace(generate_content=_synthetic_llm)),
        max_retries=0,
        sleep=lambda _: None,
    )
    converter = CurrencyConverter(
        FixedEnvExchangeRateProvider(rate=Decimal("5.50"), updated_at="2026-11-25", source="env_fixed_rate")
    )
    emb = EmbeddingService(DeterministicStubEmbeddingProvider())
    emb.embed_queries(["warmup"])

    deps = OrchestrationDependencies(
        extractor=extractor,
        query_builder=QueryBuilder(currency_converter=converter),
        search_backend=backend,
        semantic_processor=SemanticSearchProcessor(emb),
        validation_pipeline=ValidationPipeline(currency_converter=converter),
        ranking_pipeline=RankingPipeline(converter=converter),
        quota_guard=quota,
    )
    orch = SearchOrchestrator(deps)
    settings = load_settings(
        environ={
            "BUSCAAI_API_KEYS": _BENCH_API_KEY,
            "BUSCAAI_RATE_LIMIT_PER_MINUTE": str(max_rpm),
            "USD_BRL_RATE": "5.50",
            "USD_BRL_RATE_UPDATED_AT": "2026-11-25",
        }
    )
    limiter = InMemoryRateLimiter(max_requests=max_rpm, window_seconds=60.0)
    app = create_app(
        settings=settings,
        orchestrator=orch,
        embedding_service=emb,
        rate_limiter=limiter,
        preload_embeddings=False,
    )
    return app, backend, oauth


def run_benchmark(*, iterations: int = 10, concurrency: int = 1) -> dict[str, Any]:
    """Executa o benchmark HTTP E2E sintético e retorna métricas separadas de HTTP e ranking."""
    if iterations < 1 or concurrency < 1:
        raise ValueError("iterations e concurrency devem ser >= 1.")

    app, backend, oauth = _build_benchmark_app(max_rpm=10_000)
    rl_app, rl_backend, rl_oauth = _build_benchmark_app(max_rpm=1)

    regular_scenarios = [sc for sc in SCENARIOS if not sc.rate_limit_probe]
    rl_scenario = next(sc for sc in SCENARIOS if sc.rate_limit_probe)

    latencies_ms: list[float] = []
    status_matches = 0
    http_2xx = 0
    http_errors = 0

    hard_ok_cases = 0
    hard_total_cases = 0
    offer_pres_cases = 0
    offer_total_cases = 0
    ndcg_scores: list[float] = []
    mrr_scores: list[float] = []
    p3_scores: list[float] = []
    relax_ok = 0
    relax_total = 0

    started_wall = time.perf_counter()
    try:
        with TestClient(app) as client, TestClient(rl_app) as rl_client:
            work_items = [sc for _ in range(iterations) for sc in regular_scenarios]

            def _exec(sc: BenchmarkScenario):
                headers = {"X-API-Key": _BENCH_API_KEY} if sc.send_auth else {}
                t0 = time.perf_counter()
                resp = client.post("/v1/search", headers=headers, json=sc.payload)
                elapsed = (time.perf_counter() - t0) * 1000.0
                return sc, resp.status_code, resp.json(), elapsed

            if concurrency == 1:
                outcomes = [_exec(sc) for sc in work_items]
            else:
                with ThreadPoolExecutor(max_workers=concurrency) as pool:
                    outcomes = list(pool.map(_exec, work_items))

            for sc, status_code, body, elapsed_ms in outcomes:
                latencies_ms.append(elapsed_ms)
                if 200 <= status_code < 300:
                    http_2xx += 1
                else:
                    http_errors += 1

                matched = status_code == sc.expected_http_status
                if matched and sc.expected_api_status is not None:
                    matched = body.get("status") == sc.expected_api_status
                if matched:
                    status_matches += 1

                if sc.is_ranking_case and status_code == 200:
                    products = body.get("products", [])
                    returned_offer_ids = [
                        offer["item_id"]
                        for grp in products
                        for offer in grp.get("offers", [])
                    ]
                    hard_total_cases += 1
                    # Hard constraints: nenhum item de menor armazenamento/rejeitado ou acima do budget deve aparecer.
                    if sc.scenario_id == "2_smartphone_armazenamento_minimo":
                        hard_ok = "sp-128" not in returned_offer_ids and "sp-256" in returned_offer_ids
                    else:
                        hard_ok = all(
                            offer["validation"]["hard_not_satisfied_count"] == 0
                            for grp in products
                            for offer in grp.get("offers", [])
                        )
                    if hard_ok:
                        hard_ok_cases += 1

                    if sc.expected_min_offers > 0:
                        offer_total_cases += 1
                        if len(returned_offer_ids) >= sc.expected_min_offers:
                            offer_pres_cases += 1

                    if sc.expect_relaxed:
                        relax_total += 1
                        if body.get("relaxed") is True and len(body.get("relaxations", [])) >= 1:
                            relax_ok += 1

                    grades_map = sc.relevance_grades or {}
                    group_grades = [
                        max((grades_map.get(o["item_id"], 0) for o in grp.get("offers", [])), default=0)
                        for grp in products
                    ]
                    ideal_grades = sorted(grades_map.values(), reverse=True)
                    ideal_dcg = _dcg(ideal_grades, 3)
                    ndcg_scores.append(_dcg(group_grades, 3) / ideal_dcg if ideal_dcg > 0 else 1.0)

                    rel_set = set(sc.relevant_item_ids)
                    rr = 0.0
                    hits_at_3 = 0
                    for idx, grp in enumerate(products[:3], start=1):
                        offer_ids = {o["item_id"] for o in grp.get("offers", [])}
                        if offer_ids & rel_set:
                            hits_at_3 += 1
                            if rr == 0.0:
                                rr = 1.0 / idx
                    mrr_scores.append(rr)
                    denom = min(3, max(1, len(products[:3])))
                    p3_scores.append(hits_at_3 / denom)

            # Cenário 10: Rate limit excedido (1ª consome cota=1, seguintes retornam 429).
            rl_client.post("/v1/search", headers={"X-API-Key": _BENCH_API_KEY}, json=rl_scenario.payload)
            for _ in range(iterations):
                t0 = time.perf_counter()
                rl_resp = rl_client.post("/v1/search", headers={"X-API-Key": _BENCH_API_KEY}, json=rl_scenario.payload)
                latencies_ms.append((time.perf_counter() - t0) * 1000.0)
                http_errors += 1
                if rl_resp.status_code == rl_scenario.expected_http_status and "Retry-After" in rl_resp.headers:
                    status_matches += 1
    finally:
        backend.close()
        oauth.close()
        rl_backend.close()
        rl_oauth.close()

    total_wall_s = max(1e-6, time.perf_counter() - started_wall)
    sorted_lat = sorted(latencies_ms)
    total_requests = len(latencies_ms)

    return {
        "metadata": {
            "is_synthetic_benchmark": True,
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "iterations": iterations,
            "concurrency": concurrency,
            "scenarios_count": len(SCENARIOS),
            "limitations": (
                "Benchmark executado localmente via TestClient com Gemini, eBay Browse API e "
                "embeddings stubados em memória; não representa latência de rede real ou produção."
            ),
        },
        "http_metrics": {
            "total_requests": total_requests,
            "total_wall_seconds": round(total_wall_s, 3),
            "throughput_rps": round(total_requests / total_wall_s, 2),
            "mean_ms": round(statistics.mean(sorted_lat), 3),
            "p50_ms": round(statistics.median(sorted_lat), 3),
            "p95_ms": round(_percentile(sorted_lat, 0.95), 3),
            "p99_ms": round(_percentile(sorted_lat, 0.99), 3) if total_requests >= 20 else None,
            "http_2xx_rate": round(http_2xx / total_requests, 4),
            "http_expected_error_rate": round(http_errors / total_requests, 4),
            "scenario_status_compliance_rate": round(status_matches / total_requests, 4),
        },
        "ranking_metrics": {
            "evaluated_ranking_requests": hard_total_cases,
            "hard_constraint_compliance_rate": round(hard_ok_cases / max(1, hard_total_cases), 4),
            "offer_preservation_rate": round(offer_pres_cases / max(1, offer_total_cases), 4),
            "ndcg_at_3": round(statistics.mean(ndcg_scores), 4) if ndcg_scores else 0.0,
            "mrr": round(statistics.mean(mrr_scores), 4) if mrr_scores else 0.0,
            "precision_at_3": round(statistics.mean(p3_scores), 4) if p3_scores else 0.0,
            "relaxation_accuracy": round(relax_ok / max(1, relax_total), 4),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark E2E HTTP sintético do BUSCAAI (Etapa 9).")
    parser.add_argument("--iterations", type=int, default=10, help="Repetições por cenário (padrão: 10).")
    parser.add_argument("--concurrency", type=int, default=4, help="Workers simultâneos (padrão: 4).")
    parser.add_argument("--json", action="store_true", help="Imprime saída exclusivamente em JSON.")
    args = parser.parse_args(argv)

    result = run_benchmark(iterations=args.iterations, concurrency=args.concurrency)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("=== BUSCAAI Benchmark HTTP E2E Sintético (Etapa 9 / T9.3) ===")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
