"""Testes do gestor de token OAuth client_credentials em memória da eBay."""

from __future__ import annotations

import base64
import io
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs

import httpx
import pytest

from buscai.catalog.errors import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from buscai.config import load_settings
from buscai.observability import configure_logging
from buscai.search.ebay_auth import EBAY_BROWSE_SCOPE, EbayOAuthClient


class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_issues_token_with_basic_auth_and_reuses_in_memory_until_margin() -> None:
    calls: list[httpx.Request] = []
    clock = _FakeClock(start=100.0)
    counter = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal counter
        calls.append(request)
        counter += 1
        return httpx.Response(
            200,
            json={"access_token": f"fake_tok_{counter}", "expires_in": 7200, "token_type": "Application Access Token"},
        )

    log_stream = io.StringIO()
    logger = configure_logging("INFO", stream=log_stream, logger_name="buscai.test.oauth")
    client = EbayOAuthClient(
        client_id="fake_client_id",
        client_secret="fake_client_secret",
        refresh_margin_seconds=120.0,
        transport=httpx.MockTransport(handler),
        clock=clock,
        logger=logger,
    )

    try:
        tok1 = client.get_access_token(request_id="req-1")
        clock.advance(7000.0)  # ainda restam 200s (> margem 120s)
        tok2 = client.get_access_token(request_id="req-2")
        assert tok1 == tok2 == "fake_tok_1"
        assert len(calls) == 1

        req = calls[0]
        expected_basic = base64.b64encode(b"fake_client_id:fake_client_secret").decode("ascii")
        assert req.method == "POST"
        assert req.headers["Authorization"] == f"Basic {expected_basic}"
        form = parse_qs(req.content.decode("utf-8"))
        assert form == {"grant_type": ["client_credentials"], "scope": [EBAY_BROWSE_SCOPE]}

        # Entra na margem de renovação (restam 100s <= 120s) -> renova automaticamente
        clock.advance(100.0)
        tok3 = client.get_access_token(request_id="req-3")
        assert tok3 == "fake_tok_2"
        assert len(calls) == 2

        # Invalidação e force_refresh forçam emissão
        client.invalidate()
        tok4 = client.get_access_token(request_id="req-4")
        assert tok4 == "fake_tok_3"
        tok5 = client.get_access_token(force_refresh=True, request_id="req-5")
        assert tok5 == "fake_tok_4"
        assert len(calls) == 4

        # Nenhuma credencial ou token aparece em repr ou logs
        logs_and_repr = f"{repr(client)} {log_stream.getvalue()}"
        for secret in ("fake_client_id", "fake_client_secret", "fake_tok_1", "fake_tok_2", "fake_tok_3", "fake_tok_4"):
            assert secret not in logs_and_repr
    finally:
        client.close()


def test_oauth_client_is_thread_safe_and_issues_single_token_under_concurrency() -> None:
    http_calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        return httpx.Response(200, json={"access_token": "shared_tok", "expires_in": 3600})

    client = EbayOAuthClient(
        client_id="cid",
        client_secret="csec",
        transport=httpx.MockTransport(handler),
    )
    try:
        with ThreadPoolExecutor(max_workers=10) as pool:
            tokens = list(pool.map(lambda _: client.get_access_token(), range(15)))

        assert tokens == ["shared_tok"] * 15
        assert http_calls == 1
    finally:
        client.close()


@pytest.mark.parametrize(
    ("status", "headers", "body", "exc_cls"),
    [
        (400, {}, {"error": "invalid_client"}, ProviderAuthError),
        (401, {}, {"error": "unauthorized"}, ProviderAuthError),
        (403, {}, {"error": "forbidden"}, ProviderAuthError),
        (429, {"Retry-After": "12"}, {"error": "rate_limit"}, ProviderRateLimitError),
        (503, {}, {"error": "unavailable"}, ProviderUnavailableError),
        (200, {}, {"expires_in": 3600}, ProviderAuthError),
        (200, {}, {"access_token": "ok", "expires_in": 0}, ProviderAuthError),
    ],
)
def test_oauth_http_and_payload_errors_are_mapped_safely(
    status: int,
    headers: dict[str, str],
    body: dict[str, object],
    exc_cls: type[Exception],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers, json=body)

    client = EbayOAuthClient(
        client_id="secret_id_xyz",
        client_secret="secret_val_xyz",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(exc_cls) as exc_info:
            client.get_access_token()
        assert "secret_id_xyz" not in str(exc_info.value)
        assert "secret_val_xyz" not in str(exc_info.value)
        if isinstance(exc_info.value, ProviderRateLimitError):
            assert exc_info.value.retry_after_seconds == 12.0
    finally:
        client.close()


def test_oauth_timeout_and_network_errors() -> None:
    def raise_timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    timeout_client = EbayOAuthClient(
        client_id="cid",
        client_secret="csec",
        transport=httpx.MockTransport(raise_timeout),
    )
    try:
        with pytest.raises(ProviderTimeoutError):
            timeout_client.get_access_token()
    finally:
        timeout_client.close()

    def raise_net(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed")

    net_client = EbayOAuthClient(
        client_id="cid",
        client_secret="csec",
        transport=httpx.MockTransport(raise_net),
    )
    try:
        with pytest.raises(ProviderUnavailableError):
            net_client.get_access_token()
    finally:
        net_client.close()


def test_oauth_from_settings_requires_configured_credentials() -> None:
    empty_settings = load_settings(environ={})
    with pytest.raises(ProviderAuthError):
        EbayOAuthClient.from_settings(empty_settings)

    valid_settings = load_settings(
        environ={"EBAY_CLIENT_ID": "cid_ok", "EBAY_CLIENT_SECRET": "csec_ok"}
    )
    client = EbayOAuthClient.from_settings(
        valid_settings,
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(200, json={"access_token": "tok_settings", "expires_in": 1000})
        ),
    )
    try:
        assert client.get_access_token() == "tok_settings"
    finally:
        client.close()
