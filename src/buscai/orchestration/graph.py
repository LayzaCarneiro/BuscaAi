"""Thin LangGraph StateGraph builder and stateless SearchOrchestrator facade (D7)."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from buscai.search.quota import RequestCallBudget

from .nodes import (
    OrchestrationDependencies,
    finalize_node,
    prepare_intent_and_plan_node,
    process_semantics_node,
    rank_candidates_node,
    search_products_node,
    validate_candidates_node,
)
from .routing import (
    route_after_prepare,
    route_after_search,
    route_after_semantics,
    route_after_validation,
)
from .state import (
    ExecutionStatus,
    OrchestrationMetrics,
    OrchestrationResult,
    SearchGraphState,
)

TRACING_ENV_VARS: tuple[str, ...] = (
    "LANGSMITH_TRACING",
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING",
    "LANGCHAIN_TRACING_V2",
)
_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})


class TracingPolicyViolationError(RuntimeError):
    """Raised when external LangSmith/LangChain tracing is enabled in violation of D7."""


def ensure_tracing_disabled(environ: Mapping[str, str] | None = None) -> None:
    """Verify that LangSmith/LangChain tracing environment variables are disabled."""
    env = os.environ if environ is None else environ
    for var in TRACING_ENV_VARS:
        raw = env.get(var)
        if isinstance(raw, str) and raw.strip().lower() in _TRUTHY_VALUES:
            raise TracingPolicyViolationError(
                f"Tracing externo proibido pela decisão D7 (variável '{var}' ativa)."
            )


def build_search_graph(deps: OrchestrationDependencies) -> Any:
    """Compile a stateless LangGraph StateGraph with zero checkpointer, store, or tracing."""
    workflow = StateGraph(SearchGraphState)

    workflow.add_node("prepare_intent_and_plan", lambda s: prepare_intent_and_plan_node(s, deps))
    workflow.add_node("search_products", lambda s: search_products_node(s, deps))
    workflow.add_node("process_semantics", lambda s: process_semantics_node(s, deps))
    workflow.add_node("validate_candidates", lambda s: validate_candidates_node(s, deps))
    workflow.add_node("rank_candidates", lambda s: rank_candidates_node(s, deps))
    workflow.add_node("finalize", lambda s: finalize_node(s, deps))

    workflow.add_edge(START, "prepare_intent_and_plan")
    workflow.add_conditional_edges(
        "prepare_intent_and_plan",
        route_after_prepare,
        {"search_products": "search_products", "finalize": "finalize"},
    )
    workflow.add_conditional_edges(
        "search_products",
        route_after_search,
        {"process_semantics": "process_semantics", "finalize": "finalize"},
    )
    workflow.add_conditional_edges(
        "process_semantics",
        route_after_semantics,
        {"validate_candidates": "validate_candidates", "finalize": "finalize"},
    )
    workflow.add_conditional_edges(
        "validate_candidates",
        route_after_validation,
        {"rank_candidates": "rank_candidates", "finalize": "finalize"},
    )
    workflow.add_edge("rank_candidates", "finalize")
    workflow.add_edge("finalize", END)

    compiled = workflow.compile(checkpointer=None, store=None)
    if getattr(compiled, "checkpointer", None) is not None:
        raise RuntimeError("Checkpointer não é permitido no grafo do BuscaAI (D7).")
    if getattr(compiled, "store", None) is not None:
        raise RuntimeError("Store persistente não é permitido no grafo do BuscaAI (D7).")
    return compiled


class SearchOrchestrator:
    """Stateless thread-safe facade executing one independent StateGraph run per search."""

    def __init__(
        self,
        deps: OrchestrationDependencies,
        *,
        reject_tracing_env: bool = True,
    ) -> None:
        self._deps = deps
        self._reject_tracing_env = reject_tracing_env
        self._graph = build_search_graph(deps)

    @property
    def compiled_graph(self) -> Any:
        """Expose the compiled graph for inspection in compliance tests."""
        return self._graph

    def run(
        self,
        user_text: str,
        *,
        budget_brl: Decimal | int | float | str | None = None,
        profile_id: str | None = None,
        limit: int = 30,
        request_id: str | None = None,
        conversation_history: Sequence[Mapping[str, str]] | None = None,
        call_budget: RequestCallBudget | None = None,
    ) -> OrchestrationResult:
        """Execute one isolated in-memory orchestration run and discard transient state."""
        if self._reject_tracing_env:
            ensure_tracing_disabled()

        req_id = request_id or uuid4().hex
        initial_state: SearchGraphState = {
            "request_id": req_id,
            "user_text": user_text,
            "budget_brl": budget_brl,
            "profile_id": profile_id,
            "limit": limit,
            "conversation_history": tuple(conversation_history)
            if conversation_history is not None
            else None,
            "executed_nodes": (),
            "node_durations_ms": {},
            "llm_calls_used": 0,
        }
        if call_budget is not None:
            initial_state["call_budget"] = call_budget

        final_state: SearchGraphState = self._graph.invoke(
            initial_state,
            config={"callbacks": []},
        )

        budget = final_state.get("call_budget")
        sem_res = final_state.get("semantic_result")
        rank_res = final_state.get("ranking_result")
        val_res = final_state.get("validation_result")
        durations = dict(final_state.get("node_durations_ms", {}))

        metrics = OrchestrationMetrics(
            request_id=req_id,
            executed_nodes=tuple(final_state.get("executed_nodes", ())),
            node_durations_ms=durations,
            total_duration_ms=round(sum(durations.values()), 3),
            search_calls_used=budget.search_calls_used if budget is not None else 0,
            get_item_calls_used=budget.get_item_calls_used if budget is not None else 0,
            llm_calls_used=int(final_state.get("llm_calls_used", 0)),
            candidate_count=sem_res.input_count if sem_res is not None else 0,
            unique_products_count=rank_res.unique_products_count if rank_res is not None else 0,
            eligible_offers_count=rank_res.eligible_offers_count if rank_res is not None else 0,
            rejected_count=rank_res.rejected_count if rank_res is not None else 0,
            relaxation_count=len(val_res.relaxations) if val_res is not None else 0,
        )

        result = OrchestrationResult(
            request_id=req_id,
            status=final_state.get("status", ExecutionStatus.ERROR),
            clarification_needed=bool(final_state.get("clarification_needed", False)),
            clarification_question=final_state.get("clarification_question"),
            intent=final_state.get("intent"),
            search_plan=final_state.get("search_plan"),
            semantic_result=sem_res,
            validation_result=val_res,
            ranking_result=rank_res,
            error=final_state.get("error"),
            metrics=metrics,
        )

        # Explicitly drop transient graph state references before returning.
        cast(dict[str, object], initial_state).clear()
        cast(dict[str, object], final_state).clear()
        return result
