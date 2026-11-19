"""Protocolo único para fontes de busca de produtos orientadas a query e filtros."""

from __future__ import annotations

from typing import Protocol

from .models import Candidate, SearchQuery, SearchResponse
from .quota import RequestCallBudget


class SearchBackend(Protocol):
    """Abstração de busca em marketplace sem depender de detalhes HTTP do provedor."""

    def search(
        self,
        query: SearchQuery,
        *,
        budget: RequestCallBudget | None = None,
        request_id: str | None = None,
    ) -> SearchResponse:
        """Executa uma busca limitada de candidatos em memória."""
        ...

    def get_item_details(
        self,
        item_id: str,
        *,
        budget: RequestCallBudget | None = None,
        request_id: str | None = None,
    ) -> Candidate | None:
        """Recupera detalhes completos de um item específico quando necessário."""
        ...
