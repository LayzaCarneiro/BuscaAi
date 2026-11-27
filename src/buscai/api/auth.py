"""Autenticação B2B via header X-API-Key com comparação em tempo constante."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from collections.abc import Sequence

from fastapi.security import APIKeyHeader
from pydantic import SecretStr

from buscai.api.errors import APIError
from buscai.config import Settings

API_KEY_HEADER_NAME = "X-API-Key"
API_KEY_HEADER = APIKeyHeader(name=API_KEY_HEADER_NAME, auto_error=False)

_PLACEHOLDER_EXACT: frozenset[str] = frozenset(
    {
        "changeme",
        "replace_me",
        "placeholder",
        "your_api_key",
        "your-api-key",
        "api_key_here",
        "example_key",
        "sample_key",
        "dummy_key",
        "test_placeholder",
        "todo",
        "none",
        "null",
        "xxx",
        "xxxx",
        "chave_cliente",
        "chave_cliente_1",
        "chave_cliente_2",
        "coloque_sua_chave_aqui",
    }
)
_PLACEHOLDER_PREFIXES: tuple[str, ...] = (
    "coloque_",
    "insert_",
    "replace_",
    "placeholder",
    "your_",
    "example_",
    "chave_cliente",
)


def is_placeholder_api_key(value: str) -> bool:
    """Identifica valores de exemplo/placeholder que nunca devem autenticar requisições."""
    cleaned = value.strip()
    if not cleaned:
        return True
    lowered = cleaned.casefold()
    if lowered in _PLACEHOLDER_EXACT:
        return True
    if lowered.startswith(_PLACEHOLDER_PREFIXES):
        return True
    if ("<" in cleaned and ">" in cleaned) or ("{" in cleaned and "}" in cleaned):
        return True
    return False


class APIKeyAuthenticator:
    """Validador seguro de X-API-Key que nunca registra nem armazena chaves de rate limit em claro."""

    def __init__(self, api_keys: Sequence[SecretStr | str] = ()) -> None:
        self._salt = os.urandom(32)
        valid_entries: list[tuple[bytes, str]] = []
        seen_digests: set[str] = set()

        for raw_item in api_keys:
            raw_val = raw_item.get_secret_value() if isinstance(raw_item, SecretStr) else raw_item
            if not isinstance(raw_val, str):
                continue
            cleaned = raw_val.strip()
            if not cleaned or is_placeholder_api_key(cleaned):
                continue
            key_bytes = cleaned.encode("utf-8")
            client_id = hmac.new(self._salt, key_bytes, hashlib.sha256).hexdigest()[:24]
            if client_id not in seen_digests:
                seen_digests.add(client_id)
                valid_entries.append((key_bytes, client_id))

        self._valid_entries: tuple[tuple[bytes, str], ...] = tuple(valid_entries)

    @classmethod
    def from_settings(cls, settings: Settings) -> APIKeyAuthenticator:
        """Constrói o autenticador a partir das chaves configuradas em Settings."""
        return cls(api_keys=settings.api_keys)

    @property
    def is_configured(self) -> bool:
        """Indica se há ao menos uma API key válida (não placeholder) configurada."""
        return len(self._valid_entries) > 0

    def authenticate(self, provided_key: str | None) -> str:
        """Valida a chave recebida e retorna um identificador opaco (HMAC) do cliente."""
        if not self.is_configured:
            raise APIError(
                503,
                code="auth_not_configured",
                message="Autenticação da API não configurada no servidor.",
            )

        if not isinstance(provided_key, str) or not provided_key.strip():
            raise APIError(
                401,
                code="missing_api_key",
                message="Header X-API-Key obrigatório.",
                headers={"WWW-Authenticate": "ApiKey"},
            )

        candidate = provided_key.strip()
        if is_placeholder_api_key(candidate):
            raise APIError(
                401,
                code="invalid_api_key",
                message="Credencial X-API-Key inválida.",
                headers={"WWW-Authenticate": "ApiKey"},
            )

        candidate_bytes = candidate.encode("utf-8")
        matched_client_id: str | None = None
        # Percorre todas as chaves configuradas com comparação em tempo constante.
        for valid_bytes, client_id in self._valid_entries:
            if secrets.compare_digest(candidate_bytes, valid_bytes):
                matched_client_id = client_id

        if matched_client_id is None:
            raise APIError(
                401,
                code="invalid_api_key",
                message="Credencial X-API-Key inválida.",
                headers={"WWW-Authenticate": "ApiKey"},
            )

        return matched_client_id

    def __repr__(self) -> str:
        return f"APIKeyAuthenticator(configured_keys={len(self._valid_entries)})"
