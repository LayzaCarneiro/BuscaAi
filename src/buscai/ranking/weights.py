"""Profile-driven weight matrices and category overrides for Stage 6 reranking."""

from __future__ import annotations

from dataclasses import dataclass

from buscai.catalog_registry import ProfileRegistry, load_default_registry
from buscai.ranking.models import CriterionName

MAX_SEMANTIC_WEIGHT = 0.15
DEFAULT_PROFILE_ID = "best_value"


@dataclass(frozen=True)
class WeightVector:
    """Validated weight distribution over all ranking criteria."""

    weights: dict[CriterionName, float]

    def __post_init__(self) -> None:
        missing = [c for c in CriterionName if c not in self.weights]
        if missing:
            raise ValueError(f"WeightVector missing criteria: {missing}")
        for criterion, value in self.weights.items():
            if value < 0.0:
                raise ValueError(f"Negative weight for {criterion.value}: {value}")
        semantic_weight = self.weights[CriterionName.SEMANTIC_RELEVANCE]
        if semantic_weight > MAX_SEMANTIC_WEIGHT + 1e-9:
            raise ValueError(
                f"semantic_relevance weight {semantic_weight} exceeds cap {MAX_SEMANTIC_WEIGHT}"
            )
        total = sum(self.weights.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"WeightVector must sum to 1.0, got {total:.6f}")

    def get(self, criterion: CriterionName) -> float:
        """Return the base weight for ``criterion``."""
        return self.weights[criterion]

    def without_semantic(self) -> WeightVector:
        """Return a new WeightVector with semantic_relevance zeroed and renormalized."""
        remaining = {
            k: (0.0 if k is CriterionName.SEMANTIC_RELEVANCE else v)
            for k, v in self.weights.items()
        }
        total = sum(remaining.values())
        if total <= 0.0:
            raise ValueError("Cannot renormalize WeightVector with zero non-semantic weight")
        normalized = {k: v / total for k, v in remaining.items()}
        return WeightVector(weights=normalized)

    def with_semantic_cap(self, max_weight: float) -> WeightVector:
        """Clamp semantic_relevance to ``max_weight`` and renormalize remaining weights."""
        if max_weight < 0.0:
            raise ValueError("max_weight must be >= 0.0")
        if max_weight == 0.0:
            return self.without_semantic()
        current_sem = self.weights[CriterionName.SEMANTIC_RELEVANCE]
        capped_sem = min(current_sem, max_weight, MAX_SEMANTIC_WEIGHT)
        if abs(capped_sem - current_sem) <= 1e-9:
            return self
        non_sem_total = sum(
            v for k, v in self.weights.items() if k is not CriterionName.SEMANTIC_RELEVANCE
        )
        if non_sem_total <= 0.0:
            raise ValueError("Cannot redistribute weight without non-semantic criteria")
        scale = (1.0 - capped_sem) / non_sem_total
        updated = {
            k: (capped_sem if k is CriterionName.SEMANTIC_RELEVANCE else v * scale)
            for k, v in self.weights.items()
        }
        return WeightVector(weights=updated)


def _make_vector(
    *,
    cost_value: float,
    budget_fit: float,
    seller_reliability: float,
    condition_quality: float,
    technical_fit: float,
    portability: float,
    logistics: float,
    semantic_relevance: float,
) -> WeightVector:
    return WeightVector(
        weights={
            CriterionName.COST_VALUE: cost_value,
            CriterionName.BUDGET_FIT: budget_fit,
            CriterionName.SELLER_RELIABILITY: seller_reliability,
            CriterionName.CONDITION_QUALITY: condition_quality,
            CriterionName.TECHNICAL_FIT: technical_fit,
            CriterionName.PORTABILITY: portability,
            CriterionName.LOGISTICS: logistics,
            CriterionName.SEMANTIC_RELEVANCE: semantic_relevance,
        }
    )


PROFILE_WEIGHTS: dict[str, WeightVector] = {
    "best_value": _make_vector(
        cost_value=0.28,
        budget_fit=0.15,
        seller_reliability=0.12,
        condition_quality=0.10,
        technical_fit=0.22,
        portability=0.00,
        logistics=0.05,
        semantic_relevance=0.08,
    ),
    "best_performance": _make_vector(
        cost_value=0.08,
        budget_fit=0.05,
        seller_reliability=0.12,
        condition_quality=0.15,
        technical_fit=0.45,
        portability=0.00,
        logistics=0.05,
        semantic_relevance=0.10,
    ),
    "lowest_price": _make_vector(
        cost_value=0.42,
        budget_fit=0.24,
        seller_reliability=0.10,
        condition_quality=0.06,
        technical_fit=0.03,
        portability=0.00,
        logistics=0.10,
        semantic_relevance=0.05,
    ),
    "professional_use": _make_vector(
        cost_value=0.06,
        budget_fit=0.08,
        seller_reliability=0.22,
        condition_quality=0.18,
        technical_fit=0.34,
        portability=0.00,
        logistics=0.06,
        semantic_relevance=0.06,
    ),
    "study": _make_vector(
        cost_value=0.24,
        budget_fit=0.14,
        seller_reliability=0.06,
        condition_quality=0.08,
        technical_fit=0.20,
        portability=0.22,
        logistics=0.00,
        semantic_relevance=0.06,
    ),
    "everyday_use": _make_vector(
        cost_value=0.24,
        budget_fit=0.14,
        seller_reliability=0.18,
        condition_quality=0.18,
        technical_fit=0.12,
        portability=0.00,
        logistics=0.08,
        semantic_relevance=0.06,
    ),
}

# Category + profile overrides for the 4 MVP categories.
CATEGORY_PROFILE_OVERRIDES: dict[tuple[str, str], WeightVector] = {
    ("monitor", "study"): _make_vector(
        cost_value=0.28,
        budget_fit=0.18,
        seller_reliability=0.12,
        condition_quality=0.10,
        technical_fit=0.20,
        portability=0.00,
        logistics=0.06,
        semantic_relevance=0.06,
    ),
    ("notebook", "study"): _make_vector(
        cost_value=0.22,
        budget_fit=0.14,
        seller_reliability=0.08,
        condition_quality=0.08,
        technical_fit=0.18,
        portability=0.24,
        logistics=0.00,
        semantic_relevance=0.06,
    ),
    ("fone de ouvido", "everyday_use"): _make_vector(
        cost_value=0.22,
        budget_fit=0.12,
        seller_reliability=0.14,
        condition_quality=0.16,
        technical_fit=0.12,
        portability=0.12,
        logistics=0.06,
        semantic_relevance=0.06,
    ),
    ("smartphone", "everyday_use"): _make_vector(
        cost_value=0.22,
        budget_fit=0.14,
        seller_reliability=0.16,
        condition_quality=0.16,
        technical_fit=0.12,
        portability=0.08,
        logistics=0.06,
        semantic_relevance=0.06,
    ),
}

# Level-2 offer ranking weights within a single product group.
OFFER_RANKING_WEIGHTS: WeightVector = _make_vector(
    cost_value=0.45,
    budget_fit=0.15,
    seller_reliability=0.20,
    condition_quality=0.12,
    technical_fit=0.00,
    portability=0.00,
    logistics=0.08,
    semantic_relevance=0.00,
)


WEIGHT_RATIONALE: dict[str, str] = {
    "best_value": "Equilibra custo-benefício (0.28), adequação técnica (0.22) e orçamento (0.15) sem escolher apenas o mais barato.",
    "best_performance": "Prioriza especificações técnicas comprovadas (0.45), condição (0.15) e confiabilidade (0.12), com baixo peso de preço.",
    "lowest_price": "Prioriza menor custo (0.42), ajuste ao orçamento (0.24) e frete/logística (0.10).",
    "professional_use": "Prioriza requisitos técnicos (0.34), reputação do vendedor (0.22) e condição (0.18) para confiabilidade profissional.",
    "study": "Equilibra custo (0.24), portabilidade (0.22), especificações (0.20) e orçamento (0.14), com override para categorias não portáteis.",
    "everyday_use": "Equilibra custo (0.24), condição (0.18), reputação do vendedor (0.18) e praticidade cotidiana.",
}


class WeightRegistry:
    """Resolves profile and category-specific weight vectors via ProfileRegistry."""

    def __init__(
        self,
        *,
        profile_registry: ProfileRegistry | None = None,
        profile_weights: dict[str, WeightVector] | None = None,
        category_overrides: dict[tuple[str, str], WeightVector] | None = None,
    ) -> None:
        self._profile_registry = profile_registry or load_default_registry()
        self._profile_weights = dict(profile_weights or PROFILE_WEIGHTS)
        self._category_overrides = dict(category_overrides or CATEGORY_PROFILE_OVERRIDES)

    def with_profile_weights(self, profile_id: str, vector: WeightVector) -> WeightRegistry:
        """Return a new WeightRegistry with custom/additional profile weights."""
        updated = dict(self._profile_weights)
        updated[profile_id.strip().lower()] = vector
        return WeightRegistry(
            profile_registry=self._profile_registry,
            profile_weights=updated,
            category_overrides=self._category_overrides,
        )

    def with_category_profile_weights(
        self,
        category_id: str,
        profile_id: str,
        vector: WeightVector,
    ) -> WeightRegistry:
        """Return a new WeightRegistry with custom/additional category+profile weights."""
        updated = dict(self._category_overrides)
        updated[(category_id.strip().lower(), profile_id.strip().lower())] = vector
        return WeightRegistry(
            profile_registry=self._profile_registry,
            profile_weights=self._profile_weights,
            category_overrides=updated,
        )

    def get_group_weights(
        self,
        profile_id: str | None,
        category_id: str | None = None,
        *,
        enable_semantic_score: bool = True,
        semantic_max_weight: float = MAX_SEMANTIC_WEIGHT,
    ) -> WeightVector:
        """Return the WeightVector for Level-1 product group ranking."""
        resolved_prof_obj = self._profile_registry.resolve_profile(profile_id)
        resolved_profile = (
            resolved_prof_obj.profile_id
            if resolved_prof_obj is not None
            else (profile_id or DEFAULT_PROFILE_ID).strip().lower()
        )
        if resolved_profile not in self._profile_weights:
            resolved_profile = DEFAULT_PROFILE_ID

        resolved_cat_obj = self._profile_registry.resolve_category(category_id)
        resolved_category = (
            resolved_cat_obj.canonical_id.strip().lower()
            if resolved_cat_obj is not None
            else (category_id or "").strip().lower()
        )
        vector = self._category_overrides.get(
            (resolved_category, resolved_profile),
            self._profile_weights[resolved_profile],
        )
        if not enable_semantic_score:
            return vector.without_semantic()
        return vector.with_semantic_cap(semantic_max_weight)

    def get_offer_weights(self) -> WeightVector:
        """Return the WeightVector for Level-2 offer ranking inside a group."""
        return OFFER_RANKING_WEIGHTS
