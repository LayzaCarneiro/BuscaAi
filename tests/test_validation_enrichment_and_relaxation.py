"""Testes unitários de enriquecimento seletivo (`getItem`) e relaxamento controlado."""

from __future__ import annotations

from decimal import Decimal

import httpx

from buscai.pricing import CurrencyConverter, FixedEnvExchangeRateProvider
from buscai.search import EbayLiveBackend, EbayOAuthClient, QuotaGuard
from buscai.search.models import Candidate, SearchQuery, SearchResponse
from buscai.search.query_builder import QueryBuilder
from buscai.search.quota import RequestCallBudget
from buscai.validation import (
    EvidenceValidator,
    RelaxationConfig,
    RelaxationEngine,
    SelectiveEnricher,
)
from userintent.schema import PriceConstraint, UserIntent


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

        assert len(called_ids) == 2
        assert set(summary.enriched_item_ids) == {"v1|need-1|0", "v1|need-2|0"}
        assert summary.calls_used == 2
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

        budget = guard_429.new_request_budget(max_search_calls=1, max_get_item_calls=1)
        enricher = SelectiveEnricher(backend_429, max_items=4, max_concurrency=2)
        out, summary = enricher.enrich(vals, budget=budget, request_id="req-429")

        assert summary.skipped_due_to_budget == 1
        assert len(summary.selected_item_ids) == 1
        assert out[0].item_id == "v1|c1|0"
        assert out[0].details_loaded is False
        assert out[1].item_id == "v1|c2|0"
    finally:
        backend_429.close()
        oauth_429.close()

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


class _FakeSearchBackend:
    def __init__(self, extra_candidates: list[Candidate]) -> None:
        self._extra = extra_candidates
        self.search_queries: list[SearchQuery] = []

    def search(
        self, query: SearchQuery, *, budget: RequestCallBudget | None = None, request_id: str | None = None
    ) -> SearchResponse:
        if budget is not None:
            QuotaGuard().consume("search", budget=budget, request_id=request_id)
        self.search_queries.append(query)
        return SearchResponse(candidates=tuple(self._extra), total_estimated=len(self._extra), limit=query.limit)

    def get_item_details(self, item_id: str, *, budget=None, request_id=None):
        return None


def test_relaxation_skips_when_eligible_results_are_sufficient() -> None:
    validator = EvidenceValidator()
    engine = RelaxationEngine(config=RelaxationConfig(min_eligible_results=2))
    intent = UserIntent(category="notebook", preferred_features=["teclado retroiluminado"], query="notebook")
    cands = [
        Candidate(item_id="1", source="ebay", title="Lenovo Laptop Backlit Keyboard", price=Decimal("500"), currency="USD", categories=("Laptops",)),
        Candidate(item_id="2", source="ebay", title="Dell Laptop Backlit Keyboard", price=Decimal("550"), currency="USD", categories=("Laptops",)),
    ]
    vals = [validator.validate_candidate(c, intent) for c in cands]
    out_c, out_v, steps = engine.run(candidates=cands, initial_validations=vals, intent=intent, validator=validator)
    assert len(out_c) == 2
    assert len(out_v) == 2
    assert steps == ()


def test_relaxation_order_limits_and_never_relaxes_hard_constraints() -> None:
    converter = CurrencyConverter(FixedEnvExchangeRateProvider(rate="5.50"))
    validator = EvidenceValidator(currency_converter=converter, require_shipping_for_total=False)
    qb = QueryBuilder(currency_converter=converter)

    intent = UserIntent(
        category="notebook",
        brands=["Lenovo"],
        required_features=["16 GB de RAM"],
        preferred_features=["teclado retroiluminado", "USB-C"],
        use_cases=["estudos"],
        price=PriceConstraint(max_price=4400, is_hard_constraint=True, currency="BRL"),
        query="notebook Lenovo 16GB RAM",
    )

    extra = [
        Candidate(
            item_id="rel-ok",
            source="ebay",
            title="Lenovo ThinkPad 16GB RAM 256GB SSD",
            price=Decimal("750.00"),
            currency="USD",
            brand="Lenovo",
            condition="NEW",
            categories=("Laptops",),
        ),
        Candidate(
            item_id="rel-over-budget",
            source="ebay",
            title="Lenovo ThinkPad 16GB RAM 512GB SSD",
            price=Decimal("950.00"),
            currency="USD",
            brand="Lenovo",
            condition="NEW",
            categories=("Laptops",),
        ),
        Candidate(
            item_id="rel-wrong-brand",
            source="ebay",
            title="HP ProBook 16GB RAM",
            price=Decimal("600.00"),
            currency="USD",
            brand="HP",
            condition="NEW",
            categories=("Laptops",),
        ),
    ]
    backend = _FakeSearchBackend(extra)
    budget = RequestCallBudget(max_search_calls=1, max_get_item_calls=0)
    engine = RelaxationEngine(
        config=RelaxationConfig(min_eligible_results=3, max_relaxation_steps=2, max_extra_search_calls=1)
    )

    _, final_vals, steps = engine.run(
        candidates=[],
        initial_validations=[],
        intent=intent,
        validator=validator,
        search_backend=backend,
        query_builder=qb,
        budget=budget,
    )

    assert len(steps) == 2
    assert steps[0].field == "preferred_feature"
    assert steps[0].original_value == "teclado retroiluminado"
    assert steps[1].field == "preferred_feature"
    assert steps[1].original_value == "USB-C"
    assert len(backend.search_queries) == 1
    assert backend.search_queries[0].max_price_usd == Decimal("800.00")

    by_id = {v.candidate.item_id: v for v in final_vals}
    assert by_id["rel-ok"].eligible is True
    assert by_id["rel-over-budget"].eligible is False
    assert by_id["rel-wrong-brand"].eligible is False


def test_relaxation_can_relax_soft_budget_when_not_hard_constraint() -> None:
    converter = CurrencyConverter(FixedEnvExchangeRateProvider(rate="5.50"))
    validator = EvidenceValidator(currency_converter=converter, require_shipping_for_total=False)
    intent = UserIntent(
        category="monitor",
        price=PriceConstraint(max_price=1100, is_hard_constraint=False, currency="BRL"),
        query="monitor",
    )
    cand = Candidate(
        item_id="mn-250",
        source="ebay",
        title="Dell 27 inch Monitor",
        price=Decimal("250.00"),
        currency="USD",
        categories=("Monitors",),
    )
    vals = [validator.validate_candidate(cand, intent)]
    assert vals[0].eligible is True

    engine = RelaxationEngine(config=RelaxationConfig(min_eligible_results=2, max_relaxation_steps=2))
    _, final_vals, steps = engine.run(candidates=[cand], initial_validations=vals, intent=intent, validator=validator)
    assert len(steps) == 1
    assert steps[0].field == "soft_budget"
    assert final_vals[0].budget_evaluation is None
