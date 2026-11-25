"""Tratamento centralizado de erros HTTP e validação de X-Request-ID."""

from __future__ import annotations

import math
import re
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from buscai.observability import sanitize_text
from buscai.orchestration.graph import TracingPolicyViolationError
from buscai.orchestration.state import OrchestrationError

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class APIError(Exception):
    """Exceção HTTP tipada com código seguro, status HTTP e cabeçalhos opcionais."""

    def __init__(
        self,
        status_code: int,
        *,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
        details: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = dict(headers) if headers else {}
        self.details = list(details) if details else []


def validate_or_generate_request_id(raw_value: str | None) -> str:
    """Aceita X-Request-ID apenas se seguro (ASCII alfanumérico/._- até 64 chars); senão gera UUID."""
    if isinstance(raw_value, str) and REQUEST_ID_PATTERN.fullmatch(raw_value):
        return raw_value
    return uuid4().hex


def get_request_id_from_request(request: Request) -> str:
    """Obtém o request_id validado associado à requisição atual."""
    req_id = getattr(request.state, "request_id", None)
    if isinstance(req_id, str) and REQUEST_ID_PATTERN.fullmatch(req_id):
        return req_id
    generated = validate_or_generate_request_id(request.headers.get("X-Request-ID"))
    request.state.request_id = generated
    return generated


def map_orchestration_error(err: OrchestrationError | None) -> APIError:
    """Converte OrchestrationError (Etapa 7) em APIError sem vazar payloads ou exceções brutas."""
    if err is None:
        return APIError(
            500,
            code="internal_error",
            message="Erro interno ao processar a busca.",
        )

    if err.code == "provider_rate_limit":
        headers: dict[str, str] = {}
        if (
            err.retry_after_seconds is not None
            and math.isfinite(err.retry_after_seconds)
            and err.retry_after_seconds > 0
        ):
            headers["Retry-After"] = str(max(1, math.ceil(err.retry_after_seconds)))
        return APIError(
            503,
            code="upstream_rate_limit",
            message="Provedor externo de busca atingiu limite de requisições. Tente novamente em instantes.",
            headers=headers or None,
        )

    if err.code == "provider_timeout":
        return APIError(
            503,
            code="upstream_timeout",
            message="Tempo limite excedido ao consultar provedor externo de busca.",
        )

    if err.code == "provider_unavailable":
        return APIError(
            503,
            code="upstream_unavailable",
            message="Provedor externo de busca temporariamente indisponível.",
        )

    if err.code == "provider_auth_error":
        return APIError(
            502,
            code="upstream_auth_error",
            message="Falha de autenticação com o provedor externo de busca.",
        )

    if err.code == "provider_catalog_error":
        return APIError(
            502,
            code="upstream_bad_response",
            message="Resposta inválida ou falha de comunicação com o provedor externo de busca.",
        )

    if err.node == "prepare_intent_and_plan" and err.error_type == "ValidationError":
        return APIError(
            502,
            code="llm_error",
            message="Resposta estruturada inválida retornada pelo provedor de linguagem.",
        )

    if err.code == "invalid_input_or_contract":
        return APIError(
            400,
            code="invalid_request",
            message="Parâmetros da requisição inválidos para o fluxo de busca.",
        )

    if err.error_type == "LLMUnavailableError":
        return APIError(
            503,
            code="llm_unavailable",
            message="Serviço de interpretação de intenção temporariamente indisponível.",
        )

    if err.error_type == "IntentExtractionError":
        return APIError(
            502,
            code="llm_error",
            message="Não foi possível interpretar a intenção de busca junto ao provedor de linguagem.",
        )

    if err.error_type in {"EmbeddingModelLoadError", "EmbeddingInferenceError", "EmbeddingError"}:
        return APIError(
            503,
            code="embeddings_unavailable",
            message="Serviço de embeddings semânticos indisponível no momento.",
        )

    return APIError(
        500,
        code="internal_error",
        message="Erro interno controlado durante o processamento da busca.",
    )


def _error_payload(
    *,
    code: str,
    message: str,
    request_id: str,
    details: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "error": {
            "code": code,
            "message": sanitize_text(message),
            "request_id": request_id,
        }
    }
    if details:
        body["error"]["details"] = details
    return body


def _merge_response_headers(request: Request, request_id: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers: dict[str, str] = {"X-Request-ID": request_id}
    rate_headers = getattr(request.state, "rate_limit_headers", None)
    if isinstance(rate_headers, dict):
        for k, v in rate_headers.items():
            headers[str(k)] = str(v)
    if extra:
        headers.update(extra)
    return headers


def register_exception_handlers(app: FastAPI) -> None:
    """Registra handlers globais padronizados e sanitizados na aplicação FastAPI."""

    @app.exception_handler(APIError)
    async def handle_api_error(request: Request, exc: APIError) -> JSONResponse:
        request_id = get_request_id_from_request(request)
        request.state.error_type = exc.code
        headers = _merge_response_headers(request, request_id, exc.headers)
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(
                code=exc.code,
                message=exc.message,
                request_id=request_id,
                details=exc.details or None,
            ),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        request_id = get_request_id_from_request(request)
        request.state.error_type = "RequestValidationError"
        safe_details: list[dict[str, str]] = []
        for err in exc.errors()[:10]:
            loc = ".".join(str(part) for part in err.get("loc", ()))
            err_type = str(err.get("type", "value_error"))
            msg = sanitize_text(str(err.get("msg", "Valor inválido.")))
            safe_details.append({"field": loc, "type": err_type, "message": msg})
        headers = _merge_response_headers(request, request_id)
        return JSONResponse(
            status_code=422,
            content=_error_payload(
                code="validation_error",
                message="Parâmetros da requisição inválidos.",
                request_id=request_id,
                details=safe_details,
            ),
            headers=headers,
        )

    @app.exception_handler(TracingPolicyViolationError)
    async def handle_tracing_violation(request: Request, _exc: TracingPolicyViolationError) -> JSONResponse:
        request_id = get_request_id_from_request(request)
        request.state.error_type = "TracingPolicyViolationError"
        headers = _merge_response_headers(request, request_id)
        return JSONResponse(
            status_code=503,
            content=_error_payload(
                code="policy_violation",
                message="Serviço bloqueado por política de conformidade de tracing (D7).",
                request_id=request_id,
            ),
            headers=headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        request_id = get_request_id_from_request(request)
        request.state.error_type = f"HTTP_{exc.status_code}"
        code = "not_found" if exc.status_code == 404 else (
            "method_not_allowed" if exc.status_code == 405 else "http_error"
        )
        safe_msg = (
            "Recurso não encontrado."
            if exc.status_code == 404
            else ("Método HTTP não permitido." if exc.status_code == 405 else "Requisição HTTP inválida.")
        )
        headers = _merge_response_headers(request, request_id)
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(code=code, message=safe_msg, request_id=request_id),
            headers=headers,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        request_id = get_request_id_from_request(request)
        request.state.error_type = type(exc).__name__
        headers = _merge_response_headers(request, request_id)
        return JSONResponse(
            status_code=500,
            content=_error_payload(
                code="internal_error",
                message="Erro interno inesperado.",
                request_id=request_id,
            ),
            headers=headers,
        )
