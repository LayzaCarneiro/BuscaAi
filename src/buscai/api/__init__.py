"""Camada HTTP FastAPI do BUSCAAI (Etapa 8)."""

from buscai.api.app import app, create_app
from buscai.api.auth import APIKeyAuthenticator
from buscai.api.errors import APIError, validate_or_generate_request_id
from buscai.api.rate_limit import InMemoryRateLimiter, RateLimitDecision
from buscai.api.schemas import (
    ChatConversationContextSchema,
    ChatHistoryMessage,
    ChatRequest,
    ChatResponse,
    HealthResponseSchema,
    SearchRequest,
    SearchResponseSchema,
)

__all__ = [
    "APIError",
    "APIKeyAuthenticator",
    "ChatConversationContextSchema",
    "ChatHistoryMessage",
    "ChatRequest",
    "ChatResponse",
    "HealthResponseSchema",
    "InMemoryRateLimiter",
    "RateLimitDecision",
    "SearchRequest",
    "SearchResponseSchema",
    "app",
    "create_app",
    "validate_or_generate_request_id",
]
