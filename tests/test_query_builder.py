"""Testes unitários da Etapa 3 para o QueryBuilder (EN-US e compatibilidade eBay Browse API)."""

from __future__ import annotations

import ast
import json
from decimal import Decimal
from pathlib import Path

import pytest

from buscai.config import load_settings
from buscai.pricing import CurrencyConverter, FixedEnvExchangeRateProvider
from buscai.search import (
    CategorySpec,
    QueryBuilder,
    SearchPlan,
    build_ebay_search_params,
)
from userintent.schema import PriceConstraint, UserIntent

ROOT = Path(__file__).resolve().parents[1]


def _make_builder(**kwargs) -> QueryBuilder:
    settings = load_settings(
        environ={
            "USD_BRL_RATE": "5.50",
            "USD_BRL_RATE_UPDATED_AT": "2026-11-19",
            "USD_BRL_RATE_SOURCE": "env_fixed_rate",
            "EBAY_MARKETPLACE_ID": "EBAY_US",
        }
    )
    return QueryBuilder.from_settings(settings, **kwargs)


def test_query_builder_supports_all_four_mvp_categories_and_subtypes() -> None:
    builder = _make_builder(max_queries=1)

    cases = [
        ("notebook", "laptop"),
        ("smartphone", "smartphone"),
        ("celular", "smartphone"),
        ("monitor", "monitor"),
        ("fone de ouvido", "headphones"),
    ]
    for raw_cat, expected_en in cases:
        plan = builder.build(UserIntent(category=raw_cat, query=f"{raw_cat} bom"))
        assert isinstance(plan, SearchPlan)
        assert plan.is_supported_mvp_category is True
        assert plan.primary_query is not None
        assert expected_en in plan.primary_query.query_text

    # Subtipo headset e earbuds dentro de fone de ouvido
    headset_plan = builder.build(
        UserIntent(category="fone de ouvido", required_features=["microfone"], query="headset para trabalho")
    )
    assert headset_plan.canonical_category == "fone de ouvido"
    assert headset_plan.primary_query is not None
    assert "headset" in headset_plan.primary_query.query_text
    assert "microphone" in headset_plan.primary_query.query_text

    earbuds_plan = builder.build(
        UserIntent(
            category="fone de ouvido",
            required_features=["Bluetooth"],
            query="fone intra-auricular Bluetooth",
        )
    )
    assert earbuds_plan.primary_query is not None
    assert "earbuds" in earbuds_plan.primary_query.query_text
    assert "Bluetooth" in earbuds_plan.primary_query.query_text

    # Modelo auto-suficiente sem repetir "smartphone" nem inventar marca
    iphone_plan = builder.build(
        UserIntent(
            category="smartphone",
            models=["iPhone"],
            required_features=["pelo menos 256 GB de armazenamento"],
            query="iPhone 256 GB",
        )
    )
    assert iphone_plan.primary_query is not None
    assert iphone_plan.primary_query.query_text == "iPhone 256GB"
    assert "smartphone" not in iphone_plan.primary_query.query_text
    assert "Apple" not in iphone_plan.primary_query.query_text


def test_query_builder_preserves_brands_models_dedup_and_features() -> None:
    builder = _make_builder()
    intent = UserIntent(
        category="notebook",
        brands=["Lenovo"],
        models=["Lenovo ThinkPad X1"],
        required_features=["pelo menos 16 GB de RAM", "SSD NVMe de 512 GB", "14 polegadas"],
        preferred_features=["teclado retroiluminado", "bom", "bateria longa"],
        excluded_brands=["Positivo"],
        excluded_features=["tela touch"],
        use_cases=["programação", "estudos"],
        query="notebook Lenovo ThinkPad X1",
    )

    plan = builder.build(intent)
    assert plan.primary_query is not None
    q_text = plan.primary_query.query_text
    # Marca e modelo deduplicados case-insensitive
    assert q_text.count("Lenovo") == 1
    assert "ThinkPad X1" in q_text
    assert "laptop" in q_text
    assert "16GB RAM" in q_text
    assert "512GB NVMe SSD" in q_text
    assert "14 inch" in q_text
    # Preferências relaxáveis NÃO entram na consulta principal (A3)
    assert "backlit keyboard" not in q_text
    # Termos subjetivos e finalidades de uso NÃO entram na query EN-US
    assert "bom" not in q_text
    assert "programação" not in q_text
    assert "estudos" not in q_text
    assert "use_case:programação" in plan.ignored_preferences
    assert "deferred_preference:teclado retroiluminado" in plan.ignored_preferences
    assert "subjective:bom" in plan.ignored_preferences
    assert "subjective:bateria longa" in plan.ignored_preferences
    assert plan.excluded_brands == ("Positivo",)
    assert plan.excluded_features == ("tela touch",)

    # Consulta alternativa pode refinar com preferências traduzidas mantendo requisitos obrigatórios
    assert len(plan.queries) == 2
    alt = plan.queries[1].query
    assert "16GB RAM" in alt.query_text
    assert "512GB NVMe SSD" in alt.query_text
    assert "backlit keyboard" in alt.query_text


def test_query_builder_handles_min_max_budget_brl_usd_and_missing_budget() -> None:
    builder = _make_builder(max_queries=1)

    # Faixa min e max em BRL -> USD via CurrencyConverter
    brl_plan = builder.build(
        UserIntent(
            category="notebook",
            brands=["Dell"],
            price=PriceConstraint(min_price=2750, max_price=5500, is_hard_constraint=True, currency="BRL"),
            query="notebook Dell",
        )
    )
    assert brl_plan.budget.original_currency == "BRL"
    assert brl_plan.budget.min_price_usd == Decimal("500.00")
    assert brl_plan.budget.max_price_usd == Decimal("1000.00")
    assert brl_plan.budget.exchange_rate is not None
    assert brl_plan.budget.exchange_rate.is_estimate is True
    params = build_ebay_search_params(brl_plan.primary_query)  # type: ignore[arg-type]
    assert "price:[500..1000],priceCurrency:USD" in params["filter"]

    # Orçamento diretamente em USD não passa por câmbio BRL
    usd_plan = builder.build(
        UserIntent(
            category="monitor",
            price=PriceConstraint(max_price=399.9, is_hard_constraint=True, currency="USD"),
            query="monitor 4K",
        )
    )
    assert usd_plan.budget.original_currency == "USD"
    assert usd_plan.budget.max_price_usd == Decimal("399.90")
    assert usd_plan.budget.exchange_rate is None

    # Ausência de orçamento não gera filtro price no SearchQuery
    no_budget_plan = builder.build(UserIntent(category="monitor", required_features=["4K"], query="monitor 4K"))
    assert no_budget_plan.budget.min_price_usd is None
    assert no_budget_plan.budget.max_price_usd is None
    no_budget_params = build_ebay_search_params(no_budget_plan.primary_query)  # type: ignore[arg-type]
    assert "price:" not in no_budget_params["filter"]
    assert no_budget_params["filter"] == "buyingOptions:{FIXED_PRICE}"

    # Moeda não suportada é rejeitada explicitamente
    with pytest.raises(ValueError, match="Moeda de orçamento não suportada"):
        builder.build(
            UserIntent(
                category="monitor",
                price=PriceConstraint(max_price=500, currency="EUR", is_hard_constraint=True),
                query="monitor",
            )
        )


def test_query_builder_maps_official_ebay_conditions_and_soft_vs_hard_price() -> None:
    builder = _make_builder(max_queries=2)

    new_plan = builder.build(UserIntent(category="smartphone", condition="novo", query="smartphone novo"))
    assert new_plan.primary_query is not None
    assert new_plan.primary_query.conditions == ("NEW",)
    assert new_plan.primary_query.condition_ids == ()
    assert "conditions:{NEW}" in build_ebay_search_params(new_plan.primary_query)["filter"]

    used_plan = builder.build(UserIntent(category="smartphone", condition="usado", query="smartphone usado"))
    assert used_plan.primary_query is not None
    assert used_plan.primary_query.conditions == ("USED",)
    assert "conditions:{USED}" in build_ebay_search_params(used_plan.primary_query)["filter"]

    refurb_plan = builder.build(
        UserIntent(category="notebook", condition="recondicionado", query="notebook recondicionado")
    )
    assert refurb_plan.primary_query is not None
    assert refurb_plan.primary_query.conditions == ()
    assert refurb_plan.primary_query.condition_ids == ("2000", "2010", "2020", "2030")
    assert (
        "conditionIds:{2000|2010|2020|2030}"
        in build_ebay_search_params(refurb_plan.primary_query)["filter"]
    )

    open_box_plan = builder.build(UserIntent(category="monitor", condition="open box", query="monitor open box"))
    assert open_box_plan.primary_query is not None
    assert open_box_plan.primary_query.condition_ids == ("1500",)

    # Orçamento flexível permite alternativa sem teto; rígido jamais relaxa o teto
    soft_plan = builder.build(
        UserIntent(
            category="notebook",
            price=PriceConstraint(max_price=4400, is_hard_constraint=False, currency="BRL"),
            query="notebook",
        )
    )
    assert len(soft_plan.queries) == 2
    assert soft_plan.queries[0].query.max_price_usd == Decimal("800.00")
    assert soft_plan.queries[1].query.max_price_usd is None

    hard_plan = builder.build(
        UserIntent(
            category="notebook",
            required_features=["16 GB de RAM"],
            price=PriceConstraint(max_price=4400, is_hard_constraint=True, currency="BRL"),
            query="notebook 16GB RAM",
        )
    )
    assert all(item.query.max_price_usd == Decimal("800.00") for item in hard_plan.queries)


def test_query_builder_handles_clarification_incomplete_extensibility_and_no_redundancy() -> None:
    builder = _make_builder(max_queries=3)

    # Clarificação interrompe geração de SearchQuery (0 chamadas eBay planejadas)
    clarify = builder.build(
        UserIntent(
            category=None,
            query="presente",
            clarification_needed=True,
            clarification_question="Qual produto você deseja buscar?",
        )
    )
    assert clarify.clarification_needed is True
    assert clarify.clarification_question == "Qual produto você deseja buscar?"
    assert clarify.queries == ()
    assert clarify.primary_query is None

    # Consulta minimalista sem extras não gera consultas redundantes duplicadas
    minimal = builder.build(UserIntent(category="notebook", query="notebook para estudar"))
    assert len(minimal.queries) == 1
    assert minimal.primary_query is not None
    assert minimal.primary_query.query_text == "laptop"

    # Categoria fora do MVP não quebra e respeita extensibilidade quando configurada
    out_of_mvp = builder.build(UserIntent(category="teclado", required_features=["USB-C"], query="teclado USB-C"))
    assert out_of_mvp.is_supported_mvp_category is False
    assert out_of_mvp.canonical_category == "teclado"
    assert out_of_mvp.primary_query is not None
    assert out_of_mvp.primary_query.category_ids == ()

    custom_builder = QueryBuilder(
        currency_converter=CurrencyConverter(FixedEnvExchangeRateProvider(rate="5.50")),
        extra_categories=[CategorySpec(canonical_id="tablet", primary_term_en="tablet", aliases=("tablet",))],
        category_id_map={"notebook": ("177",)},
    )
    tablet_plan = custom_builder.build(UserIntent(category="tablet", query="tablet"))
    assert tablet_plan.canonical_category == "tablet"
    assert tablet_plan.primary_query is not None and "tablet" in tablet_plan.primary_query.query_text
    nb_plan = custom_builder.build(UserIntent(category="notebook", query="notebook"))
    assert nb_plan.primary_query is not None and nb_plan.primary_query.category_ids == ("177",)

    # Entradas inválidas
    with pytest.raises(TypeError):
        builder.build({"category": "notebook"})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="limit"):
        builder.build(UserIntent(category="notebook", query="notebook"), limit=201)
    with pytest.raises(ValueError, match="max_queries"):
        QueryBuilder(currency_converter=CurrencyConverter(FixedEnvExchangeRateProvider(rate="5.50")), max_queries=0)


def test_query_builder_handles_mvp_cases_from_intent_eval_dataset() -> None:
    dataset_path = ROOT / "data" / "intent_eval_50.jsonl"
    cases = [json.loads(line) for line in dataset_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    mvp_cases = [c for c in cases if c["expected"].get("category") in {"notebook", "smartphone", "monitor", "fone de ouvido"}]
    assert len(mvp_cases) >= 20

    builder = _make_builder()
    for row in mvp_cases:
        intent = UserIntent.model_validate(row["expected"])
        plan = builder.build(intent)
        assert plan.is_supported_mvp_category is True
        assert plan.primary_query is not None
        assert plan.primary_query.query_text
        # Verifica ausência de termos duplicados na mesma consulta e ausência de duplicatas no SearchPlan
        fps = {
            (item.query.query_text.casefold(), item.query.min_price_usd, item.query.max_price_usd)
            for item in plan.queries
        }
        assert len(fps) == len(plan.queries)
        params = build_ebay_search_params(plan.primary_query)
        assert "q" in params and params["q"]


def test_query_builder_module_makes_no_http_or_llm_calls() -> None:
    qb_path = ROOT / "src" / "buscai" / "search" / "query_builder.py"
    tree = ast.parse(qb_path.read_text(encoding="utf-8"), filename=str(qb_path))
    forbidden_modules = {"httpx", "requests", "urllib", "google"}

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert imported.isdisjoint(forbidden_modules)


@pytest.mark.parametrize(
    ("category", "features", "forbidden_pt_tokens", "expected_en_tokens"),
    [
        ("smartphone", ["resistente à poeira", "resistente a poeira"], ["resistente", "poeira"], ["dustproof"]),
        ("notebook", ["bateria de longa duração", "bateria de longa duracao"], ["bateria", "longa", "duracao"], []),
        ("smartphone", ["tela dobrável", "tela dobravel"], ["tela", "dobravel"], ["foldable"]),
        ("notebook", ["teclado numérico", "teclado numerico"], ["teclado", "numerico"], ["numpad"]),
        ("monitor", ["tela curva"], ["tela", "curva"], ["curved"]),
        ("notebook", ["compatibilidade com Linux", "fácil instalação de Linux"], ["compatibilidade", "instalacao", "facil", "com"], ["Linux"]),
        ("notebook", ["carcaça de alumínio escovado", "carcaca de aluminio escovado"], ["carcaca", "aluminio", "escovado"], []),
    ],
)
def test_a3_query_builder_never_leaks_portuguese_and_respects_max_length(
    category: str,
    features: list[str],
    forbidden_pt_tokens: list[str],
    expected_en_tokens: list[str],
) -> None:
    builder = _make_builder(max_queries=1)
    plan = builder.build(
        UserIntent(
            category=category,
            required_features=features,
            preferred_features=["Wi-Fi 6", "carcaça reforçada sem tradução"],
            query=f"{category} {' '.join(features)}",
        )
    )
    assert plan.primary_query is not None
    q_low = plan.primary_query.query_text.casefold()
    for pt_tok in forbidden_pt_tokens:
        assert pt_tok not in q_low
    for en_tok in expected_en_tokens:
        assert en_tok.casefold() in q_low
    # Preferências não entram na consulta principal e desconhecidas vão para ignored_preferences
    assert "wi-fi" not in q_low
    assert "deferred_preference:Wi-Fi 6" in plan.ignored_preferences
    assert "unmapped_feature:carcaça reforçada sem tradução" in plan.ignored_preferences

    # Limite de tamanho de query respeitado em fronteira de palavra
    long_builder = _make_builder(max_queries=1, max_query_length=40)
    long_plan = long_builder.build(
        UserIntent(
            category="notebook",
            brands=["Lenovo"],
            models=["ThinkPad X1 Carbon Gen 11"],
            required_features=["16 GB de RAM", "SSD NVMe de 512 GB", "14 polegadas", "teclado retroiluminado"],
            query="notebook Lenovo ThinkPad X1",
        )
    )
    assert long_plan.primary_query is not None
    assert len(long_plan.primary_query.query_text) <= 40
