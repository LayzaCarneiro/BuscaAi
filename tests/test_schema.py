from __future__ import annotations

from google.genai import _transformers as genai_transformers
from pydantic import ValidationError
import pytest

from userintent.schema import PriceConstraint, UserIntent


def _collect_keys(obj: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(str(k))
            keys.update(_collect_keys(v))
    elif isinstance(obj, list):
        for item in obj:
            keys.update(_collect_keys(item))
    return keys

from src.userintent.schema import UserIntent

def test_schema_accepts_expected_shape():
    intent = UserIntent(
        category="  notebook  ",
        price={
            "max_price": 4000,
            "max_inclusive": True,
            "is_hard_constraint": True,
            "currency": "R$",
        },
        brands=["Lenovo"],
        models=[" ThinkPad X1 ", "thinkpad x1"],
        use_cases=["programação", "jogos"],
        profile_context=["estudante de computação"],
        required_features=["16 GB de RAM"],
        condition=" novo ",
        query="  notebook para programação e jogos  ",
    )
    assert intent.category == "notebook"
    assert intent.price.max_price == 4000
    assert intent.price.currency == "BRL"
    assert intent.brands == ["Lenovo"]
    assert intent.models == ["ThinkPad X1"]
    assert intent.condition == "novo"
    assert intent.query == "notebook para programação e jogos"


def test_price_currency_and_numeric_validation():
    usd_price = PriceConstraint(max_price=800, currency="us$", is_hard_constraint=True)
    assert usd_price.currency == "USD"
    assert PriceConstraint(currency="   ").currency is None

    for bad_currency in ("US", "123", "real brasileiro"):
        with pytest.raises(ValidationError):
            PriceConstraint(max_price=100, currency=bad_currency)

    for bad_bound in (float("nan"), float("inf"), True):
        with pytest.raises(ValidationError):
            PriceConstraint(max_price=bad_bound)  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        UserIntent(query="   ")


def test_extra_fields_are_ignored_without_polluting_model():
    intent = UserIntent(
        category="notebook",
        query="notebook",
        unexpected="x",
        price={"max_price": 4000, "extra_nested": True},
    )
    dumped = intent.model_dump()
    assert "unexpected" not in dumped
    assert "extra_nested" not in dumped["price"]
    assert not hasattr(intent, "unexpected")
    assert not hasattr(intent.price, "extra_nested")
    assert isinstance(intent.price, PriceConstraint)


def test_invalid_price_range_is_rejected():
    with pytest.raises(ValidationError, match="max_price must be >= min_price"):
        UserIntent(
            category="notebook",
            query="notebook",
            price={"min_price": 3000, "max_price": 2000},
        )


def test_gemini_mldev_schema_omits_additional_properties():
    class DummyApiClient:
        vertexai = False

    schema = genai_transformers.t_schema(DummyApiClient(), UserIntent)
    payload = schema.model_dump(by_alias=True, exclude_none=True)
    keys = _collect_keys(payload)
    assert "additionalProperties" not in keys
    assert "additional_properties" not in keys
    prop_keys = _collect_keys(payload.get("properties", {}))
    assert {"models", "condition", "currency"}.issubset(prop_keys)
