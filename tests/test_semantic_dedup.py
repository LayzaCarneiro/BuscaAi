"""Testes unitários da deduplicação híbrida de produtos (`ProductDeduplicator`)."""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest

from buscai.search.models import Candidate
from buscai.semantic import (
    DeduplicationConfig,
    DeterministicStubEmbeddingProvider,
    EmbeddingService,
    ProductDeduplicator,
)


def _cand(
    item_id: str,
    title: str,
    *,
    price: str = "499.00",
    brand: str | None = None,
    model: str | None = None,
    short_description: str | None = None,
    aspects: dict[str, tuple[str, ...]] | None = None,
    condition: str | None = None,
    details_loaded: bool = False,
) -> Candidate:
    return Candidate(
        item_id=item_id,
        source="ebay",
        title=title,
        price=Decimal(price),
        currency="USD",
        brand=brand,
        model=model,
        short_description=short_description,
        aspects=aspects or {},
        condition=condition,
        details_loaded=details_loaded,
    )


def test_dedup_groups_same_notebook_model_and_preserves_distinct_offers() -> None:
    service = EmbeddingService(DeterministicStubEmbeddingProvider())
    dedup = ProductDeduplicator(service)

    sparse = _cand(
        "nb-1",
        "Lenovo ThinkPad X1 Carbon Gen 11 14 inch Laptop 16GB RAM 512GB SSD",
        price="899.00",
        brand="Lenovo",
        model="ThinkPad X1 Carbon Gen 11",
    )
    rich = _cand(
        "nb-2",
        "ThinkPad X1 Carbon Gen 11 Notebook Lenovo 16GB/512GB 14 inch Business",
        price="929.00",
        brand="Lenovo",
        model="ThinkPad X1 Carbon Gen 11",
        aspects={"Processor": ("Core i7",), "RAM Size": ("16 GB",)},
        details_loaded=True,
    )

    result = dedup.deduplicate([sparse, rich], category_hint="notebook")
    # Ofertas distintas (item_ids diferentes) permanecem disponíveis em `candidates` (A1),
    # enquanto `groups` / `canonical_candidates` agrupam a identidade do produto.
    assert len(result.candidates) == 2
    assert result.removed_count == 0
    assert result.grouped_offers_count == 1
    assert len(result.groups) == 1
    assert result.groups[0].canonical.item_id == "nb-2"
    assert len(result.groups[0].offers) == 2
    assert set(result.groups[0].duplicate_item_ids) == {"nb-1", "nb-2"}

    # Mesmo anúncio repetido em consultas distintas (mesmo item_id) é colapsado.
    dup_listing = dedup.deduplicate([sparse, sparse.model_copy(update={"details_loaded": True}), rich], category_hint="notebook")
    assert dup_listing.removed_count == 1
    assert len(dup_listing.candidates) == 2
    assert len(dup_listing.groups) == 1


def test_dedup_never_merges_variants_or_distinct_models_even_with_extreme_cosine() -> None:
    dedup = ProductDeduplicator()

    # Caso A: dois notebooks Lenovo de modelos diferentes (ThinkPad X1 vs IdeaPad 3) com cosseno 0.99
    thinkpad = _cand("len-1", "Lenovo ThinkPad X1 Carbon 14 inch Laptop 16GB RAM 512GB SSD", brand="Lenovo", model="ThinkPad X1 Carbon")
    ideapad = _cand("len-2", "Lenovo IdeaPad 3 14 inch Laptop 16GB RAM 512GB SSD", brand="Lenovo", model="IdeaPad 3")

    # Caso B: iPhone 128 GB vs iPhone 256 GB com cosseno 0.99
    iphone_128 = _cand("ip-128", "Apple iPhone 15 128GB Unlocked Smartphone", brand="Apple", model="iPhone 15")
    iphone_256 = _cand("ip-256", "Apple iPhone 15 256GB Unlocked Smartphone", brand="Apple", model="iPhone 15")

    # Caso C: iPhone 15 vs iPhone 15 Pro (mesmo armazenamento) com cosseno 0.99
    iphone_base = _cand("ip-std", "Apple iPhone 15 256GB Unlocked Smartphone", brand="Apple", model="iPhone 15")
    iphone_pro = _cand("ip-pro", "Apple iPhone 15 Pro 256GB Unlocked Smartphone", brand="Apple", model="iPhone 15 Pro")

    # Caso D: Monitor Full HD vs Monitor 4K com cosseno 0.99
    mon_fhd = _cand("mon-fhd", "Dell 27 inch Full HD 1080p IPS Monitor USB-C", brand="Dell", model="P2722H")
    mon_4k = _cand("mon-4k", "Dell 27 inch 4K UHD IPS Monitor USB-C", brand="Dell", model="S2722QC")

    identical_vectors = np.tile(np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32), (2, 1))

    for left, right, expected_reason in (
        (thinkpad, ideapad, "distinct_model_codes"),
        (iphone_128, iphone_256, "storage_mismatch"),
        (iphone_base, iphone_pro, "conflicting_model_variants"),
        (mon_fhd, mon_4k, "resolution_mismatch"),
    ):
        ev = dedup.classify_pair(left, right, semantic_similarity=0.99)
        assert ev.classification == "similar_product"
        assert expected_reason in ev.reasons

        res = dedup.deduplicate([left, right], vectors=identical_vectors)
        assert res.removed_count == 0
        assert len(res.candidates) == 2
        assert len(res.similar_pairs) == 1


def test_dedup_preserves_on_uncertainty_and_respects_safe_transitivity_and_thresholds() -> None:
    dedup = ProductDeduplicator()

    # Incerteza: um anúncio informa 256GB e outro omite capacidade -> preserva ambos
    with_storage = _cand("unc-1", "Apple iPhone 15 256GB Smartphone", brand="Apple", model="iPhone 15")
    without_storage = _cand("unc-2", "Apple iPhone 15 Smartphone", brand="Apple", model="iPhone 15")
    ev_unc = dedup.classify_pair(with_storage, without_storage, semantic_similarity=0.95)
    assert ev_unc.classification == "similar_product"
    assert "uncertain_storage_variant" in ev_unc.reasons

    # Produtos totalmente diferentes -> different_product
    phone = _cand("diff-1", "Apple iPhone 15 256GB Unlocked", brand="Apple", model="iPhone 15")
    monitor = _cand("diff-2", "LG 27 inch 4K UHD IPS Monitor", brand="LG", model="27UP850")
    ev_diff = dedup.classify_pair(phone, monitor, semantic_similarity=0.10)
    assert ev_diff.classification == "different_product"
    assert "category_mismatch" in ev_diff.reasons

    # Mesmo MPN sem conflitos -> probable_duplicate mesmo com texto curto variado
    mpn_a = _cand("mpn-1", "Dell Monitor 27 4K", brand="Dell", aspects={"MPN": ("U2723QE",)})
    mpn_b = _cand("mpn-2", "UltraSharp 27 4K IPS Display", brand="Dell", aspects={"MPN": ("U2723QE",)})
    ev_mpn = dedup.classify_pair(mpn_a, mpn_b, semantic_similarity=0.60)
    assert ev_mpn.classification == "probable_duplicate"
    assert "shared_manufacturer_id" in ev_mpn.reasons

    # Mesmo item_id -> sempre probable_duplicate
    assert dedup.classify_pair(phone, phone, semantic_similarity=0.0).classification == "probable_duplicate"

    # Proteção contra transitividade falsa: A (128GB) e C (256GB) nunca vão para o mesmo grupo
    c_a = _cand("tr-1", "Apple iPhone 15 128GB", price="699.00", brand="Apple", model="iPhone 15")
    c_b = _cand("tr-2", "Apple iPhone 15 128GB Unlocked", price="679.00", brand="Apple", model="iPhone 15")
    c_c = _cand("tr-3", "Apple iPhone 15 256GB", price="799.00", brand="Apple", model="iPhone 15")
    vecs_3 = np.tile(np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32), (3, 1))
    res_3 = dedup.deduplicate([c_a, c_b, c_c], vectors=vecs_3)
    # Todas as 3 ofertas distintas são preservadas, mas divididas em 2 grupos de produto (128GB vs 256GB).
    assert len(res_3.candidates) == 3
    assert len(res_3.groups) == 2
    group_sets = [set(g.duplicate_item_ids) for g in res_3.groups]
    assert {"tr-1", "tr-2"} in group_sets
    assert {"tr-3"} in group_sets

    # Limiares configuráveis ajustados controlam classificação quando estrutura não é conflituosa
    strict_cfg = DeduplicationConfig(
        probable_duplicate_semantic_threshold=0.95,
        rephrased_model_semantic_threshold=0.92,
        similar_product_semantic_threshold=0.70,
    )
    strict_dedup = ProductDeduplicator(config=strict_cfg)
    ev_strict = strict_dedup.classify_pair(mpn_a, mpn_b.model_copy(update={"aspects": {}}), semantic_similarity=0.88)
    assert ev_strict.classification == "similar_product"

    with pytest.raises(ValueError, match="similar_product_semantic_threshold"):
        DeduplicationConfig(
            probable_duplicate_semantic_threshold=0.80,
            rephrased_model_semantic_threshold=0.75,
            similar_product_semantic_threshold=0.78,
        )


@pytest.mark.parametrize(
    ("left_kwargs", "right_kwargs", "expected_reason"),
    [
        (
            {"item_id": "v1", "title": "Apple iPhone 13 128GB Unlocked", "brand": "Apple", "model": "iPhone 13"},
            {"item_id": "v2", "title": "Apple iPhone 13 mini 128GB Unlocked", "brand": "Apple", "model": "iPhone 13 mini"},
            "conflicting_model_variants",
        ),
        (
            {"item_id": "v3", "title": "Samsung Galaxy S24 256GB Smartphone", "brand": "Samsung", "model": "Galaxy S24"},
            {"item_id": "v4", "title": "Samsung Galaxy S24+ 256GB Smartphone", "brand": "Samsung", "model": "Galaxy S24+"},
            "conflicting_model_variants",
        ),
        (
            {"item_id": "v5", "title": "Apple iPhone 15 128GB", "brand": "Apple", "model": "iPhone 15", "condition": "NEW"},
            {"item_id": "v6", "title": "Apple iPhone 15 128GB", "brand": "Apple", "model": "iPhone 15", "condition": "USED"},
            "condition_mismatch",
        ),
        (
            {"item_id": "v7", "title": "Dell XPS 13 9315 Laptop 16GB 512GB", "brand": "Dell", "model": "XPS 13 9315", "condition": "NEW"},
            {"item_id": "v8", "title": "Dell XPS 13 9315 Laptop 16GB 512GB", "brand": "Dell", "model": "XPS 13 9315", "condition": "Certified - Refurbished"},
            "condition_mismatch",
        ),
        (
            {"item_id": "v9", "title": "Sony WH-1000XM5 Headphones", "brand": "Sony", "model": "WH-1000XM5", "condition": "Open Box"},
            {"item_id": "v10", "title": "Sony WH-1000XM5 Headphones", "brand": "Sony", "model": "WH-1000XM5", "condition": "USED"},
            "condition_mismatch",
        ),
        (
            {"item_id": "v11", "title": "Sony Wireless Headphones Bluetooth", "brand": "Sony"},
            {"item_id": "v12", "title": "Sony WH-1000XM5 Wireless Headphones Bluetooth", "brand": "Sony", "model": "WH-1000XM5"},
            "one_sided_model_identity",
        ),
        (
            {"item_id": "v13", "title": "Apple iPhone Smartphone", "brand": "Apple"},
            {"item_id": "v14", "title": "Apple iPhone 15 Smartphone", "brand": "Apple", "model": "iPhone 15"},
            "one_sided_model_code",
        ),
        (
            {"item_id": "v15", "title": "Bose QuietComfort Wireless Headphones", "brand": "Bose", "model": "QuietComfort"},
            {"item_id": "v16", "title": "Bose SoundLink Wireless Headphones", "brand": "Bose", "model": "SoundLink"},
            "distinct_model_lines",
        ),
    ],
)
def test_c1_dedup_never_merges_variants_conditions_or_generic_vs_specific(
    left_kwargs: dict[str, object],
    right_kwargs: dict[str, object],
    expected_reason: str,
) -> None:
    dedup = ProductDeduplicator()
    left = _cand(**left_kwargs)  # type: ignore[arg-type]
    right = _cand(**right_kwargs)  # type: ignore[arg-type]

    ev = dedup.classify_pair(left, right, semantic_similarity=0.95)
    assert ev.classification != "probable_duplicate"
    assert expected_reason in ev.reasons

    vecs = np.tile(np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32), (2, 1))
    res = dedup.deduplicate([left, right], vectors=vecs)
    assert len(res.groups) == 2
    assert len(res.candidates) == 2


def test_c1_plus_and_plus_word_normalize_to_same_variant_and_canonical_ignores_title_length() -> None:
    dedup = ProductDeduplicator()
    s24_plus_symbol = _cand(
        "s24-1",
        "Samsung Galaxy S24+ 256GB",
        price="799.00",
        brand="Samsung",
        model="Galaxy S24+",
    )
    s24_plus_word = _cand(
        "s24-2",
        "Samsung Galaxy S24 Plus 256GB Smartphone Unlocked Brand New Warranty Included Extra Long Stuffed Keywords",
        price="899.00",
        brand="Samsung",
        model="Galaxy S24 Plus",
    )
    ev = dedup.classify_pair(s24_plus_symbol, s24_plus_word, semantic_similarity=0.92)
    assert ev.classification == "probable_duplicate"

    vecs = np.tile(np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32), (2, 1))
    res = dedup.deduplicate([s24_plus_symbol, s24_plus_word], vectors=vecs)
    # Ambos permanecem disponíveis (A1), mas no grupo único o canônico é o de menor preço (não o título mais longo).
    assert len(res.candidates) == 2
    assert len(res.groups) == 1
    assert res.groups[0].canonical.item_id == "s24-1"
    assert {c.item_id for c in res.groups[0].offers} == {"s24-1", "s24-2"}


def test_a10_dedup_avoids_retaining_pair_evaluations_by_default_and_matches_full_eval() -> None:
    dedup = ProductDeduplicator()
    cands = [
        _cand(f"p-{idx}", f"Brand{idx % 5} Model{idx} 16GB RAM 512GB SSD Laptop", brand=f"Brand{idx % 5}", model=f"Model{idx}")
        for idx in range(25)
    ]
    rng = np.random.default_rng(42)
    vecs = rng.normal(size=(len(cands), 16)).astype(np.float32)

    fast_res = dedup.deduplicate(cands, vectors=vecs)
    full_res = dedup.deduplicate(cands, vectors=vecs, include_pair_evaluations=True)

    assert fast_res.pair_evaluations == ()
    assert len(full_res.pair_evaluations) == len(cands) * (len(cands) - 1) // 2
    assert [g.duplicate_item_ids for g in fast_res.groups] == [g.duplicate_item_ids for g in full_res.groups]
