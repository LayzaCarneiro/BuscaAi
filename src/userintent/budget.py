"""Conciliação determinística entre orçamento explícito da requisição e UserIntent."""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Protocol

from .schema import PriceConstraint, UserIntent


class CurrencyConverterLike(Protocol):
    """Protocolo mínimo para conversão bidirecional USD <-> BRL sem acoplamento circular."""

    def usd_to_brl(self, amount_usd: Decimal | int | str) -> object:
        ...

    def brl_to_usd(self, amount_brl: Decimal | int | str) -> Decimal:
        ...


def _convert_amount(
    amount: Decimal,
    *,
    from_currency: str,
    to_currency: str,
    currency_converter: CurrencyConverterLike,
) -> Decimal:
    if from_currency == to_currency:
        return amount
    if from_currency == "USD" and to_currency == "BRL":
        converted = currency_converter.usd_to_brl(amount)
        raw = getattr(converted, "amount", converted)
        return Decimal(str(raw))
    if from_currency == "BRL" and to_currency == "USD":
        return currency_converter.brl_to_usd(amount)
    raise ValueError(
        f"Conversão cambial não suportada entre {from_currency} e {to_currency}."
    )


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
    currency_converter: CurrencyConverterLike | None = None,
) -> UserIntent:
    """Concilia deterministicamente o orçamento explícito com `intent.price`.

    Regras de precedência documentadas:
    - Se `budget_brl` for `None`, preserva `intent` inalterado.
    - Se o texto não informou teto (`max_price=None`) ou trouxe teto flexível
      (`is_hard_constraint=False`), o orçamento explícito passa a ser o teto rígido.
    - Se o texto já trouxe teto rígido na mesma moeda (ou sem moeda explícita),
      prevalece o menor teto (`min(text_max, budget_brl)`); em caso de empate,
      preserva-se `max_inclusive=False` quando vindo de expressões como "menos de".
    - Se o texto trouxe restrições em moeda diferente, exige `currency_converter`
      compatível (BRL <-> USD) para comparar valores na mesma base, preservando sempre
      o teto rígido mais restritivo; rejeita combinações ambíguas sem conversor.
    """
    if budget_brl is None:
        return intent

    explicit_max = _parse_positive_budget(budget_brl)
    norm_currency = _normalize_currency_code(currency)
    price = intent.price
    text_currency = price.currency

    if text_currency is not None and text_currency != norm_currency:
        has_text_bounds = price.min_price is not None or price.max_price is not None
        if not has_text_bounds:
            merged_price = PriceConstraint(
                min_price=None,
                max_price=float(explicit_max),
                min_inclusive=True,
                max_inclusive=True,
                is_hard_constraint=True,
                currency=norm_currency,
            )
            return intent.model_copy(update={"price": merged_price})

        if currency_converter is None or {text_currency, norm_currency} != {"BRL", "USD"}:
            if price.min_price is not None or (price.max_price is not None and price.is_hard_constraint):
                raise ValueError(
                    f"Não é possível conciliar orçamento em {text_currency} com {norm_currency} "
                    "sem conversor cambial compatível."
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

        min_in_norm: float | None = None
        if price.min_price is not None:
            min_in_norm = float(
                _convert_amount(
                    Decimal(str(price.min_price)),
                    from_currency=text_currency,
                    to_currency=norm_currency,
                    currency_converter=currency_converter,
                )
            )

        if price.max_price is not None and price.is_hard_constraint:
            text_max_in_norm = _convert_amount(
                Decimal(str(price.max_price)),
                from_currency=text_currency,
                to_currency=norm_currency,
                currency_converter=currency_converter,
            )
            explicit_cmp = explicit_max.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            text_cmp = text_max_in_norm.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            if text_cmp < explicit_cmp:
                # Mantém integralmente o teto rígido mais restritivo na moeda original do texto.
                _validate_min_against_max(
                    price.min_price,
                    price.min_inclusive,
                    Decimal(str(price.max_price)),
                    price.max_inclusive,
                )
                return intent

            effective_max_inclusive = price.max_inclusive if text_cmp == explicit_cmp else True
            _validate_min_against_max(
                min_in_norm,
                price.min_inclusive,
                explicit_max,
                effective_max_inclusive,
            )
            merged_price = PriceConstraint(
                min_price=min_in_norm,
                max_price=float(explicit_max),
                min_inclusive=price.min_inclusive,
                max_inclusive=effective_max_inclusive,
                is_hard_constraint=True,
                currency=norm_currency,
            )
            return intent.model_copy(update={"price": merged_price})

        _validate_min_against_max(min_in_norm, price.min_inclusive, explicit_max, True)
        merged_price = PriceConstraint(
            min_price=min_in_norm,
            max_price=float(explicit_max),
            min_inclusive=price.min_inclusive,
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
