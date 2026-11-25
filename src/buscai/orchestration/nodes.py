"""Pure, testable LangGraph node functions wrapping Stages 1-6 domain pipelines."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TypedDict

from buscai.catalog.errors import (
    CatalogError,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from buscai.observability.logging import log_operational_event
from buscai.ranking.pipeline import RankingPipeline
from buscai.search.backend import SearchBackend
from buscai.search.models import SearchResponse
from buscai.search.query_builder import (
    IntentExtractorLike,
    QueryBuilder,
    build_search_plan_from_text,
)
from buscai.search.quota import QuotaGuard, RequestCallBudget
from buscai.semantic.pipeline import SemanticSearchProcessor
from buscai.validation.pipeline import ValidationPipeline

from .state import ExecutionStatus, OrchestrationError, SearchGraphState

_LOGGER = logging.getLogger("buscai.orchestration")


@dataclass(frozen=True)
class OrchestrationDependencies:
    """Injected domain services for constructing the LangGraph search graph."""

    extractor: IntentExtractorLike
    query_builder: QueryBuilder
    search_backend: SearchBackend
    semantic_processor: SemanticSearchProcessor
    validation_pipeline: ValidationPipeline
    ranking_pipeline: RankingPipeline
    quota_guard: QuotaGuard | None = None
    max_search_calls: int = 2
    max_get_item_calls: int = 6
    logger: logging.Logger | None = None


def _classify_error(node: str, exc: Exception) -> OrchestrationError:
    """Map exceptions into sanitized typed error contracts without leaking listing data."""
    err_type = type(exc).__name__
    if isinstance(exc, ProviderRateLimitError):
        return OrchestrationError(
            node=node,
            error_type=err_type,
            code="provider_rate_limit",
            safe_message="Limite de requisições ou cota do provedor de busca atingido.",
            retryable=True,
            retry_after_seconds=exc.retry_after_seconds,
        )
    if isinstance(exc, ProviderAuthError):
        return OrchestrationError(
            node=node,
            error_type=err_type,
            code="provider_auth_error",
            safe_message="Falha de autenticação com o provedor de busca.",
            retryable=False,
        )
    if isinstance(exc, ProviderTimeoutError):
        return OrchestrationError(
            node=node,
            error_type=err_type,
            code="provider_timeout",
            safe_message="Tempo limite excedido ao consultar o provedor de busca.",
            retryable=True,
        )
    if isinstance(exc, ProviderUnavailableError):
        return OrchestrationError(
            node=node,
            error_type=err_type,
            code="provider_unavailable",
            safe_message="Provedor de busca temporariamente indisponível.",
            retryable=True,
        )
    if isinstance(exc, CatalogError):
        return OrchestrationError(
            node=node,
            error_type=err_type,
            code="provider_catalog_error",
            safe_message="Resposta inválida ou falha de comunicação com o provedor de busca.",
            retryable=False,
        )
    if isinstance(exc, (ValueError, TypeError)):
        return OrchestrationError(
            node=node,
            error_type=err_type,
            code="invalid_input_or_contract",
            safe_message="Entrada ou contrato interno inválido durante a orquestração.",
            retryable=False,
        )
    return OrchestrationError(
        node=node,
        error_type=err_type,
        code="internal_orchestration_error",
        safe_message=f"Falha interna controlada no nó '{node}'.",
        retryable=False,
    )


class _NodeMeta(TypedDict):
    executed_nodes: tuple[str, ...]
    node_durations_ms: dict[str, float]


def _record_node(
    state: SearchGraphState,
    node_name: str,
    started_at: float,
) -> _NodeMeta:
    elapsed_ms = round((time.perf_counter() - started_at) * 1000.0, 3)
    prev_nodes = state.get("executed_nodes", ())
    prev_durations = dict(state.get("node_durations_ms", {}))
    prev_durations[node_name] = elapsed_ms
    return {
        "executed_nodes": (*prev_nodes, node_name),
        "node_durations_ms": prev_durations,
    }


def prepare_intent_and_plan_node(
    state: SearchGraphState,
    deps: OrchestrationDependencies,
) -> SearchGraphState:
    """Node 1: extract intent once and build deterministic SearchPlan."""
    started = time.perf_counter()
    node_name = "prepare_intent_and_plan"
    try:
        plan_res = build_search_plan_from_text(
            state.get("user_text", ""),
            extractor=deps.extractor,
            query_builder=deps.query_builder,
            budget_brl=state.get("budget_brl"),
            conversation_history=state.get("conversation_history"),
            request_id=state.get("request_id"),
            limit=state.get("limit", 30),
        )
        llm_calls = state.get("llm_calls_used", 0) + 1
        meta = _record_node(state, node_name, started)

        if plan_res.clarification_needed:
            question = (
                plan_res.clarification_question
                or "Pode informar mais detalhes sobre o produto desejado?"
            )
            return {
                **meta,
                "plan_result": plan_res,
                "intent": plan_res.intent,
                "search_plan": plan_res.plan,
                "llm_calls_used": llm_calls,
                "clarification_needed": True,
                "clarification_question": question,
                "status": ExecutionStatus.CLARIFICATION_NEEDED,
            }

        if not plan_res.plan.queries or plan_res.plan.primary_query is None:
            return {
                **meta,
                "plan_result": plan_res,
                "intent": plan_res.intent,
                "search_plan": plan_res.plan,
                "llm_calls_used": llm_calls,
                "clarification_needed": False,
                "status": ExecutionStatus.INVALID_PLAN,
                "error": OrchestrationError(
                    node=node_name,
                    error_type="InvalidSearchPlan",
                    code="invalid_search_plan",
                    safe_message="Não foi possível construir uma consulta de busca válida.",
                    retryable=False,
                ),
            }

        return {
            **meta,
            "plan_result": plan_res,
            "intent": plan_res.intent,
            "search_plan": plan_res.plan,
            "llm_calls_used": llm_calls,
            "clarification_needed": False,
            "clarification_question": None,
        }
    except Exception as exc:
        meta = _record_node(state, node_name, started)
        return {
            **meta,
            "status": ExecutionStatus.ERROR,
            "error": _classify_error(node_name, exc),
        }


def search_products_node(
    state: SearchGraphState,
    deps: OrchestrationDependencies,
) -> SearchGraphState:
    """Node 2: query SearchBackend preserving RequestCallBudget and QuotaGuard."""
    started = time.perf_counter()
    node_name = "search_products"
    try:
        plan = state["search_plan"]
        budget = state.get("call_budget")
        if budget is None:
            if deps.quota_guard is not None:
                budget = deps.quota_guard.new_request_budget(
                    max_search_calls=deps.max_search_calls,
                    max_get_item_calls=deps.max_get_item_calls,
                )
            else:
                budget = RequestCallBudget(
                    max_search_calls=deps.max_search_calls,
                    max_get_item_calls=deps.max_get_item_calls,
                )

        request_id = state.get("request_id")
        responses: list[SearchResponse] = []
        total_candidates = 0

        for idx, planned in enumerate(plan.queries):
            if budget.remaining_search_calls <= 0:
                break
            if idx > 0 and total_candidates >= 5:
                break
            try:
                resp = deps.search_backend.search(
                    planned.query,
                    budget=budget,
                    request_id=request_id,
                )
            except ProviderRateLimitError:
                if responses:
                    break
                raise
            responses.append(resp)
            total_candidates += len(resp.candidates)

        meta = _record_node(state, node_name, started)
        if total_candidates == 0:
            return {
                **meta,
                "call_budget": budget,
                "search_responses": tuple(responses),
                "status": ExecutionStatus.NO_RESULTS,
            }

        return {
            **meta,
            "call_budget": budget,
            "search_responses": tuple(responses),
        }
    except Exception as exc:
        meta = _record_node(state, node_name, started)
        return {
            **meta,
            "status": ExecutionStatus.ERROR,
            "error": _classify_error(node_name, exc),
        }


def process_semantics_node(
    state: SearchGraphState,
    deps: OrchestrationDependencies,
) -> SearchGraphState:
    """Node 3: run in-memory deduplication and semantic scoring."""
    started = time.perf_counter()
    node_name = "process_semantics"
    try:
        intent = state["intent"]
        plan = state["search_plan"]
        responses = state.get("search_responses", ())
        query_text = intent.query or state.get("user_text", "")
        sem_res = deps.semantic_processor.process(
            query_text,
            responses,
            intent=intent,
            category_hint=plan.canonical_category,
            request_id=state.get("request_id"),
        )
        meta = _record_node(state, node_name, started)
        if not sem_res.candidates or not sem_res.scored_candidates:
            return {
                **meta,
                "semantic_result": sem_res,
                "status": ExecutionStatus.NO_RESULTS,
            }
        return {
            **meta,
            "semantic_result": sem_res,
        }
    except Exception as exc:
        meta = _record_node(state, node_name, started)
        return {
            **meta,
            "status": ExecutionStatus.ERROR,
            "error": _classify_error(node_name, exc),
        }


def validate_candidates_node(
    state: SearchGraphState,
    deps: OrchestrationDependencies,
) -> SearchGraphState:
    """Node 4: run evidence validation, selective getItem enrichment, and relaxation."""
    started = time.perf_counter()
    node_name = "validate_candidates"
    try:
        val_res = deps.validation_pipeline.run(
            state["intent"],
            state["semantic_result"],
            profile_id=state.get("profile_id"),
            budget=state.get("call_budget"),
            request_id=state.get("request_id"),
        )
        meta = _record_node(state, node_name, started)
        if not val_res.eligible_candidates:
            return {
                **meta,
                "validation_result": val_res,
                "status": ExecutionStatus.ALL_REJECTED,
            }
        return {
            **meta,
            "validation_result": val_res,
        }
    except Exception as exc:
        meta = _record_node(state, node_name, started)
        return {
            **meta,
            "status": ExecutionStatus.ERROR,
            "error": _classify_error(node_name, exc),
        }


def rank_candidates_node(
    state: SearchGraphState,
    deps: OrchestrationDependencies,
) -> SearchGraphState:
    """Node 5: run two-level multi-criteria reranking and deterministic explainability."""
    started = time.perf_counter()
    node_name = "rank_candidates"
    try:
        val_res = state["validation_result"]
        plan = state["search_plan"]
        rank_res = deps.ranking_pipeline.run(
            val_res,
            semantic_result=state.get("semantic_result"),
            profile_id=state.get("profile_id") or val_res.active_profile_id,
            category_hint=plan.canonical_category,
            request_id=state.get("request_id"),
        )
        meta = _record_node(state, node_name, started)
        if not rank_res.ranked_groups:
            return {
                **meta,
                "ranking_result": rank_res,
                "status": ExecutionStatus.ALL_REJECTED,
            }
        return {
            **meta,
            "ranking_result": rank_res,
            "status": ExecutionStatus.SUCCESS,
        }
    except Exception as exc:
        meta = _record_node(state, node_name, started)
        return {
            **meta,
            "status": ExecutionStatus.ERROR,
            "error": _classify_error(node_name, exc),
        }


def finalize_node(
    state: SearchGraphState,
    deps: OrchestrationDependencies,
) -> SearchGraphState:
    """Node 6: preserve terminal status/errors and emit sanitized allowlist-only telemetry."""
    started = time.perf_counter()
    node_name = "finalize"
    meta = _record_node(state, node_name, started)

    status = state.get("status")
    if status is None:
        status = (
            ExecutionStatus.ERROR
            if state.get("error") is not None
            else ExecutionStatus.NO_RESULTS
        )

    budget = state.get("call_budget")
    sem_res = state.get("semantic_result")
    val_res = state.get("validation_result")
    rank_res = state.get("ranking_result")
    err = state.get("error")

    total_duration_ms = round(sum(meta["node_durations_ms"].values()), 3)
    logger = deps.logger or _LOGGER
    log_operational_event(
        logger,
        logging.INFO if err is None else logging.WARNING,
        "orchestration_completed",
        request_id=state.get("request_id", "offline"),
        stage="orchestration",
        duration_ms=total_duration_ms,
        candidate_count=sem_res.input_count if sem_res is not None else 0,
        result_count=rank_res.unique_products_count if rank_res is not None else 0,
        ebay_calls=budget.total_calls_used if budget is not None else 0,
        llm_calls=state.get("llm_calls_used", 0),
        relaxation_count=len(val_res.relaxations) if val_res is not None else 0,
        error_type=err.error_type if err is not None else None,
        provider="langgraph_thin",
    )

    return {
        **meta,
        "status": status,
    }
