"""Testes do controle de cota diária e orçamento por requisição (QuotaGuard)."""

from __future__ import annotations

import io
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from buscai.catalog.errors import ProviderRateLimitError
from buscai.observability import configure_logging
from buscai.search.quota import QuotaGuard


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_request_budget_blocks_excess_calls_without_consuming_daily_quota() -> None:
    guard = QuotaGuard(daily_limit=10, default_max_search_per_request=2, default_max_get_item_per_request=1)
    budget = guard.new_request_budget()

    guard.consume("search", budget=budget, request_id="req-1")
    guard.consume("search", budget=budget, request_id="req-1")
    assert budget.remaining_search_calls == 0

    with pytest.raises(ProviderRateLimitError, match="search"):
        guard.consume("search", budget=budget, request_id="req-1")

    guard.consume("get_item", budget=budget, request_id="req-1")
    assert budget.remaining_get_item_calls == 0

    with pytest.raises(ProviderRateLimitError, match="get_item"):
        guard.consume("get_item", budget=budget, request_id="req-1")

    assert budget.total_calls_used == 3
    assert guard.snapshot().used_in_window == 3


def test_daily_quota_limit_and_sliding_24h_window_reset() -> None:
    clock = FakeClock(start=100.0)
    guard = QuotaGuard(daily_limit=3, window_seconds=86400.0, clock=clock)

    guard.consume("search")
    clock.advance(10.0)
    guard.consume("get_item")
    clock.advance(10.0)
    snap = guard.consume("get_item")
    assert snap.used_in_window == 3
    assert snap.remaining == 0

    with pytest.raises(ProviderRateLimitError, match="Limite diário") as exc_info:
        guard.consume("search")
    assert exc_info.value.retry_after_seconds is not None
    assert exc_info.value.retry_after_seconds > 86000.0

    # Avança além de 24h desde a primeira chamada; apenas a primeira expira.
    clock.advance(86381.0)
    after_reset = guard.consume("search")
    assert after_reset.used_in_window == 3
    assert after_reset.remaining == 0

    # Avança mais 24h; toda a janela esvazia.
    clock.advance(86400.0)
    assert guard.snapshot().used_in_window == 0
    assert guard.snapshot().remaining == 3


def test_default_quota_limit_is_5000() -> None:
    guard = QuotaGuard()
    snap = guard.snapshot()
    assert snap.daily_limit == 5000
    assert snap.remaining == 5000


def test_emits_single_sanitized_warning_when_crossing_threshold() -> None:
    stream = io.StringIO()
    logger = configure_logging("WARNING", stream=stream, logger_name="buscai.test.quota")
    clock = FakeClock()
    guard = QuotaGuard(daily_limit=5, warning_ratio=0.80, clock=clock, logger=logger)

    for idx in range(5):
        guard.consume("search", request_id=f"req-{idx}")

    lines = stream.getvalue().strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["level"] == "WARNING"
    assert payload["event"] == "ebay_quota_warning"
    assert payload["quota_remaining"] == 1
    assert payload["request_id"] == "req-3"

    # Após expirar a janela e recomeçar novo ciclo, pode alertar novamente.
    clock.advance(86401.0)
    for idx in range(4):
        guard.consume("search", request_id=f"req-next-{idx}")
    assert len(stream.getvalue().strip().splitlines()) == 2


def test_quota_guard_is_thread_safe_under_concurrent_consumption() -> None:
    guard = QuotaGuard(daily_limit=10, default_max_search_per_request=50)

    def try_consume(_: int) -> bool:
        try:
            guard.consume("search")
            return True
        except ProviderRateLimitError:
            return False

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(try_consume, range(25)))

    assert sum(outcomes) == 10
    assert outcomes.count(False) == 15
    assert guard.snapshot().used_in_window == 10


@pytest.mark.parametrize(
    "kwargs",
    [
        {"daily_limit": 0},
        {"daily_limit": True},
        {"window_seconds": 0},
        {"window_seconds": float("nan")},
        {"warning_ratio": 0},
        {"warning_ratio": 1.1},
        {"default_max_search_per_request": -1},
        {"default_max_get_item_per_request": True},
    ],
)
def test_quota_guard_rejects_invalid_configuration(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        QuotaGuard(**kwargs)  # type: ignore[arg-type]
