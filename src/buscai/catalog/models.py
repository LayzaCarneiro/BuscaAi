"""Modelos compartilhados por diferentes fontes de catálogo."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class Product:
    id: str
    source: str
    title: str
    price: Decimal
    currency: str | None
    category: str | None
    attributes: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ProductPage:
    products: tuple[Product, ...]
    total: int
    skip: int
    limit: int
