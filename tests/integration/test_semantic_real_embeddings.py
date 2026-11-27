r"""Testes de integração com o modelo real `intfloat/multilingual-e5-small`.

Não executa chamadas ao Google Gemini nem à API da eBay.
"""

from __future__ import annotations

import ctypes
import gc
import importlib.metadata as importlib_metadata
import importlib.util
import io
import json
import math
import os
import sys
import time
import tracemalloc
from collections.abc import Iterator, Sequence
from ctypes import wintypes
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from huggingface_hub import snapshot_download

from buscai.api import create_app
from buscai.config import load_settings
from buscai.observability import configure_logging
from buscai.orchestration import OrchestrationDependencies, SearchOrchestrator
from buscai.pricing import CurrencyConverter, FixedEnvExchangeRateProvider
from buscai.ranking.pipeline import RankingPipeline
from buscai.search.models import Candidate, SearchQuery, SearchResponse, SellerInfo, ShippingInfo
from buscai.search.query_builder import QueryBuilder
from buscai.search.quota import QuotaGuard, RequestCallBudget
from buscai.semantic import (
    RECOMMENDED_MODEL_NAME,
    EmbeddingModelLoadError,
    EmbeddingService,
    ProductDeduplicator,
    SemanticSearchProcessor,
    SentenceTransformerEmbeddingProvider,
    cosine_similarity_matrix,
)
from buscai.validation.pipeline import ValidationPipeline
from userintent.schema import PriceConstraint, UserIntent

ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.semantic_real,
    pytest.mark.skipif(
        importlib.util.find_spec("sentence_transformers") is None
        or os.getenv("RUN_REAL_EMBEDDINGS_TESTS") == "0",
        reason="sentence-transformers ausente ou RUN_REAL_EMBEDDINGS_TESTS=0.",
    ),
]


class _PMC(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _get_rss_mb() -> float | None:
    if os.name != "nt":
        return None
    pmc = _PMC()
    pmc.cb = ctypes.sizeof(_PMC)
    k32 = ctypes.windll.kernel32
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    ps = ctypes.windll.psapi
    ps.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
    ps.GetProcessMemoryInfo.restype = wintypes.BOOL
    handle = k32.GetCurrentProcess()
    if not ps.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
        return None
    return round(pmc.WorkingSetSize / (1024 * 1024), 2)


def _dcg_at_k(relevances: Sequence[int], k: int) -> float:
    return sum((rel / math.log2(idx + 2)) for idx, rel in enumerate(relevances[:k]))


@pytest.fixture(scope="module")
def metrics_bag() -> Iterator[dict[str, Any]]:
    bag: dict[str, Any] = {
        "stage": "11.5",
        "model": RECOMMENDED_MODEL_NAME,
        "provider_class": SentenceTransformerEmbeddingProvider.__name__,
        "versions": {
            pkg: importlib_metadata.version(pkg)
            for pkg in ("sentence-transformers", "torch", "transformers", "huggingface-hub", "numpy")
        },
        "gemini_calls_executed": 0,
        "ebay_calls_executed": 0,
    }
    yield bag
    out_raw = os.getenv("STAGE11_5_METRICS_OUTPUT")
    if out_raw:
        out_path = Path(out_raw).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_name(f"{out_path.name}.tmp")
        tmp.write_text(json.dumps(bag, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(out_path)


@pytest.fixture(scope="module")
def real_provider(metrics_bag: dict[str, Any]) -> Iterator[SentenceTransformerEmbeddingProvider]:
    rss_before = _get_rss_mb()
    tracemalloc.start()
    started = time.perf_counter()
    provider = SentenceTransformerEmbeddingProvider(RECOMMENDED_MODEL_NAME, device="cpu")
    provider.ensure_loaded()
    load_seconds = round(time.perf_counter() - started, 3)
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_after = _get_rss_mb()

    snap_path = Path(snapshot_download(RECOMMENDED_MODEL_NAME, local_files_only=True))
    disk_size_mb = round(
        sum(p.stat().st_size for p in snap_path.rglob("*") if p.is_file()) / (1024 * 1024),
        2,
    )
    info = provider.info()
    metrics_bag["loading"] = {
        "initial_load_seconds": load_seconds,
        "dimension": info.dimension,
        "dtype": info.dtype,
        "requires_prefixes": info.requires_prefixes,
        "query_prefix": info.query_prefix,
        "document_prefix": info.document_prefix,
        "symmetric_prefix": info.symmetric_prefix,
        "snapshot_revision_dir": snap_path.name,
        "snapshot_size_mb": disk_size_mb,
        "rss_before_mb": rss_before,
        "rss_after_mb": rss_after,
        "rss_delta_mb": round(rss_after - rss_before, 2) if (rss_before is not None and rss_after is not None) else None,
        "tracemalloc_peak_mb": round(peak_bytes / (1024 * 1024), 2),
    }
    yield provider
    provider.unload()
    gc.collect()


@pytest.fixture(scope="module")
def real_service(real_provider: SentenceTransformerEmbeddingProvider) -> EmbeddingService:
    return EmbeddingService(real_provider, batch_size=32)


def test_real_e5_loading_singleton_prefixes_local_dir_and_error_handling(
    real_provider: SentenceTransformerEmbeddingProvider,
    metrics_bag: dict[str, Any],
) -> None:
    assert real_provider.is_loaded() is True
    info = real_provider.info()
    assert info.model_name == RECOMMENDED_MODEL_NAME
    assert info.backend == "sentence_transformers"
    assert info.dimension == 384
    assert info.requires_prefixes is True
    assert info.query_prefix == "query: "
    assert info.document_prefix == "passage: "
    assert info.symmetric_prefix == "query: "

    # Singleton em memória: chamadas subsequentes reutilizam o mesmo objeto carregado.
    first_model = real_provider.ensure_loaded()
    second_model = real_provider.ensure_loaded()
    assert first_model is second_model

    # Carregamento explícito a partir de snapshot local em cache (`local_files_only=True`).
    snap_path = snapshot_download(RECOMMENDED_MODEL_NAME, local_files_only=True)
    t0 = time.perf_counter()
    local_provider = SentenceTransformerEmbeddingProvider(
        str(snap_path),
        device="cpu",
        local_files_only=True,
    )
    local_provider.ensure_loaded()
    local_load_seconds = round(time.perf_counter() - t0, 3)
    assert local_provider.info().requires_prefixes is True
    assert local_provider.info().dimension == 384

    vec_hub = real_provider.embed_queries(["notebook Lenovo 16GB RAM"])
    vec_local = local_provider.embed_queries(["notebook Lenovo 16GB RAM"])
    assert np.allclose(vec_hub, vec_local, atol=1e-5)
    local_provider.unload()

    # Tratamento de erro quando modelo não existe localmente.
    missing_provider = SentenceTransformerEmbeddingProvider(
        "intfloat/nonexistent-buscai-model-999",
        device="cpu",
        local_files_only=True,
    )
    with pytest.raises(EmbeddingModelLoadError):
        missing_provider.ensure_loaded()

    metrics_bag["local_cache_load"] = {
        "local_load_seconds": local_load_seconds,
        "singleton_reused": first_model is second_model,
        "hub_vs_local_match": bool(np.allclose(vec_hub, vec_local, atol=1e-5)),
        "missing_model_raised_load_error": True,
    }


def test_real_e5_vectors_dimensionality_finiteness_norm_consistency_and_prefixes(
    real_service: EmbeddingService,
    metrics_bag: dict[str, Any],
) -> None:
    queries = [
        "notebook para programação com 16 GB RAM",
        "smartphone 5G com 256 GB",
        "monitor 27 polegadas 4K IPS",
        "fone bluetooth com cancelamento de ruído",
    ]
    docs = [
        "Lenovo ThinkPad T14 14 inch Laptop 16GB RAM 512GB SSD",
        "Samsung Galaxy S24 256GB 5G Unlocked Smartphone",
        "Dell UltraSharp U2723QE 27 inch 4K UHD IPS Monitor",
        "Sony WH-1000XM5 Wireless Noise Cancelling Headphones",
    ]

    t_q0 = time.perf_counter()
    q_single = real_service.embed_query(queries[0])
    q_single_ms = round((time.perf_counter() - t_q0) * 1000.0, 2)

    t_qb0 = time.perf_counter()
    q_batch = real_service.embed_queries(queries)
    q_batch_ms = round((time.perf_counter() - t_qb0) * 1000.0, 2)

    t_db0 = time.perf_counter()
    d_batch = real_service.embed_documents(docs)
    d_batch_ms = round((time.perf_counter() - t_db0) * 1000.0, 2)

    for arr, expected_rows in ((q_single, 1), (q_batch, 4), (d_batch, 4)):
        assert arr.shape == (expected_rows, 384)
        assert arr.dtype == np.float32
        assert np.all(np.isfinite(arr))
        norms = np.linalg.norm(arr, axis=1)
        assert np.allclose(norms, 1.0, atol=1e-5)
        assert not np.any(norms <= 0.0)

    # Consistência determinística entre chamadas idênticas.
    q_repeat = real_service.embed_queries(queries)
    max_repeat_diff = float(np.max(np.abs(q_batch - q_repeat)))
    assert np.allclose(q_batch, q_repeat, atol=1e-6)

    # Efeito real dos prefixos assimétricos E5 (`query: ` vs `passage: `).
    q_same = real_service.embed_queries(["notebook Lenovo 16GB RAM"])
    d_same = real_service.embed_documents(["notebook Lenovo 16GB RAM"])
    prefix_cosine = float(cosine_similarity_matrix(q_same, d_same)[0, 0])
    assert not np.allclose(q_same, d_same, atol=1e-4)

    metrics_bag["vectors"] = {
        "dimension": 384,
        "dtype": str(q_batch.dtype),
        "all_finite": True,
        "l2_norm_unit": True,
        "max_repeat_diff": round(max_repeat_diff, 8),
        "query_vs_passage_same_text_cosine": round(prefix_cosine, 4),
        "single_query_latency_ms": q_single_ms,
        "batch4_queries_latency_ms": q_batch_ms,
        "batch4_docs_latency_ms": d_batch_ms,
    }


def test_real_e5_cross_lingual_similarity_and_semantic_discrimination(
    real_service: EmbeddingService,
    metrics_bag: dict[str, Any],
) -> None:
    related_pairs = [
        ("notebook para programação com 16 GB de RAM", "laptop for software development 16GB RAM"),
        ("fone de ouvido bluetooth com cancelamento de ruído", "wireless bluetooth noise cancelling headphones"),
        ("monitor 4K IPS 27 polegadas", "27 inch 4K UHD IPS display monitor"),
    ]
    unrelated_pairs = [
        ("notebook para programação com 16 GB de RAM", "cafeteira elétrica para café expresso"),
        ("fone de ouvido bluetooth com cancelamento de ruído", "cadeira ergonômica de escritório com apoio lombar"),
        ("monitor 4K IPS 27 polegadas", "tênis de corrida masculino tamanho 42"),
    ]

    related_sims: list[float] = []
    for left, right in related_pairs:
        v_q = real_service.embed_queries([left])
        v_d = real_service.embed_documents([right])
        related_sims.append(round(float(cosine_similarity_matrix(v_q, v_d)[0, 0]), 4))

    unrelated_sims: list[float] = []
    for left, right in unrelated_pairs:
        v_q = real_service.embed_queries([left])
        v_d = real_service.embed_documents([right])
        unrelated_sims.append(round(float(cosine_similarity_matrix(v_q, v_d)[0, 0]), 4))

    for rel, unrel in zip(related_sims, unrelated_sims):
        assert rel > unrel
    assert min(related_sims) > max(unrelated_sims) + 0.03

    metrics_bag["cross_lingual_similarity"] = {
        "related_pairs": related_sims,
        "unrelated_pairs": unrelated_sims,
        "min_related": min(related_sims),
        "max_unrelated": max(unrelated_sims),
        "separation_margin": round(min(related_sims) - max(unrelated_sims), 4),
    }


def test_real_e5_semantic_pipeline_dedup_validation_ranking_and_no_persistence(
    real_service: EmbeddingService,
    metrics_bag: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    candidates = [
        Candidate(
            item_id="nb-1",
            source="controlled",
            title="Lenovo ThinkPad X1 Carbon Gen 11 14 inch Laptop 16GB RAM 512GB SSD",
            price=Decimal("899.00"),
            currency="USD",
            condition="New",
            categories=("notebook",),
            brand="Lenovo",
            model="ThinkPad X1 Carbon Gen 11",
            aspects={"RAM Size": ("16 GB",), "SSD Capacity": ("512 GB",)},
            item_web_url="https://example.invalid/itm/nb-1",
            seller=SellerInfo(username="seller_alpha", feedback_score=1200, feedback_percentage=Decimal("99.5")),
            shipping=ShippingInfo(cost=Decimal("0.00"), currency="USD", free_shipping=True),
        ),
        Candidate(
            item_id="nb-2",
            source="controlled",
            title="ThinkPad X1 Carbon Gen 11 Notebook Lenovo 16GB/512GB 14 inch Business",
            price=Decimal("929.00"),
            currency="USD",
            condition="New",
            categories=("notebook",),
            brand="Lenovo",
            model="ThinkPad X1 Carbon Gen 11",
            aspects={"Processor": ("Core i7",), "RAM Size": ("16 GB",), "SSD Capacity": ("512 GB",)},
            details_loaded=True,
            item_web_url="https://example.invalid/itm/nb-2",
            seller=SellerInfo(username="seller_beta", feedback_score=800, feedback_percentage=Decimal("98.9")),
            shipping=ShippingInfo(cost=Decimal("15.00"), currency="USD", free_shipping=False),
        ),
        # Duplicata exata de anúncio (mesmo item_id nb-1)
        Candidate(
            item_id="nb-1",
            source="controlled",
            title="Lenovo ThinkPad X1 Carbon Gen 11 14 inch Laptop 16GB RAM 512GB SSD",
            price=Decimal("899.00"),
            currency="USD",
            condition="New",
            categories=("notebook",),
            brand="Lenovo",
            model="ThinkPad X1 Carbon Gen 11",
            aspects={"RAM Size": ("16 GB",), "SSD Capacity": ("512 GB",)},
            item_web_url="https://example.invalid/itm/nb-1",
        ),
        # Variante distinta (32GB RAM / 1TB SSD) — não pode ser fundida com nb-1/nb-2
        Candidate(
            item_id="nb-32gb",
            source="controlled",
            title="Lenovo ThinkPad X1 Carbon Gen 11 14 inch Laptop 32GB RAM 1TB SSD",
            price=Decimal("1299.00"),
            currency="USD",
            condition="New",
            categories=("notebook",),
            brand="Lenovo",
            model="ThinkPad X1 Carbon Gen 11",
            aspects={"RAM Size": ("32 GB",), "SSD Capacity": ("1 TB",)},
            item_web_url="https://example.invalid/itm/nb-32gb",
            seller=SellerInfo(username="seller_gamma", feedback_score=950, feedback_percentage=Decimal("99.2")),
            shipping=ShippingInfo(cost=Decimal("0.00"), currency="USD", free_shipping=True),
        ),
        Candidate(
            item_id="mouse-irrel",
            source="controlled",
            title="Logitech MX Master 3S Wireless Mouse",
            price=Decimal("99.00"),
            currency="USD",
            condition="New",
            categories=("mouse",),
            brand="Logitech",
            model="MX Master 3S",
            item_web_url="https://example.invalid/itm/mouse-irrel",
        ),
    ]

    log_stream = io.StringIO()
    logger = configure_logging("INFO", stream=log_stream, logger_name="buscai.test.real_e5_pipeline")
    converter = CurrencyConverter(FixedEnvExchangeRateProvider(rate=Decimal("5.50")))
    processor = SemanticSearchProcessor(real_service, logger=logger)
    validator = ValidationPipeline(currency_converter=converter, logger=logger)
    ranker = RankingPipeline(converter=converter)

    intent = UserIntent(
        category="notebook",
        brands=["Lenovo"],
        price=PriceConstraint(max_price=8000.0, currency="BRL", is_hard_constraint=True),
        required_features=["16 GB de RAM"],
        query="notebook Lenovo 16GB RAM",
    )

    write_events: list[str] = []
    audit_active = True

    def _audit(event: str, args: tuple[object, ...]) -> None:
        if not audit_active:
            return
        if event == "open" and len(args) >= 2 and any(flag in str(args[1]) for flag in ("w", "a", "x", "+")):
            write_events.append(str(args[0]))

    sys.addaudithook(_audit)  # type: ignore[arg-type]
    try:
        sem_res = processor.process(
            "notebook Lenovo com 16 GB de RAM",
            candidates,
            intent=intent,
            category_hint="notebook",
            request_id="req-real-e5-pipe",
        )
        val_res = validator.run(intent, sem_res, profile_id="best_value", request_id="req-real-e5-pipe")
        rank_res = ranker.run(
            val_res,
            semantic_result=sem_res,
            profile_id="best_value",
            category_hint="notebook",
            request_id="req-real-e5-pipe",
        )
    finally:
        audit_active = False

    assert sem_res.embedding_model == RECOMMENDED_MODEL_NAME
    assert sem_res.removed_duplicates_count == 1
    assert sem_res.output_count == 4
    assert sem_res.unique_products_count == 3
    assert any(set(g.duplicate_item_ids) == {"nb-1", "nb-2"} for g in sem_res.product_groups)
    assert any(g.duplicate_item_ids == ("nb-32gb",) for g in sem_res.product_groups)

    scores_by_id = {entry.candidate.item_id: entry.semantic_score for entry in sem_res.scored_candidates}
    assert scores_by_id["nb-1"] > scores_by_id["mouse-irrel"]
    assert scores_by_id["nb-2"] > scores_by_id["mouse-irrel"]

    assert rank_res.semantic_enabled is True
    assert rank_res.rejected_count >= 1
    assert any(rej.item_id == "mouse-irrel" for rej in rank_res.rejected_candidates)
    assert rank_res.unique_products_count >= 1
    assert rank_res.ranked_groups[0].explanation.summary.strip() != ""

    assert write_events == []
    assert list(tmp_path.rglob("*")) == []
    assert not any(isinstance(v, np.ndarray) for v in vars(sem_res).values())
    logs_raw = log_stream.getvalue()
    assert "ThinkPad" not in logs_raw
    assert "example.invalid" not in logs_raw
    assert "seller_alpha" not in logs_raw

    metrics_bag["controlled_pipeline"] = {
        "input_candidates": len(candidates),
        "exact_removed": sem_res.removed_duplicates_count,
        "unique_products_groups": sem_res.unique_products_count,
        "ranked_groups": rank_res.unique_products_count,
        "rejected_count": rank_res.rejected_count,
        "thinkpad_nb1_semantic_score": scores_by_id["nb-1"],
        "thinkpad_nb2_semantic_score": scores_by_id["nb-2"],
        "mouse_semantic_score": scores_by_id["mouse-irrel"],
        "zero_disk_writes": True,
        "zero_log_leaks": True,
    }


def test_real_e5_eval_semantic_v2_benchmark_metrics(
    real_service: EmbeddingService,
    metrics_bag: dict[str, Any],
) -> None:
    eval_path = ROOT / "data" / "eval_semantic_v2_evaluation.jsonl"
    rows = [json.loads(line) for line in eval_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    retrieval_rows = [r for r in rows if r.get("task") == "retrieval"]
    dedup_rows = [r for r in rows if r.get("task") == "deduplication"]

    processor = SemanticSearchProcessor(real_service)
    dedup = ProductDeduplicator(real_service)

    tracemalloc.start()
    started = time.perf_counter()

    precision_at_2: list[float] = []
    ndcg_at_3: list[float] = []
    reciprocal_ranks: list[float] = []

    for row in retrieval_rows:
        query_pt = str(row["query_pt"])
        raw_cands = row["candidates"]
        rel_by_id = {str(item["item_id"]): int(item["relevance"]) for item in raw_cands}
        candidates = [
            Candidate(
                item_id=str(item["item_id"]),
                source="synthetic",
                title=str(item["title"]),
                price=Decimal("500.00"),
                currency="USD",
                condition=str(item["condition"]) if item.get("condition") else None,
                brand=str(item["brand"]) if item.get("brand") else None,
                model=str(item["model"]) if item.get("model") else None,
                short_description=str(item["short_description"]) if item.get("short_description") else None,
            )
            for item in raw_cands
        ]
        ranked = processor._ranker.rank(query_pt, candidates)
        ranked_rels = [rel_by_id[entry.candidate.item_id] for entry in ranked]
        top_2 = ranked_rels[:2]
        precision_at_2.append(sum(1 for rel in top_2 if rel >= 2) / min(2, len(ranked_rels)))

        ideal = sorted(ranked_rels, reverse=True)
        idcg = _dcg_at_k(ideal, 3)
        ndcg_at_3.append((_dcg_at_k(ranked_rels, 3) / idcg) if idcg > 0 else 0.0)

        rr = 0.0
        for idx, rel in enumerate(ranked_rels):
            if rel >= 2:
                rr = 1.0 / (idx + 1)
                break
        reciprocal_ranks.append(rr)

    dedup_correct = 0
    false_merges = 0
    non_dup_total = 0
    tp_dup = 0
    fp_dup = 0
    fn_dup = 0

    for row in dedup_rows:
        expected = str(row["expected_label"])
        left_raw = row["left"]
        right_raw = row["right"]
        left = Candidate(
            item_id=str(left_raw["item_id"]),
            source="synthetic",
            title=str(left_raw["title"]),
            price=Decimal("500.00"),
            currency="USD",
            condition=str(left_raw["condition"]) if left_raw.get("condition") else None,
            brand=str(left_raw["brand"]) if left_raw.get("brand") else None,
            model=str(left_raw["model"]) if left_raw.get("model") else None,
            short_description=str(left_raw["short_description"]) if left_raw.get("short_description") else None,
        )
        right = Candidate(
            item_id=str(right_raw["item_id"]),
            source="synthetic",
            title=str(right_raw["title"]),
            price=Decimal("500.00"),
            currency="USD",
            condition=str(right_raw["condition"]) if right_raw.get("condition") else None,
            brand=str(right_raw["brand"]) if right_raw.get("brand") else None,
            model=str(right_raw["model"]) if right_raw.get("model") else None,
            short_description=str(right_raw["short_description"]) if right_raw.get("short_description") else None,
        )
        vecs = real_service.embed_candidates([left, right], mode="symmetric_similarity")
        sim = float(cosine_similarity_matrix(vecs[0:1], vecs[1:2])[0, 0])
        del vecs
        ev = dedup.classify_pair(left, right, semantic_similarity=sim)
        if ev.classification == expected:
            dedup_correct += 1
        if expected == "probable_duplicate":
            if ev.classification == "probable_duplicate":
                tp_dup += 1
            else:
                fn_dup += 1
        else:
            non_dup_total += 1
            if ev.classification == "probable_duplicate":
                false_merges += 1
                fp_dup += 1

    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    mrr_val = round(sum(reciprocal_ranks) / len(reciprocal_ranks), 4)
    ndcg3_val = round(sum(ndcg_at_3) / len(ndcg_at_3), 4)
    p2_val = round(sum(precision_at_2) / len(precision_at_2), 4)
    dedup_acc = round(dedup_correct / len(dedup_rows), 4)
    false_dedup_rate = round(false_merges / non_dup_total, 4)
    dup_precision = round(tp_dup / (tp_dup + fp_dup), 4) if (tp_dup + fp_dup) else 0.0
    dup_recall = round(tp_dup / (tp_dup + fn_dup), 4) if (tp_dup + fn_dup) else 0.0

    assert false_dedup_rate == 0.0
    assert mrr_val >= 0.85

    metrics_bag["benchmark_v2_evaluation"] = {
        "retrieval_cases": len(retrieval_rows),
        "precision_at_2": p2_val,
        "ndcg_at_3": ndcg3_val,
        "mrr": mrr_val,
        "dedup_cases": len(dedup_rows),
        "dedup_accuracy": dedup_acc,
        "false_dedup_rate": false_dedup_rate,
        "probable_duplicate_precision": dup_precision,
        "probable_duplicate_recall": dup_recall,
        "latency_ms": elapsed_ms,
        "peak_traced_mb": round(peak_bytes / (1024 * 1024), 2),
    }


def test_real_e5_fastapi_e2e_health_search_and_chat(
    real_service: EmbeddingService,
    metrics_bag: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    api_key = "stage11_5_real_e5_ephemeral_key"
    settings = load_settings(
        environ={
            "BUSCAAI_API_KEYS": api_key,
            "USD_BRL_RATE": "5.50",
            "BUSCAAI_EMBEDDING_BACKEND": "sentence_transformers",
            "BUSCAAI_EMBEDDING_MODEL": RECOMMENDED_MODEL_NAME,
        }
    )

    class _ControlledExtractor:
        def extract(
            self,
            user_text: str,
            *,
            conversation_history: Sequence[dict[str, str]] | None = None,
            request_id: str | None = None,
        ) -> UserIntent:
            del conversation_history, request_id
            low = user_text.casefold()
            if "presente" in low:
                return UserIntent(
                    category=None,
                    query="presente",
                    clarification_needed=True,
                    clarification_question="Qual produto você procura e qual é o orçamento?",
                )
            if "smartphone" in low:
                return UserIntent(
                    category="smartphone",
                    price=PriceConstraint(max_price=5500, is_hard_constraint=True, currency="BRL"),
                    query="smartphone 256GB 5G",
                )
            if "monitor" in low:
                return UserIntent(
                    category="monitor",
                    price=PriceConstraint(max_price=4500, is_hard_constraint=False, currency="BRL"),
                    query="monitor 27 polegadas 4K IPS",
                )
            if "fone" in low:
                return UserIntent(
                    category="fone de ouvido",
                    price=PriceConstraint(max_price=3000, is_hard_constraint=True, currency="BRL"),
                    query="fone bluetooth cancelamento de ruido",
                )
            return UserIntent(
                category="notebook",
                brands=["Lenovo"],
                price=PriceConstraint(max_price=6500, is_hard_constraint=True, currency="BRL"),
                required_features=["16 GB de RAM"],
                query="notebook Lenovo 16GB RAM",
            )

    class _ControlledBackend:
        def search(
            self,
            query: SearchQuery,
            *,
            budget: RequestCallBudget | None = None,
            request_id: str | None = None,
        ) -> SearchResponse:
            del budget, request_id
            q = query.query_text.casefold()
            if "smartphone" in q or "256gb" in q:
                cands = (
                    Candidate(
                        item_id="sp-1",
                        source="controlled",
                        title="Samsung Galaxy S24 256GB 5G Unlocked Smartphone",
                        price=Decimal("520.00"),
                        currency="USD",
                        condition="New",
                        categories=("smartphone",),
                        brand="Samsung",
                        model="Galaxy S24",
                        aspects={"Storage Capacity": ("256 GB",)},
                        item_web_url="https://example.invalid/itm/sp-1",
                        seller=SellerInfo(username="sp_seller", feedback_score=900, feedback_percentage=Decimal("99.1")),
                        shipping=ShippingInfo(cost=Decimal("0.00"), currency="USD", free_shipping=True),
                    ),
                )
            elif "monitor" in q:
                cands = (
                    Candidate(
                        item_id="mon-1",
                        source="controlled",
                        title="Dell U2723QE 27 inch 4K UHD IPS Monitor USB-C",
                        price=Decimal("420.00"),
                        currency="USD",
                        condition="New",
                        categories=("monitor",),
                        brand="Dell",
                        model="U2723QE",
                        aspects={"Screen Size": ("27",), "Maximum Resolution": ("3840 x 2160",)},
                        item_web_url="https://example.invalid/itm/mon-1",
                        seller=SellerInfo(username="mon_seller", feedback_score=650, feedback_percentage=Decimal("98.5")),
                        shipping=None,  # Frete desconhecido intencional
                    ),
                    Candidate(
                        item_id="mon-2",
                        source="controlled",
                        title="LG 27UP850N 27 inch 4K UHD IPS Monitor USB-C",
                        price=Decimal("399.00"),
                        currency="USD",
                        condition="New",
                        categories=("monitor",),
                        brand="LG",
                        model="27UP850N",
                        aspects={"Screen Size": ("27",), "Maximum Resolution": ("3840 x 2160",)},
                        item_web_url="https://example.invalid/itm/mon-2",
                        seller=SellerInfo(username="mon_seller_2", feedback_score=780, feedback_percentage=Decimal("99.1")),
                        shipping=ShippingInfo(cost=Decimal("0.00"), currency="USD", free_shipping=True),
                    ),
                )
            elif "headphone" in q or "fone" in q:
                cands = (
                    Candidate(
                        item_id="hp-1",
                        source="controlled",
                        title="Sony WH-1000XM5 Wireless Noise Cancelling Bluetooth Headphones",
                        price=Decimal("299.00"),
                        currency="USD",
                        condition="New",
                        categories=("fone de ouvido",),
                        brand="Sony",
                        model="WH-1000XM5",
                        item_web_url="https://example.invalid/itm/hp-1",
                        seller=SellerInfo(username="hp_seller", feedback_score=1400, feedback_percentage=Decimal("99.6")),
                        shipping=ShippingInfo(cost=Decimal("0.00"), currency="USD", free_shipping=True),
                    ),
                )
            else:
                cands = (
                    Candidate(
                        item_id="e2e-nb-1",
                        source="controlled",
                        title="Lenovo ThinkPad T14 14 inch Laptop 16GB RAM 512GB SSD",
                        price=Decimal("780.00"),
                        currency="USD",
                        condition="New",
                        categories=("notebook",),
                        brand="Lenovo",
                        model="ThinkPad T14",
                        aspects={"RAM Size": ("16 GB",), "SSD Capacity": ("512 GB",)},
                        item_web_url="https://example.invalid/itm/e2e-nb-1",
                        seller=SellerInfo(username="nb_seller_1", feedback_score=1100, feedback_percentage=Decimal("99.3")),
                        shipping=ShippingInfo(cost=Decimal("0.00"), currency="USD", free_shipping=True),
                    ),
                    Candidate(
                        item_id="e2e-nb-2",
                        source="controlled",
                        title="ThinkPad T14 Notebook Lenovo 16GB RAM 512GB SSD 14 inch",
                        price=Decimal("795.00"),
                        currency="USD",
                        condition="New",
                        categories=("notebook",),
                        brand="Lenovo",
                        model="ThinkPad T14",
                        aspects={"RAM Size": ("16 GB",), "SSD Capacity": ("512 GB",)},
                        details_loaded=True,
                        item_web_url="https://example.invalid/itm/e2e-nb-2",
                        seller=SellerInfo(username="nb_seller_2", feedback_score=850, feedback_percentage=Decimal("99.0")),
                        shipping=ShippingInfo(cost=Decimal("10.00"), currency="USD", free_shipping=False),
                    ),
                    Candidate(
                        item_id="e2e-nb-32",
                        source="controlled",
                        title="Lenovo ThinkPad T14 14 inch Laptop 32GB RAM 1TB SSD",
                        price=Decimal("980.00"),
                        currency="USD",
                        condition="New",
                        categories=("notebook",),
                        brand="Lenovo",
                        model="ThinkPad T14",
                        aspects={"RAM Size": ("32 GB",), "SSD Capacity": ("1 TB",)},
                        item_web_url="https://example.invalid/itm/e2e-nb-32",
                        seller=SellerInfo(username="nb_seller_3", feedback_score=700, feedback_percentage=Decimal("98.7")),
                        shipping=ShippingInfo(cost=Decimal("0.00"), currency="USD", free_shipping=True),
                    ),
                )
            return SearchResponse(candidates=cands, total_estimated=len(cands), limit=query.limit, offset=0, calls_used=1)

        def get_item_details(self, item_id: str, **kwargs: object) -> None:
            del item_id, kwargs
            return None

    log_stream = io.StringIO()
    logger = configure_logging("INFO", stream=log_stream, logger_name="buscai.test.real_e5_e2e")
    converter = CurrencyConverter(FixedEnvExchangeRateProvider.from_settings(settings))
    backend = _ControlledBackend()
    deps = OrchestrationDependencies(
        extractor=_ControlledExtractor(),
        query_builder=QueryBuilder(currency_converter=converter),
        search_backend=backend,
        semantic_processor=SemanticSearchProcessor(real_service, logger=logger),
        validation_pipeline=ValidationPipeline(
            currency_converter=converter,
            search_backend=backend,
            max_enrich_items=0,
            logger=logger,
        ),
        ranking_pipeline=RankingPipeline(converter=converter),
        quota_guard=QuotaGuard(daily_limit=50),
        max_search_calls=1,
        max_get_item_calls=0,
        logger=logger,
    )
    app = create_app(
        settings=settings,
        orchestrator=SearchOrchestrator(deps),
        embedding_service=real_service,
        preload_embeddings=True,
        logger=logger,
    )

    e2e_rows: list[dict[str, Any]] = []
    with TestClient(app) as client:
        health = client.get("/v1/health")
        assert health.status_code == 200
        h_body = health.json()
        assert h_body["readiness"] == "ready"
        emb_comp = next(c for c in h_body["components"] if c["name"] == "embeddings")
        assert emb_comp["status"] == "ready"
        assert RECOMMENDED_MODEL_NAME in emb_comp["detail"]
        assert "sentence_transformers" in emb_comp["detail"]

        for cat_expected, q_text, budget_brl in (
            ("notebook", "notebook Lenovo 16GB RAM", 6500),
            ("smartphone", "smartphone 256GB 5G", 5500),
            ("monitor", "monitor 27 polegadas 4K IPS", None),
            ("fone de ouvido", "fone bluetooth cancelamento de ruido", 3000),
        ):
            t0 = time.perf_counter()
            req_body: dict[str, Any] = {"query": q_text, "limit": 5}
            if budget_brl is not None:
                req_body["budget_brl"] = budget_brl
            resp = client.post(
                "/v1/search",
                headers={"X-API-Key": api_key},
                json=req_body,
            )
            elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "ok"
            assert body["detected_category"] == cat_expected
            assert body["total_products"] >= 1
            for grp in body["products"]:
                assert grp["score_breakdown"]["semantic_enabled"] is True
                assert grp["explanation"]["summary"].strip() != ""
                for offer in grp["offers"]:
                    assert isinstance(offer["semantic_score"], float)
                    assert 0.0 <= offer["semantic_score"] <= 1.0
                    assert math.isfinite(offer["semantic_score"])
            if cat_expected == "notebook":
                assert body["total_products"] == 2
                assert body["total_offers"] == 3
            if cat_expected == "monitor":
                mon_offers = [o for grp in body["products"] for o in grp["offers"]]
                assert any(
                    o["shipping"]["known"] is False and o["shipping"]["free_shipping"] is None
                    for o in mon_offers
                )
                assert any("frete" in w.casefold() and "desconhecido" in w.casefold() for w in body["warnings"])

            e2e_rows.append(
                {
                    "category": cat_expected,
                    "http_status": resp.status_code,
                    "status": body["status"],
                    "total_products": body["total_products"],
                    "total_offers": body["total_offers"],
                    "semantic_enabled": body["products"][0]["score_breakdown"]["semantic_enabled"],
                    "top_semantic_score": body["products"][0]["offers"][0]["semantic_score"],
                    "latency_ms": elapsed_ms,
                }
            )

        chat_resp = client.post(
            "/v1/chat",
            headers={"X-API-Key": api_key},
            json={"message": "Quero um presente mas ainda não decidi o quê."},
        )
        assert chat_resp.status_code == 200
        chat_body = chat_resp.json()
        assert chat_body["status"] == "clarification_needed"
        assert chat_body["search_results"] is None

    logs_raw = log_stream.getvalue()
    assert api_key not in logs_raw
    assert "ThinkPad" not in logs_raw
    assert "example.invalid" not in logs_raw
    assert list(tmp_path.rglob("*")) == []

    metrics_bag["e2e_fastapi"] = {
        "health_status": health.status_code,
        "health_readiness": h_body["readiness"],
        "search_scenarios": e2e_rows,
        "chat_status": chat_resp.status_code,
        "chat_response_status": chat_body["status"],
        "zero_disk_writes": True,
        "zero_log_leaks": True,
    }
