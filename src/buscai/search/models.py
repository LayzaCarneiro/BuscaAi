"""Contratos canônicos de busca e candidatos de produtos (independentes de fornecedor)."""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _reject_bool(value: object) -> object:
    if isinstance(value, bool):
        raise ValueError("Valor numérico não pode ser booleano.")
    return value


def _clean_optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Campo textual inválido.")
    cleaned = value.strip()
    return cleaned if cleaned else None


def _clean_required_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Campo textual obrigatório não pode ser vazio.")
    return value.strip()


def _validate_currency_code(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Código de moeda deve ser textual.")
    cleaned = value.strip().upper()
    if len(cleaned) != 3 or not cleaned.isascii() or not cleaned.isalpha():
        raise ValueError("Código de moeda deve ter exatamente 3 letras ASCII (ex.: 'USD').")
    return cleaned


def _validate_http_url(value: object) -> str | None:
    cleaned = _clean_optional_text(value)
    if cleaned is None:
        return None
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("URL deve usar esquema http ou https válido.")
    return cleaned


def _clean_text_tuple(values: object) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)) or not hasattr(values, "__iter__"):
        raise ValueError("Coleção textual inválida.")
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, str):
            raise ValueError("Todos os itens da coleção devem ser texto.")
        cleaned = item.strip()
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key not in seen:
            seen.add(key)
            result.append(cleaned)
    return tuple(result)


def _validate_finite_non_negative_decimal(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    if not value.is_finite() or value < 0:
        raise ValueError("Valor monetário/numérico deve ser finito e não negativo.")
    return value


class SellerInfo(BaseModel):
    """Informações opcionais do vendedor recuperadas do anúncio."""

    model_config = ConfigDict(frozen=True)

    username: str | None = None
    feedback_score: int | None = Field(default=None, ge=0)
    feedback_percentage: Decimal | None = Field(default=None, ge=0, le=100)

    @field_validator("username", mode="before")
    @classmethod
    def _clean_username(cls, value: object) -> str | None:
        return _clean_optional_text(value)

    @field_validator("feedback_score", "feedback_percentage", mode="before")
    @classmethod
    def _no_bool(cls, value: object) -> object:
        return _reject_bool(value)

    @field_validator("feedback_percentage", mode="after")
    @classmethod
    def _finite_percentage(cls, value: Decimal | None) -> Decimal | None:
        return _validate_finite_non_negative_decimal(value)


class ShippingInfo(BaseModel):
    """Informações opcionais de frete e prazo de entrega."""

    model_config = ConfigDict(frozen=True)

    cost: Decimal | None = Field(default=None, ge=0)
    currency: str | None = None
    free_shipping: bool | None = None
    estimated_min_days: int | None = Field(default=None, ge=0)
    estimated_max_days: int | None = Field(default=None, ge=0)

    @field_validator("cost", "estimated_min_days", "estimated_max_days", mode="before")
    @classmethod
    def _no_bool(cls, value: object) -> object:
        return _reject_bool(value)

    @field_validator("cost", mode="after")
    @classmethod
    def _finite_cost(cls, value: Decimal | None) -> Decimal | None:
        return _validate_finite_non_negative_decimal(value)

    @field_validator("currency", mode="before")
    @classmethod
    def _clean_currency(cls, value: object) -> str | None:
        cleaned = _clean_optional_text(value)
        return _validate_currency_code(cleaned) if cleaned is not None else None

    @model_validator(mode="after")
    def _validate_days_range(self) -> ShippingInfo:
        if (
            self.estimated_min_days is not None
            and self.estimated_max_days is not None
            and self.estimated_max_days < self.estimated_min_days
        ):
            raise ValueError("estimated_max_days deve ser maior ou igual a estimated_min_days.")
        return self


class Candidate(BaseModel):
    """Candidato de produto efêmero normalizado em memória (campos ausentes permanecem None)."""

    model_config = ConfigDict(frozen=True)

    item_id: str
    source: str
    title: str
    price: Decimal = Field(ge=0)
    currency: str
    condition: str | None = None
    categories: tuple[str, ...] = ()
    brand: str | None = None
    model: str | None = None
    short_description: str | None = None
    aspects: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    image_url: str | None = None
    item_web_url: str | None = None
    seller: SellerInfo | None = None
    shipping: ShippingInfo | None = None
    buying_options: tuple[str, ...] = ()
    details_loaded: bool = False

    @field_validator("item_id", "source", "title", mode="before")
    @classmethod
    def _required_texts(cls, value: object) -> str:
        return _clean_required_text(value)

    @field_validator("condition", "brand", "model", "short_description", mode="before")
    @classmethod
    def _optional_texts(cls, value: object) -> str | None:
        return _clean_optional_text(value)

    @field_validator("price", mode="before")
    @classmethod
    def _no_bool_price(cls, value: object) -> object:
        return _reject_bool(value)

    @field_validator("price", mode="after")
    @classmethod
    def _finite_price(cls, value: Decimal) -> Decimal:
        _validate_finite_non_negative_decimal(value)
        return value

    @field_validator("currency", mode="before")
    @classmethod
    def _valid_currency(cls, value: object) -> str:
        return _validate_currency_code(value)

    @field_validator("categories", "buying_options", mode="before")
    @classmethod
    def _clean_tuples(cls, value: object) -> tuple[str, ...]:
        return _clean_text_tuple(value)

    @field_validator("image_url", "item_web_url", mode="before")
    @classmethod
    def _valid_urls(cls, value: object) -> str | None:
        return _validate_http_url(value)

    @field_validator("aspects", mode="before")
    @classmethod
    def _clean_aspects(cls, value: object) -> dict[str, tuple[str, ...]]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("aspects deve ser um dicionário.")
        cleaned: dict[str, tuple[str, ...]] = {}
        for raw_key, raw_vals in value.items():
            if not isinstance(raw_key, str) or not raw_key.strip():
                continue
            vals = _clean_text_tuple(raw_vals)
            if vals:
                cleaned[raw_key.strip()] = vals
        return cleaned


class SearchQuery(BaseModel):
    """Consulta estruturada enviada a um SearchBackend."""

    model_config = ConfigDict(frozen=True)

    query_text: str
    marketplace_id: str = "EBAY_US"
    category_ids: tuple[str, ...] = ()
    min_price_usd: Decimal | None = Field(default=None, ge=0)
    max_price_usd: Decimal | None = Field(default=None, ge=0)
    currency: str = "USD"
    conditions: tuple[str, ...] = ()
    condition_ids: tuple[str, ...] = ()
    fixed_price_only: bool = True
    limit: int = Field(default=30, ge=1, le=200)
    offset: int = Field(default=0, ge=0)

    @field_validator("query_text", mode="before")
    @classmethod
    def _valid_query_text(cls, value: object) -> str:
        return _clean_required_text(value)

    @field_validator("marketplace_id", mode="before")
    @classmethod
    def _valid_marketplace(cls, value: object) -> str:
        return _clean_required_text(value).upper()

    @field_validator("currency", mode="before")
    @classmethod
    def _valid_currency(cls, value: object) -> str:
        return _validate_currency_code(value)

    @field_validator("category_ids", "conditions", mode="before")
    @classmethod
    def _clean_tuples(cls, value: object) -> tuple[str, ...]:
        return _clean_text_tuple(value)

    @field_validator("condition_ids", mode="before")
    @classmethod
    def _valid_condition_ids(cls, value: object) -> tuple[str, ...]:
        cleaned = _clean_text_tuple(value)
        for item in cleaned:
            if not (item.isascii() and item.isdigit()):
                raise ValueError(
                    "condition_ids deve conter apenas códigos numéricos ASCII da eBay (ex.: '1000', '2000')."
                )
        return cleaned

    @field_validator("min_price_usd", "max_price_usd", "limit", "offset", mode="before")
    @classmethod
    def _no_bool(cls, value: object) -> object:
        return _reject_bool(value)

    @field_validator("min_price_usd", "max_price_usd", mode="after")
    @classmethod
    def _finite_bounds(cls, value: Decimal | None) -> Decimal | None:
        return _validate_finite_non_negative_decimal(value)

    @model_validator(mode="after")
    def _validate_price_bounds(self) -> SearchQuery:
        if (
            self.min_price_usd is not None
            and self.max_price_usd is not None
            and self.max_price_usd < self.min_price_usd
        ):
            raise ValueError("max_price_usd deve ser maior ou igual a min_price_usd.")
        return self


class SearchResponse(BaseModel):
    """Página efêmera de candidatos retornados por um SearchBackend."""

    model_config = ConfigDict(frozen=True)

    candidates: tuple[Candidate, ...] = ()
    total_estimated: int | None = Field(default=None, ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(default=0, ge=0)
    calls_used: int = Field(default=1, ge=0)

    @field_validator("total_estimated", "limit", "offset", "calls_used", mode="before")
    @classmethod
    def _no_bool(cls, value: object) -> object:
        return _reject_bool(value)
