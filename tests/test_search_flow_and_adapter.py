"""Integração controlada entre UserIntent, adaptador de critérios, catálogo e CLI de busca."""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from buscai.adapters import (
    ClarificationRequired,
    ConstraintUnavailableError,
    adapt_intent,
    search_intent,
)
from buscai.agent import run_search_flow
from buscai.catalog import Product, ProductPage
from buscai.catalog.errors import CatalogError
from scripts.run_search_flow import main as demo_main
from userintent.llm import IntentExtractor
from userintent.schema import UserIntent


class FakeExtractor:
    def __init__(self, intent: UserIntent) -> None:
        self.intent = intent
        self.calls: list[str] = []

    def extract(self, user_text: str) -> UserIntent:
        self.calls.append(user_text)
        return self.intent


class _FakeModelsAPI:
    def __init__(self, handler) -> None:
        self._handler = handler

    def generate_content(self, *, model: str, contents: str, config: object) -> object:
        return self._handler(model=model, contents=contents, config=config)


class _FakeGenAIClient:
    def __init__(self, handler) -> None:
        self.models = _FakeModelsAPI(handler)


class InMemoryCatalog:
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


def product(
    product_id: int,
    *,
    title: str = "notebook de teste",
    price: str = "150",
    currency: str | None = "BRL",
    category: str | None = "laptops",
    attributes: dict[str, object] | None = None,
) -> Product:
    return Product(
        id=str(product_id),
        source="test",
        title=title,
        price=Decimal(price),
        currency=currency,
        category=category,
        attributes=attributes or {},
    )


def test_adapter_preserves_intent_and_maps_category() -> None:
    intent = UserIntent(
        category="Notebook",
        price={
            "min_price": 100,
            "max_price": 200,
            "min_inclusive": False,
            "max_inclusive": False,
            "is_hard_constraint": True,
        },
        brands=["Acme"],
        excluded_brands=["Other"],
        required_features=["16 GB RAM"],
        preferred_features=["SSD"],
        excluded_features=["usado"],
        use_cases=["programação"],
        profile_context=["estudante"],
        query="notebook",
    )

    criteria = adapt_intent(intent, category_map={"notebook": "laptops"})

    assert criteria.category == "laptops"
    assert (criteria.min_price, criteria.max_price) == (Decimal("100"), Decimal("200"))
    assert (criteria.min_inclusive, criteria.max_inclusive, criteria.price_is_hard) == (False, False, True)
    assert criteria.brands == ("Acme",)
    assert criteria.excluded_brands == ("Other",)
    assert criteria.required_features == ("16 GB RAM",)
    assert criteria.preferred_features == ("SSD",)
    assert criteria.excluded_features == ("usado",)
    assert criteria.use_cases == ("programação",)
    assert criteria.profile_context == ("estudante",)


@pytest.mark.parametrize(
    ("inclusive", "expected_ids"),
    [(False, ["2"]), (True, ["1", "2", "3"])],
)
def test_hard_price_bounds_and_category_are_applied_before_pagination(
    inclusive: bool, expected_ids: list[str]
) -> None:
    intent = UserIntent(
        category="notebook",
        query="notebook",
        price={
            "min_price": 100,
            "max_price": 200,
            "min_inclusive": inclusive,
            "max_inclusive": inclusive,
            "is_hard_constraint": True,
        },
    )
    catalog = InMemoryCatalog(
        [
            product(1, price="100"),
            product(2),
            product(3, price="200"),
            product(4, price="150", category="smartphones"),
        ]
    )

    result = search_intent(intent, catalog=catalog, category_map={"notebook": "laptops"}, budget_currency="brl")

    assert [item.id for item in result.results.products] == expected_ids
    assert result.results.total_matches == len(expected_ids)


def test_soft_preferences_and_context_are_kept_without_excluding_products() -> None:
    intent = UserIntent(
        query="notebook",
        price={"max_price": 100, "is_hard_constraint": False},
        brands=["Acme"],
        preferred_features=["SSD"],
        use_cases=["programação"],
        profile_context=["estudante"],
    )
    catalog = InMemoryCatalog([product(1, price="500", attributes={"brand": "Other"})])

    result = search_intent(intent, catalog=catalog)

    assert [item.id for item in result.results.products] == ["1"]
    assert result.criteria.max_price == Decimal("100")
    assert result.unapplied_preferences == (
        "brands",
        "preferred_features",
        "use_cases",
        "profile_context",
        "price",
    )


def test_excluded_brand_and_required_feature_filter_products_with_evidence() -> None:
    intent = UserIntent(
        query="notebook",
        excluded_brands=["Other"],
        required_features=["16 GB RAM"],
    )
    catalog = InMemoryCatalog(
        [
            product(1, attributes={"brand": "Acme", "description": "Notebook com 16GB de RAM"}),
            product(2, attributes={"brand": "Other", "description": "Notebook com 16GB de RAM"}),
            product(3, attributes={"brand": "Acme", "description": "Notebook com 8GB de RAM"}),
            product(4, attributes={"brand": "Acme", "description": "Notebook com 128GB de RAM"}),
        ]
    )

    result = search_intent(intent, catalog=catalog)

    assert [item.id for item in result.results.products] == ["1"]


@pytest.mark.parametrize(
    ("intent", "candidate", "message"),
    [
        (UserIntent(query="notebook", excluded_brands=["Other"]), product(1), "marca"),
        (UserIntent(query="notebook", required_features=["16 GB"]), product(1), "descrição"),
        (UserIntent(query="notebook", category="notebook"), product(1, category=None), "categoria"),
        (
            UserIntent(query="notebook", price={"max_price": 200, "is_hard_constraint": True}),
            product(1, currency=None),
            "Moeda",
        ),
    ],
)
def test_missing_data_for_hard_constraints_is_reported(
    intent: UserIntent, candidate: Product, message: str
) -> None:
    with pytest.raises(ConstraintUnavailableError, match=message):
        search_intent(intent, catalog=InMemoryCatalog([candidate]), budget_currency="BRL")


def test_excluded_features_fail_explicitly_without_absence_data() -> None:
    intent = UserIntent(query="notebook", excluded_features=["usado"])
    catalog = InMemoryCatalog([product(1, attributes={"description": "Notebook novo"})])

    with pytest.raises(ConstraintUnavailableError, match="excluded_features"):
        search_intent(intent, catalog=catalog)
    assert catalog.calls == 1


def test_excluded_features_use_only_a_declared_complete_feature_list() -> None:
    intent = UserIntent(query="notebook", excluded_features=["usado"])
    catalog = InMemoryCatalog(
        [
            product(1, attributes={"features": ["novo", "SSD"], "features_complete": True}),
            product(2, attributes={"features": ["usado", "SSD"], "features_complete": True}),
        ]
    )

    result = search_intent(intent, catalog=catalog)

    assert [item.id for item in result.results.products] == ["1"]


def test_hard_budget_requires_currency_before_catalog_access() -> None:
    intent = UserIntent(query="notebook", price={"max_price": 200, "is_hard_constraint": True})
    catalog = InMemoryCatalog([product(1)])

    with pytest.raises(ConstraintUnavailableError, match="moeda do orçamento"):
        search_intent(intent, catalog=catalog)
    assert catalog.calls == 0


def test_clarification_is_forwarded_without_searching() -> None:
    intent = UserIntent(
        query="notebook",
        clarification_needed=True,
        clarification_question="Qual é o seu orçamento?",
    )
    catalog = InMemoryCatalog([product(1)])

    with pytest.raises(ClarificationRequired, match="Qual é o seu orçamento"):
        search_intent(intent, catalog=catalog)
    assert catalog.calls == 0


def test_flow_extracts_once_and_returns_catalog_products() -> None:
    extractor = FakeExtractor(UserIntent(category="maquiagem", query="lipstick"))
    catalog = InMemoryCatalog(
        [
            product(1, title="Red Lipstick", price="12.99", currency=None, category="beauty"),
            product(2, title="Red Lipstick", price="12.99", currency=None, category="fragrances"),
        ]
    )

    result = run_search_flow(
        "Quero um batom vermelho",
        extractor=extractor,
        catalog=catalog,
        category_map={"maquiagem": "beauty"},
    )

    assert extractor.calls == ["Quero um batom vermelho"]
    assert catalog.calls == 1
    assert result.intent is extractor.intent
    assert result.clarification_question is None
    assert result.search is not None
    assert [item.id for item in result.search.results.products] == ["1"]
    assert result.search.results.total_matches == 1


def test_default_path_uses_existing_intent_extractor(monkeypatch: pytest.MonkeyPatch) -> None:
    extractor = FakeExtractor(UserIntent(query="lipstick"))
    monkeypatch.setattr("userintent.llm.IntentExtractor", lambda: extractor)

    result = run_search_flow(
        "Quero batom",
        catalog=InMemoryCatalog([product(1, title="Red Lipstick", price="12.99", currency=None, category="beauty")]),
    )

    assert extractor.calls == ["Quero batom"]
    assert result.search is not None
    assert result.search.results.total_matches == 1


def test_flow_returns_clarification_without_catalog_call() -> None:
    extractor = FakeExtractor(
        UserIntent(
            query="notebook",
            clarification_needed=True,
            clarification_question="Qual é o orçamento?",
        )
    )
    catalog = InMemoryCatalog([product(1, title="Red Lipstick", price="12.99", currency=None, category="beauty")])

    result = run_search_flow("Quero um notebook", extractor=extractor, catalog=catalog)

    assert result.search is None
    assert result.clarification_question == "Qual é o orçamento?"
    assert catalog.calls == 0


def test_flow_returns_empty_search_when_no_product_matches() -> None:
    extractor = FakeExtractor(UserIntent(query="camera"))
    catalog = InMemoryCatalog([product(1, title="Red Lipstick", price="12.99", currency=None, category="beauty")])

    result = run_search_flow("Quero uma câmera", extractor=extractor, catalog=catalog)

    assert result.search is not None
    assert result.search.results.total_matches == 0
    assert result.search.results.products == ()


def test_flow_propagates_extractor_and_catalog_failures() -> None:
    class FailingExtractor:
        def extract(self, user_text: str) -> UserIntent:
            raise RuntimeError("Gemini indisponível")

    class FailingCatalog:
        def list_products(self, *, limit: int = 30, skip: int = 0) -> ProductPage:
            raise CatalogError("Catálogo indisponível")

    with pytest.raises(RuntimeError, match="Gemini indisponível"):
        run_search_flow("batom", extractor=FailingExtractor(), catalog=InMemoryCatalog([]))

    with pytest.raises(CatalogError, match="Catálogo indisponível"):
        run_search_flow("batom", extractor=FakeExtractor(UserIntent(query="lipstick")), catalog=FailingCatalog())


def test_flow_rejects_invalid_input_and_invalid_extractor_result() -> None:
    extractor = FakeExtractor(UserIntent(query="lipstick"))
    with pytest.raises(ValueError, match="consulta do usuário"):
        run_search_flow("  ", extractor=extractor)
    assert extractor.calls == []

    class InvalidExtractor:
        def extract(self, user_text: str) -> dict:
            return {"query": "lipstick"}

    with pytest.raises(TypeError, match="UserIntent"):
        run_search_flow("batom", extractor=InvalidExtractor())


def test_flow_reports_unverifiable_price_before_catalog_access() -> None:
    intent = UserIntent(query="lipstick", price={"max_price": 20, "is_hard_constraint": True})
    catalog = InMemoryCatalog([product(1, title="Red Lipstick", price="12.99", currency=None, category="beauty")])

    with pytest.raises(ConstraintUnavailableError, match="moeda do orçamento"):
        run_search_flow("batom até 20", extractor=FakeExtractor(intent), catalog=catalog)
    assert catalog.calls == 0


def test_cli_displays_results_or_clarification_without_gemini(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    catalog = InMemoryCatalog([product(1, title="Red Lipstick", price="12.99", currency=None, category="beauty")])

    assert demo_main(["Quero batom"], extractor=FakeExtractor(UserIntent(query="lipstick")), catalog=catalog) == 0
    output = capsys.readouterr().out
    assert "Red Lipstick" in output
    assert "Correspondências: 1" in output

    clarification = UserIntent(query="lipstick", clarification_needed=True, clarification_question="Qual cor?")
    assert demo_main(["Quero batom"], extractor=FakeExtractor(clarification), catalog=catalog) == 0
    assert "Qual cor?" in capsys.readouterr().out


def test_cli_reports_catalog_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    class FailingCatalog:
        def list_products(self, *, limit: int = 30, skip: int = 0) -> ProductPage:
            raise CatalogError("Catálogo indisponível")

    code = demo_main(["Quero batom"], extractor=FakeExtractor(UserIntent(query="lipstick")), catalog=FailingCatalog())

    assert code == 1
    assert "Catálogo indisponível" in capsys.readouterr().err


def test_search_flow_respects_budget_and_category_constraints() -> None:
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
    catalog = InMemoryCatalog(
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
    assert [item.id for item in result.search.results.products] == ["1", "2", "4"]
    assert all(item.price <= Decimal("5000.00") for item in result.search.results.products)
    assert "brands" in result.search.unapplied_preferences
    assert "use_cases" in result.search.unapplied_preferences


def test_search_flow_clarification_skips_catalog_and_llm_failure_propagates() -> None:
    clarification_json = json.dumps(
        {
            "category": None,
            "query": "presente",
            "clarification_needed": True,
            "clarification_question": "Qual produto você procura e qual o orçamento?",
        }
    )
    catalog = InMemoryCatalog(
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
