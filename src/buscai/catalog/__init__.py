"""Contratos e fornecedores de catálogo."""

from .errors import (
    CatalogError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from .models import Product, ProductPage
from .provider import CatalogProvider

__all__ = [
    "CatalogError",
    "CatalogProvider",
    "Product",
    "ProductPage",
    "ProviderAuthError",
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
]
