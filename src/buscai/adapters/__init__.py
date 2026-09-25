"""Tradução da intenção de compra para consultas ao catálogo."""

from .intent import (
    ClarificationRequired,
    ConstraintUnavailableError,
    IntentSearchCriteria,
    IntentSearchResult,
    adapt_intent,
    search_intent,
)

__all__ = [
    "ClarificationRequired",
    "ConstraintUnavailableError",
    "IntentSearchCriteria",
    "IntentSearchResult",
    "adapt_intent",
    "search_intent",
]
