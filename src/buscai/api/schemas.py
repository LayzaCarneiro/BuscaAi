"""Schemas HTTP estritos (extra='forbid') e conversores explícitos da API FastAPI."""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from buscai.catalog_registry import load_default_registry
from buscai.orchestration.state import ExecutionStatus, OrchestrationResult
from buscai.pricing import CurrencyConverter
from buscai.ranking.models import RankedOffer, RankedProductGroup, ScoreBreakdown

MAX_HTTP_QUERY_LENGTH = 500
MAX_HTTP_LIMIT_UPPER_BOUND = 50
MAX_HTTP_CHAT_MESSAGE_LENGTH = 500
MAX_HTTP_CHAT_HISTORY_MESSAGES = 50
MAX_HTTP_CHAT_HISTORY_MESSAGE_LENGTH = 2000
MAX_HTTP_CHAT_HISTORY_TOTAL_CHARS = 20000
VALID_CHAT_ROLES = frozenset({"user", "assistant"})


def _normalize_required_text(value: object, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} deve ser uma string.")
    cleaned = " ".join(value.strip().split())
    if not cleaned:
        raise ValueError(f"{field_name} não pode ser vazia.")
    if len(cleaned) > max_length:
        raise ValueError(f"{field_name} excede o limite máximo de {max_length} caracteres.")
    return cleaned


def _validate_budget_brl_value(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("budget_brl não pode ser booleano.")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("budget_brl deve ser um número finito.")
        dec = Decimal(str(value))
    elif isinstance(value, int):
        dec = Decimal(value)
    elif isinstance(value, Decimal):
        dec = value
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError("budget_brl não pode ser string vazia.")
        try:
            dec = Decimal(stripped)
        except InvalidOperation as exc:
            raise ValueError("budget_brl deve ser um número decimal válido.") from exc
    else:
        raise ValueError("budget_brl deve ser numérico.")

    if not dec.is_finite() or dec <= 0:
        raise ValueError("budget_brl deve ser finito e maior que zero.")
    return dec


def _validate_profile_id_value(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("profile_id deve ser uma string.")
    cleaned = " ".join(value.strip().split())
    if not cleaned:
        raise ValueError("profile_id não pode ser vazio.")
    resolved = load_default_registry().resolve_profile(cleaned)
    if resolved is None:
        raise ValueError(f"Perfil de busca desconhecido: {cleaned!r}.")
    return resolved.profile_id


def _validate_limit_value(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("limit deve ser um número inteiro.")
    if value < 1 or value > MAX_HTTP_LIMIT_UPPER_BOUND:
        raise ValueError(f"limit deve estar entre 1 e {MAX_HTTP_LIMIT_UPPER_BOUND}.")
    return value


class SearchRequest(BaseModel):
    """Contrato HTTP de entrada para POST /v1/search (rejeita propriedades extras)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "query": "notebook para trabalho 16gb ram ate 4500 reais",
                "budget_brl": 4500,
                "profile_id": "best_value",
                "limit": 10,
            }
        },
    )

    query: str = Field(
        ...,
        description="Consulta em linguagem natural (máximo de 500 caracteres).",
    )
    budget_brl: Decimal | None = Field(
        default=None,
        description="Orçamento máximo opcional em BRL (> 0).",
    )
    profile_id: str | None = Field(
        default=None,
        description=(
            "Perfil opcional de utilização ou prioridade "
            "(best_value, best_performance, lowest_price, professional_use, study, everyday_use)."
        ),
    )
    limit: int | None = Field(
        default=None,
        description="Quantidade máxima de grupos de produtos retornados (respeita o teto configurado).",
    )

    @field_validator("query", mode="before")
    @classmethod
    def _validate_query(cls, value: object) -> str:
        return _normalize_required_text(value, field_name="query", max_length=MAX_HTTP_QUERY_LENGTH)

    @field_validator("budget_brl", mode="before")
    @classmethod
    def _validate_budget_brl(cls, value: object) -> Decimal | None:
        return _validate_budget_brl_value(value)

    @field_validator("profile_id", mode="before")
    @classmethod
    def _validate_profile_id(cls, value: object) -> str | None:
        return _validate_profile_id_value(value)

    @field_validator("limit", mode="before")
    @classmethod
    def _validate_limit(cls, value: object) -> int | None:
        return _validate_limit_value(value)


class ChatHistoryMessage(BaseModel):
    """Turno histórico aceito em POST /v1/chat (restrito a user e assistant)."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"] = Field(
        ...,
        description="Papel do turno no histórico conversacional (apenas 'user' ou 'assistant').",
    )
    content: str = Field(
        ...,
        description="Texto do turno histórico (não vazio).",
    )

    @field_validator("role", mode="before")
    @classmethod
    def _validate_role(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("role deve ser uma string.")
        normalized = value.strip().lower()
        if normalized not in VALID_CHAT_ROLES:
            raise ValueError("role inválido: permitido apenas 'user' ou 'assistant'.")
        return normalized

    @field_validator("content", mode="before")
    @classmethod
    def _validate_content(cls, value: object) -> str:
        return _normalize_required_text(
            value,
            field_name="content",
            max_length=MAX_HTTP_CHAT_HISTORY_MESSAGE_LENGTH,
        )


class ChatRequest(BaseModel):
    """Contrato HTTP stateless de entrada para POST /v1/chat (rejeita propriedades extras)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "message": "Prefiro Lenovo e pelo menos 16 GB de RAM.",
                "conversation_history": [
                    {"role": "user", "content": "Quero um notebook para estudar até R$ 4.000."},
                    {
                        "role": "assistant",
                        "content": "Busca realizada na categoria notebook com orçamento máximo de R$ 4000.",
                    },
                ],
                "budget_brl": 4000,
                "profile_id": "study",
                "limit": 5,
            }
        },
    )

    message: str = Field(
        ...,
        description="Mensagem atual do usuário em linguagem natural (obrigatória).",
    )
    conversation_history: list[ChatHistoryMessage] = Field(
        default_factory=list,
        description="Histórico conversacional anterior enviado pelo cliente (apenas turnos user/assistant).",
    )
    budget_brl: Decimal | None = Field(
        default=None,
        description="Orçamento máximo opcional em BRL (> 0).",
    )
    profile_id: str | None = Field(
        default=None,
        description=(
            "Perfil opcional de utilização ou prioridade "
            "(best_value, best_performance, lowest_price, professional_use, study, everyday_use)."
        ),
    )
    limit: int | None = Field(
        default=None,
        description="Quantidade máxima de grupos de produtos retornados (respeita o teto configurado).",
    )

    @field_validator("message", mode="before")
    @classmethod
    def _validate_message(cls, value: object) -> str:
        return _normalize_required_text(value, field_name="message", max_length=MAX_HTTP_CHAT_MESSAGE_LENGTH)

    @field_validator("conversation_history", mode="before")
    @classmethod
    def _validate_history_list(cls, value: object) -> list[object]:
        if not isinstance(value, list):
            raise ValueError("conversation_history deve ser uma lista de turnos.")
        if len(value) > MAX_HTTP_CHAT_HISTORY_MESSAGES:
            raise ValueError(
                f"conversation_history excede o limite máximo de {MAX_HTTP_CHAT_HISTORY_MESSAGES} mensagens."
            )
        return value

    @model_validator(mode="after")
    def _validate_history_total_chars(self) -> ChatRequest:
        total_chars = sum(len(turn.content) for turn in self.conversation_history)
        if total_chars > MAX_HTTP_CHAT_HISTORY_TOTAL_CHARS:
            raise ValueError(
                f"conversation_history excede o limite máximo de {MAX_HTTP_CHAT_HISTORY_TOTAL_CHARS} caracteres."
            )
        return self

    @field_validator("budget_brl", mode="before")
    @classmethod
    def _validate_budget_brl(cls, value: object) -> Decimal | None:
        return _validate_budget_brl_value(value)

    @field_validator("profile_id", mode="before")
    @classmethod
    def _validate_profile_id(cls, value: object) -> str | None:
        return _validate_profile_id_value(value)

    @field_validator("limit", mode="before")
    @classmethod
    def _validate_limit(cls, value: object) -> int | None:
        return _validate_limit_value(value)


class ErrorDetailSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    type: str
    message: str


class ErrorBodySchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    request_id: str
    details: list[ErrorDetailSchema] | None = None


class ErrorResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: ErrorBodySchema


class ComponentHealthSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    required: bool
    status: Literal["ready", "not_ready", "optional_unavailable"]
    detail: str


class HealthResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "not_ready"]
    liveness: Literal["alive"]
    readiness: Literal["ready", "not_ready"]
    components: list[ComponentHealthSchema]


class ExchangeRateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_currency: str
    quote_currency: str
    rate: Decimal
    updated_at: str
    source: str
    is_estimate: bool = True


class EstimatedPriceSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal
    currency: str = "BRL"
    is_estimate: bool = True
    rate: Decimal
    source: str
    updated_at: str


class ShippingSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    known: bool
    cost: Decimal | None = None
    currency: str | None = None
    free_shipping: bool | None = None
    estimated_min_days: int | None = None
    estimated_max_days: int | None = None


class SellerSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    known: bool
    username: str | None = None
    feedback_score: int | None = None
    feedback_percentage: Decimal | None = None


class CriterionEvaluationSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion_id: str
    criterion_label: str
    priority: Literal["hard", "soft"]
    status: Literal["satisfied", "not_satisfied", "unknown", "not_applicable"]
    evidence: str | None = None
    source_field: str | None = None
    insufficient_information: bool = False
    note: str | None = None


class OfferValidationSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eligible: bool
    strictly_verified: bool
    approval_state: str
    detected_category: str | None = None
    profile_id: str | None = None
    hard_satisfied_count: int
    hard_not_satisfied_count: int
    hard_unknown_count: int
    soft_satisfied_count: int
    soft_not_satisfied_count: int
    soft_unknown_count: int
    contradictions: dict[str, list[str]]
    unknown_criteria: list[str]
    criteria: list[CriterionEvaluationSchema]


class CriterionScoreSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    raw_value: float | str | None = None
    normalized_score: float | None = None
    available: bool
    not_applicable: bool = False
    base_weight: float
    effective_weight: float
    weighted_contribution: float
    evidence_sources: list[str]
    note: str


class ScoreBreakdownSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    final_score: float
    weighted_sum: float
    evidence_coverage: float
    confidence_factor: float
    contradiction_count: int
    contradiction_penalty: float
    semantic_enabled: bool
    semantic_weight_applied: float
    total_cost_brl: Decimal | None = None
    shipping_known: bool
    criteria: list[CriterionScoreSchema]


class ExplanationSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    rank: int
    total_score: float
    reasons: list[str]
    tradeoffs: list[str]
    warnings: list[str]
    missing_data: list[str]
    penalties: list[str]
    relaxation_notes: list[str]
    evidence_citations: list[str]
    offer_comparison_note: str | None = None


class RelaxationSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_index: int
    field: str
    original_value: str
    relaxed_to: str
    reason: str
    eligible_count_after: int


class RankedOfferSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rank: int
    item_id: str
    title: str
    price: Decimal
    currency: str
    estimated_price_brl: EstimatedPriceSchema | None = None
    condition: str | None = None
    brand: str | None = None
    model: str | None = None
    image_url: str | None = None
    item_web_url: str | None = None
    seller: SellerSchema
    shipping: ShippingSchema
    details_loaded: bool
    is_best_offer_in_group: bool
    semantic_score: float | None = None
    approval_state: str
    score_breakdown: ScoreBreakdownSchema
    validation: OfferValidationSchema
    explanation: ExplanationSchema


class RankedProductGroupSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rank: int
    product_group_id: str
    canonical_title: str
    detected_category: str | None = None
    approval_state: str
    offer_count: int
    score_breakdown: ScoreBreakdownSchema
    explanation: ExplanationSchema
    offers: list[RankedOfferSchema]


class SearchResponseSchema(BaseModel):
    """Contrato HTTP de saída para POST /v1/search."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "clarification_needed", "no_results", "all_rejected"]
    request_id: str
    profile_id: str | None = None
    detected_category: str | None = None
    clarification_needed: bool = False
    clarification_question: str | None = None
    total_products: int = 0
    total_offers: int = 0
    rejected_count: int = 0
    relaxed: bool = False
    relaxations: list[RelaxationSchema] = Field(default_factory=list)
    exchange_rate_info: ExchangeRateSchema | None = None
    warnings: list[str] = Field(default_factory=list)
    products: list[RankedProductGroupSchema] = Field(default_factory=list)


class ChatConversationContextSchema(BaseModel):
    """Metadados seguros e não persistentes para continuidade do cliente."""

    model_config = ConfigDict(extra="forbid")

    detected_category: str | None = None
    profile_id: str | None = None
    applied_budget_brl: Decimal | None = None
    clarification_needed: bool = False
    relaxed: bool = False
    assistant_turn_summary: str


class ChatResponse(BaseModel):
    """Contrato HTTP stateless de saída para POST /v1/chat."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "clarification_needed", "no_results", "all_rejected"]
    request_id: str
    assistant_message: str
    clarification_needed: bool = False
    clarification_question: str | None = None
    search_results: SearchResponseSchema | None = None
    conversation_context: ChatConversationContextSchema
    warnings: list[str] = Field(default_factory=list)


def _convert_score_breakdown(sb: ScoreBreakdown) -> ScoreBreakdownSchema:
    return ScoreBreakdownSchema(
        final_score=sb.final_score,
        weighted_sum=sb.weighted_sum,
        evidence_coverage=sb.evidence_coverage,
        confidence_factor=sb.confidence_factor,
        contradiction_count=sb.contradiction_count,
        contradiction_penalty=sb.contradiction_penalty,
        semantic_enabled=sb.semantic_enabled,
        semantic_weight_applied=sb.semantic_weight_applied,
        total_cost_brl=sb.total_cost_brl,
        shipping_known=sb.shipping_known,
        criteria=[
            CriterionScoreSchema(
                name=c.name.value,
                raw_value=c.raw_value,
                normalized_score=c.normalized_score,
                available=c.available,
                not_applicable=c.not_applicable,
                base_weight=c.base_weight,
                effective_weight=c.effective_weight,
                weighted_contribution=c.weighted_contribution,
                evidence_sources=list(c.evidence_sources),
                note=c.note,
            )
            for c in sb.criteria
        ],
    )


def _convert_explanation(exp: object) -> ExplanationSchema:
    return ExplanationSchema(
        summary=exp.summary,  # type: ignore[attr-defined]
        rank=exp.rank,  # type: ignore[attr-defined]
        total_score=exp.total_score,  # type: ignore[attr-defined]
        reasons=list(exp.reasons),  # type: ignore[attr-defined]
        tradeoffs=list(exp.tradeoffs),  # type: ignore[attr-defined]
        warnings=list(exp.warnings),  # type: ignore[attr-defined]
        missing_data=list(exp.missing_data),  # type: ignore[attr-defined]
        penalties=list(exp.penalties),  # type: ignore[attr-defined]
        relaxation_notes=list(exp.relaxation_notes),  # type: ignore[attr-defined]
        evidence_citations=list(exp.evidence_citations),  # type: ignore[attr-defined]
        offer_comparison_note=exp.offer_comparison_note,  # type: ignore[attr-defined]
    )


def _convert_offer(
    offer: RankedOffer,
    *,
    converter: CurrencyConverter | None,
) -> RankedOfferSchema:
    cand = offer.candidate
    val = offer.validation

    est_brl: EstimatedPriceSchema | None = None
    if cand.currency == "USD" and converter is not None:
        converted = converter.usd_to_brl(cand.price)
        est_brl = EstimatedPriceSchema(
            amount=converted.amount,
            currency=converted.currency,
            is_estimate=True,
            rate=converted.rate,
            source=converted.source,
            updated_at=converted.updated_at,
        )

    seller_schema = (
        SellerSchema(
            known=True,
            username=cand.seller.username,
            feedback_score=cand.seller.feedback_score,
            feedback_percentage=cand.seller.feedback_percentage,
        )
        if cand.seller is not None
        else SellerSchema(known=False)
    )

    shipping_schema = (
        ShippingSchema(
            known=True,
            cost=cand.shipping.cost,
            currency=cand.shipping.currency,
            free_shipping=cand.shipping.free_shipping,
            estimated_min_days=cand.shipping.estimated_min_days,
            estimated_max_days=cand.shipping.estimated_max_days,
        )
        if cand.shipping is not None
        else ShippingSchema(known=False)
    )

    criteria_schemas = [
        CriterionEvaluationSchema(
            criterion_id=ev.criterion_id,
            criterion_label=ev.criterion_label,
            priority=ev.priority,
            status=ev.status,
            evidence=ev.evidence,
            source_field=ev.source_field,
            insufficient_information=ev.insufficient_information,
            note=ev.note,
        )
        for ev in val.evaluations
    ]
    unknown_criteria = [ev.criterion_id for ev in val.evaluations if ev.status == "unknown"]

    return RankedOfferSchema(
        rank=offer.rank,
        item_id=offer.item_id,
        title=cand.title,
        price=cand.price,
        currency=cand.currency,
        estimated_price_brl=est_brl,
        condition=cand.condition,
        brand=cand.brand,
        model=cand.model,
        image_url=cand.image_url,
        item_web_url=cand.item_web_url,
        seller=seller_schema,
        shipping=shipping_schema,
        details_loaded=cand.details_loaded,
        is_best_offer_in_group=offer.is_best_offer_in_group,
        semantic_score=offer.semantic_score,
        approval_state=offer.approval_state.value,
        score_breakdown=_convert_score_breakdown(offer.score_breakdown),
        validation=OfferValidationSchema(
            eligible=val.eligible,
            strictly_verified=val.strictly_verified,
            approval_state=offer.approval_state.value,
            detected_category=val.detected_category,
            profile_id=val.profile_id,
            hard_satisfied_count=val.hard_satisfied_count,
            hard_not_satisfied_count=val.hard_not_satisfied_count,
            hard_unknown_count=val.hard_unknown_count,
            soft_satisfied_count=val.soft_satisfied_count,
            soft_not_satisfied_count=val.soft_not_satisfied_count,
            soft_unknown_count=val.soft_unknown_count,
            contradictions={k: list(v) for k, v in val.contradictions.items()},
            unknown_criteria=unknown_criteria,
            criteria=criteria_schemas,
        ),
        explanation=_convert_explanation(offer.explanation),
    )


def _convert_group(
    group: RankedProductGroup,
    *,
    converter: CurrencyConverter | None,
) -> RankedProductGroupSchema:
    return RankedProductGroupSchema(
        rank=group.rank,
        product_group_id=group.product_group_id,
        canonical_title=group.canonical_title,
        detected_category=group.detected_category,
        approval_state=group.approval_state.value,
        offer_count=group.offer_count,
        score_breakdown=_convert_score_breakdown(group.score_breakdown),
        explanation=_convert_explanation(group.explanation),
        offers=[_convert_offer(offer, converter=converter) for offer in group.offers],
    )


def _map_status(status: ExecutionStatus) -> Literal["ok", "clarification_needed", "no_results", "all_rejected"]:
    if status == ExecutionStatus.SUCCESS:
        return "ok"
    if status == ExecutionStatus.CLARIFICATION_NEEDED:
        return "clarification_needed"
    if status == ExecutionStatus.ALL_REJECTED:
        return "all_rejected"
    return "no_results"


def build_search_response(
    result: OrchestrationResult,
    *,
    request_id: str,
    limit: int,
    converter: CurrencyConverter | None = None,
) -> SearchResponseSchema:
    """Converte OrchestrationResult (Etapa 7) para SearchResponseSchema sem vazar estado interno."""
    rank_res = result.ranking_result
    val_res = result.validation_result

    rate_schema: ExchangeRateSchema | None = None
    if converter is not None:
        info = converter.rate_info()
        rate_schema = ExchangeRateSchema(
            base_currency=info.base_currency,
            quote_currency=info.quote_currency,
            rate=info.rate,
            updated_at=info.updated_at,
            source=info.source,
            is_estimate=info.is_estimate,
        )

    sliced_groups = list(rank_res.ranked_groups[:limit]) if rank_res is not None else []
    products = [_convert_group(grp, converter=converter) for grp in sliced_groups]
    total_offers = sum(len(grp.offers) for grp in products)

    relaxation_steps = (
        rank_res.relaxations
        if rank_res is not None and rank_res.relaxations
        else (val_res.relaxations if val_res is not None else ())
    )
    relaxations = [
        RelaxationSchema(
            step_index=step.step_index,
            field=step.field,
            original_value=step.original_value,
            relaxed_to=step.relaxed_to,
            reason=step.reason,
            eligible_count_after=step.eligible_count_after,
        )
        for step in relaxation_steps
    ]

    warnings: list[str] = []
    if rate_schema is not None and any(
        offer.estimated_price_brl is not None for grp in products for offer in grp.offers
    ):
        warnings.append(
            "Valores em BRL são estimativas baseadas na taxa de câmbio configurada e não incluem tributos ou taxas de importação."
        )
    if any(not offer.shipping.known for grp in products for offer in grp.offers):
        warnings.append(
            "Um ou mais anúncios não informam frete; quando desconhecido, o custo total não é assumido como grátis."
        )
    if any(offer.validation.unknown_criteria for grp in products for offer in grp.offers):
        warnings.append(
            "Critérios sem evidência verificável permanecem marcados como 'unknown' sem suposições."
        )
    if result.status == ExecutionStatus.ALL_REJECTED:
        warnings.append(
            "Nenhum candidato atendeu às restrições obrigatórias após validação e relaxamento controlado."
        )

    active_profile_id = (
        rank_res.active_profile_id
        if rank_res is not None and rank_res.active_profile_id is not None
        else (val_res.active_profile_id if val_res is not None else None)
    )
    detected_category = (
        rank_res.detected_category
        if rank_res is not None and rank_res.detected_category is not None
        else (result.search_plan.canonical_category if result.search_plan is not None else None)
    )
    rejected_count = (
        rank_res.rejected_count
        if rank_res is not None
        else (len(val_res.rejected_candidates) if val_res is not None else 0)
    )
    relaxed = bool(
        (rank_res.relaxed if rank_res is not None else False)
        or (val_res.relaxed if val_res is not None else False)
        or relaxations
    )

    return SearchResponseSchema(
        status=_map_status(result.status),
        request_id=request_id,
        profile_id=active_profile_id,
        detected_category=detected_category,
        clarification_needed=result.clarification_needed,
        clarification_question=result.clarification_question,
        total_products=len(products),
        total_offers=total_offers,
        rejected_count=rejected_count,
        relaxed=relaxed,
        relaxations=relaxations,
        exchange_rate_info=rate_schema,
        warnings=warnings,
        products=products,
    )
