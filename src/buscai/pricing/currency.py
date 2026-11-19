"""Conversão cambial centralizada USD <-> BRL com marcação transparente de estimativa."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Protocol

from pydantic import BaseModel, ConfigDict, field_validator

from buscai.config import Settings

_TWO_DECIMALS = Decimal("0.01")


def _parse_decimal_input(
    value: object,
    field_name: str,
    *,
    allow_zero: bool = True,
) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
        raise ValueError(f"{field_name} deve ser um valor decimal válido.")
    try:
        dec = value if isinstance(value, Decimal) else Decimal(value.strip() if isinstance(value, str) else str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{field_name} deve ser um valor decimal válido.") from exc
    if not dec.is_finite():
        raise ValueError(f"{field_name} deve ser um número finito.")
    if allow_zero and dec < 0:
        raise ValueError(f"{field_name} não pode ser negativo.")
    if not allow_zero and dec <= 0:
        raise ValueError(f"{field_name} deve ser maior que zero.")
    return dec


def _clean_required_label(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} não pode ser vazio.")
    return value.strip()


class ExchangeRateInfo(BaseModel):
    """Metadados da taxa de câmbio USD -> BRL utilizada na conversão."""

    model_config = ConfigDict(frozen=True)

    base_currency: str = "USD"
    quote_currency: str = "BRL"
    rate: Decimal
    updated_at: str
    source: str
    is_estimate: bool = True

    @field_validator("rate", mode="before")
    @classmethod
    def _valid_rate(cls, value: object) -> Decimal:
        return _parse_decimal_input(value, "rate", allow_zero=False)

    @field_validator("updated_at", "source", mode="before")
    @classmethod
    def _valid_labels(cls, value: object, info) -> str:
        return _clean_required_label(value, str(info.field_name))


class EstimatedPriceBRL(BaseModel):
    """Preço convertido para BRL com identificação explícita de estimativa."""

    model_config = ConfigDict(frozen=True)

    amount: Decimal
    currency: str = "BRL"
    original_amount: Decimal
    original_currency: str = "USD"
    rate: Decimal
    updated_at: str
    source: str
    is_estimate: bool = True

    @field_validator("amount", "original_amount", mode="before")
    @classmethod
    def _valid_amounts(cls, value: object, info) -> Decimal:
        return _parse_decimal_input(value, str(info.field_name), allow_zero=True)

    @field_validator("rate", mode="before")
    @classmethod
    def _valid_rate(cls, value: object) -> Decimal:
        return _parse_decimal_input(value, "rate", allow_zero=False)


class ExchangeRateProvider(Protocol):
    """Protocolo extensível para fornecedores de taxa de câmbio USD -> BRL."""

    def get_usd_brl_rate(self) -> ExchangeRateInfo:
        """Retorna a taxa vigente de conversão de 1 USD para BRL."""
        ...


class FixedEnvExchangeRateProvider:
    """Provedor v1 baseado em taxa fixa configurável por variáveis de ambiente."""

    def __init__(
        self,
        *,
        rate: Decimal | int | str,
        updated_at: str = "unknown",
        source: str = "env_fixed_rate",
    ) -> None:
        self._info = ExchangeRateInfo(
            rate=_parse_decimal_input(rate, "USD_BRL_RATE", allow_zero=False),
            updated_at=_clean_required_label(updated_at, "USD_BRL_RATE_UPDATED_AT"),
            source=_clean_required_label(source, "USD_BRL_RATE_SOURCE"),
            is_estimate=True,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> FixedEnvExchangeRateProvider:
        """Constrói o provedor a partir do objeto centralizado Settings."""
        return cls(
            rate=settings.usd_brl_rate,
            updated_at=settings.usd_brl_rate_updated_at,
            source=settings.usd_brl_rate_source,
        )

    def get_usd_brl_rate(self) -> ExchangeRateInfo:
        return self._info


class CurrencyConverter:
    """Componente centralizado para conversões USD <-> BRL."""

    def __init__(self, provider: ExchangeRateProvider) -> None:
        self._provider = provider

    def rate_info(self) -> ExchangeRateInfo:
        """Retorna os metadados da taxa de câmbio atualmente fornecida."""
        return self._provider.get_usd_brl_rate()

    def usd_to_brl(self, amount_usd: Decimal | int | str) -> EstimatedPriceBRL:
        """Converte um valor em USD para uma estimativa transparente em BRL."""
        usd = _parse_decimal_input(amount_usd, "amount_usd", allow_zero=True)
        info = self.rate_info()
        amount_brl = (usd * info.rate).quantize(_TWO_DECIMALS, rounding=ROUND_HALF_UP)
        return EstimatedPriceBRL(
            amount=amount_brl,
            currency=info.quote_currency,
            original_amount=usd,
            original_currency=info.base_currency,
            rate=info.rate,
            updated_at=info.updated_at,
            source=info.source,
            is_estimate=True,
        )

    def brl_to_usd(self, amount_brl: Decimal | int | str) -> Decimal:
        """Converte um orçamento em BRL para USD (usado em filtros da eBay)."""
        brl = _parse_decimal_input(amount_brl, "amount_brl", allow_zero=True)
        info = self.rate_info()
        return (brl / info.rate).quantize(_TWO_DECIMALS, rounding=ROUND_HALF_UP)
