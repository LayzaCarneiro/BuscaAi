from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PriceConstraint(BaseModel):
    """Price restriction extracted from the user's request.

    A null bound means that side of the range was not specified.
    The inclusive flags matter for phrases such as "menos de R$ 2.500".
    """

    model_config = ConfigDict(extra="forbid")

    min_price: Optional[float] = Field(default=None, ge=0)
    max_price: Optional[float] = Field(default=None, ge=0)
    min_inclusive: bool = True
    max_inclusive: bool = True
    is_hard_constraint: bool = False

    @field_validator("max_price")
    @classmethod
    def validate_price_range(cls, v: Optional[float], info):
        min_price = info.data.get("min_price")
        if v is not None and min_price is not None and v < min_price:
            raise ValueError("max_price must be >= min_price")
        return v


class UserIntent(BaseModel):
    """Canonical representation of a natural-language shopping request."""

    model_config = ConfigDict(extra="forbid")

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
    query: str = Field(
        min_length=1,
        description="Short semantic search query, without inventing facts.",
    )
    clarification_needed: bool = False
    clarification_question: Optional[str] = None

    @field_validator("brands", "excluded_brands", "use_cases", "profile_context", "required_features", "preferred_features", "excluded_features")
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
