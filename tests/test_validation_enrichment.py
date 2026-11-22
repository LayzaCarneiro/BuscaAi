"""Testes unitários da integração seletiva com `getItem` / `enrich_candidates` (T5.4)."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from buscai.search import EbayLiveBackend, EbayOAuthClient, QuotaGuard
from buscai.search.models import Candidate
from buscai.validation import EvidenceValidator, SelectiveEnricher
from userintent.schema import UserIntent


def _make_backend(handler, *, daily_limit: int = 100) -> tuple[EbayLiveBackend, EbayOAuthClient, QuotaGuard]:
    oauth = EbayOAuthClient(
        client_id="cid",
        client_secret="csec",
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})),
    )
    guard = QuotaGuard(daily_limit=daily_limit, default_max_get_item_per_request=4)
    backend = EbayLiveBackend(
        oauth_client=oauth,
        quota_guard=guard,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    return backend, oauth, guard


def test_selective_enricher_prioritizes_incomplete_candidates_and_preserves_distinct_offers() -> None:
    called_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        item_id = request.url.path.rsplit("/", 1)[-1]
        called_ids.append(item_id)
        if "need-1" in item_id:
            return httpx.Response(
                200,
                json={
                    "itemId": "v1|need-1|0",
                    "title": "Lenovo ThinkPad X1 Laptop",
                    "price": {"value": "900.00", "currency": "USD"},
                    "localizedAspects": [{"name": "RAM Size", "value": "16 GB"}],
                },
            )
        if "need-2" in item_id:
            return httpx.Response(
                200,
                json={
                    "itemId": "v1|need-2|0",
                    "title": "Lenovo ThinkPad X1 Laptop",
                    "price": {"value": "880.00", "currency": "USD"},
                    "localizedAspects": [{"name": "RAM Size", "value": "16 GB"}],
                },
            )
        return httpx.Response(404, json={"errors": []})

    backend, oauth, guard = _make_backend(handler)
    try:
        validator = EvidenceValidator()
        intent = UserIntent(category="notebook", required_features=["16 GB de RAM"], query="notebook 16GB RAM")

        already_complete = Candidate(
            item_id="v1|complete|0",
            source="ebay",
            title="Lenovo ThinkPad X1 16GB RAM",
            price=Decimal("950.00"),
            currency="USD",
            categories=("Laptops",),
            details_loaded=True,
        )
        rejected_wrong_cat = Candidate(
            item_id="v1|wrong|0",
            source="ebay",
            title="Dell 27 inch Monitor",
            price=Decimal("300.00"),
            currency="USD",
            categories=("Monitors",),
        )
        offer_seller_a = Candidate(
            item_id="v1|need-1|0",
            source="ebay",
            title="Lenovo ThinkPad X1 Laptop",
            price=Decimal("900.00"),
            currency="USD",
            categories=("Laptops",),
        )
        offer_seller_b = Candidate(
            item_id="v1|need-2|0",
            source="ebay",
            title="Lenovo ThinkPad X1 Laptop",
            price=Decimal("880.00"),
            currency="USD",
            categories=("Laptops",),
        )

        validations = [
            validator.validate_candidate(c, intent)
            for c in (already_complete, rejected_wrong_cat, offer_seller_a, offer_seller_b)
        ]
        budget = guard.new_request_budget(max_search_calls=1, max_get_item_calls=3)
        enricher = SelectiveEnricher(backend, max_items=3, max_concurrency=2)
        enriched, summary = enricher.enrich(validations, budget=budget, request_id="req-enr-1")

        # Não chama getItem para `complete` (details_loaded=True) nem para `wrong` (já rejeitado).
        assert len(called_ids) == 2
        assert set(summary.enriched_item_ids) == {"v1|need-1|0", "v1|need-2|0"}
        assert summary.calls_used == 2
        # Ambas as ofertas distintas permanecem preservadas com seus preços originais.
        assert len(enriched) == 4
        assert enriched[2].aspects["RAM Size"] == ("16 GB",)
        assert enriched[3].aspects["RAM Size"] == ("16 GB",)
        assert enriched[2].price == Decimal("900.00")
        assert enriched[3].price == Decimal("880.00")
    finally:
        backend.close()
        oauth.close()


def test_selective_enricher_handles_http_errors_429_and_budget_exhaustion() -> None:
    def handler_429(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "1"})

    backend_429, oauth_429, guard_429 = _make_backend(handler_429)
    try:
        validator = EvidenceValidator()
        intent = UserIntent(category="notebook", required_features=["16 GB de RAM"], query="notebook 16GB RAM")
        c1 = Candidate(
            item_id="v1|c1|0",
            source="ebay",
            title="Lenovo ThinkPad Laptop",
            price=Decimal("700.00"),
            currency="USD",
            categories=("Laptops",),
        )
        c2 = Candidate(
            item_id="v1|c2|0",
            source="ebay",
            title="Dell Latitude Laptop",
            price=Decimal("720.00"),
            currency="USD",
            categories=("Laptops",),
        )
        vals = [validator.validate_candidate(c1, intent), validator.validate_candidate(c2, intent)]

        # Orçamento restrito a 1 chamada getItem -> c2 é ignorado por orçamento
        budget = guard_429.new_request_budget(max_search_calls=1, max_get_item_calls=1)
        enricher = SelectiveEnricher(backend_429, max_items=4, max_concurrency=2)
        out, summary = enricher.enrich(vals, budget=budget, request_id="req-429")

        assert summary.skipped_due_to_budget == 1
        assert len(summary.selected_item_ids) == 1
        # HTTP 429 é tratado sem abortar e preserva o candidato original
        assert out[0].item_id == "v1|c1|0"
        assert out[0].details_loaded is False
        assert out[1].item_id == "v1|c2|0"
    finally:
        backend_429.close()
        oauth_429.close()

    # Falha 5xx também preserva candidatos originais sem crash
    backend_500, oauth_500, guard_500 = _make_backend(lambda _r: httpx.Response(503))
    try:
        vals = [validator.validate_candidate(c1, intent)]
        budget = guard_500.new_request_budget(max_search_calls=1, max_get_item_calls=2)
        out_500, summary_500 = SelectiveEnricher(backend_500, max_items=2).enrich(vals, budget=budget)
        assert len(out_500) == 1
        assert out_500[0].details_loaded is False
        assert summary_500.unchanged_item_ids == ("v1|c1|0",)
    finally:
        backend_500.close()
        oauth_500.close()
