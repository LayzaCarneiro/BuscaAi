"""Contratos e fornecedores de catálogo."""

from .models import Product, ProductPage
from .provider import CatalogProvider

__all__ = ["CatalogProvider", "Product", "ProductPage"]
