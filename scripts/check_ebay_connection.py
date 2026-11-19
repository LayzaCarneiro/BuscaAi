"""Diagnóstico operacional seguro da conexão com a eBay Browse API e câmbio USD/BRL.

Imprime apenas metadados operacionais (status HTTP, latência, contagens e
configuração de câmbio). Nunca imprime títulos, preços, URLs, vendedores ou tokens.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from buscai.catalog.errors import CatalogError, ProviderError
from buscai.config import Settings, load_settings
from buscai.observability import configure_logging
from buscai.pricing import CurrencyConverter, FixedEnvExchangeRateProvider
from buscai.search import EbayLiveBackend, EbayOAuthClient, QuotaGuard, SearchQuery


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verifica autenticação OAuth, busca na eBay Browse API e câmbio USD/BRL (sem expor anúncios)."
    )
    parser.add_argument("--query", default="gaming laptop", help="Consulta diagnóstica em inglês (padrão: 'gaming laptop')")
    parser.add_argument("--limit", type=int, default=5, help="Limite de itens na busca diagnóstica (1 a 20)")
    parser.add_argument(
        "--with-details",
        action="store_true",
        help="Executa 1 chamada adicional getItem no primeiro item para validar localizedAspects",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    oauth_client: EbayOAuthClient | None = None,
    backend: EbayLiveBackend | None = None,
    converter: CurrencyConverter | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.limit < 1 or args.limit > 20:
        print("Argumento inválido: --limit deve estar entre 1 e 20.", file=sys.stderr)
        return 2

    cfg = settings if settings is not None else load_settings(load_dotenv_file=True)
    logger = configure_logging(cfg.log_level, stream=sys.stderr, logger_name="buscai.check_ebay")

    owns_oauth = False
    owns_backend = False
    active_oauth = oauth_client
    active_backend = backend

    try:
        if active_oauth is None:
            active_oauth = EbayOAuthClient.from_settings(cfg, logger=logger)
            owns_oauth = True

        quota_guard = QuotaGuard(
            daily_limit=cfg.ebay_daily_quota_limit,
            default_max_search_per_request=1,
            default_max_get_item_per_request=1 if args.with_details else 0,
            logger=logger,
        )
        budget = quota_guard.new_request_budget()

        if active_backend is None:
            active_backend = EbayLiveBackend(
                oauth_client=active_oauth,
                quota_guard=quota_guard,
                default_marketplace_id=cfg.ebay_marketplace_id,
                logger=logger,
            )
            owns_backend = True

        oauth_start = time.monotonic()
        token = active_oauth.get_access_token(request_id="cli-check-ebay")
        oauth_ms = round((time.monotonic() - oauth_start) * 1000.0, 2)
        print(f"OAuth eBay: OK | token_len={len(token)} | duration_ms={oauth_ms}")

        query = SearchQuery(
            query_text=args.query,
            marketplace_id=cfg.ebay_marketplace_id,
            limit=args.limit,
        )
        search_start = time.monotonic()
        response = active_backend.search(query, budget=budget, request_id="cli-check-ebay")
        search_ms = round((time.monotonic() - search_start) * 1000.0, 2)

        with_seller = sum(1 for item in response.candidates if item.seller is not None)
        with_shipping = sum(1 for item in response.candidates if item.shipping is not None)
        with_image = sum(1 for item in response.candidates if item.image_url is not None)
        print(
            "Busca eBay: OK | "
            f"marketplace={query.marketplace_id} | items_returned={len(response.candidates)} | "
            f"total_estimated={response.total_estimated} | calls_used={response.calls_used} | "
            f"duration_ms={search_ms} | with_seller={with_seller} | with_shipping={with_shipping} | "
            f"with_image={with_image}"
        )

        if args.with_details:
            if response.candidates:
                detail_start = time.monotonic()
                detail = active_backend.get_item_details(
                    response.candidates[0].item_id,
                    budget=budget,
                    request_id="cli-check-ebay",
                    marketplace_id=query.marketplace_id,
                )
                detail_ms = round((time.monotonic() - detail_start) * 1000.0, 2)
                aspect_count = len(detail.aspects) if detail is not None else 0
                loaded = bool(detail is not None and detail.details_loaded)
                print(
                    f"Detalhes getItem: {'OK' if detail is not None else 'NOT_FOUND'} | "
                    f"details_loaded={loaded} | aspect_count={aspect_count} | duration_ms={detail_ms}"
                )
            else:
                print("Detalhes getItem: SKIPPED (sem candidatos na busca)")

        active_converter = converter or CurrencyConverter(FixedEnvExchangeRateProvider.from_settings(cfg))
        sample_brl = active_converter.usd_to_brl("100.00")
        print(
            f"Câmbio USD/BRL: rate={sample_brl.rate} | source={sample_brl.source} | "
            f"updated_at={sample_brl.updated_at} | is_estimate={sample_brl.is_estimate} | "
            f"sample_100_usd_to_brl={sample_brl.amount}"
        )

        snap = quota_guard.snapshot()
        print(
            f"Cota local: used_in_window={snap.used_in_window} | "
            f"remaining={snap.remaining} | daily_limit={snap.daily_limit}"
        )
        return 0
    except (ProviderError, CatalogError, ValueError) as exc:
        print(f"Falha na verificação eBay: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if owns_backend and active_backend is not None:
            active_backend.close()
        if owns_oauth and active_oauth is not None:
            active_oauth.close()


if __name__ == "__main__":
    raise SystemExit(main())
