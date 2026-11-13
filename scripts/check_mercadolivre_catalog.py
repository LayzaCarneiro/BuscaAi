#!/usr/bin/env python3
"""Verifica a integração com o Mercado Livre: busca uma query e imprime os produtos.

Exemplos:
    export MERCADOLIVRE_ACCESS_TOKEN="..."
    python check_mercadolivre_catalog.py "notebook 16GB SSD"
    python check_mercadolivre_catalog.py "samsung galaxy s25" --limit 3
    python check_mercadolivre_catalog.py "tv 55 polegadas" --limit 10 --skip 10
    python check_mercadolivre_catalog.py "iphone 15" --json > resultado.json

Códigos de saída: 0 = ok, 1 = falha no catálogo (token, rede, resposta inválida), 2 = argumentos inválidos.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from decimal import Decimal

from dotenv import load_dotenv

from buscai.catalog.dummyjson import CatalogError
from buscai.catalog.mercadolivre import MercadoLivreCatalog
from buscai.catalog.models import Product, ProductPage
from pathlib import Path

# Permite rodar: python scripts/test_single_intent.py
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DEFAULT_LIMIT = 5
TOKEN_ENV_VAR = "MERCADOLIVRE_ACCESS_TOKEN"

load_dotenv()

def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' não é um número inteiro.") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("deve ser maior que zero.")
    return number


def non_negative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' não é um número inteiro.") from None
    if number < 0:
        raise argparse.ArgumentTypeError("não pode ser negativo.")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Busca produtos no catálogo do Mercado Livre e imprime o resultado em texto.",
    )
    parser.add_argument("query", help="texto da busca (use aspas se tiver espaços)")
    parser.add_argument("--limit", type=positive_int, default=DEFAULT_LIMIT,
                        help=f"quantidade de produtos do catálogo a pesquisar (padrão: {DEFAULT_LIMIT})")
    parser.add_argument("--skip", type=non_negative_int, default=0,
                        help="posição inicial dos resultados, para paginar (padrão: 0)")
    parser.add_argument("--currency", default="BRL",
                        help="moeda esperada; ofertas em outra moeda são descartadas (padrão: BRL)")
    parser.add_argument("--token", default=None,
                        help="token de acesso; se omitido, usa MERCADOLIVRE_ACCESS_TOKEN")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="imprime o resultado completo em JSON, em vez de texto")
    return parser


def format_price(price: Decimal, currency: str | None) -> str:
    text = f"{price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{currency} {text}" if currency else text


def format_value(value: object) -> str:
    if isinstance(value, bool):
        return "sim" if value else "não"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def print_product(index: int, product: Product) -> None:
    print(f"[{index}] {product.title}")
    print(f"    id:        {product.id}")
    print(f"    preço:     {format_price(product.price, product.currency)}")
    print(f"    categoria: {product.category or '-'}")
    print(f"    fonte:     {product.source}")
    specs = product.attributes.get("specs")
    for key, value in product.attributes.items():
        if key == "specs":
            continue
        print(f"    {key}: {format_value(value)}")
    if isinstance(specs, dict) and specs:
        print("    ficha técnica:")
        for name, value in specs.items():
            print(f"      - {name}: {value}")
    print()


def print_page(query: str, page: ProductPage) -> None:
    shown = len(page.products)
    print(f"Busca: {query!r}")
    print(f"Catálogo: {page.total} produto(s) no total; posição inicial {page.skip}, limite {page.limit}.")
    print(f"Retornados com oferta e preço: {shown}")
    if shown < page.limit:
        print("(produtos pai e sem oferta vencedora são descartados, por isso pode haver menos que o limite)")
    print()
    if not page.products:
        print("Nenhum produto com preço para esta busca. Tente outros termos ou aumente --limit.")
        return
    for index, product in enumerate(page.products, start=page.skip + 1):
        print_product(index, product)


def to_jsonable(page: ProductPage) -> dict:
    return json.loads(json.dumps(dataclasses.asdict(page), default=str))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    token = os.getenv(TOKEN_ENV_VAR) if args.token is None else args.token
    catalog = MercadoLivreCatalog(access_token=token, currency=args.currency)
    try:
        page = catalog.search_products(args.query, limit=args.limit, skip=args.skip)
    except CatalogError as exc:
        print(f"Erro no catálogo do Mercado Livre: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:  # argumentos aceitos pelo argparse mas recusados pela classe
        print(f"Argumento inválido: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps(to_jsonable(page), ensure_ascii=False, indent=2))
    else:
        print_page(args.query, page)
    return 0


if __name__ == "__main__":
    sys.exit(main())
