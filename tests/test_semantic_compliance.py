"""Testes estritos de conformidade eBay para a Etapa 4 (não persistência de embeddings/anúncios)."""

from __future__ import annotations

import ast
import gc
import io
import weakref
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from buscai.observability import configure_logging
from buscai.search.models import Candidate, SellerInfo, ShippingInfo
from buscai.semantic import (
    DeterministicStubEmbeddingProvider,
    EmbeddingService,
    InMemorySemanticRanker,
    ProductDeduplicator,
    SemanticSearchProcessor,
)

ROOT = Path(__file__).resolve().parents[1]
SEMANTIC_DIR = ROOT / "src" / "buscai" / "semantic"


class _TrackableArray(np.ndarray):
    """Subclasse leve para permitir `weakref.ref` sobre vetores gerados em teste."""


class _TrackingStubProvider:
    def __init__(
        self,
        *,
        fail_inference: bool = False,
        captured_doc_texts: list[str] | None = None,
        vector_weakrefs: list[weakref.ref[object]] | None = None,
    ) -> None:
        self._inner = DeterministicStubEmbeddingProvider()
        self._fail_inference = fail_inference
        self.doc_calls = 0
        self.query_calls = 0
        self._captured_doc_texts = captured_doc_texts
        self._vector_weakrefs = vector_weakrefs

    def info(self):
        return self._inner.info()

    def is_loaded(self) -> bool:
        return self._inner.is_loaded()

    def embed_queries(self, texts, *, batch_size: int = 32) -> np.ndarray:
        self.query_calls += 1
        arr = self._inner.embed_queries(texts, batch_size=batch_size).view(_TrackableArray)
        if self._vector_weakrefs is not None:
            self._vector_weakrefs.append(weakref.ref(arr))
        return arr

    def embed_documents(self, texts, *, batch_size: int = 32) -> np.ndarray:
        self.doc_calls += 1
        if self._captured_doc_texts is not None:
            self._captured_doc_texts.extend(texts)
        if self._fail_inference:
            raise RuntimeError("Inference failed on title=SENTINEL_TITLE_SECRET")
        arr = self._inner.embed_documents(texts, batch_size=batch_size).view(_TrackableArray)
        if self._vector_weakrefs is not None:
            self._vector_weakrefs.append(weakref.ref(arr))
        return arr


def _walk_object_graph_for_forbidden(obj: object, forbidden_substrings: tuple[str, ...]) -> None:
    visited: set[int] = set()
    stack: list[object] = [obj]
    while stack:
        current = stack.pop()
        obj_id = id(current)
        if obj_id in visited:
            continue
        visited.add(obj_id)

        assert not isinstance(current, np.ndarray), "Vetor np.ndarray encontrado retido em estado persistido."
        assert not isinstance(current, Candidate), "Candidate encontrado retido em serviço."

        if isinstance(current, str):
            for forbidden in forbidden_substrings:
                assert forbidden not in current, f"String proibida retida em memória de serviço: {forbidden}"
        elif isinstance(current, dict):
            stack.extend(current.keys())
            stack.extend(current.values())
        elif isinstance(current, (list, tuple, set, frozenset)):
            stack.extend(current)
        elif hasattr(current, "__dict__") and type(current).__module__.startswith("buscai.semantic"):
            stack.append(vars(current))


def test_semantic_pipeline_has_zero_disk_writes_zero_cache_and_transient_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    sentinel_cands = [
        Candidate(
            item_id="v1|SENTINEL_ITEM_ID_1|0",
            source="ebay",
            title="SENTINEL_TITLE_XYZ Lenovo ThinkPad T14 14 inch Laptop",
            price=Decimal("777.77"),
            currency="USD",
            brand="Lenovo",
            model="ThinkPad T14",
            short_description="SENTINEL_DESC_XYZ 16GB RAM 512GB SSD",
            image_url="https://img.example/SENTINEL_IMG_XYZ.jpg",
            item_web_url="https://ebay.example/itm/SENTINEL_URL_XYZ",
            seller=SellerInfo(username="SENTINEL_SELLER_XYZ"),
            shipping=ShippingInfo(cost=Decimal("12.34"), currency="USD"),
        ),
        Candidate(
            item_id="v1|SENTINEL_ITEM_ID_2|0",
            source="ebay",
            title="Dell XPS 13 Laptop 16GB RAM 512GB SSD",
            price=Decimal("888.88"),
            currency="USD",
            brand="Dell",
            model="XPS 13",
        ),
    ]

    log_stream = io.StringIO()
    logger = configure_logging("DEBUG", stream=log_stream, logger_name="buscai.test.semantic.compliance")
    captured_doc_texts: list[str] = []
    vector_weakrefs: list[weakref.ref[object]] = []
    provider = _TrackingStubProvider(
        captured_doc_texts=captured_doc_texts,
        vector_weakrefs=vector_weakrefs,
    )
    service = EmbeddingService(provider, logger=logger)
    dedup = ProductDeduplicator(service, logger=logger)
    ranker = InMemorySemanticRanker(service, logger=logger)
    processor = SemanticSearchProcessor(service, deduplicator=dedup, ranker=ranker, logger=logger)

    # Executa duas vezes a mesma consulta e mesmos candidatos -> zero cache de vetores/anúncios
    res1 = processor.process("notebook Lenovo", sentinel_cands, request_id="req-comp-1")
    res2 = processor.process("notebook Lenovo", sentinel_cands, request_id="req-comp-2")
    assert provider.doc_calls == 2
    assert provider.query_calls == 2
    assert len(res1.candidates) == 2 and len(res2.candidates) == 2

    # Garante que campos não comparativos jamais são enviados ao gerador de embeddings
    joined_inputs = "\n".join(captured_doc_texts)
    for forbidden_in_embedding in (
        "SENTINEL_ITEM_ID_1",
        "SENTINEL_URL_XYZ",
        "SENTINEL_IMG_XYZ",
        "SENTINEL_SELLER_XYZ",
        "777.77",
        "888.88",
        "12.34",
    ):
        assert forbidden_in_embedding not in joined_inputs

    # Verifica ausência de vetores ou dados de anúncios retidos nos serviços após garbage collection
    del res1, res2, captured_doc_texts, joined_inputs
    gc.collect()
    assert all(ref() is None for ref in vector_weakrefs)

    # Limpa listas do spy antes da inspeção do grafo de objetos dos serviços reais
    provider._captured_doc_texts = None
    provider._vector_weakrefs = None
    for service_obj in (processor, service, dedup, ranker):
        _walk_object_graph_for_forbidden(
            vars(service_obj),
            ("SENTINEL_TITLE_XYZ", "SENTINEL_DESC_XYZ", "SENTINEL_URL_XYZ", "SENTINEL_SELLER_XYZ"),
        )

    # Logs contêm apenas metadados operacionais
    logs_raw = log_stream.getvalue()
    for forbidden_log in (
        "SENTINEL_TITLE_XYZ",
        "SENTINEL_DESC_XYZ",
        "SENTINEL_URL_XYZ",
        "SENTINEL_SELLER_XYZ",
        "SENTINEL_ITEM_ID_1",
        "777.77",
    ):
        assert forbidden_log not in logs_raw

    # Zero persistência em disco
    assert list(tmp_path.rglob("*")) == []


def test_semantic_errors_never_leak_listing_strings_in_logs() -> None:
    log_stream = io.StringIO()
    logger = configure_logging("DEBUG", stream=log_stream, logger_name="buscai.test.semantic.err")
    provider = _TrackingStubProvider(fail_inference=True)
    processor = SemanticSearchProcessor(EmbeddingService(provider, logger=logger), logger=logger)

    cand = Candidate(
        item_id="v1|1|0",
        source="ebay",
        title="SENTINEL_TITLE_SECRET Laptop",
        price=Decimal("500.00"),
        currency="USD",
    )
    with pytest.raises(RuntimeError):
        processor.process("laptop", [cand], request_id="req-err-1")

    assert "SENTINEL_TITLE_SECRET" not in log_stream.getvalue()


def test_semantic_modules_do_not_import_persistence_caches_or_network_clients() -> None:
    forbidden_roots = {
        "sqlite3",
        "pickle",
        "shelve",
        "tempfile",
        "httpx",
        "requests",
        "urllib",
        "chromadb",
        "faiss",
        "qdrant_client",
        "sqlalchemy",
        "psycopg",
        "langgraph",
        "langchain",
    }

    offenders: list[str] = []
    for py_file in sorted(SEMANTIC_DIR.glob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in forbidden_roots:
                        offenders.append(f"{py_file.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                if root in forbidden_roots:
                    offenders.append(f"{py_file.name}: from {node.module}")

    assert offenders == [], f"Imports proibidos em src/buscai/semantic/: {offenders}"
