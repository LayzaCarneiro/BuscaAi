"""Testes de integração da Etapa 1 (IntentExtractor mockado, fluxo atual e contratos SearchBackend)."""

from __future__ import annotations

import io
import json
import logging
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from buscai.agent import run_search_flow
from buscai.catalog import Product, ProductPage
from buscai.catalog.errors import ProviderRateLimitError
from buscai.config import load_settings
from buscai.observability import configure_logging, log_operational_event
from buscai.search import (
    Candidate,
    QuotaGuard,
    RequestCallBudget,
    SearchBackend,
    SearchQuery,
    SearchResponse,
)
from userintent.llm import IntentExtractor
from userintent.schema import UserIntent


class _FakeModelsAPI:
    def __init__(self, handler) -> None:
        self._handler = handler
        self.calls: list[dict[str, object]] = []

    def generate_content(self, *, model: str, contents: str, config: object) -> object:
        self.calls.append({"model": model, "contents": contents, "config": config})
        return self._handler(model=model, contents=contents, config=config)


class _FakeGenAIClient:
    def __init__(self, handler) -> None:
        self.models = _FakeModelsAPI(handler)


class _InMemoryCatalog:
    def __init__(self, products: list[Product]) -> None:
        self.products = products
        self.calls = 0

    def list_products(self, *, limit: int = 30, skip: int = 0) -> ProductPage:
        self.calls += 1
        return ProductPage(
            products=tuple(self.products[skip : skip + limit]),
            total=len(self.products),
            skip=skip,
            limit=limit,
        )


def test_intent_extractor_uses_parsed_response_and_ignores_extra_fields() -> None:
    fake_client = _FakeGenAIClient(
        lambda **_kwargs: SimpleNamespace(
            parsed={
                "category": "notebook",
                "price": {"max_price": 5000, "is_hard_constraint": True, "extra_price_note": "ignored"},
                "brands": ["Lenovo"],
                "use_cases": ["programação", "estudos"],
                "query": "notebook Lenovo",
                "confidence": 0.99,
            },
            text=None,
        )
    )
    extractor = IntentExtractor(model="gemini-3.5-flash-lite", client=fake_client)

    intent = extractor.extract("Quero um notebook Lenovo até 5000")

    assert isinstance(intent, UserIntent)
    assert intent.category == "notebook"
    assert intent.brands == ["Lenovo"]
    assert "confidence" not in intent.model_dump()
    assert "extra_price_note" not in intent.price.model_dump()
    assert len(fake_client.models.calls) == 1
    cfg = fake_client.models.calls[0]["config"]
    assert getattr(cfg, "response_mime_type") == "application/json"
    assert getattr(cfg, "response_schema") is UserIntent


def test_intent_extractor_falls_back_to_json_text_and_ignores_extra_fields() -> None:
    payload = json.dumps(
        {
            "category": "notebook",
            "query": "notebook Lenovo",
            "extra_llm_note": "should_be_ignored",
        }
    )
    fake_client = _FakeGenAIClient(lambda **_kwargs: SimpleNamespace(parsed=None, text=payload))
    extractor = IntentExtractor(model="gemini-3.5-flash-lite", client=fake_client)

    intent = extractor.extract("Quero um notebook Lenovo")

    assert intent.category == "notebook"
    assert intent.query == "notebook Lenovo"
    assert "extra_llm_note" not in intent.model_dump()


def test_intent_extractor_handles_empty_malformed_and_provider_errors() -> None:
    empty_extractor = IntentExtractor(
        model="gemini-3.5-flash-lite",
        client=_FakeGenAIClient(lambda **_kwargs: SimpleNamespace(parsed=None, text="")),
    )
    with pytest.raises(RuntimeError, match="não retornou conteúdo"):
        empty_extractor.extract("notebook")

    invalid_json_extractor = IntentExtractor(
        model="gemini-3.5-flash-lite",
        client=_FakeGenAIClient(lambda **_kwargs: SimpleNamespace(parsed=None, text="{invalid")),
    )
    with pytest.raises(ValidationError):
        invalid_json_extractor.extract("notebook")

    def raise_unavailable(**_kwargs):
        raise RuntimeError("503 Unavailable")

    failing_extractor = IntentExtractor(
        model="gemini-3.5-flash-lite",
        client=_FakeGenAIClient(raise_unavailable),
    )
    with pytest.raises(RuntimeError, match="503 Unavailable"):
        failing_extractor.extract("notebook")


def test_e2e_stage1_search_flow_respects_budget_and_category_constraints() -> None:
    user_query = "Quero um notebook Lenovo com até 5.000 reais, para programação e estudos."
    llm_json = json.dumps(
        {
            "category": "notebook",
            "price": {
                "min_price": None,
                "max_price": 5000,
                "min_inclusive": True,
                "max_inclusive": True,
                "is_hard_constraint": True,
            },
            "brands": ["Lenovo"],
            "excluded_brands": [],
            "use_cases": ["programação", "estudos"],
            "profile_context": [],
            "required_features": [],
            "preferred_features": [],
            "excluded_features": [],
            "query": "notebook",
            "clarification_needed": False,
            "clarification_question": None,
            "reasoning": "extra_field_from_llm",
        }
    )
    extractor = IntentExtractor(
        model="gemini-3.5-flash-lite",
        client=_FakeGenAIClient(lambda **_kwargs: SimpleNamespace(parsed=None, text=llm_json)),
    )
    catalog = _InMemoryCatalog(
        [
            Product(
                id="1",
                source="synthetic",
                title="Notebook Lenovo IdeaPad",
                price=Decimal("4499.00"),
                currency="BRL",
                category="laptops",
                attributes={"brand": "Lenovo"},
            ),
            Product(
                id="2",
                source="synthetic",
                title="Notebook Lenovo ThinkPad",
                price=Decimal("5000.00"),
                currency="BRL",
                category="laptops",
                attributes={"brand": "Lenovo"},
            ),
            Product(
                id="3",
                source="synthetic",
                title="Notebook Lenovo Legion",
                price=Decimal("5000.01"),
                currency="BRL",
                category="laptops",
                attributes={"brand": "Lenovo"},
            ),
            Product(
                id="4",
                source="synthetic",
                title="Notebook Acer Aspire",
                price=Decimal("3800.00"),
                currency="BRL",
                category="laptops",
                attributes={"brand": "Acer"},
            ),
            Product(
                id="5",
                source="synthetic",
                title="Smartphone Lenovo Moto",
                price=Decimal("2500.00"),
                currency="BRL",
                category="smartphones",
                attributes={"brand": "Lenovo"},
            ),
        ]
    )

    result = run_search_flow(
        user_query,
        extractor=extractor,
        catalog=catalog,
        category_map={"notebook": "laptops"},
        budget_currency="BRL",
    )

    assert result.clarification_question is None
    assert result.intent.category == "notebook"
    assert result.intent.brands == ["Lenovo"]
    assert result.intent.use_cases == ["programação", "estudos"]
    assert result.intent.price.max_price == 5000
    assert result.intent.price.is_hard_constraint is True
    assert "reasoning" not in result.intent.model_dump()

    assert result.search is not None
    # Restrições rígidas (categoria e orçamento <= 5000) eliminam IDs 3 e 5;
    # na Etapa 1, brands e use_cases permanecem explicitados em unapplied_preferences.
    assert [item.id for item in result.search.results.products] == ["1", "2", "4"]
    assert all(item.price <= Decimal("5000.00") for item in result.search.results.products)
    assert "brands" in result.search.unapplied_preferences
    assert "use_cases" in result.search.unapplied_preferences


def test_e2e_stage1_clarification_skips_catalog_and_llm_failure_propagates() -> None:
    clarification_json = json.dumps(
        {
            "category": None,
            "query": "presente",
            "clarification_needed": True,
            "clarification_question": "Qual produto você procura e qual o orçamento?",
        }
    )
    catalog = _InMemoryCatalog(
        [
            Product(
                id="1",
                source="synthetic",
                title="Notebook",
                price=Decimal("3000.00"),
                currency="BRL",
                category="laptops",
            )
        ]
    )
    clarifier = IntentExtractor(
        model="gemini-3.5-flash-lite",
        client=_FakeGenAIClient(lambda **_kwargs: SimpleNamespace(parsed=None, text=clarification_json)),
    )

    res = run_search_flow("Quero comprar algo", extractor=clarifier, catalog=catalog)
    assert res.search is None
    assert res.clarification_question == "Qual produto você procura e qual o orçamento?"
    assert catalog.calls == 0

    def fail_llm(**_kwargs):
        raise RuntimeError("Gemini timeout")

    broken_extractor = IntentExtractor(
        model="gemini-3.5-flash-lite",
        client=_FakeGenAIClient(fail_llm),
    )
    with pytest.raises(RuntimeError, match="Gemini timeout"):
        run_search_flow("Quero notebook", extractor=broken_extractor, catalog=catalog)
    assert catalog.calls == 0


def test_e2e_stage1_search_backend_contract_quota_logging_and_no_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    settings = load_settings(
        environ={
            "GEMINI_API_KEY": "fake_gemini_secret",
            "EBAY_CLIENT_ID": "fake_ebay_id",
            "EBAY_CLIENT_SECRET": "fake_ebay_secret",
            "EBAY_DAILY_QUOTA_LIMIT": "5000",
            "USD_BRL_RATE": "5.50",
            "BUSCAAI_API_KEYS": "fake_k1",
            "BUSCAAI_LOG_LEVEL": "INFO",
        }
    )
    log_stream = io.StringIO()
    logger = configure_logging(settings.log_level, stream=log_stream, logger_name="buscai.test.e2e")
    guard = QuotaGuard(daily_limit=settings.ebay_daily_quota_limit, logger=logger)

    class MockEbaySearchBackend:
        def __init__(self, quota_guard: QuotaGuard) -> None:
            self._guard = quota_guard

        def search(
            self,
            query: SearchQuery,
            *,
            budget: RequestCallBudget | None = None,
            request_id: str | None = None,
        ) -> SearchResponse:
            self._guard.consume("search", budget=budget, request_id=request_id)
            if "nonexistent" in query.query_text:
                return SearchResponse(candidates=(), total_estimated=0, limit=query.limit, offset=0, calls_used=1)
            item = Candidate(
                item_id="v1|999|0",
                source="ebay",
                title="SENTINEL_EBAY_TITLE",
                price=Decimal("799.00"),
                currency="USD",
                item_web_url="https://ebay.example/sentinel",
                seller={"username": "SENTINEL_SELLER"},
            )
            return SearchResponse(
                candidates=(item,),
                total_estimated=1,
                limit=query.limit,
                offset=query.offset,
                calls_used=1,
            )

        def get_item_details(
            self,
            item_id: str,
            *,
            budget: RequestCallBudget | None = None,
            request_id: str | None = None,
        ) -> Candidate | None:
            self._guard.consume("get_item", budget=budget, request_id=request_id)
            return Candidate(
                item_id=item_id,
                source="ebay",
                title="SENTINEL_EBAY_TITLE",
                price=Decimal("799.00"),
                currency="USD",
                brand="Lenovo",
                aspects={"RAM": ["16 GB"]},
                details_loaded=True,
            )

    backend: SearchBackend = MockEbaySearchBackend(guard)
    budget = guard.new_request_budget(max_search_calls=1, max_get_item_calls=1)

    query = SearchQuery(query_text="lenovo laptop", max_price_usd=Decimal("900.00"))
    response = backend.search(query, budget=budget, request_id="req-e2e-1")
    assert len(response.candidates) == 1
    assert response.candidates[0].brand is None
    assert response.candidates[0].shipping is None

    enriched = backend.get_item_details(response.candidates[0].item_id, budget=budget, request_id="req-e2e-1")
    assert enriched is not None and enriched.details_loaded is True
    assert enriched.aspects == {"RAM": ("16 GB",)}

    # Segunda busca no mesmo budget deve ser bloqueada sem consumir cota diária
    used_before_block = guard.snapshot().used_in_window
    with pytest.raises(ProviderRateLimitError, match="search"):
        backend.search(query, budget=budget, request_id="req-e2e-1")
    assert guard.snapshot().used_in_window == used_before_block == 2

    # Busca com resposta vazia funciona com budget novo
    empty_budget = guard.new_request_budget(max_search_calls=1, max_get_item_calls=0)
    empty_resp = backend.search(
        SearchQuery(query_text="nonexistent item"), budget=empty_budget, request_id="req-e2e-2"
    )
    assert empty_resp.candidates == ()
    assert empty_resp.total_estimated == 0

    # Logger estruturado não vaza sentinelas nem credenciais
    log_operational_event(
        logger,
        logging.INFO,
        "mock_search_finished GEMINI_API_KEY=fake_gemini_secret",
        request_id="req-e2e-1",
        stage="search",
        item_count=len(response.candidates),
        ebay_calls=budget.total_calls_used,
        title="SENTINEL_EBAY_TITLE",
        url="https://ebay.example/sentinel",
        seller="SENTINEL_SELLER",
        api_key="fake_k1",
    )
    logs_raw = log_stream.getvalue()
    assert "SENTINEL_EBAY_TITLE" not in logs_raw
    assert "https://ebay.example/sentinel" not in logs_raw
    assert "SENTINEL_SELLER" not in logs_raw
    assert "fake_gemini_secret" not in logs_raw
    assert "fake_k1" not in logs_raw
    assert "GEMINI_API_KEY=[REDACTED]" in logs_raw

    # Zero persistência em disco
    assert list(tmp_path.rglob("*")) == []
