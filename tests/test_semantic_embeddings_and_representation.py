"""Testes unitários do serviço de embeddings, provedores locais/stub, representação textual e extração de atributos."""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest

from buscai.config import load_settings
from buscai.search.models import Candidate, SellerInfo, ShippingInfo
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
from buscai.semantic.representation import (
    build_candidate_embedding_text,
    build_query_embedding_text,
    build_semantic_query_text,
    extract_product_attributes,
)
from userintent.schema import PriceConstraint, UserIntent


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

    assert load_calls == 1
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


def test_a14_e5_uses_query_prefix_for_symmetric_dedup_and_passage_for_retrieval() -> None:
    encode_calls: list[list[str]] = []

    class FakeE5Model:
        def get_sentence_embedding_dimension(self) -> int:
            return 4

        def encode(self, texts, **_kwargs):
            encode_calls.append(list(texts))
            return np.ones((len(texts), 4), dtype=np.float32)

    provider = SentenceTransformerEmbeddingProvider(RECOMMENDED_MODEL_NAME, model_loader=lambda _n, _d: FakeE5Model())
    service = EmbeddingService(provider)
    assert service.model_info().symmetric_prefix == "query: "

    cands = [
        Candidate(item_id="1", source="ebay", title="Lenovo ThinkPad X1 Carbon", price=Decimal("900"), currency="USD"),
        Candidate(item_id="2", source="ebay", title="ThinkPad X1 Carbon Lenovo", price=Decimal("880"), currency="USD"),
    ]
    service.embed_candidates(cands, mode="symmetric_similarity")
    service.embed_candidates(cands, mode="retrieval_document")

    assert all(text.startswith("query: ") for text in encode_calls[0])
    assert all(text.startswith("passage: ") for text in encode_calls[1])


def test_build_candidate_embedding_text_excludes_non_comparison_and_sensitive_fields() -> None:
    cand = Candidate(
        item_id="v1|SENTINEL_ID|0",
        source="ebay",
        title="Lenovo ThinkPad T14 14 inch Laptop",
        price=Decimal("849.99"),
        currency="USD",
        condition="USED",
        categories=("Computers", "Laptops"),
        brand="Lenovo",
        model="ThinkPad T14",
        short_description="16GB RAM 512GB NVMe SSD",
        aspects={
            "Processor": ("Core i7",),
            "SellerNote": ("SENTINEL_ASPECT_SELLER",),
            "ExternalUrl": ("https://ebay.example/SENTINEL_ASPECT_URL",),
            "NAField": ("Does not apply",),
        },
        image_url="https://img.example.com/SENTINEL_IMAGE.jpg",
        item_web_url="https://ebay.example.com/itm/SENTINEL_WEB_URL",
        seller=SellerInfo(username="SENTINEL_SELLER_USER", feedback_score=900, feedback_percentage=Decimal("99.5")),
        shipping=ShippingInfo(cost=Decimal("19.99"), currency="USD", free_shipping=False),
        buying_options=("FIXED_PRICE",),
        details_loaded=True,
    )

    text = build_candidate_embedding_text(cand)
    assert "Lenovo ThinkPad T14 14 inch Laptop" in text
    assert "Model: ThinkPad T14" not in text
    assert "Processor: Core i7" in text
    assert "Category: Computers > Laptops" in text

    cand_with_extra_model = cand.model_copy(update={"model": "21HD001XUS"})
    assert "Model: 21HD001XUS" in build_candidate_embedding_text(cand_with_extra_model)

    for forbidden in (
        "SENTINEL_ID",
        "849.99",
        "19.99",
        "SENTINEL_IMAGE",
        "SENTINEL_WEB_URL",
        "SENTINEL_SELLER_USER",
        "SENTINEL_ASPECT_SELLER",
        "SENTINEL_ASPECT_URL",
        "Does not apply",
        "FIXED_PRICE",
    ):
        assert forbidden not in text

    with pytest.raises(ValueError, match="vazio"):
        build_query_embedding_text("   ")
    assert build_query_embedding_text("  notebook   Lenovo  ") == "notebook Lenovo"


def test_extract_structured_attributes_across_categories_and_specs() -> None:
    laptop = Candidate(
        item_id="1",
        source="ebay",
        title="Lenovo ThinkPad X1 Carbon Gen 11 14 inch Notebook 16GB/512GB SSD",
        price=Decimal("900.00"),
        currency="USD",
        condition="NEW",
        aspects={"MPN": ("21HM0001US",), "UPC": ("Does not apply",)},
    )
    lattr = extract_product_attributes(laptop)
    assert lattr.canonical_category == "notebook"
    assert lattr.brand == "lenovo"
    assert lattr.ram_gb == 16
    assert lattr.storage_gb == 512
    assert lattr.screen_size_inches == Decimal("14")
    assert lattr.manufacturer_ids == ("21HM0001US",)
    assert lattr.condition_bucket == "new"
    assert {"thinkpad", "x1", "carbon", "gen", "11"}.issubset(set(lattr.model_tokens))

    iphone = Candidate(
        item_id="2",
        source="ebay",
        title="Apple iPhone 15 Pro 256GB 5G Unlocked Smartphone",
        price=Decimal("899.00"),
        currency="USD",
    )
    iattr = extract_product_attributes(iphone)
    assert iattr.canonical_category == "smartphone"
    assert iattr.brand == "apple"
    assert iattr.storage_gb == 256
    assert "pro" in iattr.model_tokens
    assert "5g" in iattr.connectivity_features

    monitor = Candidate(
        item_id="3",
        source="ebay",
        title='Dell UltraSharp 27" 4K UHD IPS Monitor 144Hz USB-C P/N:U2723QE',
        price=Decimal("550.00"),
        currency="USD",
    )
    mattr = extract_product_attributes(monitor)
    assert mattr.canonical_category == "monitor"
    assert mattr.brand == "dell"
    assert mattr.screen_size_inches == Decimal("27")
    assert mattr.resolution == "uhd_4k"
    assert mattr.refresh_rate_hz == 144
    assert mattr.panel_type == "ips"
    assert "usb-c" in mattr.connectivity_features
    assert "U2723QE" in mattr.manufacturer_ids

    headphones = Candidate(
        item_id="4",
        source="ebay",
        title="Sony WH-1000XM5 Wireless Over-Ear ANC Bluetooth Headphones",
        price=Decimal("320.00"),
        currency="USD",
        condition="Certified - Refurbished",
    )
    hattr = extract_product_attributes(headphones)
    assert hattr.canonical_category == "fone de ouvido"
    assert hattr.brand == "sony"
    assert hattr.form_factor == "over-ear"
    assert hattr.condition_bucket == "refurbished"
    assert "anc" in hattr.connectivity_features
    assert "bluetooth" in hattr.connectivity_features
    assert "wh1000xm5" in hattr.model_tokens


def test_extract_attributes_keeps_missing_fields_as_none() -> None:
    minimal = Candidate(
        item_id="min-1",
        source="ebay",
        title="Generic Laptop Item",
        price=Decimal("200.00"),
        currency="USD",
    )
    attrs = extract_product_attributes(minimal)
    assert attrs.brand is None
    assert attrs.ram_gb is None
    assert attrs.storage_gb is None
    assert attrs.resolution is None
    assert attrs.manufacturer_ids == ()


def test_a13_embedding_text_prioritizes_technical_specs_over_alphabetical_noise() -> None:
    many_aspects = {f"AlphaNoise{idx:02d}": (f"Extra marketing filler value {idx}",) for idx in range(45)}
    many_aspects.update(
        {
            "RAM Size": ("32 GB",),
            "SSD Capacity": ("1 TB",),
            "Processor": ("Intel Core i7-1365U",),
            "Screen Size": ("14 in",),
            "MPN": ("21HM0001US",),
        }
    )
    cand = Candidate(
        item_id="nb-a13",
        source="ebay",
        title="Lenovo ThinkPad X1 Carbon Gen 11 Laptop",
        price=Decimal("1299.00"),
        currency="USD",
        brand="Lenovo",
        model="ThinkPad X1 Carbon Gen 11",
        aspects=many_aspects,
    )

    text = build_candidate_embedding_text(cand, category_hint="notebook", max_chars=500)
    assert len(text) <= 500
    assert "Processor: Intel Core i7-1365U" in text
    assert "RAM Size: 32 GB" in text
    assert "SSD Capacity: 1 TB" in text
    assert "Screen Size: 14 in" in text
    assert "MPN: 21HM0001US" in text
    assert "AlphaNoise44" not in text


def test_a14_build_semantic_query_text_strips_transactional_budget_noise() -> None:
    intent = UserIntent(
        category="notebook",
        brands=["Lenovo"],
        models=["ThinkPad"],
        required_features=["16GB RAM"],
        use_cases=["programação"],
        price=PriceConstraint(max_price=5000, is_hard_constraint=True, currency="BRL"),
        query="notebook Lenovo ThinkPad até R$ 5.000 para programação",
    )
    from_intent = build_semantic_query_text(intent.query, intent=intent)
    assert "5000" not in from_intent and "5.000" not in from_intent
    assert "Lenovo" in from_intent
    assert "16GB RAM" in from_intent

    from_raw = build_semantic_query_text("monitor 4K IPS até R$ 2.500")
    assert "2.500" not in from_raw
    assert "monitor 4K IPS" in from_raw
