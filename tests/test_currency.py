"""Testes do componente centralizado de conversão cambial USD <-> BRL."""

from __future__ import annotations

from decimal import Decimal

import pytest

from buscai.config import load_settings
from buscai.pricing import (
    CurrencyConverter,
    EstimatedPriceBRL,
    ExchangeRateInfo,
    ExchangeRateProvider,
    FixedEnvExchangeRateProvider,
)


def test_fixed_env_provider_from_settings_and_usd_to_brl_estimate() -> None:
    settings = load_settings(
        environ={
            "USD_BRL_RATE": "5.45",
            "USD_BRL_RATE_UPDATED_AT": "2026-11-19",
            "USD_BRL_RATE_SOURCE": "env_fixed_rate",
        }
    )
    provider = FixedEnvExchangeRateProvider.from_settings(settings)
    converter = CurrencyConverter(provider)

    info = converter.rate_info()
    assert info.base_currency == "USD"
    assert info.quote_currency == "BRL"
    assert info.rate == Decimal("5.45")
    assert info.updated_at == "2026-11-19"
    assert info.source == "env_fixed_rate"
    assert info.is_estimate is True

    estimated = converter.usd_to_brl(Decimal("100.00"))
    assert isinstance(estimated, EstimatedPriceBRL)
    assert estimated.amount == Decimal("545.00")
    assert estimated.currency == "BRL"
    assert estimated.original_amount == Decimal("100.00")
    assert estimated.original_currency == "USD"
    assert estimated.rate == Decimal("5.45")
    assert estimated.updated_at == "2026-11-19"
    assert estimated.source == "env_fixed_rate"
    assert estimated.is_estimate is True


def test_brl_to_usd_rounds_half_up_with_decimal_precision() -> None:
    provider = FixedEnvExchangeRateProvider(
        rate="5.50",
        updated_at="2026-11-19",
        source="manual_test",
    )
    converter = CurrencyConverter(provider)

    # 5000 / 5.50 = 909.0909... -> 909.09
    assert converter.brl_to_usd("5000.00") == Decimal("909.09")
    # 10.00 * 5.555 = 55.55; 10.01 * 5.55 = 55.5555 -> 55.56
    converter_round = CurrencyConverter(FixedEnvExchangeRateProvider(rate="5.55"))
    assert converter_round.usd_to_brl("10.01").amount == Decimal("55.56")
    assert converter.usd_to_brl(0).amount == Decimal("0.00")


def test_currency_converter_accepts_custom_extensible_provider() -> None:
    class CustomDynamicProvider:
        def get_usd_brl_rate(self) -> ExchangeRateInfo:
            return ExchangeRateInfo(
                rate=Decimal("6.00"),
                updated_at="2026-11-19T12:00:00Z",
                source="custom_provider",
                is_estimate=True,
            )

    provider: ExchangeRateProvider = CustomDynamicProvider()
    converter = CurrencyConverter(provider)

    converted = converter.usd_to_brl("50")
    assert converted.amount == Decimal("300.00")
    assert converted.source == "custom_provider"
    assert converted.updated_at == "2026-11-19T12:00:00Z"
    assert converted.is_estimate is True
    assert converter.brl_to_usd("300.00") == Decimal("50.00")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rate": "0"},
        {"rate": "-5.50"},
        {"rate": "NaN"},
        {"rate": True},
        {"rate": "abc"},
        {"rate": "5.50", "updated_at": "   "},
        {"rate": "5.50", "source": ""},
    ],
)
def test_fixed_env_provider_rejects_invalid_configuration(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        FixedEnvExchangeRateProvider(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_amount", [-1, "-0.01", "NaN", True, "abc", None, 12.34])
def test_converter_rejects_invalid_monetary_inputs(bad_amount: object) -> None:
    converter = CurrencyConverter(FixedEnvExchangeRateProvider(rate="5.50"))
    with pytest.raises(ValueError):
        converter.usd_to_brl(bad_amount)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        converter.brl_to_usd(bad_amount)  # type: ignore[arg-type]
