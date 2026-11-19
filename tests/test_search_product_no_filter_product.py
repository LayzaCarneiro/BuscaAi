from buscai.catalog.dummyjson import DummyJsonCatalog
from buscai.tools import search_products

if __name__ == '__main__':
    cat = DummyJsonCatalog(currency="BRL")
    r = search_products("iPhone", 4000, catalog=cat, budget_currency="BRL", limit=200)  # sem filter_product
    print(f"\nResultados encontrados: {[(p.title, p.category, p.price) for p in r.products]}")