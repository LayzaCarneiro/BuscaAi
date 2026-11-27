"""Fábrica da aplicação FastAPI, lifespan e middleware de Request ID."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
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


DEFAULT_MAX_REQUEST_BODY_BYTES = 65_536


class _RequestBodyTooLargeError(Exception):
    """Exceção interna para interromper leitura de corpo HTTP acima do limite configurado."""


class RequestContextMiddleware:
    """Middleware ASGI puro para X-Request-ID validado, limite de corpo HTTP, headers defensivos e logging."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        logger: logging.Logger,
        max_body_bytes: int = DEFAULT_MAX_REQUEST_BODY_BYTES,
    ) -> None:
        self._app = app
        self._logger = logger
        self._max_body_bytes = max_body_bytes

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
        response_started = False

        async def _send_wrapper(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = int(message.get("status", 500))
                mutable_headers = MutableHeaders(scope=message)
                mutable_headers["X-Request-ID"] = request_id
                mutable_headers.setdefault("X-Content-Type-Options", "nosniff")
                mutable_headers.setdefault("X-Frame-Options", "DENY")
                mutable_headers.setdefault("Referrer-Policy", "no-referrer")
                if path.startswith("/v1/"):
                    mutable_headers.setdefault("Cache-Control", "no-store")
                rate_headers = state.get("rate_limit_headers")
                if isinstance(rate_headers, dict):
                    for key, val in rate_headers.items():
                        mutable_headers.setdefault(str(key), str(val))
            await send(message)

        async def _send_payload_too_large() -> None:
            state["error_type"] = "payload_too_large"
            resp = JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "code": "payload_too_large",
                        "message": "Corpo da requisição excede o tamanho máximo permitido.",
                        "request_id": request_id,
                    }
                },
            )
            await resp(scope, receive, _send_wrapper)

        received_bytes = 0

        async def _limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message.get("type") == "http.request":
                chunk = message.get("body", b"")
                if isinstance(chunk, (bytes, bytearray)):
                    received_bytes += len(chunk)
                    if received_bytes > self._max_body_bytes:
                        raise _RequestBodyTooLargeError()
            return message

        try:
            content_length_raw = headers.get("content-length")
            if content_length_raw is not None:
                try:
                    content_length = int(content_length_raw.strip())
                except ValueError:
                    content_length = -1
                if content_length > self._max_body_bytes:
                    await _send_payload_too_large()
                    return

            await self._app(scope, _limited_receive, _send_wrapper)
        except _RequestBodyTooLargeError:
            if not response_started:
                await _send_payload_too_large()
            else:
                raise
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

    max_body_bytes = (
        settings.max_request_body_bytes if settings is not None else DEFAULT_MAX_REQUEST_BODY_BYTES
    )
    register_exception_handlers(fastapi_app)
    fastapi_app.add_middleware(
        RequestContextMiddleware,
        logger=bootstrap_logger,
        max_body_bytes=max_body_bytes,
    )
    fastapi_app.include_router(router)
    return fastapi_app


app = create_app()
