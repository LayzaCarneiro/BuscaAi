r"""Teste de integração opt-in da API FastAPI contra a eBay Browse API real (EBAY_US) nas 4 categorias MVP.

Desativado por padrão para evitar consumo involuntário da cota de 5.000 chamadas/dia.
Consumo máximo esperado:
- 1 chamada OAuth client_credentials (reutilizada em memória)
- Até 8 chamadas à Browse API (<= 2 por categoria MVP: 1 item_summary/search + até 1 getItem)
- 0 chamadas ao Gemini (usa extrator determinístico local para as 4 consultas de teste)
- 0 downloads de modelos de embeddings (usa DeterministicStubEmbeddingProvider)

Para executar manualmente com credenciais eBay configuradas no .env:
    $env:RUN_EBAY_INTEGRATION_TESTS="1"
    .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -v -m ebay_live tests/integration/test_api_ebay_live.py
"""

from __future__ import annotations

import io
import os
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from buscai.api import create_app
from buscai.config import load_settings
from buscai.observability import configure_logging
from buscai.orchestration import OrchestrationDependencies, SearchOrchestrator
from buscai.pricing import CurrencyConverter, FixedEnvExchangeRateProvider
from buscai.ranking.pipeline import RankingPipeline
from buscai.search import EbayLiveBackend, EbayOAuthClient, QuotaGuard
from buscai.search.query_builder import QueryBuilder
from buscai.semantic.embeddings import DeterministicStubEmbeddingProvider, EmbeddingService
from buscai.semantic.pipeline import SemanticSearchProcessor
from buscai.validation.pipeline import ValidationPipeline
from userintent.schema import UserIntent

pytestmark = [
    pytest.mark.ebay_live,
    pytest.mark.skipif(
        os.getenv("RUN_EBAY_INTEGRATION_TESTS") != "1",
        reason=(
            "Teste real da API HTTP contra eBay desativado por padrão; "
            "defina RUN_EBAY_INTEGRATION_TESTS=1 para habilitar (consome até 8 chamadas Browse)."
        ),
    ),
]


class _DeterministicMvpExtractor:
    """Extrator determinístico local (0 chamadas Gemini) cobrindo as 4 categorias MVP."""

    _CATEGORY_QUERIES: dict[str, tuple[str, str]] = {
        "notebook": ("notebook", "laptop 16GB"),
        "smartphone": ("smartphone", "smartphone 128GB"),
        "monitor": ("monitor", "monitor 27 inch"),
        "fone": ("fone de ouvido", "bluetooth headphones"),
    }

    def extract(self, user_text: str) -> UserIntent:
        low = user_text.casefold()
        for trigger, (category, en_query) in self._CATEGORY_QUERIES.items():
            if trigger in low:
                return UserIntent(category=category, query=en_query)
        return UserIntent(category="notebook", query="laptop")


def test_api_ebay_live_four_mvp_categories_controlled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    base_settings = load_settings(load_dotenv_file=True)
    assert base_settings.is_ebay_configured, "EBAY_CLIENT_ID e EBAY_CLIENT_SECRET devem estar configurados no .env."

    api_key = "optin_live_local_ephemeral_key_01"
    settings = base_settings.model_copy(
        update={
            "api_keys": base_settings.api_keys or (),
        }
    )
    settings_with_key = load_settings(
        environ={
            "EBAY_CLIENT_ID": settings.ebay_client_id or "",
            "EBAY_CLIENT_SECRET": settings.ebay_client_secret.get_secret_value()
            if settings.ebay_client_secret is not None
            else "",
            "EBAY_MARKETPLACE_ID": settings.ebay_marketplace_id,
            "EBAY_DAILY_QUOTA_LIMIT": str(settings.ebay_daily_quota_limit),
            "USD_BRL_RATE": str(settings.usd_brl_rate),
            "USD_BRL_RATE_UPDATED_AT": settings.usd_brl_rate_updated_at,
            "BUSCAAI_API_KEYS": api_key,
        }
    )

    log_stream = io.StringIO()
    logger = configure_logging("INFO", stream=log_stream, logger_name="buscai.test.api_ebay_live")
    quota_guard = QuotaGuard(
        daily_limit=settings_with_key.ebay_daily_quota_limit,
        default_max_search_per_request=1,
        default_max_get_item_per_request=1,
        logger=logger,
    )
    oauth = EbayOAuthClient.from_settings(settings_with_key, logger=logger)
    backend = EbayLiveBackend(
        oauth_client=oauth,
        quota_guard=quota_guard,
        default_marketplace_id=settings_with_key.ebay_marketplace_id,
        logger=logger,
    )
    converter = CurrencyConverter(FixedEnvExchangeRateProvider.from_settings(settings_with_key))
    emb = EmbeddingService(DeterministicStubEmbeddingProvider(), logger=logger)
    emb.embed_queries(["warmup"])

    deps = OrchestrationDependencies(
        extractor=_DeterministicMvpExtractor(),
        query_builder=QueryBuilder(currency_converter=converter),
        search_backend=backend,
        semantic_processor=SemanticSearchProcessor(emb, logger=logger),
        validation_pipeline=ValidationPipeline(
            currency_converter=converter,
            search_backend=backend,
            max_enrich_items=1,
            logger=logger,
        ),
        ranking_pipeline=RankingPipeline(converter=converter),
        quota_guard=quota_guard,
        max_search_calls=1,
        max_get_item_calls=1,
        logger=logger,
    )
    orch = SearchOrchestrator(deps)
    app = create_app(
        settings=settings_with_key,
        orchestrator=orch,
        embedding_service=emb,
        preload_embeddings=False,
        logger=logger,
    )

    seen_titles: list[str] = []
    seen_urls: list[str] = []
    writes_outside_tmp: list[str] = []
    active = True

    def _audit(event: str, args: tuple) -> None:
        if active and event == "open" and len(args) >= 2 and any(f in str(args[1]) for f in ("w", "a", "x", "+")):
            writes_outside_tmp.append(str(args[0]))

    sys.addaudithook(_audit)
    try:
        with TestClient(app) as client:
            health = client.get("/v1/health")
            assert health.status_code == 200
            assert health.json()["readiness"] == "ready"

            queries = [
                ("notebook", "notebook 16gb", Decimal("6000")),
                ("smartphone", "smartphone 128gb", Decimal("5000")),
                ("monitor", "monitor 27 polegadas", Decimal("4500")),
                ("fone de ouvido", "fone bluetooth", Decimal("2500")),
            ]
            for expected_cat, q_text, budget in queries:
                resp = client.post(
                    "/v1/search",
                    headers={"X-API-Key": api_key},
                    json={"query": q_text, "budget_brl": str(budget), "limit": 3},
                )
                assert resp.status_code == 200
                body = resp.json()
                assert body["detected_category"] == expected_cat
                assert body["status"] in {"ok", "no_results", "all_rejected"}
                for prod in body["products"]:
                    for offer in prod["offers"]:
                        seen_titles.append(offer["title"])
                        if offer.get("item_web_url"):
                            seen_urls.append(offer["item_web_url"])
    finally:
        active = False
        backend.close()
        oauth.close()

    # Garante consumo estritamente limitado (<= 8 chamadas Browse nas 4 categorias).
    assert quota_guard.snapshot().used_in_window <= 8
    assert list(tmp_path.rglob("*")) == []
    assert writes_outside_tmp == []

    logs_raw = log_stream.getvalue()
    assert api_key not in logs_raw
    for title in seen_titles:
        assert title not in logs_raw
    for url in seen_urls:
        assert url not in logs_raw
