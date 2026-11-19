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
        price=Decimal("499.00"),
        currency="USD",
        brand=brand,
        model=model,
        short_description=short_description,
        aspects=aspects or {},
        condition=condition,
        details_loaded=details_loaded,
    )


def test_dedup_groups_same_notebook_model_with_different_titles_and_picks_richest() -> None:
    service = EmbeddingService(DeterministicStubEmbeddingProvider())
    dedup = ProductDeduplicator(service)

    sparse = _cand(
        "nb-1",
        "Lenovo ThinkPad X1 Carbon Gen 11 14 inch Laptop 16GB RAM 512GB SSD",
        brand="Lenovo",
        model="ThinkPad X1 Carbon Gen 11",
    )
    rich = _cand(
        "nb-2",
        "ThinkPad X1 Carbon Gen 11 Notebook Lenovo 16GB/512GB 14 inch Business",
        brand="Lenovo",
        model="ThinkPad X1 Carbon Gen 11",
        aspects={"Processor": ("Core i7",), "RAM Size": ("16 GB",)},
        details_loaded=True,
    )

    result = dedup.deduplicate([sparse, rich], category_hint="notebook")
    assert len(result.candidates) == 1
    assert result.removed_count == 1
    # Deve selecionar o candidato mais rico (`details_loaded=True` e `aspects`) preservando ambos os item_ids.
    assert result.candidates[0].item_id == "nb-2"
    assert set(result.groups[0].duplicate_item_ids) == {"nb-1", "nb-2"}


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
    c_a = _cand("tr-1", "Apple iPhone 15 128GB", brand="Apple", model="iPhone 15")
    c_b = _cand("tr-2", "Apple iPhone 15 128GB Unlocked", brand="Apple", model="iPhone 15")
    c_c = _cand("tr-3", "Apple iPhone 15 256GB", brand="Apple", model="iPhone 15")
    vecs_3 = np.tile(np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32), (3, 1))
    res_3 = dedup.deduplicate([c_a, c_b, c_c], vectors=vecs_3)
    assert len(res_3.candidates) == 2
    kept_tr = {c.item_id for c in res_3.candidates}
    assert "tr-3" in kept_tr
    assert ("tr-1" in kept_tr) ^ ("tr-2" in kept_tr)

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
