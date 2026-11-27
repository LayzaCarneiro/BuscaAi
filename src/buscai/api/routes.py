"""Rotas HTTP das Etapas 8 e 10: GET /v1/health, POST /v1/search e POST /v1/chat."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from buscai.api.chat import build_chat_response, sanitize_conversation_history, validate_chat_limits
from buscai.api.dependencies import AppContainer, get_container, require_authenticated_client
from buscai.api.errors import APIError, get_request_id_from_request, map_orchestration_error
from buscai.api.schemas import (
    ChatRequest,
    ChatResponse,
    ErrorResponseSchema,
    HealthResponseSchema,
    SearchRequest,
    SearchResponseSchema,
    build_search_response,
)
from buscai.orchestration import ExecutionStatus, ensure_tracing_disabled

router = APIRouter(prefix="/v1")


@router.get(
    "/health",
    tags=["Health"],
    summary="Estado operacional (liveness e readiness)",
    response_model=HealthResponseSchema,
    responses={
        200: {"model": HealthResponseSchema, "description": "Aplicação pronta para processar buscas."},
        503: {"model": HealthResponseSchema, "description": "Aplicação viva, porém dependência obrigatória não pronta."},
    },
)
async def health_check(
    container: AppContainer = Depends(get_container),
) -> JSONResponse:
    """Informa liveness e readiness sem expor segredos nem executar chamadas externas."""
    report = container.build_health_report()
    status_code = 200 if report.readiness == "ready" else 503
    return JSONResponse(status_code=status_code, content=report.model_dump())


@router.post(
    "/search",
    tags=["Search"],
    summary="Busca inteligente e recomendação de produtos",
    response_model=SearchResponseSchema,
    responses={
        400: {"model": ErrorResponseSchema, "description": "Requisição semanticamente inválida."},
        401: {"model": ErrorResponseSchema, "description": "Header X-API-Key ausente ou inválido."},
        422: {"model": ErrorResponseSchema, "description": "Erro de validação do payload HTTP."},
        429: {"model": ErrorResponseSchema, "description": "Limite de requisições excedido."},
        500: {"model": ErrorResponseSchema, "description": "Erro interno inesperado."},
        502: {"model": ErrorResponseSchema, "description": "Falha ou resposta inválida de provedor externo."},
        503: {"model": ErrorResponseSchema, "description": "Dependência ou serviço indisponível."},
    },
)
async def search_products(
    payload: SearchRequest,
    request: Request,
    response: Response,
    container: AppContainer = Depends(get_container),
    _client_id: str = Depends(require_authenticated_client),
) -> SearchResponseSchema:
    """Executa o fluxo completo de busca (Etapa 7) sem persistir anúncios da eBay."""
    request_id = get_request_id_from_request(request)
    effective_limit = (
        payload.limit
        if payload.limit is not None
        else container.settings.search_default_limit
    )
    if effective_limit > container.settings.search_max_limit:
        raise APIError(
            422,
            code="validation_error",
            message=f"limit excede o máximo configurado ({container.settings.search_max_limit}).",
            details=[
                {
                    "field": "body.limit",
                    "type": "less_than_equal",
                    "message": f"Valor máximo permitido: {container.settings.search_max_limit}.",
                }
            ],
        )

    ensure_tracing_disabled()

    if container.orchestrator is None:
        raise APIError(
            503,
            code="service_not_ready",
            message="Orquestrador de busca indisponível no momento.",
        )

    rate_headers = getattr(request.state, "rate_limit_headers", None)
    if isinstance(rate_headers, dict):
        for header_name, header_value in rate_headers.items():
            response.headers[str(header_name)] = str(header_value)

    retrieval_limit = max(30, effective_limit)
    result = await run_in_threadpool(
        container.orchestrator.run,
        payload.query,
        budget_brl=payload.budget_brl,
        profile_id=payload.profile_id,
        limit=retrieval_limit,
        request_id=request_id,
    )

    if result.status == ExecutionStatus.ERROR:
        raise map_orchestration_error(result.error)

    if result.status == ExecutionStatus.INVALID_PLAN:
        raise APIError(
            400,
            code="invalid_search_plan",
            message="Não foi possível construir um plano de busca válido para a consulta informada.",
        )

    return build_search_response(
        result,
        request_id=request_id,
        limit=effective_limit,
        converter=container.currency_converter,
    )


@router.post(
    "/chat",
    tags=["Chat"],
    summary="Chat conversacional multi-turn de busca de produtos",
    response_model=ChatResponse,
    responses={
        400: {"model": ErrorResponseSchema, "description": "Requisição semanticamente inválida."},
        401: {"model": ErrorResponseSchema, "description": "Header X-API-Key ausente ou inválido."},
        422: {"model": ErrorResponseSchema, "description": "Erro de validação do payload HTTP."},
        429: {"model": ErrorResponseSchema, "description": "Limite de requisições excedido."},
        500: {"model": ErrorResponseSchema, "description": "Erro interno inesperado."},
        502: {"model": ErrorResponseSchema, "description": "Falha ou resposta inválida de provedor externo."},
        503: {"model": ErrorResponseSchema, "description": "Dependência ou serviço indisponível."},
    },
)
async def chat_products(
    payload: ChatRequest,
    request: Request,
    response: Response,
    container: AppContainer = Depends(get_container),
    _client_id: str = Depends(require_authenticated_client),
) -> ChatResponse:
    """Executa busca conversacional stateless (Etapa 10) reutilizando SearchOrchestrator sem persistir sessões."""
    request_id = get_request_id_from_request(request)
    effective_limit = validate_chat_limits(payload, settings=container.settings)

    ensure_tracing_disabled()

    if container.orchestrator is None:
        raise APIError(
            503,
            code="service_not_ready",
            message="Orquestrador de busca indisponível no momento.",
        )

    rate_headers = getattr(request.state, "rate_limit_headers", None)
    if isinstance(rate_headers, dict):
        for header_name, header_value in rate_headers.items():
            response.headers[str(header_name)] = str(header_value)

    sanitized_history = sanitize_conversation_history(payload.conversation_history, payload.message)
    retrieval_limit = max(30, effective_limit)
    result = await run_in_threadpool(
        container.orchestrator.run,
        payload.message,
        budget_brl=payload.budget_brl,
        profile_id=payload.profile_id,
        limit=retrieval_limit,
        request_id=request_id,
        conversation_history=sanitized_history,
    )

    if result.status == ExecutionStatus.ERROR:
        raise map_orchestration_error(result.error)

    if result.status == ExecutionStatus.INVALID_PLAN:
        raise APIError(
            400,
            code="invalid_search_plan",
            message="Não foi possível construir um plano de busca válido para a mensagem informada.",
        )

    return build_chat_response(
        result,
        request_id=request_id,
        limit=effective_limit,
        explicit_budget_brl=payload.budget_brl,
        converter=container.currency_converter,
    )
