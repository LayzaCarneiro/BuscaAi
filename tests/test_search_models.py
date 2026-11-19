"""Testes dos modelos canônicos de busca e candidatos (SearchQuery, Candidate, SearchResponse)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from buscai.search.models import (
    Candidate,
    SearchQuery,
    SearchResponse,
    SellerInfo,
    ShippingInfo,
)


def test_minimal_candidate_preserves_missing_fields_as_none() -> None:
    candidate = Candidate(
        item_id=" v1|12345|0 ",
        source=" ebay ",
        title=" Example Laptop 16GB ",
        price=Decimal("799.90"),
        currency="usd",
    )

    assert candidate.item_id == "v1|12345|0"
    assert candidate.source == "ebay"
    assert candidate.title == "Example Laptop 16GB"
    assert candidate.price == Decimal("799.90")
    assert candidate.currency == "USD"
    assert candidate.condition is None
    assert candidate.categories == ()
    assert candidate.brand is None
    assert candidate.model is None
    assert candidate.short_description is None
    assert candidate.aspects == {}
    assert candidate.image_url is None
    assert candidate.item_web_url is None
    assert candidate.seller is None
    assert candidate.shipping is None
    assert candidate.buying_options == ()
    assert candidate.details_loaded is False


def test_full_candidate_normalizes_seller_shipping_aspects_and_urls() -> None:
    candidate = Candidate(
        item_id="v1|999|0",
        source="ebay",
        title="Monitor 27 IPS",
        price="249.50",
        currency="usd",
        condition="NEW",
        categories=["Computers", "Monitors", "monitors"],
        brand="Acme",
        model="M27",
        short_description="  27-inch QHD monitor  ",
        aspects={
            "Refresh Rate": ["165 Hz", " 165 hz "],
            "Empty": ["", "   "],
            "   ": ["ignored"],
        },
        image_url="https://img.example.com/m27.jpg",
        item_web_url="https://www.ebay.example.com/itm/999",
        seller=SellerInfo(
            username=" top_seller ",
            feedback_score=1500,
            feedback_percentage=Decimal("99.4"),
        ),
        shipping=ShippingInfo(
            cost=Decimal("0.00"),
            currency="usd",
            free_shipping=True,
            estimated_min_days=3,
            estimated_max_days=7,
        ),
        buying_options=["FIXED_PRICE", "fixed_price"],
        details_loaded=True,
    )

    assert candidate.categories == ("Computers", "Monitors")
    assert candidate.aspects == {"Refresh Rate": ("165 Hz",)}
    assert candidate.buying_options == ("FIXED_PRICE",)
    assert candidate.seller is not None and candidate.seller.username == "top_seller"
    assert candidate.shipping is not None and candidate.shipping.currency == "USD"
    assert candidate.details_loaded is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"item_id": "  ", "source": "ebay", "title": "X", "price": Decimal("10"), "currency": "USD"},
        {"item_id": "1", "source": "ebay", "title": " ", "price": Decimal("10"), "currency": "USD"},
        {"item_id": "1", "source": "ebay", "title": "X", "price": Decimal("-1"), "currency": "USD"},
        {"item_id": "1", "source": "ebay", "title": "X", "price": Decimal("NaN"), "currency": "USD"},
        {"item_id": "1", "source": "ebay", "title": "X", "price": True, "currency": "USD"},
        {"item_id": "1", "source": "ebay", "title": "X", "price": Decimal("10"), "currency": "US"},
        {"item_id": "1", "source": "ebay", "title": "X", "price": Decimal("10"), "currency": "123"},
        {
            "item_id": "1",
            "source": "ebay",
            "title": "X",
            "price": Decimal("10"),
            "currency": "USD",
            "image_url": "ftp://img.example.com/a.jpg",
        },
        {
            "item_id": "1",
            "source": "ebay",
            "title": "X",
            "price": Decimal("10"),
            "currency": "USD",
            "item_web_url": "javascript:alert(1)",
        },
    ],
)
def test_candidate_rejects_invalid_fields(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Candidate(**kwargs)  # type: ignore[arg-type]


def test_shipping_info_rejects_inverted_delivery_days() -> None:
    with pytest.raises(ValidationError, match="estimated_max_days"):
        ShippingInfo(estimated_min_days=7, estimated_max_days=3)


def test_search_query_defaults_and_validation() -> None:
    query = SearchQuery(
        query_text="  gaming laptop 16gb  ",
        marketplace_id=" ebay_us ",
        min_price_usd=Decimal("300"),
        max_price_usd=Decimal("800"),
        currency="usd",
        conditions=["NEW", "new"],
        condition_ids=["2000", "2010", "2000"],
    )

    assert query.query_text == "gaming laptop 16gb"
    assert query.marketplace_id == "EBAY_US"
    assert query.currency == "USD"
    assert query.conditions == ("NEW",)
    assert query.condition_ids == ("2000", "2010")
    assert query.fixed_price_only is True
    assert query.limit == 30
    assert query.offset == 0

    with pytest.raises(ValidationError):
        SearchQuery(query_text="   ")
    with pytest.raises(ValidationError):
        SearchQuery(query_text="laptop", condition_ids=["NEW"])
    with pytest.raises(ValidationError):
        SearchQuery(query_text="laptop", min_price_usd=Decimal("900"), max_price_usd=Decimal("500"))
    with pytest.raises(ValidationError):
        SearchQuery(query_text="laptop", limit=0)
    with pytest.raises(ValidationError):
        SearchQuery(query_text="laptop", limit=201)


def test_search_response_holds_candidates_and_metadata() -> None:
    item = Candidate(
        item_id="1",
        source="ebay",
        title="Synthetic Headphones",
        price=Decimal("99.99"),
        currency="USD",
    )
    response = SearchResponse(
        candidates=(item,),
        total_estimated=150,
        limit=30,
        offset=0,
        calls_used=1,
    )

    assert len(response.candidates) == 1
    assert response.total_estimated == 150
    assert response.calls_used == 1
