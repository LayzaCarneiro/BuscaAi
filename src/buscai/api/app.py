"""Fábrica da aplicação FastAPI, lifespan e middleware de Request ID."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from buscai.api.auth import APIKeyAuthenticator
from buscai.api.dependencies import AppContainer, build_default_container
from buscai.api.errors import register_exception_handlers, validate_or_generate_request_id
from buscai.api.rate_limit import InMemoryRateLimiter
from buscai.api.routes import router
from buscai.config import Settings
from buscai.observability import log_operational_event
from buscai.orchestration import SearchOrchestrator
from buscai.pricing import CurrencyConverter
from buscai.semantic.embeddings import EmbeddingService


class RequestContextMiddleware:
    """Middleware ASGI puro para X-Request-ID validado e logging operacional por allowlist."""

    def __init__(self, app: ASGIApp, *, logger: logging.Logger) -> None:
        self._app = app
        self._logger = logger

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = validate_or_generate_request_id(headers.get("X-Request-ID"))
        state = scope.setdefault("state", {})
        state["request_id"] = request_id

        method = str(scope.get("method", "GET"))
        path = str(scope.get("path", "/"))
        started = time.perf_counter()
        status_code = 500

        async def _send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message.get("status", 500))
                mutable_headers = MutableHeaders(scope=message)
                mutable_headers["X-Request-ID"] = request_id
                rate_headers = state.get("rate_limit_headers")
                if isinstance(rate_headers, dict):
                    for key, val in rate_headers.items():
                        mutable_headers.setdefault(str(key), str(val))
            await send(message)

        try:
            await self._app(scope, receive, _send_wrapper)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
            error_type = state.get("error_type")
            level = (
                logging.INFO
                if status_code < 400
                else (logging.WARNING if status_code < 500 else logging.ERROR)
            )
            log_operational_event(
                self._logger,
                level,
                "http_request_completed",
                request_id=request_id,
                stage="http_api",
                method=method,
                path=path,
                status_code=status_code,
                duration_ms=duration_ms,
                error_type=str(error_type) if error_type else None,
            )


def create_app(
    *,
    settings: Settings | None = None,
    orchestrator: SearchOrchestrator | None = None,
    embedding_service: EmbeddingService | None = None,
    authenticator: APIKeyAuthenticator | None = None,
    rate_limiter: InMemoryRateLimiter | None = None,
    currency_converter: CurrencyConverter | None = None,
    preload_embeddings: bool | None = None,
    logger: logging.Logger | None = None,
) -> FastAPI:
    """Cria instância FastAPI configurada com lifespan compartilhado e injeção opcional."""
    container_holder: dict[str, AppContainer] = {}
    bootstrap_logger = logger or logging.getLogger("buscai.api")

    @asynccontextmanager
    async def lifespan(fastapi_app: FastAPI) -> AsyncIterator[None]:
        container = build_default_container(
            settings=settings,
            orchestrator=orchestrator,
            embedding_service=embedding_service,
            authenticator=authenticator,
            rate_limiter=rate_limiter,
            currency_converter=currency_converter,
            preload_embeddings=preload_embeddings,
            logger=logger,
        )
        container_holder["instance"] = container
        fastapi_app.state.container = container
        try:
            yield
        finally:
            container.close()

    fastapi_app = FastAPI(
        title="BUSCAAI API",
        version="0.2.0",
        description=(
            "API HTTP B2B de busca inteligente e recomendação de produtos do BUSCAAI. "
            "Opera de forma stateless em memória sem persistência de anúncios da eBay."
        ),
        debug=False,
        lifespan=lifespan,
    )

    register_exception_handlers(fastapi_app)
    fastapi_app.add_middleware(RequestContextMiddleware, logger=bootstrap_logger)
    fastapi_app.include_router(router)
    return fastapi_app


app = create_app()
