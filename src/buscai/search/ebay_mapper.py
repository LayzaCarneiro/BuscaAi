"""Normalizador defensivo de payloads da eBay Browse API para Candidate e SearchResponse."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from buscai.catalog.errors import CatalogError
from buscai.search.models import Candidate, SearchResponse, SellerInfo, ShippingInfo

EBAY_SOURCE = "ebay"


def parse_json_decimal(raw_text: str) -> object:
    """Decodifica JSON preservando números decimais sem imprecisão de float."""
    try:
        return json.loads(raw_text, parse_float=Decimal)
    except (ValueError, TypeError) as exc:
        raise CatalogError("Resposta da eBay Browse API não contém JSON válido.") from exc


def _clean_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def _parse_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, int):
        dec = Decimal(value)
    elif isinstance(value, str) and value.strip():
        try:
            dec = Decimal(value.strip())
        except InvalidOperation:
            return None
    else:
        return None
    return dec if dec.is_finite() else None


def _parse_currency(value: object) -> str | None:
    text = _clean_str(value)
    if text is None:
        return None
    upper = text.upper()
    if len(upper) != 3 or not upper.isascii() or not upper.isalpha():
        return None
    return upper


def _parse_price(obj: object) -> tuple[Decimal, str] | None:
    if not isinstance(obj, dict):
        return None
    amount = _parse_decimal(obj.get("value"))
    currency = _parse_currency(obj.get("currency"))
    if amount is None or amount < 0 or currency is None:
        return None
    return amount, currency


def _valid_http_url(value: object) -> str | None:
    text = _clean_str(value)
    if text is None:
        return None
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return text


def _extract_image_url(item: dict[str, object]) -> str | None:
    image_obj = item.get("image")
    if isinstance(image_obj, dict):
        url = _valid_http_url(image_obj.get("imageUrl"))
        if url is not None:
            return url

    for key in ("thumbnailImages", "additionalImages"):
        gallery = item.get(key)
        if not isinstance(gallery, list):
            continue
        for entry in gallery:
            if isinstance(entry, dict):
                url = _valid_http_url(entry.get("imageUrl"))
                if url is not None:
                    return url
    return None


def _extract_seller(raw_seller: object) -> SellerInfo | None:
    if not isinstance(raw_seller, dict):
        return None
    username = _clean_str(raw_seller.get("username"))
    raw_score = raw_seller.get("feedbackScore")
    score = raw_score if isinstance(raw_score, int) and not isinstance(raw_score, bool) and raw_score >= 0 else None

    pct = _parse_decimal(raw_seller.get("feedbackPercentage"))
    if pct is not None and not (Decimal("0") <= pct <= Decimal("100")):
        pct = None

    if username is None and score is None and pct is None:
        return None
    return SellerInfo(username=username, feedback_score=score, feedback_percentage=pct)


def _extract_shipping(raw_options: object) -> ShippingInfo | None:
    if not isinstance(raw_options, list):
        return None
    for option in raw_options:
        if not isinstance(option, dict):
            continue
        parsed_cost = _parse_price(option.get("shippingCost"))
        cost = parsed_cost[0] if parsed_cost is not None else None
        currency = parsed_cost[1] if parsed_cost is not None else None
        cost_type = _clean_str(option.get("shippingCostType"))

        free_shipping: bool | None = None
        if cost is not None:
            free_shipping = cost == Decimal("0")
        elif cost_type is not None and cost_type.upper() == "FREE":
            free_shipping = True

        min_days = option.get("estimatedMinDays")
        max_days = option.get("estimatedMaxDays")
        clean_min = min_days if isinstance(min_days, int) and not isinstance(min_days, bool) and min_days >= 0 else None
        clean_max = max_days if isinstance(max_days, int) and not isinstance(max_days, bool) and max_days >= 0 else None
        if clean_min is not None and clean_max is not None and clean_max < clean_min:
            clean_min = clean_max = None

        if cost is None and currency is None and free_shipping is None and clean_min is None and clean_max is None:
            continue

        return ShippingInfo(
            cost=cost,
            currency=currency,
            free_shipping=free_shipping,
            estimated_min_days=clean_min,
            estimated_max_days=clean_max,
        )
    return None


def _extract_categories(payload: dict[str, object]) -> tuple[str, ...]:
    categories: list[str] = []
    seen: set[str] = set()

    raw_cats = payload.get("categories")
    if isinstance(raw_cats, list):
        for entry in raw_cats:
            if isinstance(entry, dict):
                name = _clean_str(entry.get("categoryName"))
                if name and name.casefold() not in seen:
                    seen.add(name.casefold())
                    categories.append(name)

    if not categories:
        raw_path = _clean_str(payload.get("categoryPath"))
        if raw_path:
            for part in raw_path.split("|"):
                cleaned = part.strip()
                if cleaned and cleaned.casefold() not in seen:
                    seen.add(cleaned.casefold())
                    categories.append(cleaned)

    return tuple(categories)


def _extract_buying_options(payload: dict[str, object]) -> tuple[str, ...]:
    raw_opts = payload.get("buyingOptions")
    if not isinstance(raw_opts, list):
        return ()
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw_opts:
        text = _clean_str(item)
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            cleaned.append(text)
    return tuple(cleaned)


def _extract_aspects(raw_aspects: object) -> dict[str, tuple[str, ...]]:
    if not isinstance(raw_aspects, list):
        return {}
    aspects: dict[str, list[str]] = {}
    seen_per_key: dict[str, set[str]] = {}
    for entry in raw_aspects:
        if not isinstance(entry, dict):
            continue
        name = _clean_str(entry.get("name"))
        if name is None:
            continue
        raw_value = entry.get("value")
        if raw_value is None:
            raw_value = entry.get("values")
        values_list = raw_value if isinstance(raw_value, list) else [raw_value]

        bucket = aspects.setdefault(name, [])
        seen = seen_per_key.setdefault(name, set())
        for val in values_list:
            cleaned_val = _clean_str(val)
            if cleaned_val and cleaned_val.casefold() not in seen:
                seen.add(cleaned_val.casefold())
                bucket.append(cleaned_val)

    return {k: tuple(v) for k, v in aspects.items() if v}


def _aspect_lookup(aspects: dict[str, tuple[str, ...]], key_name: str) -> str | None:
    target = key_name.casefold()
    for key, values in aspects.items():
        if key.casefold() == target and values:
            return values[0]
    return None


def _build_candidate(payload: object, *, include_details: bool) -> Candidate | None:
    if not isinstance(payload, dict):
        return None
    item_id = _clean_str(payload.get("itemId"))
    title = _clean_str(payload.get("title"))
    parsed_price = _parse_price(payload.get("price"))
    if item_id is None or title is None or parsed_price is None:
        return None

    aspects = _extract_aspects(payload.get("localizedAspects")) if include_details else {}
    brand = _clean_str(payload.get("brand")) or (_aspect_lookup(aspects, "Brand") if include_details else None)
    model = _clean_str(payload.get("model")) or (_aspect_lookup(aspects, "Model") if include_details else None)

    return Candidate(
        item_id=item_id,
        source=EBAY_SOURCE,
        title=title,
        price=parsed_price[0],
        currency=parsed_price[1],
        condition=_clean_str(payload.get("condition")),
        categories=_extract_categories(payload),
        brand=brand,
        model=model,
        short_description=_clean_str(payload.get("shortDescription")),
        aspects=aspects,
        image_url=_extract_image_url(payload),
        item_web_url=_valid_http_url(payload.get("itemWebUrl")),
        seller=_extract_seller(payload.get("seller")),
        shipping=_extract_shipping(payload.get("shippingOptions")),
        buying_options=_extract_buying_options(payload),
        details_loaded=include_details,
    )


def map_item_summary(item: object) -> Candidate | None:
    """Converte um item de `itemSummaries` da Browse API em Candidate (ou None se inválido)."""
    return _build_candidate(item, include_details=False)


def map_item_detail(payload: object) -> Candidate | None:
    """Converte resposta de `getItem` em Candidate com aspectos estruturados."""
    return _build_candidate(payload, include_details=True)


def merge_candidate_details(summary: Candidate, detail: Candidate) -> Candidate:
    """Combina resumo da busca com detalhes recuperados via `getItem`, sem descartar dados úteis."""
    merged_aspects = dict(summary.aspects)
    for key, values in detail.aspects.items():
        merged_aspects[key] = values

    return Candidate(
        item_id=detail.item_id or summary.item_id,
        source=detail.source or summary.source,
        title=detail.title or summary.title,
        price=detail.price,
        currency=detail.currency,
        condition=detail.condition or summary.condition,
        categories=detail.categories or summary.categories,
        brand=detail.brand or summary.brand,
        model=detail.model or summary.model,
        short_description=detail.short_description or summary.short_description,
        aspects=merged_aspects,
        image_url=detail.image_url or summary.image_url,
        item_web_url=detail.item_web_url or summary.item_web_url,
        seller=detail.seller or summary.seller,
        shipping=detail.shipping or summary.shipping,
        buying_options=detail.buying_options or summary.buying_options,
        details_loaded=True,
    )


def parse_search_response(
    payload: object,
    *,
    limit: int,
    offset: int,
    calls_used: int = 1,
) -> SearchResponse:
    """Converte o JSON raiz de `item_summary/search` em SearchResponse."""
    if not isinstance(payload, dict):
        raise CatalogError("Resposta inválida da eBay Browse API: payload não é um objeto.")

    raw_items = payload.get("itemSummaries")
    if raw_items is None:
        raw_items = []
    elif not isinstance(raw_items, list):
        raise CatalogError("Resposta inválida da eBay Browse API: campo 'itemSummaries' incorreto.")

    raw_total = payload.get("total")
    total_estimated = (
        raw_total
        if isinstance(raw_total, int) and not isinstance(raw_total, bool) and raw_total >= 0
        else None
    )

    candidates = tuple(
        candidate
        for item in raw_items
        if (candidate := map_item_summary(item)) is not None
    )

    return SearchResponse(
        candidates=candidates,
        total_estimated=total_estimated,
        limit=limit,
        offset=offset,
        calls_used=calls_used,
    )
