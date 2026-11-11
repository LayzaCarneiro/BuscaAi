"""Ponte entre a interface Streamlit e o agente do BuscAI.

A tela só conhece `ask_agent()` e `AgentReply`. Assim, trocar o mock pelo
pipeline real (ou, depois, por chamadas à API FastAPI) não mexe na interface.

Modo de execução:
    BUSCAI_USE_MOCK=1  (padrão)  -> respostas simuladas, para desenvolver a tela
    BUSCAI_USE_MOCK=0            -> usa o pipeline real (ajuste os imports em `_real`)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

USE_MOCK = os.getenv("BUSCAI_USE_MOCK", "1") == "1"


@dataclass(frozen=True)
class AgentReply:
    kind: Literal["clarification", "results", "error"]
    text: str | None = None   # pergunta de esclarecimento ou mensagem de erro
    payload: Any = None       # IntentSearchResult quando kind == "results"


def ask_agent(user_messages: list[str]) -> AgentReply:
    """Recebe todas as mensagens do usuário na conversa (da mais antiga à mais nova)."""
    if USE_MOCK:
        return _mock(user_messages)
    # IntentExtractor.extract() recebe uma única string, então as respostas às
    # perguntas de esclarecimento são concatenadas ao pedido original.
    text = "\n".join(m.strip() for m in user_messages if m.strip())
    return _real(text)


def _mock(user_messages: list[str]) -> AgentReply:
    first = user_messages[0] if user_messages else ""
    if len(user_messages) == 1 and len(first.split()) < 5:
        return AgentReply(
            kind="clarification",
            text="Qual produto você procura e qual é o seu orçamento? "
                 "Se puder, conte também para que vai usar.",
        )
    return AgentReply(kind="results", payload={"query": " ".join(user_messages)})


def _real(text: str) -> AgentReply:
    # AJUSTE OS IMPORTS conforme a estrutura do repositório.
    from buscai.adapters.intent import (  # noqa: F401
        ClarificationRequired,
        ConstraintUnavailableError,
        search_intent,
    )
    from buscai.catalog.dummyjson import CatalogError, DummyJsonCatalog
    from userintent.llm import IntentExtractor

    try:
        intent = IntentExtractor().extract(text)
        result = search_intent(
            intent,
            catalog=DummyJsonCatalog(currency="BRL"),
            budget_currency="BRL",
        )
    except ClarificationRequired as exc:
        return AgentReply(kind="clarification", text=str(exc))
    except ConstraintUnavailableError:
        return AgentReply(
            kind="error",
            text="Não consegui verificar um dos critérios do seu pedido com os dados "
                 "do catálogo. Tente reformular sem esse critério.",
        )
    except CatalogError:
        return AgentReply(
            kind="error",
            text="O catálogo não respondeu. Tente novamente em instantes.",
        )
    except Exception:  # falha do modelo, JSON inválido etc.
        return AgentReply(
            kind="error",
            text="Não consegui interpretar seu pedido agora. Tente novamente.",
        )
    return AgentReply(kind="results", payload=result)
