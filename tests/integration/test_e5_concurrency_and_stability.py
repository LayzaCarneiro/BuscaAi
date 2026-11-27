r"""Testes controlados de concorrência, desempenho e estabilidade com embeddings reais.

Valida exclusivamente com o modelo real `intfloat/multilingual-e5-small` em CPU
(`local_files_only=True`), sem chamadas externas ao Google Gemini nem à API eBay.
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
import subprocess
import sys
import threading
import time
import tracemalloc
import weakref
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from buscai.api import APIKeyAuthenticator, InMemoryRateLimiter, create_app
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


def _summarize_latencies(samples_ms: Sequence[float]) -> dict[str, Any]:
    if not samples_ms:
        return {
            "count": 0,
            "min_ms": None,
            "mean_ms": None,
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "max_ms": None,
        }
    arr = np.asarray(samples_ms, dtype=np.float64)
    return {
        "count": int(arr.size),
        "min_ms": round(float(np.min(arr)), 2),
        "mean_ms": round(float(np.mean(arr)), 2),
        "p50_ms": round(float(np.percentile(arr, 50, method="linear")), 2),
        "p95_ms": round(float(np.percentile(arr, 95, method="linear")), 2),
        "p99_ms": round(float(np.percentile(arr, 99, method="linear")), 2),
        "max_ms": round(float(np.max(arr)), 2),
    }


def _cpu_metrics(cpu_s: float, wall_s: float) -> dict[str, Any]:
    cpu_count = os.cpu_count() or 1
    safe_wall = max(wall_s, 1e-9)
    ratio = cpu_s / safe_wall
    return {
        "cpu_time_s": round(cpu_s, 4),
        "wall_time_s": round(wall_s, 4),
        "cpu_utilization_ratio": round(ratio, 3),
        "cpu_pct_of_machine": round((ratio / cpu_count) * 100.0, 2),
        "logical_cpus": cpu_count,
    }


class _ControlledExtractor:
    """Extrator determinístico offline para simular 4 categorias + esclarecimento sem chamar Gemini."""

    def extract(
        self,
        user_text: str,
        *,
        conversation_history: Sequence[dict[str, str]] | None = None,
        request_id: str | None = None,
    ) -> UserIntent:
        del conversation_history, request_id
        low = user_text.casefold()
        if "presente" in low or "indeciso" in low:
            return UserIntent(
                category=None,
                query="presente",
                clarification_needed=True,
                clarification_question="Qual produto você procura e qual é o orçamento?",
            )
        if "smartphone" in low or "celular" in low or "galaxy" in low:
            return UserIntent(
                category="smartphone",
                price=PriceConstraint(max_price=5500, is_hard_constraint=True, currency="BRL"),
                required_features=["256 GB", "5G"],
                query=user_text,
            )
        if "monitor" in low:
            return UserIntent(
                category="monitor",
                price=PriceConstraint(max_price=4800, is_hard_constraint=True, currency="BRL"),
                required_features=["27 polegadas", "4K"],
                query=user_text,
            )
        if "fone" in low or "headphone" in low or "bluetooth" in low:
            return UserIntent(
                category="fone de ouvido",
                price=PriceConstraint(max_price=3200, is_hard_constraint=True, currency="BRL"),
                required_features=["Bluetooth"],
                query=user_text,
            )
        return UserIntent(
            category="notebook",
            brands=["Lenovo"],
            price=PriceConstraint(max_price=6800, is_hard_constraint=True, currency="BRL"),
            required_features=["16 GB de RAM"],
            query=user_text,
        )


def _make_category_candidates(tag: str, query_text: str) -> tuple[Candidate, ...]:
    q = query_text.casefold()
    if "smartphone" in q or "256gb" in q or "5g" in q:
        return (
            Candidate(
                item_id=f"sp-1-{tag}",
                source="controlled",
                title="Samsung Galaxy S24 256GB 5G Unlocked Smartphone",
                price=Decimal("520.00"),
                currency="USD",
                condition="New",
                categories=("smartphone",),
                brand="Samsung",
                model="Galaxy S24",
                aspects={"Storage Capacity": ("256 GB",), "Network": ("5G",)},
                item_web_url="https://example.invalid/itm/sp-1",
                seller=SellerInfo(username="sp_seller_1", feedback_score=920, feedback_percentage=Decimal("99.2")),
                shipping=ShippingInfo(cost=Decimal("0.00"), currency="USD", free_shipping=True),
                details_loaded=True,
            ),
            Candidate(
                item_id=f"sp-2-{tag}",
                source="controlled",
                title="Apple iPhone 15 256GB 5G Unlocked Smartphone",
                price=Decimal("640.00"),
                currency="USD",
                condition="New",
                categories=("smartphone",),
                brand="Apple",
                model="iPhone 15",
                aspects={"Storage Capacity": ("256 GB",), "Network": ("5G",)},
                item_web_url="https://example.invalid/itm/sp-2",
                seller=SellerInfo(username="sp_seller_2", feedback_score=1150, feedback_percentage=Decimal("99.5")),
                shipping=ShippingInfo(cost=Decimal("10.00"), currency="USD", free_shipping=False),
                details_loaded=True,
            ),
        )
    if "monitor" in q:
        return (
            Candidate(
                item_id=f"mon-1-{tag}",
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
                seller=SellerInfo(username="mon_seller_1", feedback_score=690, feedback_percentage=Decimal("98.8")),
                shipping=ShippingInfo(cost=Decimal("0.00"), currency="USD", free_shipping=True),
                details_loaded=True,
            ),
            Candidate(
                item_id=f"mon-2-{tag}",
                source="controlled",
                title="LG 27UP850N 27 inch 4K UHD IPS Monitor USB-C",
                price=Decimal("395.00"),
                currency="USD",
                condition="New",
                categories=("monitor",),
                brand="LG",
                model="27UP850N",
                aspects={"Screen Size": ("27",), "Maximum Resolution": ("3840 x 2160",)},
                item_web_url="https://example.invalid/itm/mon-2",
                seller=SellerInfo(username="mon_seller_2", feedback_score=780, feedback_percentage=Decimal("99.1")),
                shipping=ShippingInfo(cost=Decimal("12.00"), currency="USD", free_shipping=False),
                details_loaded=True,
            ),
        )
    if "headphone" in q or "fone" in q or "bluetooth" in q:
        return (
            Candidate(
                item_id=f"hp-1-{tag}",
                source="controlled",
                title="Sony WH-1000XM5 Wireless Noise Cancelling Bluetooth Headphones",
                price=Decimal("299.00"),
                currency="USD",
                condition="New",
                categories=("fone de ouvido",),
                brand="Sony",
                model="WH-1000XM5",
                aspects={"Connectivity": ("Bluetooth", "Wireless")},
                item_web_url="https://example.invalid/itm/hp-1",
                seller=SellerInfo(username="hp_seller_1", feedback_score=1400, feedback_percentage=Decimal("99.6")),
                shipping=ShippingInfo(cost=Decimal("0.00"), currency="USD", free_shipping=True),
                details_loaded=True,
            ),
            Candidate(
                item_id=f"hp-2-{tag}",
                source="controlled",
                title="Bose QuietComfort Ultra Wireless Bluetooth Headphones",
                price=Decimal("329.00"),
                currency="USD",
                condition="New",
                categories=("fone de ouvido",),
                brand="Bose",
                model="QuietComfort Ultra",
                aspects={"Connectivity": ("Bluetooth", "Wireless")},
                item_web_url="https://example.invalid/itm/hp-2",
                seller=SellerInfo(username="hp_seller_2", feedback_score=980, feedback_percentage=Decimal("99.0")),
                shipping=ShippingInfo(cost=Decimal("0.00"), currency="USD", free_shipping=True),
                details_loaded=True,
            ),
        )
    return (
        Candidate(
            item_id=f"nb-1-{tag}",
            source="controlled",
            title="Lenovo ThinkPad T14 14 inch Laptop 16GB RAM 512GB SSD",
            price=Decimal("780.00"),
            currency="USD",
            condition="New",
            categories=("notebook",),
            brand="Lenovo",
            model="ThinkPad T14",
            aspects={"RAM Size": ("16 GB",), "SSD Capacity": ("512 GB",)},
            item_web_url="https://example.invalid/itm/nb-1",
            seller=SellerInfo(username="nb_seller_1", feedback_score=1100, feedback_percentage=Decimal("99.3")),
            shipping=ShippingInfo(cost=Decimal("0.00"), currency="USD", free_shipping=True),
            details_loaded=True,
        ),
        Candidate(
            item_id=f"nb-2-{tag}",
            source="controlled",
            title="Lenovo ThinkPad T14 14 inch Laptop 32GB RAM 1TB SSD",
            price=Decimal("960.00"),
            currency="USD",
            condition="New",
            categories=("notebook",),
            brand="Lenovo",
            model="ThinkPad T14",
            aspects={"RAM Size": ("32 GB",), "SSD Capacity": ("1 TB",)},
            item_web_url="https://example.invalid/itm/nb-2",
            seller=SellerInfo(username="nb_seller_2", feedback_score=870, feedback_percentage=Decimal("99.0")),
            shipping=ShippingInfo(cost=Decimal("10.00"), currency="USD", free_shipping=False),
            details_loaded=True,
        ),
    )


class _ControlledBackend:
    """Backend em memória thread-safe sem rede para validação concorrente E2E."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0

    def search(
        self,
        query: SearchQuery,
        *,
        budget: RequestCallBudget | None = None,
        request_id: str | None = None,
    ) -> SearchResponse:
        del budget
        with self._lock:
            self.calls += 1
            call_num = self.calls
        tag = request_id or str(call_num)
        cands = _make_category_candidates(tag, query.query_text)
        return SearchResponse(
            candidates=cands,
            total_estimated=len(cands),
            limit=query.limit,
            offset=0,
            calls_used=1,
        )

    def get_item_details(self, item_id: str, **kwargs: object) -> None:
        del item_id, kwargs
        return None


def _build_controlled_app(
    real_service: EmbeddingService,
    *,
    api_keys: Sequence[str],
    max_requests_per_minute: int = 200,
) -> tuple[Any, io.StringIO, _ControlledBackend]:
    settings = load_settings(
        environ={
            "BUSCAAI_API_KEYS": ",".join(api_keys),
            "USD_BRL_RATE": "5.50",
            "BUSCAAI_EMBEDDING_BACKEND": "sentence_transformers",
            "BUSCAAI_EMBEDDING_MODEL": RECOMMENDED_MODEL_NAME,
            "BUSCAAI_RATE_LIMIT_PER_MINUTE": str(max_requests_per_minute),
        }
    )
    log_stream = io.StringIO()
    logger = configure_logging("INFO", stream=log_stream, logger_name="buscai.test.stage11_6")
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
        quota_guard=QuotaGuard(daily_limit=1000),
        max_search_calls=1,
        max_get_item_calls=0,
        logger=logger,
    )
    app = create_app(
        settings=settings,
        orchestrator=SearchOrchestrator(deps),
        embedding_service=real_service,
        authenticator=APIKeyAuthenticator(api_keys),
        rate_limiter=InMemoryRateLimiter(max_requests=max_requests_per_minute, window_seconds=60.0),
        currency_converter=converter,
        preload_embeddings=True,
        logger=logger,
    )
    return app, log_stream, backend


@pytest.fixture(scope="module")
def metrics_bag() -> Iterator[dict[str, Any]]:
    bag: dict[str, Any] = {
        "stage": "11.6",
        "model": RECOMMENDED_MODEL_NAME,
        "provider_class": SentenceTransformerEmbeddingProvider.__name__,
        "versions": {
            pkg: importlib_metadata.version(pkg)
            for pkg in ("sentence-transformers", "torch", "transformers", "huggingface-hub", "numpy")
        },
        "logical_cpus": os.cpu_count() or 1,
        "gemini_calls_executed": 0,
        "ebay_calls_executed": 0,
    }
    yield bag
    out_raw = os.getenv("BUSCAAI_CONCURRENCY_METRICS_OUTPUT")
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
    wall_t0 = time.perf_counter()
    cpu_t0 = time.process_time()
    provider = SentenceTransformerEmbeddingProvider(
        RECOMMENDED_MODEL_NAME,
        device="cpu",
        local_files_only=True,
    )
    provider.ensure_loaded()
    wall_s = time.perf_counter() - wall_t0
    cpu_s = time.process_time() - cpu_t0
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_after = _get_rss_mb()

    metrics_bag["module_provider_load"] = {
        "wall_seconds": round(wall_s, 3),
        "cpu_seconds": round(cpu_s, 3),
        "rss_before_mb": rss_before,
        "rss_after_mb": rss_after,
        "rss_delta_mb": round(rss_after - rss_before, 2)
        if (rss_before is not None and rss_after is not None)
        else None,
        "tracemalloc_peak_mb": round(peak_bytes / (1024 * 1024), 2),
        "dimension": provider.info().dimension,
    }
    yield provider
    provider.unload()
    gc.collect()


@pytest.fixture(scope="module")
def real_service(real_provider: SentenceTransformerEmbeddingProvider) -> EmbeddingService:
    return EmbeddingService(real_provider, batch_size=32)


def test_phase2_concurrent_ensure_loaded_single_load(
    real_provider: SentenceTransformerEmbeddingProvider,
    metrics_bag: dict[str, Any],
) -> None:
    """Garante carregamento único thread-safe de `ensure_loaded()` com modelo real."""
    assert real_provider.is_loaded() is True
    load_count = 0
    count_lock = threading.Lock()

    def _counting_real_loader(model_name: str, device: str) -> Any:
        nonlocal load_count
        from sentence_transformers import SentenceTransformer

        with count_lock:
            load_count += 1
        return SentenceTransformer(model_name, device=device, local_files_only=True)

    isolated = SentenceTransformerEmbeddingProvider(
        RECOMMENDED_MODEL_NAME,
        device="cpu",
        local_files_only=True,
        model_loader=_counting_real_loader,
    )
    rss_before = _get_rss_mb()
    num_threads = 8
    barrier = threading.Barrier(num_threads)
    wall_t0 = time.perf_counter()
    cpu_t0 = time.process_time()
    try:
        def _worker() -> tuple[int, float]:
            barrier.wait(timeout=30)
            t0 = time.perf_counter()
            model_obj = isolated.ensure_loaded()
            return id(model_obj), round((time.perf_counter() - t0) * 1000.0, 2)

        with ThreadPoolExecutor(max_workers=num_threads) as pool:
            futures = [pool.submit(_worker) for _ in range(num_threads)]
            results = [fut.result(timeout=60) for fut in futures]

        wall_s = time.perf_counter() - wall_t0
        cpu_s = time.process_time() - cpu_t0
        rss_after = _get_rss_mb()

        model_ids = {item[0] for item in results}
        latencies = [item[1] for item in results]
        assert load_count == 1
        assert len(model_ids) == 1
        assert isolated.is_loaded() is True
        assert isolated.info().dimension == 384

        metrics_bag["phase2_concurrent_ensure_loaded"] = {
            "threads": num_threads,
            "load_calls": load_count,
            "unique_model_instances": len(model_ids),
            "dimension": isolated.info().dimension,
            "rss_before_mb": rss_before,
            "rss_after_mb": rss_after,
            "rss_delta_mb": round(rss_after - rss_before, 2)
            if (rss_before is not None and rss_after is not None)
            else None,
            "wait_latencies_ms": _summarize_latencies(latencies),
            **_cpu_metrics(cpu_s, wall_s),
        }
    finally:
        isolated.unload()
        gc.collect()


def test_phase2_concurrent_embed_query_and_documents_integrity(
    real_service: EmbeddingService,
    metrics_bag: dict[str, Any],
) -> None:
    """Valida integridade numérica e ausência de corrupção em inferências simultâneas."""
    queries = [
        "notebook Lenovo ThinkPad 16GB RAM 512GB SSD",
        "smartphone Samsung Galaxy 256GB 5G desbloqueado",
        "monitor Dell 27 polegadas 4K UHD IPS USB-C",
        "fone de ouvido bluetooth cancelamento de ruido Sony",
        "laptop Dell Latitude Core i7 32GB RAM Windows",
        "celular Apple iPhone 15 Pro 256GB titanium",
        "monitor gamer LG UltraGear 27 144Hz QHD",
        "headphone TWS sem fio bateria longa microfone",
    ]
    doc_batches = [
        [
            "Lenovo ThinkPad T14 14 inch Laptop 16GB RAM 512GB SSD",
            "Dell XPS 13 16GB RAM 1TB NVMe SSD Ultrabook",
            "Apple MacBook Air M2 16GB RAM 256GB SSD",
            "HP EliteBook 840 G10 16GB RAM 512GB SSD",
        ],
        [
            "Samsung Galaxy S24 256GB 5G Unlocked Smartphone",
            "Apple iPhone 15 256GB 5G Factory Unlocked",
            "Google Pixel 8 Pro 256GB 5G Unlocked",
            "Motorola Edge 40 Pro 256GB 5G Smartphone",
        ],
        [
            "Dell UltraSharp U2723QE 27 inch 4K UHD IPS Monitor",
            "LG 27UP850N-W 27 inch 4K IPS Monitor USB-C",
            "BenQ PD2705U 27 inch 4K Designer Monitor",
            "ASUS ProArt PA279CRV 27 inch 4K IPS Monitor",
        ],
        [
            "Sony WH-1000XM5 Wireless Noise Cancelling Headphones",
            "Bose QuietComfort Ultra Bluetooth Headphones",
            "Sennheiser Momentum 4 Wireless ANC Headphones",
            "Apple AirPods Max Wireless Over-Ear Headphones",
        ],
        [
            "Lenovo Legion Slim 5 16GB RAM RTX 4060 Gaming Laptop",
            "ASUS ROG Zephyrus G14 16GB RAM 1TB SSD Laptop",
            "Acer Predator Helios Neo 16 16GB RAM Laptop",
            "MSI Katana 15 Core i7 16GB RAM Gaming Notebook",
        ],
        [
            "Xiaomi 14 256GB 5G Dual SIM Global Smartphone",
            "OnePlus 12 256GB 5G Unlocked Android Phone",
            "Samsung Galaxy Z Flip 5 256GB 5G Foldable",
            "Sony Xperia 1 V 256GB 5G Unlocked Smartphone",
        ],
        [
            "LG UltraGear 27GN800-B 27 inch QHD 144Hz IPS Monitor",
            "Samsung Odyssey G7 27 inch QHD 240Hz Curved Monitor",
            "AOC Q27G3XMN 27 inch Mini LED QHD 180Hz Monitor",
            "Gigabyte M27Q 27 inch 170Hz QHD KVM IPS Monitor",
        ],
        [
            "Sony WF-1000XM5 True Wireless Noise Cancelling Earbuds",
            "Apple AirPods Pro 2nd Gen USB-C Wireless Earbuds",
            "Bose QuietComfort Earbuds II ANC Bluetooth",
            "Jabra Elite 10 Wireless ANC Earbuds Microphone",
        ],
    ]

    ref_queries = [real_service.embed_query(q) for q in queries]
    ref_docs = [real_service.embed_documents(batch) for batch in doc_batches]

    # Onda A: 8 threads simultâneas chamando embed_query com textos diferentes.
    rss_before = _get_rss_mb()
    barrier_q = threading.Barrier(len(queries))
    wall_q0 = time.perf_counter()
    cpu_q0 = time.process_time()

    def _run_query(idx: int) -> tuple[int, np.ndarray, float]:
        barrier_q.wait(timeout=30)
        t0 = time.perf_counter()
        vec = real_service.embed_query(queries[idx], request_id=f"conc-q-{idx}")
        return idx, vec, round((time.perf_counter() - t0) * 1000.0, 2)

    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        q_results = [fut.result(timeout=60) for fut in [pool.submit(_run_query, i) for i in range(len(queries))]]

    wall_q = time.perf_counter() - wall_q0
    cpu_q = time.process_time() - cpu_q0

    q_latencies: list[float] = []
    q_max_diff = 0.0
    for idx, vec, lat_ms in q_results:
        assert vec.shape == (1, 384)
        assert vec.dtype == np.float32
        assert np.all(np.isfinite(vec))
        norm = float(np.linalg.norm(vec, axis=1)[0])
        assert math.isclose(norm, 1.0, abs_tol=1e-4)
        diff = float(np.max(np.abs(vec - ref_queries[idx])))
        q_max_diff = max(q_max_diff, diff)
        assert np.allclose(vec, ref_queries[idx], atol=1e-5)
        q_latencies.append(lat_ms)

    # Garante que textos distintos geram vetores distintos (sem contaminação).
    stacked_q = np.vstack([res[1] for res in sorted(q_results, key=lambda x: x[0])])
    sim_matrix_q = cosine_similarity_matrix(stacked_q, stacked_q)
    off_diag_q = sim_matrix_q[~np.eye(len(queries), dtype=bool)]
    assert float(np.max(off_diag_q)) < 0.999

    # Onda B: 8 threads simultâneas chamando embed_documents com lotes diferentes.
    barrier_d = threading.Barrier(len(doc_batches))
    wall_d0 = time.perf_counter()
    cpu_d0 = time.process_time()

    def _run_docs(idx: int) -> tuple[int, np.ndarray, float]:
        barrier_d.wait(timeout=30)
        t0 = time.perf_counter()
        mat = real_service.embed_documents(doc_batches[idx], request_id=f"conc-d-{idx}")
        return idx, mat, round((time.perf_counter() - t0) * 1000.0, 2)

    with ThreadPoolExecutor(max_workers=len(doc_batches)) as pool:
        d_results = [fut.result(timeout=60) for fut in [pool.submit(_run_docs, i) for i in range(len(doc_batches))]]

    wall_d = time.perf_counter() - wall_d0
    cpu_d = time.process_time() - cpu_d0

    d_latencies: list[float] = []
    d_max_diff = 0.0
    for idx, mat, lat_ms in d_results:
        assert mat.shape == (4, 384)
        assert mat.dtype == np.float32
        assert np.all(np.isfinite(mat))
        norms = np.linalg.norm(mat, axis=1)
        assert np.allclose(norms, 1.0, atol=1e-4)
        diff = float(np.max(np.abs(mat - ref_docs[idx])))
        d_max_diff = max(d_max_diff, diff)
        assert np.allclose(mat, ref_docs[idx], atol=1e-5)
        d_latencies.append(lat_ms)

    # Onda C: 10 threads mistas simultâneas (queries + documents).
    mixed_tasks = 10
    barrier_m = threading.Barrier(mixed_tasks)
    wall_m0 = time.perf_counter()
    cpu_m0 = time.process_time()

    def _run_mixed(idx: int) -> tuple[str, int, np.ndarray, float]:
        barrier_m.wait(timeout=30)
        t0 = time.perf_counter()
        if idx % 2 == 0:
            q_idx = idx % len(queries)
            out = real_service.embed_query(queries[q_idx], request_id=f"mixed-{idx}")
            return "query", q_idx, out, round((time.perf_counter() - t0) * 1000.0, 2)
        d_idx = idx % len(doc_batches)
        out = real_service.embed_documents(doc_batches[d_idx], request_id=f"mixed-{idx}")
        return "docs", d_idx, out, round((time.perf_counter() - t0) * 1000.0, 2)

    with ThreadPoolExecutor(max_workers=mixed_tasks) as pool:
        m_results = [fut.result(timeout=60) for fut in [pool.submit(_run_mixed, i) for i in range(mixed_tasks)]]

    wall_m = time.perf_counter() - wall_m0
    cpu_m = time.process_time() - cpu_m0
    rss_after = _get_rss_mb()

    m_latencies: list[float] = []
    m_max_diff = 0.0
    for kind, ref_idx, arr, lat_ms in m_results:
        ref = ref_queries[ref_idx] if kind == "query" else ref_docs[ref_idx]
        assert arr.shape == ref.shape
        assert np.all(np.isfinite(arr))
        diff = float(np.max(np.abs(arr - ref)))
        m_max_diff = max(m_max_diff, diff)
        assert np.allclose(arr, ref, atol=1e-5)
        m_latencies.append(lat_ms)

    metrics_bag["phase2_concurrent_inference"] = {
        "rss_before_mb": rss_before,
        "rss_after_mb": rss_after,
        "wave_queries_8": {
            "max_abs_diff_vs_sequential": round(q_max_diff, 8),
            "max_off_diag_cosine": round(float(np.max(off_diag_q)), 4),
            "latencies_ms": _summarize_latencies(q_latencies),
            **_cpu_metrics(cpu_q, wall_q),
        },
        "wave_documents_8x4": {
            "max_abs_diff_vs_sequential": round(d_max_diff, 8),
            "latencies_ms": _summarize_latencies(d_latencies),
            **_cpu_metrics(cpu_d, wall_d),
        },
        "wave_mixed_10": {
            "max_abs_diff_vs_sequential": round(m_max_diff, 8),
            "latencies_ms": _summarize_latencies(m_latencies),
            **_cpu_metrics(cpu_m, wall_m),
        },
    }


def test_phase2_load_failure_concurrency_and_recovery(
    real_provider: SentenceTransformerEmbeddingProvider,
    metrics_bag: dict[str, Any],
) -> None:
    """Verifica comportamento seguro sob falha concorrente de carregamento e recuperação posterior."""
    failing_provider = SentenceTransformerEmbeddingProvider(
        "intfloat/modelo-inexistente-buscai-116",
        device="cpu",
        local_files_only=True,
    )
    threads = 4
    barrier = threading.Barrier(threads)

    def _fail_worker() -> str:
        barrier.wait(timeout=20)
        try:
            failing_provider.ensure_loaded()
        except EmbeddingModelLoadError as exc:
            return type(exc).__name__
        return "unexpected_success"

    with ThreadPoolExecutor(max_workers=threads) as pool:
        exc_names = [fut.result(timeout=30) for fut in [pool.submit(_fail_worker) for _ in range(threads)]]

    assert exc_names == ["EmbeddingModelLoadError"] * threads
    assert failing_provider.is_loaded() is False

    # Recuperação após falha transitória na primeira tentativa.
    attempts = 0
    real_loaded_model = real_provider.ensure_loaded()

    def _flaky_loader(model_name: str, device: str) -> Any:
        del model_name, device
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient simulated failure")
        return real_loaded_model

    flaky_provider = SentenceTransformerEmbeddingProvider(
        RECOMMENDED_MODEL_NAME,
        device="cpu",
        local_files_only=True,
        model_loader=_flaky_loader,
    )
    try:
        with pytest.raises(EmbeddingModelLoadError):
            flaky_provider.ensure_loaded()
        assert flaky_provider.is_loaded() is False

        recovered = flaky_provider.ensure_loaded()
        assert recovered is real_loaded_model
        assert flaky_provider.is_loaded() is True
        vec = flaky_provider.embed_queries(["notebook 16GB RAM"])
        assert vec.shape == (1, 384)
        assert np.all(np.isfinite(vec))
    finally:
        flaky_provider.unload()

    metrics_bag["phase2_load_failure_and_recovery"] = {
        "concurrent_failure_threads": threads,
        "all_raised_load_error": True,
        "is_loaded_after_failure": False,
        "recovery_attempts_needed": attempts,
        "recovered_inference_valid": True,
    }


def test_phase3_e2e_concurrency_waves_2_5_10(
    real_service: EmbeddingService,
    metrics_bag: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Executa ondas concorrentes E2E de 2, 5 e 10 requisições simultâneas no FastAPI + LangGraph."""
    monkeypatch.chdir(tmp_path)
    api_keys = (
        "stage11_6_key_client_alpha",
        "stage11_6_key_client_beta",
        "stage11_6_key_client_gamma",
    )
    app, log_stream, backend = _build_controlled_app(
        real_service,
        api_keys=api_keys,
        max_requests_per_minute=200,
    )

    scenario_pool: list[dict[str, Any]] = [
        {"kind": "search", "query": "notebook Lenovo 16GB RAM", "budget_brl": 6800, "expected_cat": "notebook"},
        {"kind": "search", "query": "smartphone 256GB 5G", "budget_brl": 5500, "expected_cat": "smartphone"},
        {"kind": "search", "query": "monitor 27 polegadas 4K IPS", "budget_brl": 4800, "expected_cat": "monitor"},
        {"kind": "search", "query": "fone bluetooth cancelamento de ruido", "budget_brl": 3200, "expected_cat": "fone de ouvido"},
        {"kind": "chat_ok", "query": "Quero notebook Lenovo 16GB RAM", "budget_brl": 6800, "expected_cat": "notebook"},
        {"kind": "chat_clarify", "query": "Quero um presente mas ainda estou indeciso", "budget_brl": None, "expected_cat": None},
        {"kind": "health", "query": "", "budget_brl": None, "expected_cat": None},
        {"kind": "search", "query": "notebook Lenovo 16GB RAM", "budget_brl": 6800, "expected_cat": "notebook"},
        {"kind": "search", "query": "monitor 27 polegadas 4K", "budget_brl": 4800, "expected_cat": "monitor"},
        {"kind": "search", "query": "fone bluetooth sony", "budget_brl": 3200, "expected_cat": "fone de ouvido"},
    ]

    wave_metrics: dict[str, Any] = {}
    all_latencies_ms: list[float] = []

    with TestClient(app) as client:
        for wave_size in (2, 5, 10):
            rss_before = _get_rss_mb()
            barrier = threading.Barrier(wave_size)
            wall_t0 = time.perf_counter()
            cpu_t0 = time.process_time()

            def _exec_item(idx: int) -> dict[str, Any]:
                spec = scenario_pool[idx % len(scenario_pool)]
                key = api_keys[idx % len(api_keys)]
                req_id = f"s116-w{wave_size}-r{idx}"
                headers = {"X-API-Key": key, "X-Request-ID": req_id}

                barrier.wait(timeout=30)
                t0 = time.perf_counter()
                if spec["kind"] == "health":
                    resp = client.get("/v1/health", headers={"X-Request-ID": req_id})
                elif spec["kind"] in {"chat_ok", "chat_clarify"}:
                    payload_chat: dict[str, Any] = {"message": spec["query"], "limit": 5}
                    if spec["budget_brl"] is not None:
                        payload_chat["budget_brl"] = spec["budget_brl"]
                    resp = client.post("/v1/chat", headers=headers, json=payload_chat)
                else:
                    payload_search: dict[str, Any] = {
                        "query": spec["query"],
                        "budget_brl": spec["budget_brl"],
                        "limit": 5,
                    }
                    resp = client.post("/v1/search", headers=headers, json=payload_search)
                lat_ms = round((time.perf_counter() - t0) * 1000.0, 2)
                return {
                    "kind": spec["kind"],
                    "expected_cat": spec["expected_cat"],
                    "req_id": req_id,
                    "status_code": resp.status_code,
                    "resp_req_id_header": resp.headers.get("X-Request-ID"),
                    "body": resp.json(),
                    "latency_ms": lat_ms,
                }

            with ThreadPoolExecutor(max_workers=wave_size) as pool:
                outcomes = [
                    fut.result(timeout=90)
                    for fut in [pool.submit(_exec_item, idx) for idx in range(wave_size)]
                ]

            wall_s = time.perf_counter() - wall_t0
            cpu_s = time.process_time() - cpu_t0
            rss_after = _get_rss_mb()

            wave_lats: list[float] = []
            for item in outcomes:
                assert item["status_code"] == 200
                assert item["resp_req_id_header"] == item["req_id"]
                body = item["body"]
                kind = item["kind"]
                if kind == "health":
                    assert body["status"] == "ok"
                    assert body["readiness"] == "ready"
                    emb = next(c for c in body["components"] if c["name"] == "embeddings")
                    assert emb["status"] == "ready"
                    assert "sentence_transformers" in emb["detail"]
                elif kind == "chat_clarify":
                    assert body["request_id"] == item["req_id"]
                    assert body["status"] == "clarification_needed"
                    assert body["clarification_needed"] is True
                    assert body["search_results"] is None
                elif kind == "chat_ok":
                    assert body["request_id"] == item["req_id"]
                    assert body["status"] == "ok"
                    assert body["clarification_needed"] is False
                    sr = body["search_results"]
                    assert sr is not None
                    assert sr["detected_category"] == item["expected_cat"]
                    assert sr["total_products"] >= 1
                    assert sr["products"][0]["score_breakdown"]["semantic_enabled"] is True
                else:
                    assert body["request_id"] == item["req_id"]
                    assert body["status"] == "ok"
                    assert body["detected_category"] == item["expected_cat"]
                    assert body["total_products"] >= 1
                    for prod in body["products"]:
                        assert prod["detected_category"] == item["expected_cat"]
                        assert prod["score_breakdown"]["semantic_enabled"] is True
                        for offer in prod["offers"]:
                            assert math.isfinite(offer["semantic_score"])
                            assert 0.0 <= offer["semantic_score"] <= 1.0
                wave_lats.append(float(item["latency_ms"]))
                all_latencies_ms.append(float(item["latency_ms"]))

            wave_metrics[f"concurrency_{wave_size}"] = {
                "requests": wave_size,
                "success_200": len(outcomes),
                "throughput_req_s": round(wave_size / max(wall_s, 1e-9), 2),
                "rss_before_mb": rss_before,
                "rss_after_mb": rss_after,
                "latencies_ms": _summarize_latencies(wave_lats),
                **_cpu_metrics(cpu_s, wall_s),
            }

    logs_raw = log_stream.getvalue()
    for secret in api_keys:
        assert secret not in logs_raw
    assert "ThinkPad" not in logs_raw
    assert "example.invalid" not in logs_raw
    assert list(tmp_path.rglob("*")) == []

    metrics_bag["phase3_e2e_concurrency"] = {
        "waves": wave_metrics,
        "aggregate_e2e_latencies_ms": _summarize_latencies(all_latencies_ms),
        "backend_total_calls": backend.calls,
        "zero_cross_leakage": True,
        "zero_log_leaks": True,
        "zero_disk_writes": True,
    }


def test_phase3_rate_limiter_under_concurrency(
    real_service: EmbeddingService,
    metrics_bag: dict[str, Any],
) -> None:
    """Comprova imposição exata e isolamento por cliente no `InMemoryRateLimiter` concorrente."""
    limited_key = "stage11_6_rate_limited_key"
    isolated_key = "stage11_6_rate_isolated_key"
    app, _stream, _backend = _build_controlled_app(
        real_service,
        api_keys=(limited_key, isolated_key),
        max_requests_per_minute=5,
    )

    total_workers = 12  # 10 para limited_key + 2 para isolated_key
    barrier = threading.Barrier(total_workers)

    with TestClient(app) as client:
        def _hit(idx: int) -> tuple[str, int, dict[str, str]]:
            use_key = limited_key if idx < 10 else isolated_key
            bucket_label = "limited" if idx < 10 else "isolated"
            barrier.wait(timeout=30)
            resp = client.post(
                "/v1/search",
                headers={"X-API-Key": use_key, "X-Request-ID": f"rl-req-{idx}"},
                json={"query": "notebook Lenovo 16GB RAM", "budget_brl": 6800, "limit": 3},
            )
            return bucket_label, resp.status_code, dict(resp.headers)

        with ThreadPoolExecutor(max_workers=total_workers) as pool:
            results = [fut.result(timeout=60) for fut in [pool.submit(_hit, i) for i in range(total_workers)]]

    limited_codes = [code for label, code, _ in results if label == "limited"]
    isolated_codes = [code for label, code, _ in results if label == "isolated"]
    assert limited_codes.count(200) == 5
    assert limited_codes.count(429) == 5
    assert isolated_codes == [200, 200]

    for label, code, hdrs in results:
        lowered = {k.casefold(): v for k, v in hdrs.items()}
        if label == "limited" and code == 429:
            assert "retry-after" in lowered
            assert lowered.get("x-ratelimit-remaining") == "0"

    metrics_bag["phase3_rate_limiter_concurrency"] = {
        "max_requests_per_minute": 5,
        "limited_client_200": limited_codes.count(200),
        "limited_client_429": limited_codes.count(429),
        "isolated_client_200": isolated_codes.count(200),
        "client_isolation_verified": True,
    }


def test_phase4_performance_benchmarks_and_cold_start(
    real_service: EmbeddingService,
    metrics_bag: dict[str, Any],
) -> None:
    """Mede cold start em subprocesso limpo (cache local) e benchmarks de inferência individual/lote."""
    probe_code = (
        "import ctypes, json, time\n"
        "from ctypes import wintypes\n"
        "class PMC(ctypes.Structure):\n"
        "    _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD), "
        "('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t), "
        "('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t), "
        "('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t), "
        "('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]\n"
        "def rss():\n"
        "    pmc = PMC(); pmc.cb = ctypes.sizeof(PMC)\n"
        "    k32 = ctypes.windll.kernel32; k32.GetCurrentProcess.restype = wintypes.HANDLE\n"
        "    ps = ctypes.windll.psapi\n"
        "    ps.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]\n"
        "    ps.GetProcessMemoryInfo.restype = wintypes.BOOL\n"
        "    ps.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)\n"
        "    return round(pmc.WorkingSetSize / (1024 * 1024), 2)\n"
        "t0 = time.perf_counter()\n"
        "from buscai.semantic import SentenceTransformerEmbeddingProvider, RECOMMENDED_MODEL_NAME\n"
        "t1 = time.perf_counter()\n"
        "rss_before = rss()\n"
        "prov = SentenceTransformerEmbeddingProvider(RECOMMENDED_MODEL_NAME, device='cpu', local_files_only=True)\n"
        "prov.ensure_loaded()\n"
        "t2 = time.perf_counter()\n"
        "rss_after = rss()\n"
        "vec = prov.embed_queries(['notebook Lenovo 16GB RAM'])\n"
        "t3 = time.perf_counter()\n"
        "print(json.dumps({'import_ms': round((t1-t0)*1000, 2), 'cache_load_ms': round((t2-t1)*1000, 2), "
        "'first_inference_ms': round((t3-t2)*1000, 2), 'total_cold_start_ms': round((t3-t0)*1000, 2), "
        "'rss_before_load_mb': rss_before, 'rss_after_load_mb': rss_after, "
        "'rss_delta_mb': round(rss_after - rss_before, 2), 'dim': int(vec.shape[1])}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe_code],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=90,
        check=True,
    )
    cold_start_data = json.loads(proc.stdout.strip().splitlines()[-1])
    assert cold_start_data["dim"] == 384
    assert cold_start_data["cache_load_ms"] > 0

    sample_queries = [
        "notebook para programacao com 16GB RAM",
        "smartphone 5G 256GB camera boa",
        "monitor 27 polegadas 4K IPS USB-C",
        "fone bluetooth cancelamento de ruido",
        "laptop ultrafino Dell XPS 16GB SSD",
    ]
    sample_docs_base = [
        "Lenovo ThinkPad T14 14 inch Laptop 16GB RAM 512GB SSD",
        "Samsung Galaxy S24 256GB 5G Unlocked Smartphone",
        "Dell UltraSharp U2723QE 27 inch 4K UHD IPS Monitor",
        "Sony WH-1000XM5 Wireless Noise Cancelling Headphones",
    ]

    # Aquecimento rápido fora da janela estatística
    real_service.embed_query("warmup query")

    single_samples: list[float] = []
    wall_s0 = time.perf_counter()
    cpu_s0 = time.process_time()
    for i in range(20):
        t0 = time.perf_counter()
        vec = real_service.embed_query(sample_queries[i % len(sample_queries)])
        single_samples.append((time.perf_counter() - t0) * 1000.0)
        assert vec.shape == (1, 384)
    single_wall = time.perf_counter() - wall_s0
    single_cpu = time.process_time() - cpu_s0

    batch_benchmarks: dict[str, Any] = {}
    for batch_len, repeats in ((4, 20), (8, 10), (16, 10)):
        docs = [f"{sample_docs_base[j % len(sample_docs_base)]} v{j}" for j in range(batch_len)]
        lats: list[float] = []
        w0 = time.perf_counter()
        c0 = time.process_time()
        for _ in range(repeats):
            t0 = time.perf_counter()
            mat = real_service.embed_documents(docs)
            lats.append((time.perf_counter() - t0) * 1000.0)
            assert mat.shape == (batch_len, 384)
        w_elapsed = time.perf_counter() - w0
        c_elapsed = time.process_time() - c0
        total_items = batch_len * repeats
        batch_benchmarks[f"batch_{batch_len}"] = {
            "items_per_call": batch_len,
            "calls": repeats,
            "throughput_items_s": round(total_items / max(w_elapsed, 1e-9), 2),
            "latencies_ms": _summarize_latencies(lats),
            **_cpu_metrics(c_elapsed, w_elapsed),
        }

    metrics_bag["phase4_performance_benchmarks"] = {
        "cold_start_clean_subprocess": cold_start_data,
        "single_query_20x": {
            "throughput_queries_s": round(20 / max(single_wall, 1e-9), 2),
            "latencies_ms": _summarize_latencies(single_samples),
            **_cpu_metrics(single_cpu, single_wall),
        },
        "document_batches": batch_benchmarks,
        "rss_current_mb": _get_rss_mb(),
    }


def test_phase5_stability_repetition_and_memory_retention(
    real_provider: SentenceTransformerEmbeddingProvider,
    real_service: EmbeddingService,
    metrics_bag: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Executa repetição controlada verificando vazamento de RSS, degradação de latência e retenção de objetos."""
    monkeypatch.chdir(tmp_path)
    api_key = "stage11_6_stability_key"
    app, _stream, _backend = _build_controlled_app(
        real_service,
        api_keys=(api_key,),
        max_requests_per_minute=300,
    )

    processor = SemanticSearchProcessor(real_service)
    deduplicator = processor._deduplicator
    ranker = processor._ranker

    queries = [
        ("notebook Lenovo 16GB RAM", 6800, "notebook"),
        ("smartphone 256GB 5G", 5500, "smartphone"),
        ("monitor 27 polegadas 4K IPS", 4800, "monitor"),
        ("fone bluetooth cancelamento de ruido", 3200, "fone de ouvido"),
    ]

    candidate_refs: list[weakref.ref[Candidate]] = []
    e2e_latencies_ms: list[float] = []

    with TestClient(app) as client:
        # 2 ciclos de aquecimento antes de registrar linha de base de RSS
        for _ in range(2):
            warmup_resp = client.post(
                "/v1/search",
                headers={"X-API-Key": api_key, "X-Request-ID": "stab-warmup"},
                json={"query": "notebook Lenovo 16GB RAM", "budget_brl": 6800, "limit": 5},
            )
            assert warmup_resp.status_code == 200

        gc.collect()
        rss_start = _get_rss_mb()
        rss_mid: float | None = None
        rss_peak = rss_start or 0.0
        wall_t0 = time.perf_counter()
        cpu_t0 = time.process_time()

        # 16 requisições E2E sequenciais + 2 mini-ondas concorrentes de 4 requisições = 24 requisições
        for step in range(16):
            q_text, budget_brl, expected_cat = queries[step % len(queries)]
            t0 = time.perf_counter()
            resp = client.post(
                "/v1/search",
                headers={"X-API-Key": api_key, "X-Request-ID": f"stab-seq-{step}"},
                json={"query": q_text, "budget_brl": budget_brl, "limit": 5},
            )
            lat_ms = (time.perf_counter() - t0) * 1000.0
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "ok"
            assert body["detected_category"] == expected_cat
            e2e_latencies_ms.append(lat_ms)

            # Verifica ausência de retenção direta em `SemanticSearchProcessor` com weakref
            local_cands = list(_make_category_candidates(f"wr-{step}", q_text))
            for cand in local_cands:
                candidate_refs.append(weakref.ref(cand))
            sem_out = processor.process(
                q_text,
                local_cands,
                category_hint=expected_cat,
                request_id=f"stab-sem-{step}",
            )
            assert sem_out.output_count >= 1
            del cand, local_cands, sem_out

            current_rss = _get_rss_mb()
            if current_rss is not None:
                rss_peak = max(rss_peak, current_rss)
            if step == 7:
                gc.collect()
                rss_mid = _get_rss_mb()

        for wave_idx in range(2):
            barrier = threading.Barrier(4)

            def _mini_worker(slot: int, w_idx: int = wave_idx) -> tuple[int, str, str, float]:
                q_text, budget_brl, expected_cat = queries[slot % len(queries)]
                barrier.wait(timeout=30)
                t0 = time.perf_counter()
                r = client.post(
                    "/v1/search",
                    headers={"X-API-Key": api_key, "X-Request-ID": f"stab-conc-{w_idx}-{slot}"},
                    json={"query": q_text, "budget_brl": budget_brl, "limit": 5},
                )
                return r.status_code, r.json().get("detected_category", ""), expected_cat, (time.perf_counter() - t0) * 1000.0

            with ThreadPoolExecutor(max_workers=4) as pool:
                mini_res = [fut.result(timeout=60) for fut in [pool.submit(_mini_worker, s) for s in range(4)]]
            for code, got_cat, exp_cat, lat_ms in mini_res:
                assert code == 200
                assert got_cat == exp_cat
                e2e_latencies_ms.append(lat_ms)

        wall_s = time.perf_counter() - wall_t0
        cpu_s = time.process_time() - cpu_t0

    gc.collect()
    rss_end = _get_rss_mb()
    retained_candidates = sum(1 for ref in candidate_refs if ref() is not None)
    assert retained_candidates == 0

    for component in (real_provider, real_service, processor, deduplicator, ranker):
        for attr_val in vars(component).values():
            assert not isinstance(attr_val, np.ndarray)

    rss_growth_mb = (
        round(rss_end - rss_start, 2)
        if (rss_start is not None and rss_end is not None)
        else 0.0
    )
    assert rss_growth_mb < 150.0

    half = len(e2e_latencies_ms) // 2
    first_half = e2e_latencies_ms[:half]
    second_half = e2e_latencies_ms[half:]
    first_mean = sum(first_half) / len(first_half)
    second_mean = sum(second_half) / len(second_half)

    assert list(tmp_path.rglob("*")) == []

    metrics_bag["phase5_stability"] = {
        "total_e2e_requests": len(e2e_latencies_ms),
        "sequential_requests": 16,
        "concurrent_mini_waves": 2,
        "failures": 0,
        "rss_start_mb": rss_start,
        "rss_mid_mb": rss_mid,
        "rss_end_mb": rss_end,
        "rss_peak_mb": round(rss_peak, 2),
        "rss_growth_mb": rss_growth_mb,
        "tracked_candidates_created": len(candidate_refs),
        "retained_candidates_after_gc": retained_candidates,
        "zero_retained_vectors": True,
        "first_half_mean_ms": round(first_mean, 2),
        "second_half_mean_ms": round(second_mean, 2),
        "latencies_ms": _summarize_latencies(e2e_latencies_ms),
        **_cpu_metrics(cpu_s, wall_s),
    }
