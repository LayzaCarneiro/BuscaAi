"""Testes integrados offline multi-turn da Etapa 10 (T10.9): 8 cenários sintéticos ponta a ponta."""

from __future__ import annotations

import threading
from decimal import Decimal

from fastapi.testclient import TestClient

from buscai.api import create_app
from buscai.config import load_settings
from buscai.orchestration import OrchestrationDependencies, SearchOrchestrator
from buscai.pricing import CurrencyConverter, FixedEnvExchangeRateProvider
from buscai.ranking.pipeline import RankingPipeline
from buscai.search.models import Candidate, SearchQuery, SearchResponse, SellerInfo, ShippingInfo
from buscai.search.query_builder import QueryBuilder
from buscai.search.quota import QuotaGuard, RequestCallBudget
from buscai.semantic.embeddings import DeterministicStubEmbeddingProvider, EmbeddingService
from buscai.semantic.pipeline import SemanticSearchProcessor
from buscai.validation.pipeline import ValidationPipeline
from userintent.schema import PriceConstraint, UserIntent

_API_KEY = "buscai_multiturn_test_key_010"


class _ContextualSyntheticExtractor:
    """Stub determinístico que combina conversation_history + mensagem atual para testar continuidade real."""

    def __init__(self) -> None:
        self.history_snapshots: list[tuple[dict[str, str], ...]] = []
        self._lock = threading.Lock()

    def extract(
        self,
        user_text: str,
        *,
        conversation_history: list[dict[str, str]] | tuple[dict[str, str], ...] | None = None,
        request_id: str | None = None,
    ) -> UserIntent:
        history_tuple = tuple(dict(item) for item in (conversation_history or ()))
        with self._lock:
            self.history_snapshots.append(history_tuple)

        prior_user_texts = [t["content"].casefold() for t in history_tuple if t.get("role") == "user"]
        current = user_text.casefold()
        combined = " | ".join([*prior_user_texts, current])

        # Cenário 5: mensagem inicial ambígua sem categoria no histórico nem no turno atual
        if "presente" in combined and not any(
            kw in combined for kw in ("notebook", "smartphone", "monitor", "fone", "headphone")
        ):
            return UserIntent(
                category=None,
                clarification_needed=True,
                clarification_question="Qual produto você procura e qual a faixa de orçamento?",
                query=user_text,
            )

        category: str | None = None
        if "notebook" in combined:
            category = "notebook"
        elif "smartphone" in combined or "celular" in combined:
            category = "smartphone"
        elif "monitor" in combined:
            category = "monitor"
        elif "headphone" in combined or "fone" in combined:
            category = "fone de ouvido"

        brands: list[str] = []
        for brand in ("Lenovo", "Dell", "Apple", "Samsung", "Sony", "LG"):
            if brand.casefold() in combined:
                brands.append(brand)

        use_cases: list[str] = []
        if "estudo" in combined or "estudar" in combined:
            use_cases.append("estudos")
        if "trabalho" in combined:
            use_cases.append("trabalho")

        required_features: list[str] = []
        preferred_features: list[str] = []

        if "16 gb" in combined or "16gb" in combined:
            required_features.append("16GB RAM")
        if "256 gb" in combined or "256gb" in combined:
            required_features.append("256GB")
        if "64 gb" in combined and "ram" in combined:
            required_features.append("64GB RAM")
        if "4k" in combined:
            required_features.append("4K")
        if "anc" in combined or "cancelamento de ruído" in combined:
            required_features.append("ANC")

        if "27 polegadas" in combined:
            preferred_features.append("27 polegadas")
        if "oled" in combined:
            preferred_features.append("tela OLED")
        if "stylus" in combined:
            preferred_features.append("caneta stylus")

        # Remoção explícita no turno atual (Cenário 8)
        if "touchscreen" in " | ".join(prior_user_texts) and (
            "não precisa ter touchscreen" in current or "nao precisa ter touchscreen" in current
        ):
            pass
        elif "touchscreen" in combined:
            preferred_features.append("touchscreen")

        # Orçamento: alteração explícita no turno atual sobrepõe orçamento do histórico
        price = PriceConstraint()
        if "até r$ 3.500" in current or "ate r$ 3.500" in current or "3.500" in current:
            price = PriceConstraint(max_price=3500.0, max_inclusive=True, is_hard_constraint=True, currency="BRL")
        elif "esticar um pouco" in current or "uns r$ 2.000" in combined or "2.000" in combined:
            price = PriceConstraint(max_price=2000.0, max_inclusive=True, is_hard_constraint=False, currency="BRL")
        elif "até r$ 4.000" in combined or "4.000" in combined or "4000" in combined:
            price = PriceConstraint(max_price=4000.0, max_inclusive=True, is_hard_constraint=True, currency="BRL")
        elif "até r$ 500" in combined or "500 reais" in combined:
            price = PriceConstraint(max_price=500.0, max_inclusive=True, is_hard_constraint=True, currency="BRL")

        condition: str | None = None
        if "novo" in combined:
            condition = "novo"

        return UserIntent(
            category=category,
            brands=brands,
            use_cases=use_cases,
            required_features=required_features,
            preferred_features=preferred_features,
            condition=condition,
            price=price,
            clarification_needed=False,
            clarification_question=None,
            query=user_text,
        )


class _MultiCategorySyntheticBackend:
    """Catálogo sintético offline cobrindo notebook, smartphone, monitor e fone de ouvido."""

    def __init__(self) -> None:
        self.calls = 0

    def search(
        self,
        query: SearchQuery,
        *,
        budget: RequestCallBudget | None = None,
        request_id: str | None = None,
    ) -> SearchResponse:
        self.calls += 1
        if budget is not None:
            budget.search_calls_used += 1
        q = query.query_text.casefold()

        catalog: dict[str, tuple[Candidate, ...]] = {
            "notebook": (
                Candidate(
                    item_id="nb-lenovo-1",
                    source="synthetic",
                    title="Lenovo ThinkPad T14 16GB RAM 512GB SSD Laptop",
                    price=Decimal("520.00"),
                    currency="USD",
                    condition="New",
                    categories=("notebook",),
                    brand="Lenovo",
                    model="ThinkPad T14",
                    aspects={"RAM Size": ("16 GB",), "SSD Capacity": ("512 GB",)},
                    shipping=ShippingInfo(cost=Decimal("0"), currency="USD", free_shipping=True, estimated_max_days=4),
                    seller=SellerInfo(username="lenovo_pro", feedback_score=900, feedback_percentage=Decimal("99.5")),
                    details_loaded=True,
                ),
                Candidate(
                    item_id="nb-dell-perf",
                    source="synthetic",
                    title="Dell Latitude 7430 16GB RAM 1TB SSD Touchscreen Laptop",
                    price=Decimal("680.00"),
                    currency="USD",
                    condition="New",
                    categories=("notebook",),
                    brand="Dell",
                    model="Latitude 7430",
                    aspects={
                        "RAM Size": ("16 GB",),
                        "SSD Capacity": ("1 TB",),
                        "Features": ("Touchscreen",),
                    },
                    shipping=ShippingInfo(cost=Decimal("0"), currency="USD", free_shipping=True, estimated_max_days=3),
                    seller=SellerInfo(username="dell_fast", feedback_score=1200, feedback_percentage=Decimal("99.8")),
                    details_loaded=True,
                ),
                Candidate(
                    item_id="nb-acer-budget",
                    source="synthetic",
                    title="Acer Aspire 5 16GB RAM 512GB SSD Laptop",
                    price=Decimal("430.00"),
                    currency="USD",
                    condition="New",
                    categories=("notebook",),
                    brand="Acer",
                    model="Aspire 5",
                    aspects={"RAM Size": ("16 GB",), "SSD Capacity": ("512 GB",)},
                    shipping=ShippingInfo(cost=Decimal("0"), currency="USD", free_shipping=True, estimated_max_days=6),
                    seller=SellerInfo(username="acer_store", feedback_score=500, feedback_percentage=Decimal("98.5")),
                    details_loaded=True,
                ),
            ),
            "smartphone": (
                Candidate(
                    item_id="sp-samsung-1",
                    source="synthetic",
                    title="Samsung Galaxy S24 256GB 5G Smartphone",
                    price=Decimal("550.00"),
                    currency="USD",
                    condition="New",
                    categories=("smartphone",),
                    brand="Samsung",
                    model="Galaxy S24",
                    aspects={"Storage Capacity": ("256 GB",)},
                    shipping=ShippingInfo(cost=Decimal("0"), currency="USD", free_shipping=True, estimated_max_days=5),
                    seller=SellerInfo(username="mobile_hub", feedback_score=700, feedback_percentage=Decimal("99.0")),
                    details_loaded=True,
                ),
            ),
            "monitor": (
                Candidate(
                    item_id="mon-lg-27",
                    source="synthetic",
                    title="LG 27UP850 27 inch 4K UHD IPS Monitor",
                    price=Decimal("340.00"),
                    currency="USD",
                    condition="New",
                    categories=("monitor",),
                    brand="LG",
                    model="27UP850",
                    aspects={"Resolution": ("4K",), "Screen Size": ("27 in",)},
                    shipping=ShippingInfo(cost=Decimal("0"), currency="USD", free_shipping=True, estimated_max_days=5),
                    seller=SellerInfo(username="display_world", feedback_score=650, feedback_percentage=Decimal("99.1")),
                    details_loaded=True,
                ),
                Candidate(
                    item_id="mon-dell-32",
                    source="synthetic",
                    title="Dell UltraSharp 32 inch 4K Monitor",
                    price=Decimal("420.00"),
                    currency="USD",
                    condition="New",
                    categories=("monitor",),
                    brand="Dell",
                    model="U3223QE",
                    aspects={"Resolution": ("4K",), "Screen Size": ("32 in",)},
                    shipping=ShippingInfo(cost=Decimal("0"), currency="USD", free_shipping=True, estimated_max_days=4),
                    seller=SellerInfo(username="display_world", feedback_score=650, feedback_percentage=Decimal("99.1")),
                    details_loaded=True,
                ),
            ),
            "headphones": (
                Candidate(
                    item_id="hp-sony-1",
                    source="synthetic",
                    title="Sony WH-1000XM5 Wireless ANC Headphones",
                    price=Decimal("280.00"),
                    currency="USD",
                    condition="New",
                    categories=("fone de ouvido",),
                    brand="Sony",
                    model="WH-1000XM5",
                    aspects={"Features": ("ANC", "Noise Cancellation")},
                    shipping=ShippingInfo(cost=Decimal("0"), currency="USD", free_shipping=True, estimated_max_days=4),
                    seller=SellerInfo(username="audio_plus", feedback_score=800, feedback_percentage=Decimal("99.3")),
                    details_loaded=True,
                ),
            ),
        }

        if "monitor" in q:
            items = catalog["monitor"]
        elif "smartphone" in q:
            items = catalog["smartphone"]
        elif "headphone" in q:
            items = catalog["headphones"]
        else:
            items = catalog["notebook"]

        return SearchResponse(
            candidates=items,
            total_estimated=len(items),
            limit=query.limit,
            offset=0,
            calls_used=1,
        )

    def get_item_details(self, item_id: str, **kwargs) -> Candidate | None:
        return None


def _build_multiturn_app():
    converter = CurrencyConverter(
        FixedEnvExchangeRateProvider(rate=Decimal("5.50"), updated_at="2026-11-25", source="env_fixed_rate")
    )
    emb = EmbeddingService(DeterministicStubEmbeddingProvider())
    emb.embed_queries(["warmup"])
    extractor = _ContextualSyntheticExtractor()
    backend = _MultiCategorySyntheticBackend()
    deps = OrchestrationDependencies(
        extractor=extractor,
        query_builder=QueryBuilder(currency_converter=converter),
        search_backend=backend,
        semantic_processor=SemanticSearchProcessor(emb),
        validation_pipeline=ValidationPipeline(currency_converter=converter),
        ranking_pipeline=RankingPipeline(converter=converter),
        quota_guard=QuotaGuard(),
    )
    orch = SearchOrchestrator(deps)
    settings = load_settings(
        environ={
            "BUSCAAI_API_KEYS": _API_KEY,
            "USD_BRL_RATE": "5.50",
            "BUSCAAI_RATE_LIMIT_PER_MINUTE": "200",
        }
    )
    app = create_app(settings=settings, orchestrator=orch, preload_embeddings=False)
    return app, extractor, backend


def test_multiturn_scenario_1_notebook_study_budget_then_lenovo_16gb() -> None:
    app, extractor, _ = _build_multiturn_app()
    with TestClient(app) as client:
        r1 = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY},
            json={"message": "Quero um notebook para estudar até R$ 4.000."},
        )
        assert r1.status_code == 200
        b1 = r1.json()
        assert b1["status"] == "ok"

        history = [
            {"role": "user", "content": "Quero um notebook para estudar até R$ 4.000."},
            {"role": "assistant", "content": b1["conversation_context"]["assistant_turn_summary"]},
        ]
        r2 = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY},
            json={
                "message": "Prefiro Lenovo e pelo menos 16 GB de RAM.",
                "conversation_history": history,
            },
        )
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["status"] == "ok"
    assert b2["search_results"]["detected_category"] == "notebook"
    assert b2["conversation_context"]["applied_budget_brl"] == "4000.0"
    assert b2["search_results"]["products"][0]["offers"][0]["brand"] == "Lenovo"
    assert len(extractor.history_snapshots[-1]) == 2


def test_multiturn_scenario_2_smartphone_256gb_then_budget_change() -> None:
    app, _, _ = _build_multiturn_app()
    with TestClient(app) as client:
        r1 = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY},
            json={"message": "Procuro smartphone com pelo menos 256 GB até R$ 4.000."},
        )
        assert r1.status_code == 200

        history = [
            {"role": "user", "content": "Procuro smartphone com pelo menos 256 GB até R$ 4.000."},
            {"role": "assistant", "content": r1.json()["conversation_context"]["assistant_turn_summary"]},
        ]
        r2 = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY},
            json={"message": "Agora posso gastar até R$ 3.500.", "conversation_history": history},
        )
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["status"] == "ok"
    assert b2["search_results"]["detected_category"] == "smartphone"
    assert b2["conversation_context"]["applied_budget_brl"] == "3500.0"
    assert any(
        c["criterion_id"] == "hard_feature:256gb" and c["status"] == "satisfied"
        for c in b2["search_results"]["products"][0]["offers"][0]["validation"]["criteria"]
    )


def test_multiturn_scenario_3_monitor_4k_then_27_inch_preference_remains_soft() -> None:
    app, _, _ = _build_multiturn_app()
    with TestClient(app) as client:
        r1 = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY},
            json={"message": "Quero um monitor 4K."},
        )
        assert r1.status_code == 200

        history = [
            {"role": "user", "content": "Quero um monitor 4K."},
            {"role": "assistant", "content": r1.json()["conversation_context"]["assistant_turn_summary"]},
        ]
        r2 = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY},
            json={"message": "Prefiro um monitor de 27 polegadas.", "conversation_history": history},
        )
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["status"] == "ok"
    assert b2["search_results"]["detected_category"] == "monitor"
    # Ambos 27 e 32 polegadas permanecem elegíveis porque 27 polegadas é preferência flexível (soft).
    assert b2["search_results"]["rejected_count"] == 0
    assert b2["search_results"]["total_products"] == 2


def test_multiturn_scenario_4_headphones_anc_then_new_condition() -> None:
    app, _, _ = _build_multiturn_app()
    with TestClient(app) as client:
        r1 = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY},
            json={"message": "Quero headphones ANC."},
        )
        assert r1.status_code == 200

        history = [
            {"role": "user", "content": "Quero headphones ANC."},
            {"role": "assistant", "content": r1.json()["conversation_context"]["assistant_turn_summary"]},
        ]
        r2 = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY},
            json={"message": "Quero novo.", "conversation_history": history},
        )
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["status"] == "ok"
    assert b2["search_results"]["detected_category"] == "fone de ouvido"
    assert b2["search_results"]["products"][0]["offers"][0]["condition"] == "New"


def test_multiturn_scenario_5_ambiguous_clarification_then_resolved_search() -> None:
    app, _, backend = _build_multiturn_app()
    with TestClient(app) as client:
        r1 = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY},
            json={"message": "Quero um presente legal."},
        )
        assert r1.status_code == 200
        b1 = r1.json()
        assert b1["status"] == "clarification_needed"
        assert b1["clarification_needed"] is True
        assert b1["search_results"] is None
        assert backend.calls == 0

        history = [
            {"role": "user", "content": "Quero um presente legal."},
            {"role": "assistant", "content": b1["clarification_question"]},
        ]
        r2 = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY},
            json={"message": "É um notebook para estudar até R$ 4.000.", "conversation_history": history},
        )
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["status"] == "ok"
    assert b2["clarification_needed"] is False
    assert backend.calls == 1
    assert b2["search_results"]["detected_category"] == "notebook"


def test_multiturn_scenario_6_soft_preference_relaxed_when_needed() -> None:
    app, _, _ = _build_multiturn_app()
    with TestClient(app) as client:
        r1 = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY},
            json={"message": "Quero um notebook Dell até R$ 4.000."},
        )
        assert r1.status_code == 200

        history = [
            {"role": "user", "content": "Quero um notebook Dell até R$ 4.000."},
            {"role": "assistant", "content": r1.json()["conversation_context"]["assistant_turn_summary"]},
        ]
        # Há apenas 1 notebook Dell (< min_eligible_results=3) e nenhum tem tela OLED -> RelaxationEngine relaxa preferred_feature.
        r2 = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY},
            json={
                "message": "De preferência com tela OLED e caneta stylus.",
                "conversation_history": history,
            },
        )
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["status"] == "ok"
    assert b2["search_results"]["relaxed"] is True
    assert b2["conversation_context"]["relaxed"] is True
    assert "Preferências flexíveis relaxadas" in b2["assistant_message"]


def test_multiturn_scenario_7_hard_constraint_not_relaxed_causes_all_rejected() -> None:
    app, _, _ = _build_multiturn_app()
    with TestClient(app) as client:
        r1 = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY},
            json={"message": "Quero um notebook Lenovo."},
        )
        assert r1.status_code == 200

        history = [
            {"role": "user", "content": "Quero um notebook Lenovo."},
            {"role": "assistant", "content": r1.json()["conversation_context"]["assistant_turn_summary"]},
        ]
        r2 = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY},
            json={"message": "Tem que custar no máximo até R$ 500.", "conversation_history": history},
        )
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["status"] == "all_rejected"
    assert b2["search_results"]["rejected_count"] >= 1
    assert "requisitos obrigatórios" in b2["assistant_message"]
    assert "não são relaxados automaticamente" in b2["assistant_message"]


def test_multiturn_scenario_8_explicit_profile_switch_and_feature_removal() -> None:
    app, _, _ = _build_multiturn_app()
    with TestClient(app) as client:
        r1 = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY},
            json={
                "message": "Quero um notebook com touchscreen até R$ 4.000.",
                "profile_id": "best_value",
            },
        )
        assert r1.status_code == 200
        b1 = r1.json()
        assert b1["search_results"]["profile_id"] == "best_value"

        history = [
            {"role": "user", "content": "Quero um notebook com touchscreen até R$ 4.000."},
            {"role": "assistant", "content": b1["conversation_context"]["assistant_turn_summary"]},
        ]
        r2 = client.post(
            "/v1/chat",
            headers={"X-API-Key": _API_KEY},
            json={
                "message": "Na verdade, não precisa ter touchscreen, e quero trocar o perfil para melhor desempenho.",
                "profile_id": "best_performance",
                "conversation_history": history,
            },
        )
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["status"] == "ok"
    assert b2["search_results"]["profile_id"] == "best_performance"
    assert "best_performance" in b2["assistant_message"]
    # Após remoção explícita, touchscreen não deve constar nos critérios avaliados.
    top_criteria_ids = [c["criterion_id"] for c in b2["search_results"]["products"][0]["offers"][0]["validation"]["criteria"]]
    assert not any("touchscreen" in cid.casefold() for cid in top_criteria_ids)
