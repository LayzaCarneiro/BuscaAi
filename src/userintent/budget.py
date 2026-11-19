"""Conciliação determinística entre orçamento explícito da requisição e UserIntent."""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation

from .schema import PriceConstraint, UserIntent


def _parse_positive_budget(value: Decimal | int | float | str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float, str)):
        raise ValueError("Orçamento explícito deve ser um valor numérico positivo.")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Orçamento explícito deve ser um número finito.")
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError("Orçamento explícito inválido.") from exc
    if not dec.is_finite() or dec <= 0:
        raise ValueError("Orçamento explícito deve ser maior que zero.")
    return dec


def _normalize_currency_code(currency: str) -> str:
    if not isinstance(currency, str):
        raise ValueError("Código de moeda deve ser textual.")
    cleaned = currency.strip().upper()
    if len(cleaned) != 3 or not cleaned.isascii() or not cleaned.isalpha():
        raise ValueError("Código de moeda deve conter exatamente 3 letras ASCII (ex.: 'BRL').")
    return cleaned


def _validate_min_against_max(
    min_price: float | None,
    min_inclusive: bool,
    max_price: Decimal,
    max_inclusive: bool,
) -> None:
    if min_price is None:
        return
    min_dec = Decimal(str(min_price))
    if min_dec > max_price:
        raise ValueError("Orçamento máximo explícito é menor que o valor mínimo informado na consulta.")
    if min_dec == max_price and not (min_inclusive and max_inclusive):
        raise ValueError("Faixa de orçamento resultante é inconsistente (limite mínimo e máximo excludentes).")


def merge_explicit_budget(
    intent: UserIntent,
    budget_brl: Decimal | int | float | str | None,
    *,
    currency: str = "BRL",
) -> UserIntent:
    """Concilia deterministicamente o orçamento explícito com `intent.price`.

    Regras de precedência documentadas:
    - Se `budget_brl` for `None`, preserva `intent` inalterado.
    - Se o texto não informou teto (`max_price=None`) ou trouxe teto flexível
      (`is_hard_constraint=False`), o orçamento explícito passa a ser o teto rígido.
    - Se o texto já trouxe teto rígido na mesma moeda (ou sem moeda explícita),
      prevalece o menor teto (`min(text_max, budget_brl)`); em caso de empate,
      preserva-se `max_inclusive=False` quando vindo de expressões como "menos de".
    - Se o texto trouxe teto em moeda diferente, o orçamento explícito prevalece na
      moeda informada quando não houver `min_price` conflitante.
    """
    if budget_brl is None:
        return intent

    explicit_max = _parse_positive_budget(budget_brl)
    norm_currency = _normalize_currency_code(currency)
    price = intent.price
    text_currency = price.currency

    # Caso o texto especifique explicitamente outra moeda, evita combinar limites
    # heterogêneos antes da conversão centralizada.
    if text_currency is not None and text_currency != norm_currency:
        if price.min_price is not None:
            raise ValueError(
                f"Valor mínimo em {text_currency} conflita com orçamento explícito em {norm_currency}."
            )
        merged_price = PriceConstraint(
            min_price=None,
            max_price=float(explicit_max),
            min_inclusive=True,
            max_inclusive=True,
            is_hard_constraint=True,
            currency=norm_currency,
        )
        return intent.model_copy(update={"price": merged_price})

    if price.max_price is None or not price.is_hard_constraint:
        _validate_min_against_max(price.min_price, price.min_inclusive, explicit_max, True)
        merged_price = PriceConstraint(
            min_price=price.min_price,
            max_price=float(explicit_max),
            min_inclusive=price.min_inclusive,
            max_inclusive=True,
            is_hard_constraint=True,
            currency=norm_currency,
        )
        return intent.model_copy(update={"price": merged_price})

    text_max = Decimal(str(price.max_price))
    if text_max < explicit_max:
        effective_max = text_max
        effective_max_inclusive = price.max_inclusive
    elif explicit_max < text_max:
        effective_max = explicit_max
        effective_max_inclusive = True
    else:
        effective_max = explicit_max
        effective_max_inclusive = price.max_inclusive

    _validate_min_against_max(
        price.min_price,
        price.min_inclusive,
        effective_max,
        effective_max_inclusive,
    )
    merged_price = PriceConstraint(
        min_price=price.min_price,
        max_price=float(effective_max),
        min_inclusive=price.min_inclusive,
        max_inclusive=effective_max_inclusive,
        is_hard_constraint=True,
        currency=norm_currency,
    )
    return intent.model_copy(update={"price": merged_price})
