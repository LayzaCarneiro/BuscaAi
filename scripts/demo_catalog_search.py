"""Demonstração offline/legada de busca a partir de UserIntent manual (sem Gemini)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from buscai.catalog import CatalogProvider
from buscai.catalog.dummyjson import DummyJsonCatalog
from buscai.catalog.errors import CatalogError
from buscai.tools import search_products
from userintent.schema import UserIntent


def main(argv: list[str] | None = None, *, catalog: CatalogProvider | None = None) -> int:
    parser = argparse.ArgumentParser(description="Busca produtos em catálogo legado sem chamar Gemini")
    parser.add_argument("query", help="Termos da busca textual")
    parser.add_argument("--limit", type=int, default=10, help="Resultados por página (1 a 200)")
    parser.add_argument("--offset", type=int, default=0, help="Primeiro resultado da página")
    args = parser.parse_args(argv)

    try:
        # Nesta etapa, somente query é passada à busca; os demais campos do
        # UserIntent exigem o adaptador e a política de moeda da fase 2.
        intent = UserIntent(query=args.query)
        selected_catalog = catalog if catalog is not None else DummyJsonCatalog()
        result = search_products(intent.query, catalog=selected_catalog, limit=args.limit, offset=args.offset)
    except (CatalogError, ValidationError, ValueError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1

    print(f"Correspondências: {result.total_matches} | Página: offset={result.offset}, limit={result.limit}")
    if not result.products:
        print("Nenhum produto nesta página.")
        return 0

    for product in result.products:
        currency = product.currency or "moeda não informada"
        print(f"ID: {product.id} | Nome: {product.title} | Preço: {product.price} ({currency})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
