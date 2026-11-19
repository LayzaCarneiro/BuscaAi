"""Exceções compartilhadas de catálogos e provedores de busca."""

from __future__ import annotations


class CatalogError(RuntimeError):
    """Falha de comunicação ou resposta inválida do fornecedor."""


class ProviderError(CatalogError):
    """Erro base para provedores de busca externos (ex.: eBay)."""


class ProviderAuthError(ProviderError):
    """Falha de autenticação ou autorização (HTTP 401/403 ou credenciais ausentes)."""


class ProviderRateLimitError(ProviderError):
    """Limite de requisições ou cota diária atingido (HTTP 429 ou guarda local)."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ProviderTimeoutError(ProviderError):
    """Timeout de conexão ou leitura junto ao provedor externo."""


class ProviderUnavailableError(ProviderError):
    """Indisponibilidade temporária do provedor (HTTP 5xx ou erro de rede)."""
