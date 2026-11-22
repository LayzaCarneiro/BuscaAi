"""Pure conditional routing functions for Stage 7 LangGraph orchestration."""

from __future__ import annotations

from typing import Literal

from .state import ExecutionStatus, SearchGraphState


def route_after_prepare(
    state: SearchGraphState,
) -> Literal["search_products", "finalize"]:
    """Skip search/semantics/validation/ranking on error, invalid plan, or clarification."""
    if state.get("error") is not None:
        return "finalize"
    if state.get("clarification_needed"):
        return "finalize"
    status = state.get("status")
    if status in (
        ExecutionStatus.CLARIFICATION_NEEDED,
        ExecutionStatus.INVALID_PLAN,
        ExecutionStatus.ERROR,
    ):
        return "finalize"
    plan = state.get("search_plan")
    if plan is None or not plan.queries or plan.primary_query is None:
        return "finalize"
    return "search_products"


def route_after_search(
    state: SearchGraphState,
) -> Literal["process_semantics", "finalize"]:
    """Skip embeddings/validation/ranking when search fails or returns zero candidates."""
    if state.get("error") is not None:
        return "finalize"
    if state.get("status") in (ExecutionStatus.ERROR, ExecutionStatus.NO_RESULTS):
        return "finalize"
    responses = state.get("search_responses", ())
    if not any(resp.candidates for resp in responses):
        return "finalize"
    return "process_semantics"


def route_after_semantics(
    state: SearchGraphState,
) -> Literal["validate_candidates", "finalize"]:
    """Skip validation/ranking when semantic processing fails or yields no candidates."""
    if state.get("error") is not None:
        return "finalize"
    if state.get("status") in (ExecutionStatus.ERROR, ExecutionStatus.NO_RESULTS):
        return "finalize"
    sem_res = state.get("semantic_result")
    if sem_res is None or not sem_res.candidates or not sem_res.scored_candidates:
        return "finalize"
    return "validate_candidates"


def route_after_validation(
    state: SearchGraphState,
) -> Literal["rank_candidates", "finalize"]:
    """Skip ranking when validation fails or rejects all candidates."""
    if state.get("error") is not None:
        return "finalize"
    if state.get("status") in (ExecutionStatus.ERROR, ExecutionStatus.ALL_REJECTED):
        return "finalize"
    val_res = state.get("validation_result")
    if val_res is None or not val_res.eligible_candidates:
        return "finalize"
    return "rank_candidates"
