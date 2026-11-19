"""Script manual de verificação da busca sem filter_product usando DummyJSON."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from buscai.catalog.dummyjson import DummyJsonCatalog
from buscai.tools import search_products

if __name__ == "__main__":
    cat = DummyJsonCatalog(currency="BRL")
    r = search_products("iPhone", 4000, catalog=cat, budget_currency="BRL", limit=200)  # sem filter_product
    print(f"\nResultados encontrados: {[(p.title, p.category, p.price) for p in r.products]}")
