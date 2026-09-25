"""Mostra os primeiros produtos normalizados do catálogo DummyJSON."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from buscai.catalog.dummyjson import CatalogError, DummyJsonCatalog


DISPLAY_LIMIT = 5


def main() -> int:
    try:
        page = DummyJsonCatalog().list_products(limit=DISPLAY_LIMIT)
    except CatalogError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1

    if not page.products:
        print("O DummyJSON não retornou produtos.")
        return 0

    print("Primeiros produtos do DummyJSON:")
    for product in page.products:
        currency = product.currency or "moeda não informada"
        print(f"ID: {product.id} | Nome: {product.title} | Preço: {product.price} ({currency})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
