"""Typed transient state and output contracts for Stage 7 LangGraph orchestration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import TypedDict

from buscai.ranking.models import RankingPipelineResult
from buscai.search.models import SearchResponse
from buscai.search.query_builder import IntentQueryPlanResult, SearchPlan
from buscai.search.quota import RequestCallBudget
from buscai.semantic.pipeline import SemanticProcessingResult
from buscai.validation.models import ValidationPipelineResult
from userintent.schema import UserIntent


class ExecutionStatus(str, Enum):
    """Final execution status of a single search orchestration run."""

    SUCCESS = "success"
    CLARIFICATION_NEEDED = "clarification_needed"
    INVALID_PLAN = "invalid_plan"
    NO_RESULTS = "no_results"
    ALL_REJECTED = "all_rejected"
    ERROR = "error"


@dataclass(frozen=True)
class OrchestrationError:
    """Sanitized, typed error metadata without exposing listing contents or PII."""

    node: str
    error_type: str
    code: str
    safe_message: str
    retryable: bool = False
    retry_after_seconds: float | None = None


@dataclass(frozen=True)
class OrchestrationMetrics:
    """Operational telemetry counters for a single orchestration run (allowlist-safe)."""

    request_id: str
    executed_nodes: tuple[str, ...] = ()
    node_durations_ms: dict[str, float] = field(default_factory=dict)
    total_duration_ms: float = 0.0
    search_calls_used: int = 0
    get_item_calls_used: int = 0
    llm_calls_used: int = 0
    candidate_count: int = 0
    unique_products_count: int = 0
    eligible_offers_count: int = 0
    rejected_count: int = 0
    relaxation_count: int = 0


class SearchGraphState(TypedDict, total=False):
    """Ephemeral per-request LangGraph state kept strictly in memory."""

    # Request inputs
    request_id: str
    user_text: str
    budget_brl: Decimal | int | float | str | None
    profile_id: str | None
    limit: int
    conversation_history: tuple[Mapping[str, str], ...] | None

    # Per-request call budget
    call_budget: RequestCallBudget

    # Domain stage outputs
    plan_result: IntentQueryPlanResult
    intent: UserIntent
    search_plan: SearchPlan
    search_responses: tuple[SearchResponse, ...]
    semantic_result: SemanticProcessingResult
    validation_result: ValidationPipelineResult
    ranking_result: RankingPipelineResult

    # Flow control & observability
    status: ExecutionStatus
    clarification_needed: bool
    clarification_question: str | None
    error: OrchestrationError | None
    executed_nodes: tuple[str, ...]
    node_durations_ms: dict[str, float]
    llm_calls_used: int


@dataclass(frozen=True)
class OrchestrationResult:
    """Public return contract produced at the end of a graph run."""

    request_id: str
    status: ExecutionStatus
    clarification_needed: bool = False
    clarification_question: str | None = None
    intent: UserIntent | None = None
    search_plan: SearchPlan | None = None
    semantic_result: SemanticProcessingResult | None = None
    validation_result: ValidationPipelineResult | None = None
    ranking_result: RankingPipelineResult | None = None
    error: OrchestrationError | None = None
    metrics: OrchestrationMetrics | None = None
