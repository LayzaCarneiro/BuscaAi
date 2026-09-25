"""Demonstra texto → Gemini UserIntent → busca no catálogo DummyJSON."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from buscai.agent import IntentExtractorProtocol, run_search_flow
from buscai.catalog import CatalogProvider
from buscai.catalog.dummyjson import DummyJsonCatalog


# Mapeamento inicial do catálogo de demonstração; pode ser ampliado sem mudar UserIntent.
DUMMYJSON_CATEGORY_MAP = {
    "maquiagem": "beauty",
    "batom": "beauty",
    "notebook": "laptops",
    "celular": "smartphones",
    "perfume": "fragrances",
}


def main(
    argv: list[str] | None = None,
    *,
    extractor: IntentExtractorProtocol | None = None,
    catalog: CatalogProvider | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Extrai intenção com Gemini e consulta o catálogo")
    parser.add_argument("text", help="Pedido do usuário em linguagem natural")
    parser.add_argument("--limit", type=int, default=10, help="Produtos por página (1 a 50)")
    parser.add_argument("--offset", type=int, default=0, help="Primeiro resultado da página")
    args = parser.parse_args(argv)

    try:
        result = run_search_flow(
            args.text,
            extractor=extractor,
            catalog=catalog if catalog is not None else DummyJsonCatalog(),
            category_map=DUMMYJSON_CATEGORY_MAP,
            limit=args.limit,
            offset=args.offset,
        )
    except Exception as exc:
        print(f"Erro no fluxo de busca: {exc}", file=sys.stderr)
        return 1

    if result.clarification_question is not None:
        print(f"Pergunta de esclarecimento: {result.clarification_question}")
        return 0

    assert result.search is not None
    page = result.search.results
    print(f"Intenção: {result.intent.query}")
    print(f"Correspondências: {page.total_matches} | Página: offset={page.offset}, limit={page.limit}")
    if result.search.unapplied_preferences:
        print("Preferências ainda não aplicadas: " + ", ".join(result.search.unapplied_preferences))
    if not page.products:
        print("Nenhum produto encontrado nesta página.")
        return 0

    for product in page.products:
        currency = product.currency or "moeda não informada"
        print(f"ID: {product.id} | Nome: {product.title} | Preço: {product.price} ({currency})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
