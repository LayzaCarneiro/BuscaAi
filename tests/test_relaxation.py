"""Testes unitários do mecanismo determinístico de relaxamento controlado (T5.5)."""

from __future__ import annotations

from decimal import Decimal

from buscai.pricing import CurrencyConverter, FixedEnvExchangeRateProvider
from buscai.search.models import Candidate, SearchQuery, SearchResponse
from buscai.search.query_builder import QueryBuilder
from buscai.search.quota import QuotaGuard, RequestCallBudget
from buscai.validation import (
    EvidenceValidator,
    RelaxationConfig,
    RelaxationEngine,
)
from userintent.schema import PriceConstraint, UserIntent


class _FakeSearchBackend:
    def __init__(self, extra_candidates: list[Candidate]) -> None:
        self._extra = extra_candidates
        self.search_queries: list[SearchQuery] = []

    def search(self, query: SearchQuery, *, budget: RequestCallBudget | None = None, request_id: str | None = None) -> SearchResponse:
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

    # Intent com hard constraints (categoria=notebook, brand=Lenovo, required=16GB RAM, orçamento rígido=4400 BRL = 800 USD)
    # + 2 preferências flexíveis + perfil study
    intent = UserIntent(
        category="notebook",
        brands=["Lenovo"],
        required_features=["16 GB de RAM"],
        preferred_features=["teclado retroiluminado", "USB-C"],
        use_cases=["estudos"],
        price=PriceConstraint(max_price=4400, is_hard_constraint=True, currency="BRL"),
        query="notebook Lenovo 16GB RAM",
    )

    # Candidatos retornados na busca relaxada incluem:
    # - 1 candidato válido dentro do orçamento (750 USD <= 800 USD)
    # - 1 candidato acima do orçamento rígido (950 USD > 800 USD -> deve continuar REJEITADO!)
    # - 1 candidato da marca errada (HP -> deve continuar REJEITADO!)
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

    # Respeitou limite de max_relaxation_steps=2 e max_extra_search_calls=1
    assert len(steps) == 2
    assert steps[0].field == "preferred_feature"
    assert steps[0].original_value == "teclado retroiluminado"
    assert steps[1].field == "preferred_feature"
    assert steps[1].original_value == "USB-C"
    assert len(backend.search_queries) == 1
    # Orçamento rígido nunca é removido da query enviada
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
        price=PriceConstraint(max_price=1100, is_hard_constraint=False, currency="BRL"),  # 200 USD flexível
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
    assert vals[0].eligible is True  # soft budget not_satisfied não torna inelegível, mas relaxamento registra passo

    engine = RelaxationEngine(config=RelaxationConfig(min_eligible_results=2, max_relaxation_steps=2))
    _, final_vals, steps = engine.run(candidates=[cand], initial_validations=vals, intent=intent, validator=validator)
    assert len(steps) == 1
    assert steps[0].field == "soft_budget"
    assert final_vals[0].budget_evaluation is None
