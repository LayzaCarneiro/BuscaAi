r"""Runner controlado e progressivo da Etapa 11 (Gemini real + E2E Gemini/eBay).

Limites globais estritos e compartilhados em toda a execução:
- Google Gemini API: teto máximo de 30 tentativas HTTP reais (incluindo retries), compartilhado entre Avaliação A (Gemini) e Avaliação B (E2E).
- eBay Browse API (EBAY_US): teto máximo de 8 tentativas Browse e 1 emissão OAuth.
- Captura progressiva com `flush=True` após cada cenário e preservação de resultados parciais via `--output`.
- Zero download de pesos de modelos, zero persistência de anúncios e zero exposição de segredos, títulos ou URLs.

Uso explícito (somente mediante autorização):
    $env:RUN_GEMINI_INTEGRATION_TESTS="1"
    $env:RUN_EBAY_INTEGRATION_TESTS="1"
    .\.venv\Scripts\python.exe -u scripts/run_stage11_live_validation.py --mode both --output .\.tmp_stage11_metrics.json
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import statistics
import sys
import threading
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, TextIO

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import httpx
from fastapi.testclient import TestClient

from buscai.api import create_app
from buscai.config import Settings, load_settings
from buscai.observability import configure_logging
from buscai.orchestration import OrchestrationDependencies, SearchOrchestrator
from buscai.pricing import CurrencyConverter, FixedEnvExchangeRateProvider
from buscai.ranking.pipeline import RankingPipeline
from buscai.search import EbayLiveBackend, EbayOAuthClient, QuotaGuard, SearchQuery
from buscai.search.query_builder import QueryBuilder
from buscai.semantic.embeddings import DeterministicStubEmbeddingProvider, EmbeddingService
from buscai.semantic.pipeline import SemanticSearchProcessor
from buscai.validation.pipeline import ValidationPipeline
from userintent import IntentExtractor
from userintent.evaluator import (
    compute_failed_fields,
    compute_informational_divergences,
    evaluate_case,
    normalize_set,
    normalize_text,
)
from userintent.llm import _extract_http_status, _extract_retry_after_seconds

MAX_ALLOWED_GEMINI_CALLS = 30
MAX_ALLOWED_EBAY_BROWSE_CALLS = 8
MAX_ALLOWED_EBAY_OAUTH_CALLS = 1
DEFAULT_SLEEP_BETWEEN_CALLS_SECONDS = 4.0
PLANNED_SINGLE_TURN_IDS: tuple[int, ...] = (1, 2, 3, 4, 6, 15, 21, 26, 29, 32)
PLANNED_MULTITURN_IDS: tuple[str, ...] = ("M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8")
ValidationMode = Literal["gemini", "e2e", "both", "ebay"]


class BudgetExhaustedError(RuntimeError):
    """Interrompe execução ao atingir o teto global de chamadas autorizado."""


def _classify_gemini_exception(
    exc: Exception,
) -> tuple[str, int | None, float | None, bool, str | None]:
    code = _extract_http_status(exc)
    retry_after = _extract_retry_after_seconds(exc)
    msg = str(exc).casefold()
    if code == 429 or any(marker in msg for marker in ("429", "rate limit", "resource_exhausted")):
        return "http_429_rate_limit", code or 429, retry_after, True, "http_429_rate_limit"
    if code == 503:
        return "http_503_unavailable", 503, retry_after, False, None
    if code in {500, 502, 504}:
        return f"http_{code}_server_error", code, retry_after, False, None
    if code in {400, 401, 403, 404}:
        return f"http_{code}_client_error", code, None, True, f"http_{code}_client_error"
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "timeout", code, None, False, None
    if isinstance(exc, (ConnectionError, httpx.NetworkError, httpx.RemoteProtocolError)):
        return "network_error", code, None, False, None
    if "503" in msg or "unavailable" in msg:
        return "http_503_unavailable", code, retry_after, False, None
    return f"exception_{type(exc).__name__}", code, retry_after, False, None


class _GlobalGeminiModelsProxy:
    """Proxy thread-safe compartilhado que contabiliza tentativas HTTP e retries no Gemini."""

    def __init__(self, wrapped_models: object, *, max_calls: int = MAX_ALLOWED_GEMINI_CALLS) -> None:
        if isinstance(max_calls, bool) or max_calls < 1 or max_calls > MAX_ALLOWED_GEMINI_CALLS:
            raise ValueError(
                f"max_gemini_calls deve estar entre 1 e {MAX_ALLOWED_GEMINI_CALLS} (recebido: {max_calls})."
            )
        self._wrapped = wrapped_models
        self.max_calls = max_calls
        self._lock = threading.Lock()
        self.calls_made = 0
        self.logical_requests = 0
        self.retries_counted = 0
        self.latencies_ms: list[float] = []
        self.prompt_tokens: list[int] = []
        self.output_tokens: list[int] = []
        self.total_tokens: list[int] = []
        self.errors: list[str] = []
        self.error_counts_by_category: dict[str, int] = {}
        self.fatal_stop = False
        self.stop_reason: str | None = None
        self.retry_after_seconds: float | None = None
        self.attempt_trail: list[dict[str, Any]] = []
        self._active_logical: dict[int, dict[str, Any]] = {}

    def begin_logical_request(
        self,
        *,
        scenario_id: str | None = None,
        operation: str = "extract",
    ) -> None:
        tid = threading.get_ident()
        with self._lock:
            self.logical_requests += 1
            self._active_logical[tid] = {
                "scenario_id": scenario_id,
                "operation": operation,
                "attempts": 0,
            }

    def end_logical_request(self) -> None:
        tid = threading.get_ident()
        with self._lock:
            self._active_logical.pop(tid, None)

    def record_backoff(
        self,
        attempt: int,
        delay_seconds: float,
        http_status: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        del attempt, http_status
        with self._lock:
            for item in reversed(self.attempt_trail):
                if item.get("outcome") == "error" and item.get("backoff_seconds") is None:
                    item["backoff_seconds"] = round(float(delay_seconds), 4)
                    if retry_after_seconds is not None and item.get("retry_after_seconds") is None:
                        item["retry_after_seconds"] = round(float(retry_after_seconds), 4)
                    break

    def generate_content(self, *args: object, **kwargs: object) -> object:
        tid = threading.get_ident()
        with self._lock:
            if self.fatal_stop or self.calls_made >= self.max_calls:
                if self.stop_reason is None:
                    self.stop_reason = "budget_exhausted"
                raise BudgetExhaustedError(f"Limite global Gemini ({self.max_calls}) atingido ou parada fatal.")
            self.calls_made += 1
            logical_state = self._active_logical.get(tid)
            if logical_state is None and len(self._active_logical) == 1:
                logical_state = next(iter(self._active_logical.values()))
            if logical_state is not None:
                logical_state["attempts"] = int(logical_state["attempts"]) + 1
                attempt_no = int(logical_state["attempts"])
                scenario_id = logical_state.get("scenario_id")
                operation = str(logical_state.get("operation", "extract"))
                is_retry = attempt_no > 1
                if is_retry:
                    self.retries_counted += 1
            else:
                attempt_no = 1
                scenario_id = None
                operation = "extract"
                is_retry = False

        started = time.monotonic()
        try:
            resp = self._wrapped.generate_content(*args, **kwargs)  # type: ignore[attr-defined]
        except Exception as exc:
            elapsed = round((time.monotonic() - started) * 1000.0, 2)
            error_category, code, retry_after, is_fatal, reason = _classify_gemini_exception(exc)
            with self._lock:
                self.latencies_ms.append(elapsed)
                err_name = type(exc).__name__
                self.errors.append(f"{err_name}:{code}")
                self.error_counts_by_category[error_category] = (
                    self.error_counts_by_category.get(error_category, 0) + 1
                )
                if retry_after is not None:
                    self.retry_after_seconds = round(float(retry_after), 4)
                if is_fatal:
                    self.fatal_stop = True
                    if self.stop_reason is None:
                        self.stop_reason = reason or error_category
                elif self.calls_made >= self.max_calls and self.stop_reason is None:
                    self.stop_reason = "budget_exhausted"
                self.attempt_trail.append(
                    {
                        "scenario_id": scenario_id,
                        "attempt": attempt_no,
                        "operation": operation,
                        "http_status": code,
                        "outcome": "error",
                        "latency_ms": elapsed,
                        "tokens": None,
                        "is_retry": is_retry,
                        "backoff_seconds": None,
                        "interrupted": bool(self.fatal_stop or self.calls_made >= self.max_calls),
                        "error_category": error_category,
                        "retry_after_seconds": round(float(retry_after), 4)
                        if retry_after is not None
                        else None,
                    }
                )
            raise

        elapsed = round((time.monotonic() - started) * 1000.0, 2)
        usage = getattr(resp, "usage_metadata", None)
        p_tok = getattr(usage, "prompt_token_count", None) if usage is not None else None
        c_tok = getattr(usage, "candidates_token_count", None) if usage is not None else None
        t_tok = getattr(usage, "total_token_count", None) if usage is not None else None
        with self._lock:
            self.latencies_ms.append(elapsed)
            if isinstance(p_tok, int):
                self.prompt_tokens.append(p_tok)
            if isinstance(c_tok, int):
                self.output_tokens.append(c_tok)
            if isinstance(t_tok, int):
                self.total_tokens.append(t_tok)
            if self.calls_made >= self.max_calls and self.stop_reason is None:
                self.stop_reason = "budget_exhausted"
            self.attempt_trail.append(
                {
                    "scenario_id": scenario_id,
                    "attempt": attempt_no,
                    "operation": operation,
                    "http_status": 200,
                    "outcome": "ok",
                    "latency_ms": elapsed,
                    "tokens": t_tok if isinstance(t_tok, int) else None,
                    "is_retry": is_retry,
                    "backoff_seconds": None,
                    "interrupted": False,
                    "error_category": None,
                    "retry_after_seconds": None,
                }
            )
        return resp

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "calls_made": self.calls_made,
                "max_allowed": self.max_calls,
                "logical_requests": self.logical_requests,
                "retries_counted": self.retries_counted,
                "errors": list(self.errors),
                "error_counts_by_category": dict(self.error_counts_by_category),
                "fatal_stop": self.fatal_stop,
                "interrupted": bool(self.fatal_stop or (self.stop_reason is not None)),
                "stop_reason": self.stop_reason,
                "retry_after_seconds": self.retry_after_seconds,
                "attempt_trail": [dict(entry) for entry in self.attempt_trail],
                "latency": _percentile_summary(list(self.latencies_ms)),
                "tokens_reported_calls": len(self.total_tokens),
                "prompt_tokens_sum": sum(self.prompt_tokens) if self.prompt_tokens else None,
                "output_tokens_sum": sum(self.output_tokens) if self.output_tokens else None,
                "total_tokens_sum": sum(self.total_tokens) if self.total_tokens else None,
            }


class _GlobalGeminiClientProxy:
    def __init__(self, raw_client: object, *, max_calls: int = MAX_ALLOWED_GEMINI_CALLS) -> None:
        self.models = _GlobalGeminiModelsProxy(raw_client.models, max_calls=max_calls)  # type: ignore[attr-defined]


class _GuardedEbayTransport(httpx.BaseTransport):
    """Transportador único que contabiliza TODAS as tentativas HTTP (OAuth/Browse) sem salvar payloads."""

    def __init__(
        self,
        *,
        max_calls: int,
        kind: str,
        inner_transport: httpx.BaseTransport | None = None,
    ) -> None:
        limit_cap = MAX_ALLOWED_EBAY_OAUTH_CALLS if kind == "oauth" else MAX_ALLOWED_EBAY_BROWSE_CALLS
        if isinstance(max_calls, bool) or max_calls < 1 or max_calls > limit_cap:
            raise ValueError(f"max_calls para eBay {kind} deve estar entre 1 e {limit_cap}.")
        # Nota técnica (httpx 0.28.1): HTTPTransport.__init__ não aceita `timeout`;
        # timeouts são configurados diretamente no httpx.Client pelos clientes OAuth/Browse.
        self._inner: httpx.BaseTransport = inner_transport if inner_transport is not None else httpx.HTTPTransport()
        self.max_calls = max_calls
        self.kind = kind
        self._lock = threading.Lock()
        self.calls_made = 0
        self.status_codes: list[int] = []
        self.latencies_ms: list[float] = []
        self.fatal_stop = False

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            if self.fatal_stop or self.calls_made >= self.max_calls:
                raise BudgetExhaustedError(f"Limite global eBay {self.kind} ({self.max_calls}) atingido.")
            self.calls_made += 1

        started = time.monotonic()
        resp = self._inner.handle_request(request)
        elapsed = round((time.monotonic() - started) * 1000.0, 2)
        with self._lock:
            self.status_codes.append(resp.status_code)
            self.latencies_ms.append(elapsed)
            if resp.status_code in {401, 403}:
                self.fatal_stop = True
        return resp

    def close(self) -> None:
        self._inner.close()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "calls_made": self.calls_made,
                "max_allowed": self.max_calls,
                "statuses": list(self.status_codes),
                "latency": _percentile_summary(list(self.latencies_ms)),
                "fatal_stop": self.fatal_stop,
            }


def build_ebay_clients(
    settings: Settings,
    *,
    oauth_transport: httpx.BaseTransport,
    browse_transport: httpx.BaseTransport,
    quota_guard: QuotaGuard,
    max_retries: int = 1,
    logger: Any = None,
) -> tuple[EbayOAuthClient, EbayLiveBackend]:
    """Constrói clientes OAuth e Browse preservando os timeouts nativos no httpx.Client."""
    oauth = EbayOAuthClient.from_settings(settings, transport=oauth_transport, logger=logger)
    backend = EbayLiveBackend(
        oauth_client=oauth,
        quota_guard=quota_guard,
        default_marketplace_id=settings.ebay_marketplace_id,
        max_retries=max_retries,
        transport=browse_transport,
        logger=logger,
    )
    return oauth, backend


class ProgressiveMetricsRecorder:
    """Emite progresso incremental com flush=True e persiste apenas métricas agregadas em --output."""

    def __init__(
        self,
        *,
        report: dict[str, Any],
        progress_stream: TextIO | None = None,
        output_path: Path | None = None,
        refresh_globals: Callable[[], None] | None = None,
    ) -> None:
        self.report = report
        self._stream = progress_stream if progress_stream is not None else sys.stderr
        self.output_path = output_path
        self._refresh_globals = refresh_globals
        self._lock = threading.Lock()
        self.events: list[dict[str, Any]] = []

    def set_refresh_globals(self, fn: Callable[[], None] | None) -> None:
        with self._lock:
            self._refresh_globals = fn

    def record_step(
        self,
        *,
        block: str,
        scenario_id: str,
        status: str,
        gemini_guard: _GlobalGeminiModelsProxy | None = None,
        browse_transport: _GuardedEbayTransport | None = None,
        oauth_transport: _GuardedEbayTransport | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        event: dict[str, Any] = {
            "block": block,
            "scenario_id": scenario_id,
            "status": status,
            "gemini_calls_made": gemini_guard.calls_made if gemini_guard is not None else 0,
            "gemini_retries": gemini_guard.retries_counted if gemini_guard is not None else 0,
            "gemini_total_tokens": sum(gemini_guard.total_tokens)
            if gemini_guard is not None and gemini_guard.total_tokens
            else None,
            "gemini_fatal_stop": gemini_guard.fatal_stop if gemini_guard is not None else False,
            "gemini_stop_reason": gemini_guard.stop_reason if gemini_guard is not None else None,
            "ebay_browse_calls_made": browse_transport.calls_made if browse_transport is not None else 0,
            "ebay_oauth_calls_made": oauth_transport.calls_made if oauth_transport is not None else 0,
        }
        if extra:
            event.update(extra)

        with self._lock:
            if self._refresh_globals is not None:
                self._refresh_globals()
            self.events.append(event)
            self._stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._stream.flush()
            self._write_output_unlocked()

    def flush_final(self) -> None:
        with self._lock:
            if self._refresh_globals is not None:
                self._refresh_globals()
            self._write_output_unlocked()

    def _write_output_unlocked(self) -> None:
        if self.output_path is None:
            return
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.output_path.with_name(f"{self.output_path.name}.tmp")
        tmp_path.write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self.output_path)


def _percentile_summary(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean_ms": None, "p50_ms": None, "p95_ms": None, "min_ms": None, "max_ms": None}
    ordered = sorted(values)
    p95_idx = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
    return {
        "mean_ms": round(statistics.mean(ordered), 2),
        "p50_ms": round(statistics.median(ordered), 2),
        "p95_ms": round(ordered[p95_idx], 2),
        "min_ms": round(ordered[0], 2),
        "max_ms": round(ordered[-1], 2),
    }


def _summarize_status_counts(rows: list[dict[str, Any]], planned_count: int) -> dict[str, Any]:
    passed_count = sum(1 for row in rows if row.get("status") == "passed")
    failed_count = sum(1 for row in rows if row.get("status") == "failed")
    error_count = sum(1 for row in rows if row.get("status") == "error")
    not_run_count = sum(1 for row in rows if row.get("status") == "not_run")
    completed_count = passed_count + failed_count
    pass_rate_over_planned = (
        round(passed_count / planned_count, 4) if planned_count > 0 else None
    )
    pass_rate_over_completed = (
        round(passed_count / completed_count, 4) if completed_count > 0 else None
    )
    return {
        "planned_count": planned_count,
        "passed_count": passed_count,
        "failed_count": failed_count,
        "error_count": error_count,
        "not_run_count": not_run_count,
        "completed_count": completed_count,
        "pass_rate_over_planned": pass_rate_over_planned,
        "pass_rate_over_completed": pass_rate_over_completed,
    }


def _apply_pacing(
    *,
    pacing_seconds: float,
    pacing_sleep: Callable[[float], None],
    pacing_state: dict[str, Any],
) -> None:
    if pacing_seconds <= 0.0:
        return
    pacing_sleep(pacing_seconds)
    pacing_state["pauses_applied"] = int(pacing_state.get("pauses_applied", 0)) + 1
    pacing_state["total_pacing_sleep_seconds"] = round(
        float(pacing_state.get("total_pacing_sleep_seconds", 0.0)) + pacing_seconds,
        4,
    )


def _multiturn_specs() -> list[dict[str, Any]]:
    def check_m1(u: Any) -> list[dict[str, str]]:
        failures: list[dict[str, str]] = []
        if normalize_text(u.category or "") != "notebook":
            failures.append({"field": "category", "category": "category_mismatch"})
        if u.price.max_price != 4000 or u.price.is_hard_constraint is not True or u.price.currency != "BRL":
            failures.append({"field": "price_constraint", "category": "price_or_currency_mismatch"})
        if "estudos" not in normalize_set(u.use_cases):
            failures.append({"field": "use_cases", "category": "missing_expected_items"})
        if u.clarification_needed is not False:
            failures.append({"field": "clarification_required", "category": "clarification_flag_mismatch"})
        return failures

    def check_m2(u: Any) -> list[dict[str, str]]:
        failures: list[dict[str, str]] = []
        if normalize_text(u.category or "") != "notebook":
            failures.append({"field": "category", "category": "category_mismatch"})
        if "lenovo" not in normalize_set(u.brands):
            failures.append({"field": "brands", "category": "missing_expected_items"})
        if not any("16" in f and "ram" in f for f in normalize_set(u.required_features)):
            failures.append({"field": "required_features", "category": "missing_expected_items"})
        if u.price.max_price != 4000 or u.price.is_hard_constraint is not True:
            failures.append({"field": "price_constraint", "category": "hard_constraint_lost"})
        if u.clarification_needed is not False:
            failures.append({"field": "clarification_required", "category": "clarification_flag_mismatch"})
        return failures

    def check_m3(u: Any) -> list[dict[str, str]]:
        failures: list[dict[str, str]] = []
        if normalize_text(u.category or "") != "notebook":
            failures.append({"field": "category", "category": "category_mismatch"})
        if u.price.max_price != 4000:
            failures.append({"field": "price_constraint", "category": "price_bounds_mismatch"})
        all_feat = " ".join(u.required_features + u.preferred_features + u.excluded_features).casefold()
        if "touchscreen" in all_feat:
            failures.append({"field": "feature_removal", "category": "negated_requirement_retained"})
        return failures

    def check_m4(u: Any) -> list[dict[str, str]]:
        failures: list[dict[str, str]] = []
        if normalize_text(u.category or "") != "notebook":
            failures.append({"field": "category", "category": "category_mismatch"})
        if not {"lenovo", "acer"}.issubset(normalize_set(u.brands)):
            failures.append({"field": "brands", "category": "missing_expected_items"})
        if u.price.max_price != 4000:
            failures.append({"field": "price_constraint", "category": "price_bounds_mismatch"})
        return failures

    def check_m5(u: Any) -> list[dict[str, str]]:
        failures: list[dict[str, str]] = []
        if normalize_text(u.category or "") != "notebook":
            failures.append({"field": "category", "category": "category_mismatch"})
        if u.price.max_price != 4000:
            failures.append({"field": "price_constraint", "category": "price_bounds_mismatch"})
        if u.clarification_needed is not False:
            failures.append({"field": "clarification_required", "category": "clarification_flag_mismatch"})
        return failures

    def check_m6(u: Any) -> list[dict[str, str]]:
        failures: list[dict[str, str]] = []
        if normalize_text(u.category or "") != "smartphone":
            failures.append({"field": "category", "category": "category_mismatch"})
        if not any("256" in f for f in normalize_set(u.required_features)):
            failures.append({"field": "required_features", "category": "missing_expected_items"})
        if any("256" in f for f in normalize_set(u.excluded_features)):
            failures.append({"field": "excluded_features", "category": "min_spec_inverted_to_exclusion"})
        return failures

    def check_m7(u: Any) -> list[dict[str, str]]:
        failures: list[dict[str, str]] = []
        if normalize_text(u.category or "") != "fone de ouvido":
            failures.append({"field": "category", "category": "category_mismatch"})
        if "sony" not in normalize_set(u.brands):
            failures.append({"field": "brands", "category": "missing_expected_items"})
        if not any("wh-1000xm5" in normalize_text(m) for m in u.models):
            failures.append({"field": "models", "category": "missing_expected_items"})
        if normalize_text(u.condition or "") not in {"novo", "new"}:
            failures.append({"field": "condition", "category": "condition_mismatch"})
        if u.price.max_price != 350 or u.price.currency != "USD" or u.price.is_hard_constraint is not True:
            failures.append({"field": "price_constraint", "category": "currency_or_price_mismatch"})
        return failures

    def check_m8(u: Any) -> list[dict[str, str]]:
        failures: list[dict[str, str]] = []
        if normalize_text(u.category or "") != "monitor":
            failures.append({"field": "category", "category": "category_mismatch"})
        if not any("4k" in normalize_text(f) for f in u.required_features + u.preferred_features):
            failures.append({"field": "required_features", "category": "missing_expected_items"})
        if u.price.max_price != 2500 or u.price.currency != "BRL":
            failures.append({"field": "price_constraint", "category": "price_or_currency_mismatch"})
        if u.clarification_needed is not False:
            failures.append({"field": "clarification_required", "category": "clarification_flag_mismatch"})
        return failures

    return [
        {
            "id": "M1",
            "input": "Quero um notebook para estudar até R$ 4.000.",
            "history": None,
            "check_fields": check_m1,
            "kind": "hard_budget_baseline",
        },
        {
            "id": "M2",
            "input": "Prefiro Lenovo e pelo menos 16 GB de RAM.",
            "history": [
                {"role": "user", "content": "Quero um notebook para estudar até R$ 4.000."},
                {"role": "assistant", "content": "Busca concluída na categoria notebook até R$ 4000."},
            ],
            "check_fields": check_m2,
            "kind": "multiturn_hard_preserve",
        },
        {
            "id": "M3",
            "input": "Na verdade, não precisa ter touchscreen.",
            "history": [
                {"role": "user", "content": "Quero um notebook com touchscreen até R$ 4.000."},
                {"role": "assistant", "content": "Busca realizada na categoria notebook até R$ 4000."},
            ],
            "check_fields": check_m3,
            "kind": "explicit_removal_negation",
        },
        {
            "id": "M4",
            "input": "Pode ser Acer também.",
            "history": [
                {"role": "user", "content": "Quero um notebook Lenovo até R$ 4.000."},
                {"role": "assistant", "content": "Busca realizada na categoria notebook marca Lenovo até R$ 4000."},
            ],
            "check_fields": check_m4,
            "kind": "multiturn_brand_addition",
        },
        {
            "id": "M5",
            "input": "Quero trocar para o perfil de melhor desempenho.",
            "history": [
                {"role": "user", "content": "Quero um notebook para trabalho até R$ 4.000."},
                {
                    "role": "assistant",
                    "content": "Busca realizada na categoria notebook com perfil best_value até R$ 4000.",
                },
            ],
            "check_fields": check_m5,
            "kind": "multiturn_profile_switch",
        },
        {
            "id": "M6",
            "input": "Não quero um celular com menos de 256 GB.",
            "history": None,
            "check_fields": check_m6,
            "kind": "negation_min_spec",
        },
        {
            "id": "M7",
            "input": "Quero um fone Sony WH-1000XM5 novo até US$ 350.",
            "history": None,
            "check_fields": check_m7,
            "kind": "currency_usd_condition_model",
        },
        {
            "id": "M8",
            "input": "É um monitor 4K até R$ 2.500.",
            "history": [
                {"role": "user", "content": "Quero um presente."},
                {"role": "assistant", "content": "Qual produto você procura e qual o orçamento?"},
            ],
            "check_fields": check_m8,
            "kind": "clarification_resolution",
        },
    ]


def _refresh_gemini_block_summary(
    *,
    block_report: dict[str, Any],
    model_name: str,
    calls_used_in_block: int,
    single_results: list[dict[str, Any]],
    multi_results: list[dict[str, Any]],
    abs_budget_errors: list[float],
    hard_preserved_flags: list[bool],
    currency_matches: list[bool],
    guard: _GlobalGeminiModelsProxy,
) -> None:
    single_summary = _summarize_status_counts(single_results, len(PLANNED_SINGLE_TURN_IDS))
    multi_summary = _summarize_status_counts(multi_results, len(PLANNED_MULTITURN_IDS))
    overall_summary = _summarize_status_counts(
        single_results + multi_results,
        len(PLANNED_SINGLE_TURN_IDS) + len(PLANNED_MULTITURN_IDS),
    )

    completed_single = [r for r in single_results if r.get("status") in {"passed", "failed"}]
    completed_multi = [r for r in multi_results if r.get("status") in {"passed", "failed"}]
    interrupted = bool(guard.fatal_stop or (overall_summary["not_run_count"] > 0 and guard.calls_made >= guard.max_calls))
    interruption_reason = guard.stop_reason if interrupted else None

    if interruption_reason is not None:
        for row in single_results + multi_results:
            if row.get("status") == "not_run" and row.get("not_run_reason") is None:
                row["not_run_reason"] = interruption_reason

    negation_rows = [r for r in completed_multi if r.get("id") in {"M3", "M6"}]
    m7_row = next((r for r in multi_results if r.get("id") == "M7"), None)
    m8_row = next((r for r in multi_results if r.get("id") == "M8"), None)

    block_report.update(
        {
            "model": model_name,
            "calls_used_in_block": calls_used_in_block,
            "interrupted": interrupted,
            "interruption_reason": interruption_reason,
            "single_turn_total": len(PLANNED_SINGLE_TURN_IDS),
            "single_turn_completed": single_summary["completed_count"],
            "single_turn_summary": single_summary,
            "single_turn_core_pass_rate": single_summary["pass_rate_over_completed"],
            "single_turn_pass_rate_over_planned": single_summary["pass_rate_over_planned"],
            "single_turn_category_accuracy": round(
                sum(1 for r in completed_single if r["category_correct"]) / len(completed_single), 4
            )
            if completed_single
            else None,
            "single_turn_price_accuracy": round(
                sum(1 for r in completed_single if r["price_correct"]) / len(completed_single), 4
            )
            if completed_single
            else None,
            "single_turn_brands_f1": round(statistics.mean(float(r["brands_f1"]) for r in completed_single), 4)
            if completed_single
            else None,
            "single_turn_required_features_f1": round(
                statistics.mean(float(r["required_features_f1"]) for r in completed_single), 4
            )
            if completed_single
            else None,
            "single_turn_excluded_features_f1": round(
                statistics.mean(float(r["excluded_features_f1"]) for r in completed_single), 4
            )
            if completed_single
            else None,
            "single_turn_clarification_accuracy": round(
                sum(1 for r in completed_single if r["clarification_correct"]) / len(completed_single), 4
            )
            if completed_single
            else None,
            "single_turn_mean_budget_abs_error_brl": round(statistics.mean(abs_budget_errors), 2)
            if abs_budget_errors
            else None,
            "single_turn_currency_accuracy": round(sum(currency_matches) / len(currency_matches), 4)
            if currency_matches
            else None,
            "single_turn_hard_constraint_preservation_rate": round(
                sum(hard_preserved_flags) / len(hard_preserved_flags), 4
            )
            if hard_preserved_flags
            else None,
            "single_turn_results": single_results,
            "multiturn_total": len(PLANNED_MULTITURN_IDS),
            "multiturn_completed": multi_summary["completed_count"],
            "multiturn_summary": multi_summary,
            "multiturn_pass_rate": multi_summary["pass_rate_over_completed"],
            "multiturn_pass_rate_over_planned": multi_summary["pass_rate_over_planned"],
            "multiturn_results": multi_results,
            "overall_summary": overall_summary,
            "negation_accuracy": round(
                sum(1 for r in negation_rows if r.get("status") == "passed") / len(negation_rows),
                4,
            )
            if negation_rows
            else None,
            "f1_condition_m7": m7_row.get("passed") if m7_row and m7_row.get("status") in {"passed", "failed"} else None,
            "f1_clarification_m8": m8_row.get("passed") if m8_row and m8_row.get("status") in {"passed", "failed"} else None,
        }
    )


def _run_gemini_eval(
    extractor: IntentExtractor,
    guard: _GlobalGeminiModelsProxy,
    recorder: ProgressiveMetricsRecorder,
    block_report: dict[str, Any],
    *,
    pacing_seconds: float = 0.0,
    pacing_sleep: Callable[[float], None] = time.sleep,
    pacing_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dataset_path = ROOT / "data" / "intent_eval_50.jsonl"
    raw_cases = {
        int(row["case_id"]): row
        for row in (json.loads(line) for line in dataset_path.read_text(encoding="utf-8").splitlines() if line.strip())
    }
    selected_ids = list(PLANNED_SINGLE_TURN_IDS)
    multiturn_specs = _multiturn_specs()
    single_results: list[dict[str, Any]] = [
        {
            "case_id": cid,
            "scenario_id": f"single_{cid}",
            "status": "not_run",
            "core_pass": None,
            "failed_fields": [],
            "informational_divergences": [],
            "not_run_reason": None,
        }
        for cid in selected_ids
    ]
    multi_results: list[dict[str, Any]] = [
        {
            "id": str(spec["id"]),
            "scenario_id": str(spec["id"]),
            "kind": str(spec["kind"]),
            "status": "not_run",
            "passed": None,
            "failed_fields": [],
            "not_run_reason": None,
        }
        for spec in multiturn_specs
    ]
    abs_budget_errors: list[float] = []
    hard_preserved_flags: list[bool] = []
    currency_matches: list[bool] = []
    effective_pacing_state = pacing_state if pacing_state is not None else {}

    calls_before = guard.calls_made
    scenario_counter = 0

    for idx, cid in enumerate(selected_ids):
        if guard.fatal_stop or guard.calls_made >= guard.max_calls:
            break
        if scenario_counter > 0:
            _apply_pacing(
                pacing_seconds=pacing_seconds,
                pacing_sleep=pacing_sleep,
                pacing_state=effective_pacing_state,
            )
        scenario_counter += 1
        scenario_id = f"single_{cid}"
        case = raw_cases[cid]
        started = time.monotonic()
        guard.begin_logical_request(scenario_id=scenario_id, operation="single_turn_extract")
        try:
            pred = extractor.extract(str(case["input"]), request_id=f"s11-gem-{cid}")
            expected = dict(case["expected"])
            ev = evaluate_case(pred, expected, cid)
            exp_price = expected.get("price", {})
            exp_max = exp_price.get("max_price")
            if exp_max is not None and pred.price.max_price is not None:
                abs_budget_errors.append(abs(float(pred.price.max_price) - float(exp_max)))
            if exp_max is not None:
                currency_matches.append(pred.price.currency == "BRL")
            if exp_price.get("is_hard_constraint"):
                hard_preserved_flags.append(bool(pred.price.is_hard_constraint))
            failed_fields = compute_failed_fields(ev, pred, expected)
            informational_divergences = compute_informational_divergences(ev, pred, expected)
            status = "passed" if ev.core_pass else "failed"
            single_results[idx] = {
                **ev.to_dict(),
                "scenario_id": scenario_id,
                "status": status,
                "failed_fields": failed_fields,
                "informational_divergences": informational_divergences,
                "not_run_reason": None,
            }
        except Exception as exc:
            root_exc = exc.__cause__ if isinstance(exc.__cause__, Exception) else exc
            err_cat, http_status, _, _, _ = _classify_gemini_exception(root_exc)
            single_results[idx] = {
                "case_id": cid,
                "scenario_id": scenario_id,
                "status": "error",
                "core_pass": None,
                "error": type(exc).__name__,
                "error_type": type(exc).__name__,
                "error_category": err_cat,
                "http_status": http_status,
                "failed_fields": [],
                "informational_divergences": [],
                "not_run_reason": None,
            }
            status = "error"
        finally:
            guard.end_logical_request()

        elapsed_ms = round((time.monotonic() - started) * 1000.0, 2)
        _refresh_gemini_block_summary(
            block_report=block_report,
            model_name=extractor.model,
            calls_used_in_block=guard.calls_made - calls_before,
            single_results=single_results,
            multi_results=multi_results,
            abs_budget_errors=abs_budget_errors,
            hard_preserved_flags=hard_preserved_flags,
            currency_matches=currency_matches,
            guard=guard,
        )
        recorder.record_step(
            block="t11_3_gemini_eval",
            scenario_id=scenario_id,
            status=status,
            gemini_guard=guard,
            extra={"latency_ms": elapsed_ms},
        )
        if guard.fatal_stop:
            break

    for idx, spec in enumerate(multiturn_specs):
        if guard.fatal_stop or guard.calls_made >= guard.max_calls:
            break
        if scenario_counter > 0:
            _apply_pacing(
                pacing_seconds=pacing_seconds,
                pacing_sleep=pacing_sleep,
                pacing_state=effective_pacing_state,
            )
        scenario_counter += 1
        scenario_id = str(spec["id"])
        started = time.monotonic()
        guard.begin_logical_request(scenario_id=scenario_id, operation="multiturn_extract")
        try:
            pred = extractor.extract(
                str(spec["input"]),
                conversation_history=spec["history"],
                request_id=f"s11-gem-{spec['id']}",
            )
            failed_fields = list(spec["check_fields"](pred))
            passed = len(failed_fields) == 0
            status = "passed" if passed else "failed"
            multi_results[idx] = {
                "id": scenario_id,
                "scenario_id": scenario_id,
                "kind": str(spec["kind"]),
                "status": status,
                "passed": passed,
                "failed_fields": failed_fields,
                "not_run_reason": None,
            }
        except Exception as exc:
            root_exc = exc.__cause__ if isinstance(exc.__cause__, Exception) else exc
            err_cat, http_status, _, _, _ = _classify_gemini_exception(root_exc)
            multi_results[idx] = {
                "id": scenario_id,
                "scenario_id": scenario_id,
                "kind": str(spec["kind"]),
                "status": "error",
                "passed": None,
                "error": type(exc).__name__,
                "error_type": type(exc).__name__,
                "error_category": err_cat,
                "http_status": http_status,
                "failed_fields": [],
                "not_run_reason": None,
            }
            status = "error"
        finally:
            guard.end_logical_request()

        elapsed_ms = round((time.monotonic() - started) * 1000.0, 2)
        _refresh_gemini_block_summary(
            block_report=block_report,
            model_name=extractor.model,
            calls_used_in_block=guard.calls_made - calls_before,
            single_results=single_results,
            multi_results=multi_results,
            abs_budget_errors=abs_budget_errors,
            hard_preserved_flags=hard_preserved_flags,
            currency_matches=currency_matches,
            guard=guard,
        )
        recorder.record_step(
            block="t11_3_gemini_eval",
            scenario_id=scenario_id,
            status=status,
            gemini_guard=guard,
            extra={"latency_ms": elapsed_ms},
        )
        if guard.fatal_stop:
            break

    _refresh_gemini_block_summary(
        block_report=block_report,
        model_name=extractor.model,
        calls_used_in_block=guard.calls_made - calls_before,
        single_results=single_results,
        multi_results=multi_results,
        abs_budget_errors=abs_budget_errors,
        hard_preserved_flags=hard_preserved_flags,
        currency_matches=currency_matches,
        guard=guard,
    )
    return block_report


def _run_ebay_controlled(
    *,
    oauth: EbayOAuthClient,
    backend: EbayLiveBackend,
    guard: QuotaGuard,
    marketplace_id: str,
    log_stream: io.StringIO,
    recorder: ProgressiveMetricsRecorder,
    browse_transport: _GuardedEbayTransport,
    oauth_transport: _GuardedEbayTransport,
) -> dict[str, Any]:
    budget = guard.new_request_budget(max_search_calls=4, max_get_item_calls=1)
    token_first = oauth.get_access_token(request_id="s11-ebay-1")
    token_second = oauth.get_access_token(request_id="s11-ebay-1")
    oauth_reused = token_first == token_second and len(token_first) > 10

    category_specs = [
        ("notebook", "laptop", Decimal("1000.00")),
        ("smartphone", "smartphone 128GB", Decimal("900.00")),
        ("monitor", "monitor 27 inch", Decimal("800.00")),
        ("fone de ouvido", "bluetooth headphones", Decimal("450.00")),
    ]

    categories_summary: list[dict[str, Any]] = []
    checked_titles: list[str] = []
    checked_urls: list[str] = []
    first_notebook_item_id: str | None = None

    for cat_name, q_text, max_usd in category_specs:
        started = time.monotonic()
        try:
            search_resp = backend.search(
                SearchQuery(
                    query_text=q_text,
                    marketplace_id=marketplace_id,
                    max_price_usd=max_usd,
                    limit=3,
                ),
                budget=budget,
                request_id=f"s11-ebay-{cat_name}",
            )
            elapsed_ms = round((time.monotonic() - started) * 1000.0, 2)
            currencies: set[str] = set()
            with_condition = 0
            with_seller = 0
            with_shipping = 0
            with_price = 0
            for item in search_resp.candidates:
                checked_titles.append(item.title)
                if item.item_web_url:
                    checked_urls.append(item.item_web_url)
                if first_notebook_item_id is None and cat_name == "notebook":
                    first_notebook_item_id = item.item_id
                if isinstance(item.price, Decimal) and item.price >= 0:
                    with_price += 1
                currencies.add(item.currency)
                if item.condition:
                    with_condition += 1
                if item.seller is not None:
                    with_seller += 1
                if item.shipping is not None:
                    with_shipping += 1

            categories_summary.append(
                {
                    "category": cat_name,
                    "status": "ok",
                    "latency_ms": elapsed_ms,
                    "candidates_count": len(search_resp.candidates),
                    "total_estimated": search_resp.total_estimated,
                    "currencies_seen": sorted(currencies),
                    "with_valid_price": with_price,
                    "with_condition": with_condition,
                    "with_seller": with_seller,
                    "with_shipping": with_shipping,
                }
            )
            recorder.record_step(
                block="t11_2_ebay_live",
                scenario_id=cat_name,
                status="ok",
                browse_transport=browse_transport,
                oauth_transport=oauth_transport,
                extra={"latency_ms": elapsed_ms},
            )
        except Exception as exc:
            elapsed_ms = round((time.monotonic() - started) * 1000.0, 2)
            categories_summary.append(
                {
                    "category": cat_name,
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "latency_ms": elapsed_ms,
                }
            )
            recorder.record_step(
                block="t11_2_ebay_live",
                scenario_id=cat_name,
                status="error",
                browse_transport=browse_transport,
                oauth_transport=oauth_transport,
                extra={"latency_ms": elapsed_ms},
            )
            break

    detail_ok = False
    detail_loaded = False
    detail_error: str | None = None
    if first_notebook_item_id is not None and budget.remaining_get_item_calls > 0:
        try:
            detail = backend.get_item_details(
                first_notebook_item_id,
                budget=budget,
                request_id="s11-ebay-getitem",
            )
            detail_ok = detail is not None
            detail_loaded = bool(detail.details_loaded) if detail is not None else False
            if detail is not None:
                checked_titles.append(detail.title)
                if detail.item_web_url:
                    checked_urls.append(detail.item_web_url)
        except Exception as exc:
            detail_error = type(exc).__name__

    logs_raw = log_stream.getvalue()
    leak_detected = (
        token_first in logs_raw
        or any(title in logs_raw for title in checked_titles if title)
        or any(url in logs_raw for url in checked_urls if url)
    )

    return {
        "oauth_token_reused_in_memory": oauth_reused,
        "categories": categories_summary,
        "get_item_called": first_notebook_item_id is not None,
        "get_item_parsed": detail_ok,
        "get_item_details_loaded": detail_loaded,
        "get_item_error": detail_error,
        "browse_calls_used_in_block": budget.total_calls_used,
        "log_leak_detected": leak_detected,
    }


def _run_e2e_integrated(
    *,
    settings: Settings,
    extractor: IntentExtractor,
    backend: EbayLiveBackend,
    quota_guard: QuotaGuard,
    gemini_guard: _GlobalGeminiModelsProxy,
    browse_transport: _GuardedEbayTransport,
    oauth_transport: _GuardedEbayTransport,
    log_stream: io.StringIO,
    logger: Any,
    recorder: ProgressiveMetricsRecorder,
    block_report: dict[str, Any],
    pacing_seconds: float = 0.0,
    pacing_sleep: Callable[[float], None] = time.sleep,
    pacing_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    api_key = "s11_ephemeral_e2e_key_001"
    api_settings = settings.model_copy(
        update={
            "api_keys": load_settings(environ={"BUSCAAI_API_KEYS": api_key}).api_keys,
            "rate_limit_per_minute": 30,
        }
    )
    converter = CurrencyConverter(FixedEnvExchangeRateProvider.from_settings(api_settings))
    emb = EmbeddingService(DeterministicStubEmbeddingProvider(), logger=logger)
    emb.embed_queries(["warmup"])

    deps = OrchestrationDependencies(
        extractor=extractor,
        query_builder=QueryBuilder(currency_converter=converter),
        search_backend=backend,
        semantic_processor=SemanticSearchProcessor(emb, logger=logger),
        validation_pipeline=ValidationPipeline(
            currency_converter=converter,
            search_backend=backend,
            max_enrich_items=0,
            logger=logger,
        ),
        ranking_pipeline=RankingPipeline(converter=converter),
        quota_guard=quota_guard,
        max_search_calls=1,
        max_get_item_calls=0,
        logger=logger,
    )
    orch = SearchOrchestrator(deps)
    app = create_app(
        settings=api_settings,
        orchestrator=orch,
        embedding_service=emb,
        preload_embeddings=False,
        logger=logger,
    )

    scenarios = [
        ("notebook", "/v1/search", {"query": "notebook Lenovo 16gb ram", "budget_brl": 6000, "limit": 3}),
        ("smartphone", "/v1/search", {"query": "smartphone 256gb 5g", "budget_brl": 5500, "limit": 3}),
        ("monitor", "/v1/search", {"query": "monitor 27 polegadas 4k", "budget_brl": 4500, "limit": 3}),
        (
            "fone de ouvido",
            "/v1/search",
            {"query": "fone bluetooth cancelamento de ruido", "budget_brl": 3000, "limit": 3},
        ),
        ("clarification_chat", "/v1/chat", {"message": "Quero comprar um presente legal mas não decidi o quê."}),
    ]

    rows: list[dict[str, Any]] = []
    block_report["scenarios"] = rows
    block_report["embeddings_note"] = (
        "DeterministicStubEmbeddingProvider usado por bloqueio do modelo real multilingual-e5-small."
    )
    seen_titles: list[str] = []
    seen_urls: list[str] = []
    effective_pacing_state = pacing_state if pacing_state is not None else {}
    executed_count = 0

    with TestClient(app) as client:
        for name, path, body in scenarios:
            if gemini_guard.fatal_stop or gemini_guard.calls_made >= gemini_guard.max_calls:
                rows.append({"scenario": name, "skipped_reason": "gemini_budget_or_fatal_stop"})
                recorder.record_step(
                    block="t11_4_e2e",
                    scenario_id=name,
                    status="skipped_budget",
                    gemini_guard=gemini_guard,
                    browse_transport=browse_transport,
                    oauth_transport=oauth_transport,
                )
                continue
            if path == "/v1/search" and (
                browse_transport.fatal_stop or browse_transport.calls_made >= browse_transport.max_calls
            ):
                rows.append({"scenario": name, "skipped_reason": "ebay_budget_or_fatal_stop"})
                recorder.record_step(
                    block="t11_4_e2e",
                    scenario_id=name,
                    status="skipped_budget",
                    gemini_guard=gemini_guard,
                    browse_transport=browse_transport,
                    oauth_transport=oauth_transport,
                )
                continue

            if executed_count > 0:
                _apply_pacing(
                    pacing_seconds=pacing_seconds,
                    pacing_sleep=pacing_sleep,
                    pacing_state=effective_pacing_state,
                )
            executed_count += 1

            started = time.monotonic()
            guard_started_calls = gemini_guard.calls_made
            gemini_guard.begin_logical_request(scenario_id=f"e2e_{name}", operation=path)
            try:
                resp = client.post(path, headers={"X-API-Key": api_key}, json=body)
                elapsed_ms = round((time.monotonic() - started) * 1000.0, 2)
                payload = resp.json()
                entry: dict[str, Any] = {
                    "scenario": name,
                    "path": path,
                    "http_status": resp.status_code,
                    "latency_ms": elapsed_ms,
                    "gemini_calls_delta": gemini_guard.calls_made - guard_started_calls,
                    "status": payload.get("status") or payload.get("error", {}).get("code"),
                }
                if path == "/v1/search" and resp.status_code == 200:
                    entry["detected_category"] = payload.get("detected_category")
                    entry["total_products"] = payload.get("total_products")
                    entry["total_offers"] = payload.get("total_offers")
                    entry["rejected_count"] = payload.get("rejected_count")
                    entry["relaxed"] = payload.get("relaxed")
                    for prod in payload.get("products", []):
                        for offer in prod.get("offers", []):
                            if offer.get("title"):
                                seen_titles.append(str(offer["title"]))
                            if offer.get("item_web_url"):
                                seen_urls.append(str(offer["item_web_url"]))
                elif path == "/v1/chat" and resp.status_code == 200:
                    entry["clarification_needed"] = payload.get("clarification_needed")
                    entry["search_results_is_none"] = payload.get("search_results") is None
            finally:
                gemini_guard.end_logical_request()

            rows.append(entry)
            recorder.record_step(
                block="t11_4_e2e",
                scenario_id=name,
                status=str(entry["status"]),
                gemini_guard=gemini_guard,
                browse_transport=browse_transport,
                oauth_transport=oauth_transport,
                extra={"latency_ms": elapsed_ms, "http_status": entry["http_status"]},
            )

    logs_raw = log_stream.getvalue()
    leak_detected = (
        api_key in logs_raw
        or any(title in logs_raw for title in seen_titles)
        or any(url in logs_raw for url in seen_urls)
    )
    block_report["log_leak_detected"] = leak_detected
    return block_report


def run_validation(
    *,
    mode: ValidationMode = "both",
    max_gemini_calls: int = MAX_ALLOWED_GEMINI_CALLS,
    max_browse_calls: int = MAX_ALLOWED_EBAY_BROWSE_CALLS,
    max_oauth_calls: int = MAX_ALLOWED_EBAY_OAUTH_CALLS,
    sleep_between_calls: float | None = None,
    pacing_sleep: Callable[[float], None] | None = None,
    retry_sleep: Callable[[float], None] | None = None,
    random_fn: Callable[[], float] | None = None,
    max_retries: int = 1,
    base_backoff_seconds: float = 1.0,
    max_backoff_seconds: float = 8.0,
    jitter_ratio: float = 0.25,
    settings: Settings | None = None,
    gemini_client: object | None = None,
    oauth_inner_transport: httpx.BaseTransport | None = None,
    browse_inner_transport: httpx.BaseTransport | None = None,
    progress_stream: TextIO | None = None,
    output_path: Path | None = None,
    run_gemini_flag: bool | None = None,
    run_ebay_flag: bool | None = None,
) -> dict[str, Any]:
    """Executa os blocos selecionados compartilhando contadores globais e registrando progresso."""
    if isinstance(max_gemini_calls, bool) or max_gemini_calls < 1 or max_gemini_calls > MAX_ALLOWED_GEMINI_CALLS:
        raise ValueError(f"--max-gemini-calls deve estar entre 1 e {MAX_ALLOWED_GEMINI_CALLS}.")
    if isinstance(max_browse_calls, bool) or max_browse_calls < 1 or max_browse_calls > MAX_ALLOWED_EBAY_BROWSE_CALLS:
        raise ValueError(f"--max-browse-calls deve estar entre 1 e {MAX_ALLOWED_EBAY_BROWSE_CALLS}.")
    if isinstance(max_oauth_calls, bool) or max_oauth_calls < 1 or max_oauth_calls > MAX_ALLOWED_EBAY_OAUTH_CALLS:
        raise ValueError(f"--max-oauth-calls deve estar entre 1 e {MAX_ALLOWED_EBAY_OAUTH_CALLS}.")

    if sleep_between_calls is None:
        effective_pacing_seconds = 0.0 if gemini_client is not None else DEFAULT_SLEEP_BETWEEN_CALLS_SECONDS
    else:
        if (
            isinstance(sleep_between_calls, bool)
            or not isinstance(sleep_between_calls, (int, float))
            or not math.isfinite(float(sleep_between_calls))
            or float(sleep_between_calls) < 0.0
        ):
            raise ValueError("--sleep-between-calls deve ser numérico, finito e não negativo.")
        effective_pacing_seconds = float(sleep_between_calls)

    effective_pacing_sleep = pacing_sleep if pacing_sleep is not None else time.sleep
    effective_retry_sleep = (
        retry_sleep
        if retry_sleep is not None
        else ((lambda _seconds: None) if gemini_client is not None else time.sleep)
    )

    run_gemini = (
        run_gemini_flag
        if run_gemini_flag is not None
        else (os.getenv("RUN_GEMINI_INTEGRATION_TESTS") == "1")
    )
    run_ebay = (
        run_ebay_flag
        if run_ebay_flag is not None
        else (os.getenv("RUN_EBAY_INTEGRATION_TESTS") == "1")
    )

    want_gemini_eval = mode in {"gemini", "both"}
    want_e2e = mode in {"e2e", "both"}
    want_ebay_isolated = mode == "ebay"

    if (want_gemini_eval or want_e2e) and not run_gemini:
        return {
            "status": "SKIPPED",
            "mode": mode,
            "reason": "Defina RUN_GEMINI_INTEGRATION_TESTS=1 para habilitar chamadas ao Gemini.",
        }
    if (want_e2e or want_ebay_isolated) and not run_ebay:
        return {
            "status": "SKIPPED",
            "mode": mode,
            "reason": "Defina RUN_EBAY_INTEGRATION_TESTS=1 para habilitar chamadas à eBay.",
        }

    active_settings = settings if settings is not None else load_settings(load_dotenv_file=True)
    log_stream = io.StringIO()
    logger = configure_logging("INFO", stream=log_stream, logger_name="buscai.stage11.live")

    gemini_guard: _GlobalGeminiModelsProxy | None = None
    extractor: IntentExtractor | None = None
    oauth: EbayOAuthClient | None = None
    backend: EbayLiveBackend | None = None
    oauth_transport: _GuardedEbayTransport | None = None
    browse_transport: _GuardedEbayTransport | None = None
    quota_guard: QuotaGuard | None = None

    pacing_state: dict[str, Any] = {
        "configured_sleep_between_calls_seconds": effective_pacing_seconds,
        "pauses_applied": 0,
        "total_pacing_sleep_seconds": 0.0,
    }

    report: dict[str, Any] = {
        "mode": mode,
        "environment": {
            "python_version": sys.version.split()[0],
            "gemini_configured": active_settings.is_gemini_configured,
            "gemini_model": active_settings.gemini_model,
            "ebay_configured": active_settings.is_ebay_configured,
            "ebay_marketplace_id": active_settings.ebay_marketplace_id,
            "sleep_between_calls_seconds": effective_pacing_seconds,
            "max_retries": max_retries,
            "base_backoff_seconds": base_backoff_seconds,
            "max_backoff_seconds": max_backoff_seconds,
            "jitter_ratio": jitter_ratio,
        },
        "t11_3_gemini_eval": {"status": "NOT_RUN"},
        "t11_2_ebay_live": {"status": "NOT_RUN", "note": "Não repetido automaticamente (já validado na Etapa 11)."}
        if mode != "ebay"
        else {"status": "NOT_RUN"},
        "t11_4_e2e": {"status": "NOT_RUN"},
        "pacing": pacing_state,
        "global_usage_and_limits": {},
    }

    def _refresh_globals() -> None:
        logs_raw = log_stream.getvalue()
        secret_leak = any(
            sec and sec in logs_raw
            for sec in (
                active_settings.gemini_api_key.get_secret_value() if active_settings.gemini_api_key else "",
                active_settings.ebay_client_secret.get_secret_value() if active_settings.ebay_client_secret else "",
            )
        )
        gemini_snap = (
            gemini_guard.snapshot()
            if gemini_guard is not None
            else {
                "calls_made": 0,
                "max_allowed": max_gemini_calls,
                "logical_requests": 0,
                "retries_counted": 0,
                "errors": [],
                "error_counts_by_category": {},
                "fatal_stop": False,
                "interrupted": False,
                "stop_reason": None,
                "retry_after_seconds": None,
                "attempt_trail": [],
                "latency": _percentile_summary([]),
                "tokens_reported_calls": 0,
                "prompt_tokens_sum": None,
                "output_tokens_sum": None,
                "total_tokens_sum": None,
            }
        )
        oauth_snap = (
            oauth_transport.snapshot()
            if oauth_transport is not None
            else {"calls_made": 0, "max_allowed": max_oauth_calls, "statuses": [], "latency": _percentile_summary([]), "fatal_stop": False}
        )
        browse_snap = (
            browse_transport.snapshot()
            if browse_transport is not None
            else {"calls_made": 0, "max_allowed": max_browse_calls, "statuses": [], "latency": _percentile_summary([]), "fatal_stop": False}
        )
        gem_block_cur = report.get("t11_3_gemini_eval")
        overall_sum = gem_block_cur.get("overall_summary") if isinstance(gem_block_cur, dict) else None
        interrupted = bool(
            gemini_snap.get("interrupted")
            or (isinstance(gem_block_cur, dict) and gem_block_cur.get("interrupted"))
            or oauth_snap["fatal_stop"]
            or browse_snap["fatal_stop"]
        )
        interruption_reason = (
            (gem_block_cur.get("interruption_reason") if isinstance(gem_block_cur, dict) else None)
            or gemini_snap.get("stop_reason")
        )
        execution_state = {
            "interrupted": interrupted,
            "interruption_reason": interruption_reason,
            "scenarios_planned": overall_sum.get("planned_count") if isinstance(overall_sum, dict) else 0,
            "scenarios_completed": overall_sum.get("completed_count") if isinstance(overall_sum, dict) else 0,
            "scenarios_passed": overall_sum.get("passed_count") if isinstance(overall_sum, dict) else 0,
            "scenarios_failed": overall_sum.get("failed_count") if isinstance(overall_sum, dict) else 0,
            "scenarios_error": overall_sum.get("error_count") if isinstance(overall_sum, dict) else 0,
            "scenarios_not_run": overall_sum.get("not_run_count") if isinstance(overall_sum, dict) else 0,
        }
        report["global_usage_and_limits"] = {
            "gemini": gemini_snap,
            "ebay": {
                "oauth_calls_made": oauth_snap["calls_made"],
                "oauth_max_allowed": oauth_snap["max_allowed"],
                "oauth_statuses": oauth_snap["statuses"],
                "oauth_latency": oauth_snap["latency"],
                "browse_calls_made": browse_snap["calls_made"],
                "browse_max_allowed": browse_snap["max_allowed"],
                "browse_statuses": browse_snap["statuses"],
                "browse_latency": browse_snap["latency"],
                "fatal_stop": bool(oauth_snap["fatal_stop"] or browse_snap["fatal_stop"]),
            },
            "execution_state": execution_state,
            "pacing": dict(pacing_state),
            "secret_leak_in_logs": secret_leak,
        }

    recorder = ProgressiveMetricsRecorder(
        report=report,
        progress_stream=progress_stream,
        output_path=output_path,
        refresh_globals=_refresh_globals,
    )
    _refresh_globals()

    try:
        if (want_gemini_eval or want_e2e) and (gemini_client is not None or active_settings.is_gemini_configured):
            if gemini_client is not None:
                gemini_proxy = _GlobalGeminiClientProxy(gemini_client, max_calls=max_gemini_calls)
                model_name = active_settings.gemini_model
            else:
                raw_extractor = IntentExtractor.from_settings(active_settings, max_retries=max_retries, logger=logger)
                gemini_proxy = _GlobalGeminiClientProxy(raw_extractor.client, max_calls=max_gemini_calls)
                model_name = raw_extractor.model
            gemini_guard = gemini_proxy.models
            extractor_kwargs: dict[str, Any] = {
                "model": model_name,
                "client": gemini_proxy,
                "max_retries": max_retries,
                "base_backoff_seconds": base_backoff_seconds,
                "max_backoff_seconds": max_backoff_seconds,
                "jitter_ratio": jitter_ratio,
                "retry_on_429": False,
                "on_retry": gemini_guard.record_backoff,
                "sleep": effective_retry_sleep,
                "logger": logger,
            }
            if random_fn is not None:
                extractor_kwargs["random_fn"] = random_fn
            extractor = IntentExtractor(**extractor_kwargs)

        if want_gemini_eval and extractor is not None and gemini_guard is not None:
            gem_block: dict[str, Any] = {"status": "RUNNING"}
            report["t11_3_gemini_eval"] = gem_block
            try:
                _run_gemini_eval(
                    extractor,
                    gemini_guard,
                    recorder,
                    gem_block,
                    pacing_seconds=effective_pacing_seconds,
                    pacing_sleep=effective_pacing_sleep,
                    pacing_state=pacing_state,
                )
                gem_block["status"] = "EXECUTED"
            except Exception as exc:
                gem_block["status"] = "PARTIAL_ERROR"
                gem_block["error_type"] = type(exc).__name__
                recorder.flush_final()
                raise

        if (want_e2e or want_ebay_isolated) and active_settings.is_ebay_configured:
            quota_guard = QuotaGuard(
                daily_limit=min(max_browse_calls, active_settings.ebay_daily_quota_limit),
                default_max_search_per_request=4 if want_ebay_isolated else 1,
                default_max_get_item_per_request=1 if want_ebay_isolated else 0,
                logger=logger,
            )
            oauth_transport = _GuardedEbayTransport(
                max_calls=max_oauth_calls,
                kind="oauth",
                inner_transport=oauth_inner_transport,
            )
            browse_transport = _GuardedEbayTransport(
                max_calls=max_browse_calls,
                kind="browse",
                inner_transport=browse_inner_transport,
            )
            oauth, backend = build_ebay_clients(
                active_settings,
                oauth_transport=oauth_transport,
                browse_transport=browse_transport,
                quota_guard=quota_guard,
                max_retries=1,
                logger=logger,
            )

        if want_ebay_isolated and oauth is not None and backend is not None and quota_guard is not None:
            assert oauth_transport is not None and browse_transport is not None
            report["t11_2_ebay_live"] = {
                "status": "EXECUTED",
                **_run_ebay_controlled(
                    oauth=oauth,
                    backend=backend,
                    guard=quota_guard,
                    marketplace_id=active_settings.ebay_marketplace_id,
                    log_stream=log_stream,
                    recorder=recorder,
                    browse_transport=browse_transport,
                    oauth_transport=oauth_transport,
                ),
            }

        if (
            want_e2e
            and extractor is not None
            and gemini_guard is not None
            and backend is not None
            and quota_guard is not None
            and browse_transport is not None
            and oauth_transport is not None
        ):
            e2e_block: dict[str, Any] = {"status": "RUNNING"}
            report["t11_4_e2e"] = e2e_block
            if gemini_guard.fatal_stop or gemini_guard.calls_made >= gemini_guard.max_calls:
                e2e_block["status"] = "SKIPPED_GEMINI_BUDGET"
            elif browse_transport.fatal_stop or browse_transport.calls_made >= browse_transport.max_calls:
                e2e_block["status"] = "SKIPPED_EBAY_BUDGET"
            else:
                try:
                    _run_e2e_integrated(
                        settings=active_settings,
                        extractor=extractor,
                        backend=backend,
                        quota_guard=quota_guard,
                        gemini_guard=gemini_guard,
                        browse_transport=browse_transport,
                        oauth_transport=oauth_transport,
                        log_stream=log_stream,
                        logger=logger,
                        recorder=recorder,
                        block_report=e2e_block,
                        pacing_seconds=effective_pacing_seconds,
                        pacing_sleep=effective_pacing_sleep,
                        pacing_state=pacing_state,
                    )
                    e2e_block["status"] = "EXECUTED"
                except Exception as exc:
                    e2e_block["status"] = "PARTIAL_ERROR"
                    e2e_block["error_type"] = type(exc).__name__
                    raise
    finally:
        if backend is not None:
            backend.close()
        if oauth is not None:
            oauth.close()
        if browse_transport is not None:
            browse_transport.close()
        if oauth_transport is not None:
            oauth_transport.close()
        recorder.flush_final()

    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Runner controlado da Etapa 11 (Gemini real e E2E Gemini+eBay) com contador global."
    )
    parser.add_argument(
        "--mode",
        choices=("gemini", "e2e", "both", "ebay"),
        default="both",
        help="Seleção de avaliação: 'gemini' (A), 'e2e' (B), 'both' (A+B compartilhando contador), ou 'ebay' (isolada).",
    )
    parser.add_argument(
        "--max-gemini-calls",
        type=int,
        default=MAX_ALLOWED_GEMINI_CALLS,
        help=f"Teto global máximo de chamadas Gemini incluindo retries (1..{MAX_ALLOWED_GEMINI_CALLS}).",
    )
    parser.add_argument(
        "--max-browse-calls",
        type=int,
        default=MAX_ALLOWED_EBAY_BROWSE_CALLS,
        help=f"Teto global máximo de chamadas eBay Browse incluindo retries (1..{MAX_ALLOWED_EBAY_BROWSE_CALLS}).",
    )
    parser.add_argument(
        "--max-oauth-calls",
        type=int,
        default=MAX_ALLOWED_EBAY_OAUTH_CALLS,
        help=f"Teto global máximo de emissões OAuth eBay (1..{MAX_ALLOWED_EBAY_OAUTH_CALLS}).",
    )
    parser.add_argument(
        "--sleep-between-calls",
        type=float,
        default=DEFAULT_SLEEP_BETWEEN_CALLS_SECONDS,
        help=f"Pausa em segundos entre cenários consecutivos (pacing; padrão: {DEFAULT_SLEEP_BETWEEN_CALLS_SECONDS}).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Arquivo opcional para gravar snapshots parciais e resultado agregado final em JSON.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_validation(
        mode=args.mode,
        max_gemini_calls=args.max_gemini_calls,
        max_browse_calls=args.max_browse_calls,
        max_oauth_calls=args.max_oauth_calls,
        sleep_between_calls=args.sleep_between_calls,
        output_path=args.output,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
