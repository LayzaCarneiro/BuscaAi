"""Camada de validação por evidências, perfis de uso, getItem seletivo e relaxamento controlado (Etapa 5)."""

from .enrichment import SelectiveEnricher
from .evidence import extract_candidate_evidences, match_model_tokens_in_candidate
from .models import (
    BudgetEvaluation,
    CandidateEvidenceMap,
    CandidateValidationResult,
    ConstraintPriority,
    CriterionEvaluation,
    EnrichmentSummary,
    ExtractedEvidence,
    RelaxationStep,
    ValidationPipelineResult,
    ValidationStatus,
)
from .pipeline import ValidationPipeline
from .registry import (
    CategoryDefinition,
    DEFAULT_PROFILES_PATH,
    ProfileCriterion,
    ProfileRegistry,
    UsageProfile,
    load_default_registry,
)
from .relaxation import RelaxationConfig, RelaxationEngine
from .validator import EvidenceValidator, evaluate_budget

__all__ = [
    "BudgetEvaluation",
    "CandidateEvidenceMap",
    "CandidateValidationResult",
    "CategoryDefinition",
    "ConstraintPriority",
    "CriterionEvaluation",
    "DEFAULT_PROFILES_PATH",
    "EnrichmentSummary",
    "EvidenceValidator",
    "ExtractedEvidence",
    "ProfileCriterion",
    "ProfileRegistry",
    "RelaxationConfig",
    "RelaxationEngine",
    "RelaxationStep",
    "SelectiveEnricher",
    "UsageProfile",
    "ValidationPipeline",
    "ValidationPipelineResult",
    "ValidationStatus",
    "evaluate_budget",
    "extract_candidate_evidences",
    "load_default_registry",
    "match_model_tokens_in_candidate",
]
