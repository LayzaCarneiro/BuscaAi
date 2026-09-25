"""Liga extração de intenção à busca no catálogo, sem gerar recomendação."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from buscai.adapters import IntentSearchResult, search_intent
from buscai.catalog import CatalogProvider
from userintent.schema import UserIntent


class IntentExtractorProtocol(Protocol):
    def extract(self, user_text: str) -> UserIntent:
        """Converte texto do usuário em UserIntent validado."""
        ...


@dataclass(frozen=True)
class SearchFlowResult:
    intent: UserIntent
    search: IntentSearchResult | None
    clarification_question: str | None


def run_search_flow(
    user_text: str,
    *,
    extractor: IntentExtractorProtocol | None = None,
    catalog: CatalogProvider | None = None,
    category_map: Mapping[str, str] | None = None,
    budget_currency: str | None = None,
    limit: int = 10,
    offset: int = 0,
) -> SearchFlowResult:
    """Extrai a intenção e busca produtos ou encaminha esclarecimento."""
    if not isinstance(user_text, str) or not user_text.strip():
        raise ValueError("A consulta do usuário deve conter texto.")

    if extractor is None:
        # Importação tardia mantém testes locais independentes do SDK e da chave.
        from userintent.llm import IntentExtractor

        extractor = IntentExtractor()

    intent = extractor.extract(user_text)
    if not isinstance(intent, UserIntent):
        raise TypeError("O extrator deve retornar um UserIntent validado.")

    if intent.clarification_needed:
        question = intent.clarification_question or "Pode informar mais detalhes sobre o produto desejado?"
        return SearchFlowResult(intent=intent, search=None, clarification_question=question)

    search = search_intent(
        intent,
        catalog=catalog,
        category_map=category_map,
        budget_currency=budget_currency,
        limit=limit,
        offset=offset,
    )
    return SearchFlowResult(intent=intent, search=search, clarification_question=None)
