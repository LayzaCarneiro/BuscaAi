from __future__ import annotations

import math
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_CURRENCY_ALIASES: dict[str, str] = {
    "brl": "BRL",
    "r$": "BRL",
    "real": "BRL",
    "reais": "BRL",
    "usd": "USD",
    "us$": "USD",
    "$": "USD",
    "dolar": "USD",
    "dólar": "USD",
    "dolares": "USD",
    "dólares": "USD",
    "eur": "EUR",
    "euro": "EUR",
    "euros": "EUR",
    "€": "EUR",
}


def _reject_bool_numeric(value: object) -> object:
    if isinstance(value, bool):
        raise ValueError("Numeric price bound cannot be boolean.")
    return value


def _validate_finite_bound(value: Optional[float]) -> Optional[float]:
    if value is not None and not math.isfinite(value):
        raise ValueError("Numeric price bound must be finite.")
    return value


def _clean_optional_text(value: object) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Optional text field must be a string.")
    cleaned = " ".join(value.strip().split())
    return cleaned if cleaned else None


class PriceConstraint(BaseModel):
    """Price restriction extracted from the user's request.

    A null bound means that side of the range was not specified.
    The inclusive flags matter for phrases such as "menos de R$ 2.500".
    """

    # Ignora campos extras para manter compatibilidade com response_schema do Gemini
    # (que rejeita additionalProperties) e evitar falhas por propriedades extras do LLM.
    model_config = ConfigDict(extra="ignore")

    min_price: Optional[float] = Field(default=None, ge=0)
    max_price: Optional[float] = Field(default=None, ge=0)
    min_inclusive: bool = True
    max_inclusive: bool = True
    is_hard_constraint: bool = False
    currency: Optional[str] = Field(
        default=None,
        description="Explicit ISO-3 currency code (e.g. BRL, USD), or null if not stated.",
    )

    @field_validator("min_price", "max_price", mode="before")
    @classmethod
    def _reject_bool_price(cls, value: object) -> object:
        return _reject_bool_numeric(value)

    @field_validator("min_price", "max_price", mode="after")
    @classmethod
    def _validate_finite_price(cls, value: Optional[float]) -> Optional[float]:
        return _validate_finite_bound(value)

    @field_validator("currency", mode="before")
    @classmethod
    def _normalize_currency(cls, value: object) -> Optional[str]:
        cleaned = _clean_optional_text(value)
        if cleaned is None:
            return None
        alias = _CURRENCY_ALIASES.get(cleaned.casefold())
        if alias is not None:
            return alias
        upper = cleaned.upper()
        if len(upper) == 3 and upper.isascii() and upper.isalpha():
            return upper
        raise ValueError("currency must be a valid 3-letter ISO currency code (e.g. BRL, USD).")

    @field_validator("max_price")
    @classmethod
    def validate_price_range(cls, v: Optional[float], info):
        min_price = info.data.get("min_price")
        if v is not None and min_price is not None and v < min_price:
            raise ValueError("max_price must be >= min_price")
        return v

    @model_validator(mode="after")
    def _normalize_empty_bounds(self) -> "PriceConstraint":
        """Garante valores neutros quando nenhum limite numérico de preço foi definido."""
        if self.min_price is None and self.max_price is None:
            self.min_inclusive = True
            self.max_inclusive = True
            self.is_hard_constraint = False
        return self


class UserIntent(BaseModel):
    """Canonical representation of a natural-language shopping request."""

    # Ignora campos extras pelo mesmo motivo de PriceConstraint (compatibilidade com Gemini).
    model_config = ConfigDict(extra="ignore")

    category: Optional[str] = Field(
        default=None,
        description="Main product category. Null if the user did not provide enough information.",
    )
    price: PriceConstraint = Field(
        default_factory=PriceConstraint,
        description="Explicit price/budget constraints only; do not invent numeric bounds.",
    )
    brands: list[str] = Field(
        default_factory=list,
        description="Brands explicitly requested or strongly preferred by the user.",
    )
    excluded_brands: list[str] = Field(default_factory=list)
    models: list[str] = Field(
        default_factory=list,
        description="Product models or lines explicitly named by the user (e.g. iPhone, ThinkPad). Never invent.",
    )
    use_cases: list[str] = Field(
        default_factory=list,
        description="Canonical use cases such as programação, estudos, jogos, fotografia.",
    )
    profile_context: list[str] = Field(
        default_factory=list,
        description="Useful user-profile context explicitly stated, e.g. estudante de computação.",
    )
    required_features: list[str] = Field(default_factory=list)
    preferred_features: list[str] = Field(default_factory=list)
    excluded_features: list[str] = Field(default_factory=list)
    condition: Optional[str] = Field(
        default=None,
        description="Explicit product condition requested (novo, usado, recondicionado, open box) or null.",
    )
    query: str = Field(
        min_length=1,
        description="Short semantic search query, without inventing facts.",
    )
    clarification_needed: bool = False
    clarification_question: Optional[str] = None

    @field_validator("category", "condition", "clarification_question", mode="before")
    @classmethod
    def _clean_optional_fields(cls, value: object) -> Optional[str]:
        return _clean_optional_text(value)

    @field_validator("query", mode="before")
    @classmethod
    def _clean_query(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("query must be a string.")
        return " ".join(value.strip().split())

    @field_validator(
        "brands",
        "excluded_brands",
        "models",
        "use_cases",
        "profile_context",
        "required_features",
        "preferred_features",
        "excluded_features",
    )
    @classmethod
    def clean_list(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for value in values:
            value = " ".join(value.strip().split())
            if not value:
                continue
            key = value.casefold()
            if key not in seen:
                seen.add(key)
                cleaned.append(value)
        return cleaned
