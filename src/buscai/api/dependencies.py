"""Contêiner de dependências compartilhadas, lifespan e readiness da API FastAPI."""

from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from fastapi import Depends, Request, Security

from buscai.api.auth import API_KEY_HEADER, APIKeyAuthenticator
from buscai.api.errors import APIError
from buscai.api.rate_limit import InMemoryRateLimiter
from buscai.api.schemas import ComponentHealthSchema, HealthResponseSchema
from buscai.catalog_registry import ProfileRegistry, load_default_registry
from buscai.config import Settings, load_settings
from buscai.observability import configure_logging, log_operational_event
from buscai.orchestration import (
    OrchestrationDependencies,
    SearchOrchestrator,
    TracingPolicyViolationError,
    ensure_tracing_disabled,
)
from buscai.pricing import CurrencyConverter, FixedEnvExchangeRateProvider
from buscai.ranking.pipeline import RankingPipeline
from buscai.search.ebay_auth import EbayOAuthClient
from buscai.search.ebay_backend import EbayLiveBackend
from buscai.search.query_builder import QueryBuilder
from buscai.search.quota import QuotaGuard
from buscai.semantic.embeddings import EmbeddingService
from buscai.semantic.pipeline import SemanticSearchProcessor
from buscai.validation.pipeline import ValidationPipeline
from userintent.llm import IntentExtractor


def _try_preload_embeddings(
    embedding_service: EmbeddingService,
    logger: logging.Logger,
) -> str | None:
    """Tenta pré-carregar pesos já disponíveis localmente em memória sem falhar o startup."""
    provider = getattr(embedding_service, "_provider", None)
    try:
        ensure_loaded_fn = getattr(provider, "ensure_loaded", None)
        if callable(ensure_loaded_fn):
            ensure_loaded_fn()
        elif not embedding_service.is_ready():
            embedding_service.embed_queries(["buscai warmup"])
        return None
    except Exception as exc:
        log_operational_event(
            logger,
            logging.WARNING,
            "embedding_preload_failed",
            stage="startup",
            error_type=type(exc).__name__,
        )
        return type(exc).__name__


def _extract_embedding_service(orchestrator: SearchOrchestrator | None) -> EmbeddingService | None:
    if orchestrator is None:
        return None
    deps = getattr(orchestrator, "_deps", None)
    sem_proc = getattr(deps, "semantic_processor", None)
    emb = getattr(sem_proc, "_embedding_service", None)
    return emb if isinstance(emb, EmbeddingService) else None


@dataclass
class AppContainer:
    """Dependências compartilhadas e thread-safe construídas no lifespan da aplicação."""

    settings: Settings
    authenticator: APIKeyAuthenticator
    rate_limiter: InMemoryRateLimiter
    profile_registry: ProfileRegistry
    currency_converter: CurrencyConverter
    orchestrator: SearchOrchestrator | None
    embedding_service: EmbeddingService | None
    logger: logging.Logger
    owned_closables: list[Any] = field(default_factory=list)
    startup_issues: tuple[str, ...] = ()

    def build_health_report(self) -> HealthResponseSchema:
        """Avalia liveness e readiness sem disparar buscas, downloads ou expor segredos."""
        components: list[ComponentHealthSchema] = []

        auth_ok = self.authenticator.is_configured
        components.append(
            ComponentHealthSchema(
                name="api_auth",
                required=True,
                status="ready" if auth_ok else "not_ready",
                detail=(
                    "Autenticação X-API-Key configurada."
                    if auth_ok
                    else "Nenhuma API key válida configurada."
                ),
            )
        )

        orch_ok = self.orchestrator is not None
        components.append(
            ComponentHealthSchema(
                name="search_orchestrator",
                required=True,
                status="ready" if orch_ok else "not_ready",
                detail=(
                    "Orquestrador LangGraph pronto em memória."
                    if orch_ok
                    else "Orquestrador indisponível (verifique credenciais ou inicialização)."
                ),
            )
        )

        emb = self.embedding_service
        emb_ready = emb is not None and emb.is_ready()
        if emb is None:
            emb_detail = "Serviço de embeddings não inicializado."
        elif not emb_ready:
            emb_detail = "Modelo de embeddings obrigatório ainda não carregado em memória."
        else:
            info = emb.model_info()
            emb_detail = f"Modelo '{info.model_name}' carregado em memória ({info.backend})."
        components.append(
            ComponentHealthSchema(
                name="embeddings",
                required=True,
                status="ready" if emb_ready else "not_ready",
                detail=emb_detail,
            )
        )

        try:
            ensure_tracing_disabled()
            tracing_ok = True
            tracing_detail = "Tracing externo desativado conforme política D7."
        except TracingPolicyViolationError:
            tracing_ok = False
            tracing_detail = "Variável de tracing externo ativa em violação à política D7."
        components.append(
            ComponentHealthSchema(
                name="tracing_policy",
                required=True,
                status="ready" if tracing_ok else "not_ready",
                detail=tracing_detail,
            )
        )

        rate_info = self.currency_converter.rate_info()
        components.append(
            ComponentHealthSchema(
                name="exchange_rate",
                required=False,
                status="ready",
                detail=f"Provedor cambial '{rate_info.source}' ativo (estimativa USD->BRL).",
            )
        )

        all_required_ready = all(
            comp.status == "ready" for comp in components if comp.required
        )
        return HealthResponseSchema(
            status="ok" if all_required_ready else "not_ready",
            liveness="alive",
            readiness="ready" if all_required_ready else "not_ready",
            components=components,
        )

    def close(self) -> None:
        """Fecha clientes HTTP gerenciados pelo contêiner ao encerrar o lifespan."""
        for resource in self.owned_closables:
            close_fn = getattr(resource, "close", None)
            if callable(close_fn):
                with suppress(Exception):
                    close_fn()


def build_default_container(
    *,
    settings: Settings | None = None,
    orchestrator: SearchOrchestrator | None = None,
    embedding_service: EmbeddingService | None = None,
    authenticator: APIKeyAuthenticator | None = None,
    rate_limiter: InMemoryRateLimiter | None = None,
    currency_converter: CurrencyConverter | None = None,
    preload_embeddings: bool | None = None,
    logger: logging.Logger | None = None,
) -> AppContainer:
    """Constrói dependências compartilhadas para o lifespan da aplicação FastAPI."""
    active_settings = settings or load_settings(load_dotenv_file=False)
    active_logger = logger or configure_logging(active_settings.log_level, logger_name="buscai")
    active_auth = authenticator or APIKeyAuthenticator.from_settings(active_settings)
    active_limiter = rate_limiter or InMemoryRateLimiter(
        max_requests=active_settings.rate_limit_per_minute,
        window_seconds=active_settings.rate_limit_window_seconds,
    )
    profile_registry = load_default_registry()
    converter = currency_converter or CurrencyConverter(
        FixedEnvExchangeRateProvider.from_settings(active_settings)
    )

    should_preload = (
        active_settings.embedding_preload
        if preload_embeddings is None
        else preload_embeddings
    )
    owned_closables: list[Any] = []
    startup_issues: list[str] = []

    if orchestrator is not None:
        active_orchestrator: SearchOrchestrator | None = orchestrator
        active_emb = embedding_service or _extract_embedding_service(orchestrator)
        if should_preload and active_emb is not None and not active_emb.is_ready():
            issue = _try_preload_embeddings(active_emb, active_logger)
            if issue is not None:
                startup_issues.append(f"embeddings:{issue}")
    else:
        active_emb = embedding_service or EmbeddingService.from_settings(
            active_settings,
            logger=active_logger,
        )
        if should_preload and not active_emb.is_ready():
            issue = _try_preload_embeddings(active_emb, active_logger)
            if issue is not None:
                startup_issues.append(f"embeddings:{issue}")

        active_orchestrator = None
        if active_settings.is_gemini_configured and active_settings.is_ebay_configured:
            try:
                quota_guard = QuotaGuard(
                    daily_limit=active_settings.ebay_daily_quota_limit,
                    logger=active_logger,
                )
                oauth_client = EbayOAuthClient.from_settings(
                    active_settings,
                    logger=active_logger,
                )
                owned_closables.append(oauth_client)
                ebay_backend = EbayLiveBackend(
                    oauth_client=oauth_client,
                    quota_guard=quota_guard,
                    default_marketplace_id=active_settings.ebay_marketplace_id,
                    logger=active_logger,
                )
                owned_closables.append(ebay_backend)

                extractor = IntentExtractor.from_settings(
                    active_settings,
                    logger=active_logger,
                )
                query_builder = QueryBuilder(
                    currency_converter=converter,
                    marketplace_id=active_settings.ebay_marketplace_id,
                )
                semantic_processor = SemanticSearchProcessor(
                    active_emb,
                    logger=active_logger,
                )
                validation_pipeline = ValidationPipeline(
                    registry=profile_registry,
                    currency_converter=converter,
                    search_backend=ebay_backend,
                    query_builder=query_builder,
                    logger=active_logger,
                )
                ranking_pipeline = RankingPipeline(
                    converter=converter,
                    profile_registry=profile_registry,
                )
                deps = OrchestrationDependencies(
                    extractor=extractor,
                    query_builder=query_builder,
                    search_backend=ebay_backend,
                    semantic_processor=semantic_processor,
                    validation_pipeline=validation_pipeline,
                    ranking_pipeline=ranking_pipeline,
                    quota_guard=quota_guard,
                    logger=active_logger,
                )
                active_orchestrator = SearchOrchestrator(deps)
            except Exception as exc:
                startup_issues.append(f"orchestrator:{type(exc).__name__}")
                log_operational_event(
                    active_logger,
                    logging.WARNING,
                    "orchestrator_startup_not_ready",
                    stage="startup",
                    error_type=type(exc).__name__,
                )
                for res in owned_closables:
                    close_fn = getattr(res, "close", None)
                    if callable(close_fn):
                        with suppress(Exception):
                            close_fn()
                owned_closables.clear()
                active_orchestrator = None
        else:
            startup_issues.append("orchestrator:missing_provider_credentials")

    return AppContainer(
        settings=active_settings,
        authenticator=active_auth,
        rate_limiter=active_limiter,
        profile_registry=profile_registry,
        currency_converter=converter,
        orchestrator=active_orchestrator,
        embedding_service=active_emb,
        logger=active_logger,
        owned_closables=owned_closables,
        startup_issues=tuple(startup_issues),
    )


def get_container(request: Request) -> AppContainer:
    """Obtém o AppContainer compartilhado anexado ao estado da aplicação."""
    container = getattr(request.app.state, "container", None)
    if not isinstance(container, AppContainer):
        raise APIError(
            503,
            code="service_not_ready",
            message="Aplicação ainda não inicializada.",
        )
    return container


def require_authenticated_client(
    request: Request,
    api_key: str | None = Security(API_KEY_HEADER),
    container: AppContainer = Depends(get_container),
) -> str:
    """Autentica via X-API-Key e aplica rate limiting por cliente em memória."""
    client_id = container.authenticator.authenticate(api_key)
    decision = container.rate_limiter.check_and_consume(client_id)
    rate_headers = decision.to_headers()
    request.state.rate_limit_headers = rate_headers
    if not decision.allowed:
        raise APIError(
            429,
            code="rate_limit_exceeded",
            message="Limite de requisições por janela excedido. Aguarde antes de tentar novamente.",
            headers=rate_headers,
        )
    return client_id
