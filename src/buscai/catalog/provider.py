"""Interface usada para substituir a origem dos produtos."""

from __future__ import annotations

from typing import Protocol

from .models import ProductPage


class CatalogProvider(Protocol):
    def list_products(self, *, limit: int = 30, skip: int = 0) -> ProductPage:
        """Retorna uma página de produtos normalizados."""
        ...
