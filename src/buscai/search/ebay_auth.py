"""Gestor de token OAuth 2.0 client_credentials em memória para a eBay Browse API."""

from __future__ import annotations

import base64
import logging
import math
import threading
import time
from collections.abc import Callable, Mapping

import httpx
from pydantic import SecretStr

from buscai.catalog.errors import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from buscai.config import Settings
from buscai.observability import log_operational_event

EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_BROWSE_SCOPE = "https://api.ebay.com/oauth/api_scope"
DEFAULT_REFRESH_MARGIN_SECONDS = 120.0
DEFAULT_OAUTH_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)


def parse_retry_after_header(headers: Mapping[str, str]) -> float | None:
    """Extrai Retry-After numérico em segundos quando informado e válido."""
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


class EbayOAuthClient:
    """Gerencia token OAuth de aplicação da eBay exclusivamente em memória.

    Reutiliza o token até próximo do vencimento para respeitar o limite diário
    do endpoint `/identity/v1/oauth2/token`, sem persistir nem registrar credenciais.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str | SecretStr,
        token_url: str = EBAY_TOKEN_URL,
        scope: str = EBAY_BROWSE_SCOPE,
        refresh_margin_seconds: float = DEFAULT_REFRESH_MARGIN_SECONDS,
        timeout: httpx.Timeout | float = DEFAULT_OAUTH_TIMEOUT,
        http_client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        if not isinstance(client_id, str) or not client_id.strip():
            raise ProviderAuthError("EBAY_CLIENT_ID ausente ou inválido.")

        raw_secret = (
            client_secret.get_secret_value()
            if isinstance(client_secret, SecretStr)
            else client_secret
        )
        if not isinstance(raw_secret, str) or not raw_secret.strip():
            raise ProviderAuthError("EBAY_CLIENT_SECRET ausente ou inválido.")

        if not isinstance(token_url, str) or not token_url.strip().startswith(("http://", "https://")):
            raise ValueError("token_url deve ser uma URL HTTP/HTTPS válida.")
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError("scope OAuth da eBay não pode ser vazio.")
        if isinstance(refresh_margin_seconds, bool) or not isinstance(refresh_margin_seconds, (int, float)):
            raise ValueError("refresh_margin_seconds deve ser numérico.")
        if not math.isfinite(refresh_margin_seconds) or refresh_margin_seconds < 0:
            raise ValueError("refresh_margin_seconds deve ser finito e não negativo.")

        self._client_id = client_id.strip()
        self._client_secret = SecretStr(raw_secret.strip())
        self._token_url = token_url.strip()
        self._scope = scope.strip()
        self._refresh_margin_seconds = float(refresh_margin_seconds)
        self._clock = clock
        self._logger = logger

        if http_client is not None:
            self._client = http_client
            self._owns_client = False
        else:
            self._client = httpx.Client(timeout=timeout, transport=transport)
            self._owns_client = True

        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"EbayOAuthClient(token_url={self._token_url!r}, has_cached_token={self._access_token is not None})"

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        **kwargs: object,
    ) -> EbayOAuthClient:
        """Constrói o cliente OAuth a partir de Settings validado."""
        if not settings.is_ebay_configured or settings.ebay_client_id is None or settings.ebay_client_secret is None:
            raise ProviderAuthError(
                "Credenciais da eBay (EBAY_CLIENT_ID / EBAY_CLIENT_SECRET) não configuradas."
            )
        return cls(
            client_id=settings.ebay_client_id,
            client_secret=settings.ebay_client_secret,
            **kwargs,  # type: ignore[arg-type]
        )

    def get_access_token(
        self,
        *,
        force_refresh: bool = False,
        request_id: str | None = None,
    ) -> str:
        """Retorna token válido em memória ou solicita novo token via client_credentials."""
        with self._lock:
            now = self._clock()
            if (
                not force_refresh
                and self._access_token is not None
                and now < (self._expires_at - self._refresh_margin_seconds)
            ):
                return self._access_token

            token, expires_in = self._request_new_token(request_id=request_id)
            self._access_token = token
            self._expires_at = self._clock() + expires_in
            return token

    def invalidate(self) -> None:
        """Descarta o token em memória, forçando renovação na próxima chamada."""
        with self._lock:
            self._access_token = None
            self._expires_at = 0.0

    def close(self) -> None:
        """Libera o cliente HTTP interno quando gerenciado pela própria instância."""
        if self._owns_client:
            self._client.close()

    def _request_new_token(self, *, request_id: str | None) -> tuple[str, float]:
        credentials = f"{self._client_id}:{self._client_secret.get_secret_value()}".encode("utf-8")
        basic_header = base64.b64encode(credentials).decode("ascii")

        started = time.monotonic()
        try:
            response = self._client.post(
                self._token_url,
                headers={
                    "Authorization": f"Basic {basic_header}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                data={
                    "grant_type": "client_credentials",
                    "scope": self._scope,
                },
            )
        except httpx.TimeoutException as exc:
            self._log_event(
                logging.ERROR,
                "ebay_oauth_timeout",
                request_id=request_id,
                duration_ms=self._elapsed_ms(started),
                error_type="ProviderTimeoutError",
            )
            raise ProviderTimeoutError("Timeout ao obter token OAuth da eBay.") from exc
        except httpx.HTTPError as exc:
            self._log_event(
                logging.ERROR,
                "ebay_oauth_network_error",
                request_id=request_id,
                duration_ms=self._elapsed_ms(started),
                error_type="ProviderUnavailableError",
            )
            raise ProviderUnavailableError("Falha de rede ao obter token OAuth da eBay.") from exc

        duration_ms = self._elapsed_ms(started)
        status = response.status_code

        if status == 429:
            retry_after = parse_retry_after_header(response.headers)
            self._log_event(
                logging.WARNING,
                "ebay_oauth_rate_limited",
                request_id=request_id,
                status_code=status,
                duration_ms=duration_ms,
                error_type="ProviderRateLimitError",
            )
            raise ProviderRateLimitError(
                "Limite de emissões OAuth da eBay atingido (HTTP 429).",
                retry_after_seconds=retry_after,
            )

        if status in (400, 401, 403):
            self._log_event(
                logging.ERROR,
                "ebay_oauth_auth_rejected",
                request_id=request_id,
                status_code=status,
                duration_ms=duration_ms,
                error_type="ProviderAuthError",
            )
            raise ProviderAuthError(f"A eBay recusou as credenciais OAuth (HTTP {status}).")

        if status >= 500:
            self._log_event(
                logging.ERROR,
                "ebay_oauth_unavailable",
                request_id=request_id,
                status_code=status,
                duration_ms=duration_ms,
                error_type="ProviderUnavailableError",
            )
            raise ProviderUnavailableError(f"Serviço OAuth da eBay indisponível (HTTP {status}).")

        if status != 200:
            self._log_event(
                logging.ERROR,
                "ebay_oauth_unexpected_status",
                request_id=request_id,
                status_code=status,
                duration_ms=duration_ms,
                error_type="ProviderAuthError",
            )
            raise ProviderAuthError(f"Resposta inesperada ao obter token OAuth da eBay (HTTP {status}).")

        try:
            payload = response.json()
        except ValueError as exc:
            self._log_event(
                logging.ERROR,
                "ebay_oauth_invalid_json",
                request_id=request_id,
                status_code=status,
                duration_ms=duration_ms,
                error_type="ProviderAuthError",
            )
            raise ProviderAuthError("Resposta OAuth da eBay não contém JSON válido.") from exc

        if not isinstance(payload, dict):
            raise ProviderAuthError("Resposta OAuth da eBay em formato inválido.")

        token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not isinstance(token, str) or not token.strip():
            raise ProviderAuthError("Resposta OAuth da eBay sem access_token válido.")
        if (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, (int, float))
            or not math.isfinite(expires_in)
            or expires_in <= 0
        ):
            raise ProviderAuthError("Resposta OAuth da eBay sem expires_in válido.")

        self._log_event(
            logging.INFO,
            "ebay_oauth_token_issued",
            request_id=request_id,
            status_code=status,
            duration_ms=duration_ms,
        )
        return token.strip(), float(expires_in)

    def _log_event(self, level: int, event: str, **fields: object) -> None:
        if self._logger is not None:
            log_operational_event(
                self._logger,
                level,
                event,
                stage="ebay_oauth",
                provider="ebay",
                **fields,
            )

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return round((time.monotonic() - started) * 1000.0, 2)
