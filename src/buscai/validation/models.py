"""Contratos imutáveis da camada de validação por evidências e relaxamento controlado (Etapa 5)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from buscai.pricing import ExchangeRateInfo
from buscai.search.models import Candidate
from buscai.semantic.dedup import DuplicateGroup

ValidationStatus = Literal["satisfied", "not_satisfied", "unknown", "not_applicable"]
ConstraintPriority = Literal["hard", "soft"]


class ExtractedEvidence(BaseModel):
    """Evidência concreta extraída de um campo real do `Candidate`."""

    model_config = ConfigDict(frozen=True)

    attribute: str
    value: Any
    source_field: str
    raw_text: str | None = None
    from_get_item: bool = False


class CandidateEvidenceMap(BaseModel):
    """Mapa de evidências observadas e eventuais contradições internas do anúncio."""

    model_config = ConfigDict(frozen=True)

    item_id: str
    canonical_category: str | None = None
    evidences: dict[str, ExtractedEvidence] = Field(default_factory=dict)
    contradictions: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    details_loaded: bool = False


class CriterionEvaluation(BaseModel):
    """Resultado auditável da avaliação de um único critério sobre um candidato."""

    model_config = ConfigDict(frozen=True)

    criterion_id: str
    criterion_label: str
    priority: ConstraintPriority
    status: ValidationStatus
    evidence: str | None = None
    source_field: str | None = None
    insufficient_information: bool = False
    note: str | None = None

    @model_validator(mode="after")
    def _enforce_unknown_has_no_invented_evidence(self) -> CriterionEvaluation:
        if self.status == "unknown":
            if self.evidence is not None or self.source_field is not None or not self.insufficient_information:
                raise ValueError(
                    "Critério com status 'unknown' não pode conter evidência inventada e deve marcar insufficient_information=True."
                )
        return self


class BudgetEvaluation(BaseModel):
    """Avaliação financeira determinística com `Decimal`, câmbio transparente e frete."""

    model_config = ConfigDict(frozen=True)

    status: ValidationStatus
    priority: ConstraintPriority
    budget_currency: str | None = None
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    min_inclusive: bool = True
    max_inclusive: bool = True
    item_price_in_budget_currency: Decimal | None = None
    shipping_cost_in_budget_currency: Decimal | None = None
    total_price_in_budget_currency: Decimal | None = None
    shipping_known: bool = False
    is_estimated_conversion: bool = False
    exchange_rate: ExchangeRateInfo | None = None
    note: str | None = None


class CandidateValidationResult(BaseModel):
    """Veredito estruturado por evidências de um candidato preservando todos os seus dados."""

    model_config = ConfigDict(frozen=True)

    candidate: Candidate
    eligible: bool
    strictly_verified: bool
    needs_enrichment: bool
    detected_category: str | None = None
    profile_id: str | None = None
    evaluations: tuple[CriterionEvaluation, ...] = ()
    budget_evaluation: BudgetEvaluation | None = None
    hard_satisfied_count: int = Field(default=0, ge=0)
    hard_not_satisfied_count: int = Field(default=0, ge=0)
    hard_unknown_count: int = Field(default=0, ge=0)
    soft_satisfied_count: int = Field(default=0, ge=0)
    soft_not_satisfied_count: int = Field(default=0, ge=0)
    soft_unknown_count: int = Field(default=0, ge=0)
    contradictions: dict[str, tuple[str, ...]] = Field(default_factory=dict)


class EnrichmentSummary(BaseModel):
    """Resumo operacional do enriquecimento seletivo via `getItem`."""

    model_config = ConfigDict(frozen=True)

    selected_item_ids: tuple[str, ...] = ()
    enriched_item_ids: tuple[str, ...] = ()
    unchanged_item_ids: tuple[str, ...] = ()
    skipped_due_to_budget: int = Field(default=0, ge=0)
    partial_failures: int = Field(default=0, ge=0)
    calls_used: int = Field(default=0, ge=0)


class RelaxationStep(BaseModel):
    """Registro transparente de uma preferência flexível relaxada."""

    model_config = ConfigDict(frozen=True)

    step_index: int = Field(ge=1)
    field: str
    original_value: str
    relaxed_to: str
    reason: str
    triggered_search: bool = False
    eligible_count_after: int = Field(default=0, ge=0)


class ValidationPipelineResult(BaseModel):
    """Saída completa da Etapa 5 pronta para consumo pelo reranking da Etapa 6."""

    model_config = ConfigDict(frozen=True)

    eligible_candidates: tuple[CandidateValidationResult, ...] = ()
    rejected_candidates: tuple[CandidateValidationResult, ...] = ()
    all_validations: tuple[CandidateValidationResult, ...] = ()
    product_groups: tuple[DuplicateGroup, ...] = ()
    active_profile_id: str | None = None
    enrichment: EnrichmentSummary = Field(default_factory=EnrichmentSummary)
    relaxations: tuple[RelaxationStep, ...] = ()
    relaxed: bool = False
    hard_constraints_preserved: bool = True
    total_candidates_evaluated: int = Field(default=0, ge=0)


__all__ = [
    "BudgetEvaluation",
    "CandidateEvidenceMap",
    "CandidateValidationResult",
    "ConstraintPriority",
    "CriterionEvaluation",
    "EnrichmentSummary",
    "ExtractedEvidence",
    "RelaxationStep",
    "ValidationPipelineResult",
    "ValidationStatus",
]
