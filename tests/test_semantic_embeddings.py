"""Testes unitários do serviço de embeddings, provedores locais/stub, lotes e ranker."""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest

from buscai.config import load_settings
from buscai.search.models import Candidate
from buscai.semantic import (
    ALTERNATIVE_MODEL_NAME,
    RECOMMENDED_MODEL_NAME,
    DeterministicStubEmbeddingProvider,
    EmbeddingInferenceError,
    EmbeddingModelLoadError,
    EmbeddingService,
    FastEmbedEmbeddingProvider,
    InMemorySemanticRanker,
    SentenceTransformerEmbeddingProvider,
    cosine_similarity_matrix,
)


def test_model_specs_and_from_settings_select_backend() -> None:
    cfg_st = load_settings(environ={})
    svc_st = EmbeddingService.from_settings(cfg_st)
    assert svc_st.model_info().model_name == RECOMMENDED_MODEL_NAME
    assert svc_st.model_info().backend == "sentence_transformers"
    assert svc_st.model_info().dimension == 384
    assert svc_st.model_info().requires_prefixes is True
    assert svc_st.is_ready() is False

    cfg_fe = load_settings(
        environ={
            "BUSCAAI_EMBEDDING_BACKEND": "fastembed",
            "BUSCAAI_EMBEDDING_MODEL": ALTERNATIVE_MODEL_NAME,
            "BUSCAAI_EMBEDDING_BATCH_SIZE": "8",
        }
    )
    svc_fe = EmbeddingService.from_settings(cfg_fe)
    assert svc_fe.model_info().backend == "fastembed"
    assert svc_fe.model_info().model_name == ALTERNATIVE_MODEL_NAME
    assert svc_fe.model_info().requires_prefixes is False


def test_sentence_transformer_provider_loads_once_applies_prefixes_and_batches() -> None:
    load_calls = 0
    encode_calls: list[dict[str, object]] = []

    class FakeSTModel:
        def get_sentence_embedding_dimension(self) -> int:
            return 4

        def encode(self, texts, *, batch_size, normalize_embeddings, show_progress_bar, convert_to_numpy):
            encode_calls.append({"texts": list(texts), "batch_size": batch_size})
            return np.ones((len(texts), 4), dtype=np.float32) * 2.0

    def loader(name: str, device: str):
        nonlocal load_calls
        load_calls += 1
        assert name == RECOMMENDED_MODEL_NAME
        assert device == "cpu"
        return FakeSTModel()

    provider = SentenceTransformerEmbeddingProvider(RECOMMENDED_MODEL_NAME, model_loader=loader)
    service = EmbeddingService(provider, batch_size=2)

    q_vec = service.embed_query("notebook Lenovo")
    d_vecs = service.embed_documents(["Lenovo Laptop 1", "Lenovo Laptop 2", "Dell Laptop"])

    assert load_calls == 1  # pesos reutilizados em memória sem recarregamento
    assert service.is_ready() is True
    assert q_vec.shape == (1, 4)
    assert q_vec.dtype == np.float32
    assert d_vecs.shape == (3, 4)
    assert np.allclose(np.linalg.norm(d_vecs, axis=1), 1.0)
    assert encode_calls[0]["texts"] == ["query: notebook Lenovo"]
    assert encode_calls[1]["texts"] == [
        "passage: Lenovo Laptop 1",
        "passage: Lenovo Laptop 2",
        "passage: Dell Laptop",
    ]
    assert encode_calls[1]["batch_size"] == 2

    provider.unload()
    assert provider.is_loaded() is False


def test_fastembed_provider_batches_and_handles_load_and_inference_errors() -> None:
    load_calls = 0

    class FakeFastEmbed:
        def embed(self, batch):
            for _ in batch:
                yield np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)

    def loader(name: str):
        nonlocal load_calls
        load_calls += 1
        return FakeFastEmbed()

    provider = FastEmbedEmbeddingProvider(ALTERNATIVE_MODEL_NAME, model_loader=loader, dimension=4)
    vecs = provider.embed_documents(["item a", "item b", "item c"], batch_size=2)
    assert load_calls == 1
    assert vecs.shape == (3, 4)

    failing_loader = SentenceTransformerEmbeddingProvider(
        RECOMMENDED_MODEL_NAME,
        model_loader=lambda _n, _d: (_ for _ in ()).throw(RuntimeError("download failed")),
    )
    with pytest.raises(EmbeddingModelLoadError, match="Falha ao carregar modelo"):
        failing_loader.embed_queries(["notebook"])

    class BrokenInference:
        def encode(self, texts, **_kw):
            return np.full((len(texts), 384), np.nan, dtype=np.float32)

    bad_inf = SentenceTransformerEmbeddingProvider(
        RECOMMENDED_MODEL_NAME,
        model_loader=lambda _n, _d: BrokenInference(),
    )
    with pytest.raises(EmbeddingInferenceError, match="NaN"):
        bad_inf.embed_documents(["notebook"])


def test_stub_provider_cross_lingual_similarity_validation_and_ranker() -> None:
    service = EmbeddingService(DeterministicStubEmbeddingProvider())

    with pytest.raises(ValueError, match="vazio"):
        service.embed_query("   ")
    with pytest.raises(ValueError, match="vazia"):
        service.embed_documents([])
    with pytest.raises(ValueError, match="não vazias"):
        service.embed_documents(["ok", "  "])
    with pytest.raises(ValueError, match="vazia"):
        service.embed_candidates([])

    ranker = InMemorySemanticRanker(service)
    candidates = [
        Candidate(
            item_id="mouse",
            source="ebay",
            title="Logitech Wireless Mouse",
            price=Decimal("25.00"),
            currency="USD",
        ),
        Candidate(
            item_id="monitor-fhd",
            source="ebay",
            title="AOC 24 inch Full HD 1080p Gaming Monitor",
            price=Decimal("150.00"),
            currency="USD",
        ),
        Candidate(
            item_id="monitor-4k",
            source="ebay",
            title="Dell 27 inch 4K UHD IPS Monitor for Work",
            price=Decimal("420.00"),
            currency="USD",
        ),
    ]

    ranked = ranker.rank("monitor 4K IPS para trabalhar", candidates)
    assert len(ranked) == 3
    assert ranked[0].candidate.item_id == "monitor-4k"
    assert 0.0 <= ranked[0].semantic_score <= 1.0
    assert ranked[0].semantic_score > ranked[-1].semantic_score

    sim = cosine_similarity_matrix(service.embed_query("fone Bluetooth"), service.embed_query("Bluetooth headphones"))
    assert sim[0, 0] > 0.5
