"""Integração controlada de extração, intenção e catálogo."""

from __future__ import annotations

from decimal import Decimal

import pytest

from buscai.agent import run_search_flow
from buscai.adapters import ConstraintUnavailableError
from buscai.catalog import Product, ProductPage
from buscai.catalog.dummyjson import CatalogError
from scripts.run_search_flow import main as demo_main
from userintent.schema import UserIntent


class FakeExtractor:
    def __init__(self, intent: UserIntent) -> None:
        self.intent = intent
        self.calls: list[str] = []

    def extract(self, user_text: str) -> UserIntent:
        self.calls.append(user_text)
        return self.intent


class FakeCatalog:
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


def product(product_id: int, *, category: str = "beauty") -> Product:
    return Product(
        id=str(product_id), source="test", title="Red Lipstick", price=Decimal("12.99"),
        currency=None, category=category,
    )


def test_flow_extracts_once_and_returns_catalog_products() -> None:
    extractor = FakeExtractor(UserIntent(category="maquiagem", query="lipstick"))
    catalog = FakeCatalog([product(1), product(2, category="fragrances")])

    result = run_search_flow(
        "Quero um batom vermelho", extractor=extractor, catalog=catalog,
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

    result = run_search_flow("Quero batom", catalog=FakeCatalog([product(1)]))

    assert extractor.calls == ["Quero batom"]
    assert result.search is not None
    assert result.search.results.total_matches == 1


def test_flow_returns_clarification_without_catalog_call() -> None:
    extractor = FakeExtractor(UserIntent(
        query="notebook", clarification_needed=True,
        clarification_question="Qual é o orçamento?",
    ))
    catalog = FakeCatalog([product(1)])

    result = run_search_flow("Quero um notebook", extractor=extractor, catalog=catalog)

    assert result.search is None
    assert result.clarification_question == "Qual é o orçamento?"
    assert catalog.calls == 0


def test_flow_returns_empty_search_when_no_product_matches() -> None:
    extractor = FakeExtractor(UserIntent(query="camera"))
    catalog = FakeCatalog([product(1)])

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
        run_search_flow("batom", extractor=FailingExtractor(), catalog=FakeCatalog([]))

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
    catalog = FakeCatalog([product(1)])

    with pytest.raises(ConstraintUnavailableError, match="moeda do orçamento"):
        run_search_flow("batom até 20", extractor=FakeExtractor(intent), catalog=catalog)
    assert catalog.calls == 0


def test_cli_displays_results_or_clarification_without_gemini(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    catalog = FakeCatalog([product(1)])

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
