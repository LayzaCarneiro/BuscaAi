"""Rate limiter em memória por cliente autenticado (local ao processo)."""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitDecision:
    """Resultado de uma verificação de rate limit para um cliente."""

    allowed: bool
    limit: int
    remaining: int
    reset_after_seconds: int
    retry_after_seconds: int | None = None

    def to_headers(self) -> dict[str, str]:
        """Converte a decisão para cabeçalhos HTTP padronizados."""
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(self.reset_after_seconds),
        }
        if self.retry_after_seconds is not None:
            headers["Retry-After"] = str(self.retry_after_seconds)
        return headers


class InMemoryRateLimiter:
    """Limitador por janela deslizante em memória seguro para concorrência.

    Observação operacional (D5): os contadores residem exclusivamente na memória do
    processo atual; em execuções com múltiplos workers ou instâncias, cada processo
    mantém seu próprio contador independente.
    """

    def __init__(
        self,
        *,
        max_requests: int = 30,
        window_seconds: float = 60.0,
        max_clients: int = 10_000,
        cleanup_interval: int = 100,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 1:
            raise ValueError("max_requests deve ser um inteiro >= 1.")
        if (
            isinstance(window_seconds, bool)
            or not isinstance(window_seconds, (int, float))
            or not math.isfinite(window_seconds)
            or window_seconds <= 0
        ):
            raise ValueError("window_seconds deve ser numérico finito > 0.")
        if isinstance(max_clients, bool) or not isinstance(max_clients, int) or max_clients < 1:
            raise ValueError("max_clients deve ser um inteiro >= 1.")
        if isinstance(cleanup_interval, bool) or not isinstance(cleanup_interval, int) or cleanup_interval < 1:
            raise ValueError("cleanup_interval deve ser um inteiro >= 1.")

        self._max_requests = max_requests
        self._window_seconds = float(window_seconds)
        self._max_clients = max_clients
        self._cleanup_interval = cleanup_interval
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[str, deque[float]] = {}
        self._ops_since_cleanup = 0

    @property
    def max_requests(self) -> int:
        return self._max_requests

    @property
    def window_seconds(self) -> float:
        return self._window_seconds

    def tracked_clients_count(self) -> int:
        """Retorna a quantidade de clientes atualmente rastreados em memória."""
        with self._lock:
            return len(self._buckets)

    def cleanup_expired(self) -> int:
        """Remove entradas expiradas da memória e retorna quantos clientes foram removidos."""
        with self._lock:
            now = self._clock()
            return self._cleanup_locked(now)

    def check_and_consume(self, client_id: str) -> RateLimitDecision:
        """Verifica e consome 1 unidade de cota para o identificador opaco de cliente."""
        if not isinstance(client_id, str) or not client_id.strip():
            raise ValueError("client_id deve ser uma string não vazia.")

        with self._lock:
            now = self._clock()
            cutoff = now - self._window_seconds

            self._ops_since_cleanup += 1
            if self._ops_since_cleanup >= self._cleanup_interval:
                self._cleanup_locked(now)
                self._ops_since_cleanup = 0

            bucket = self._buckets.get(client_id)
            if bucket is None:
                if len(self._buckets) >= self._max_clients:
                    self._cleanup_locked(now)
                if len(self._buckets) >= self._max_clients:
                    oldest_key = min(
                        self._buckets,
                        key=lambda key: self._buckets[key][-1] if self._buckets[key] else -math.inf,
                    )
                    self._buckets.pop(oldest_key, None)
                bucket = deque()
                self._buckets[client_id] = bucket

            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= self._max_requests:
                oldest_ts = bucket[0]
                wait_seconds = max(1, math.ceil((oldest_ts + self._window_seconds) - now))
                return RateLimitDecision(
                    allowed=False,
                    limit=self._max_requests,
                    remaining=0,
                    reset_after_seconds=wait_seconds,
                    retry_after_seconds=wait_seconds,
                )

            bucket.append(now)
            remaining = max(0, self._max_requests - len(bucket))
            oldest_ts = bucket[0]
            reset_after = max(1, math.ceil((oldest_ts + self._window_seconds) - now))
            return RateLimitDecision(
                allowed=True,
                limit=self._max_requests,
                remaining=remaining,
                reset_after_seconds=reset_after,
                retry_after_seconds=None,
            )

    def _cleanup_locked(self, now: float) -> int:
        cutoff = now - self._window_seconds
        expired_keys: list[str] = []
        for key, bucket in self._buckets.items():
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if not bucket:
                expired_keys.append(key)
        for key in expired_keys:
            self._buckets.pop(key, None)
        return len(expired_keys)
