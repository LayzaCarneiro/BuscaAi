"""Testes unitários de representação textual e extração de atributos estruturados."""

from __future__ import annotations

from decimal import Decimal

import pytest

from buscai.search.models import Candidate, SellerInfo, ShippingInfo
from buscai.semantic.representation import (
    build_candidate_embedding_text,
    build_query_embedding_text,
    extract_product_attributes,
)


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
    assert "Model: ThinkPad T14" not in text  # não duplica modelo já presente no título
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
