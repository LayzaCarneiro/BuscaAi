"""Controle de cota diária e orçamento por requisição para a eBay Browse API."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from buscai.catalog.errors import ProviderRateLimitError
from buscai.observability import log_operational_event

DEFAULT_EBAY_DAILY_QUOTA_LIMIT = 5000
DEFAULT_WINDOW_SECONDS = 86400.0


def _validate_non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} deve ser um inteiro não negativo.")
    return value


@dataclass
class RequestCallBudget:
    """Orçamento máximo de chamadas à eBay permitido dentro de uma única requisição."""

    max_search_calls: int = 2
    max_get_item_calls: int = 6
    search_calls_used: int = 0
    get_item_calls_used: int = 0

    def __post_init__(self) -> None:
        _validate_non_negative_int(self.max_search_calls, "max_search_calls")
        _validate_non_negative_int(self.max_get_item_calls, "max_get_item_calls")
        _validate_non_negative_int(self.search_calls_used, "search_calls_used")
        _validate_non_negative_int(self.get_item_calls_used, "get_item_calls_used")

    @property
    def total_calls_used(self) -> int:
        return self.search_calls_used + self.get_item_calls_used

    @property
    def remaining_search_calls(self) -> int:
        return max(0, self.max_search_calls - self.search_calls_used)

    @property
    def remaining_get_item_calls(self) -> int:
        return max(0, self.max_get_item_calls - self.get_item_calls_used)


@dataclass(frozen=True)
class QuotaSnapshot:
    """Visão instantânea do consumo de cota na janela deslizante de 24 horas."""

    daily_limit: int
    used_in_window: int
    remaining: int
    utilization_ratio: float
    warning_threshold_reached: bool


class QuotaGuard:
    """Guarda de consumo em memória para respeitar o limite padrão de 5.000 chamadas/dia.

    Opera exclusivamente em memória (sem persistir anúncios ou contadores em disco).
    Limitação conhecida: em execuções com múltiplas instâncias/workers independentes,
    cada processo controla apenas sua própria janela local.
    """

    def __init__(
        self,
        *,
        daily_limit: int = DEFAULT_EBAY_DAILY_QUOTA_LIMIT,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        warning_ratio: float = 0.80,
        default_max_search_per_request: int = 2,
        default_max_get_item_per_request: int = 6,
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        if isinstance(daily_limit, bool) or not isinstance(daily_limit, int) or daily_limit < 1:
            raise ValueError("daily_limit deve ser um inteiro positivo.")
        if isinstance(window_seconds, bool) or not isinstance(window_seconds, (int, float)):
            raise ValueError("window_seconds deve ser um número positivo.")
        if not math.isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError("window_seconds deve ser um número positivo finito.")
        if isinstance(warning_ratio, bool) or not isinstance(warning_ratio, (int, float)):
            raise ValueError("warning_ratio deve estar no intervalo (0, 1].")
        if not math.isfinite(warning_ratio) or not 0 < warning_ratio <= 1.0:
            raise ValueError("warning_ratio deve estar no intervalo (0, 1].")

        _validate_non_negative_int(default_max_search_per_request, "default_max_search_per_request")
        _validate_non_negative_int(default_max_get_item_per_request, "default_max_get_item_per_request")

        self._daily_limit = daily_limit
        self._window_seconds = float(window_seconds)
        self._warning_ratio = float(warning_ratio)
        self._default_max_search = default_max_search_per_request
        self._default_max_get_item = default_max_get_item_per_request
        self._clock = clock
        self._logger = logger
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()
        self._warned_in_cycle = False

    def new_request_budget(
        self,
        *,
        max_search_calls: int | None = None,
        max_get_item_calls: int | None = None,
    ) -> RequestCallBudget:
        """Cria um orçamento isolado de chamadas para uma requisição."""
        return RequestCallBudget(
            max_search_calls=(
                self._default_max_search if max_search_calls is None else max_search_calls
            ),
            max_get_item_calls=(
                self._default_max_get_item if max_get_item_calls is None else max_get_item_calls
            ),
        )

    def consume(
        self,
        operation: Literal["search", "get_item"],
        *,
        budget: RequestCallBudget | None = None,
        request_id: str | None = None,
    ) -> QuotaSnapshot:
        """Consome 1 unidade de cota se orçamento por requisição e limite diário permitirem."""
        if operation not in ("search", "get_item"):
            raise ValueError(f"Operação de cota desconhecida: {operation!r}.")

        with self._lock:
            if budget is not None:
                if operation == "search" and budget.search_calls_used >= budget.max_search_calls:
                    raise ProviderRateLimitError(
                        "Orçamento de chamadas 'search' por requisição atingido."
                    )
                if operation == "get_item" and budget.get_item_calls_used >= budget.max_get_item_calls:
                    raise ProviderRateLimitError(
                        "Orçamento de chamadas 'get_item' por requisição atingido."
                    )

            now = self._clock()
            self._prune_expired(now)

            if len(self._timestamps) >= self._daily_limit:
                oldest = self._timestamps[0]
                retry_after = max(0.0, self._window_seconds - (now - oldest))
                raise ProviderRateLimitError(
                    "Limite diário local da cota eBay atingido.",
                    retry_after_seconds=retry_after,
                )

            self._timestamps.append(now)
            if budget is not None:
                if operation == "search":
                    budget.search_calls_used += 1
                else:
                    budget.get_item_calls_used += 1

            snap = self._build_snapshot()
            self._handle_warning(snap, request_id=request_id)
            return snap

    def snapshot(self) -> QuotaSnapshot:
        """Retorna o estado atual da cota após descartar chamadas expiradas da janela."""
        with self._lock:
            self._prune_expired(self._clock())
            snap = self._build_snapshot()
            if not snap.warning_threshold_reached:
                self._warned_in_cycle = False
            return snap

    def _prune_expired(self, now: float) -> None:
        cutoff = now - self._window_seconds
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.popleft()

    def _build_snapshot(self) -> QuotaSnapshot:
        used = len(self._timestamps)
        remaining = max(0, self._daily_limit - used)
        ratio = used / self._daily_limit
        return QuotaSnapshot(
            daily_limit=self._daily_limit,
            used_in_window=used,
            remaining=remaining,
            utilization_ratio=ratio,
            warning_threshold_reached=ratio >= self._warning_ratio,
        )

    def _handle_warning(self, snap: QuotaSnapshot, *, request_id: str | None) -> None:
        if not snap.warning_threshold_reached:
            self._warned_in_cycle = False
            return
        if self._warned_in_cycle:
            return
        self._warned_in_cycle = True
        if self._logger is not None:
            log_operational_event(
                self._logger,
                logging.WARNING,
                "ebay_quota_warning",
                request_id=request_id,
                stage="quota",
                provider="ebay",
                quota_remaining=snap.remaining,
            )
