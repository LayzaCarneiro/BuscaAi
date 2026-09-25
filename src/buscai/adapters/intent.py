"""Converte UserIntent em critérios verificáveis pelo catálogo."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from buscai.catalog import CatalogProvider, Product
from buscai.tools import SearchResultPage, search_products
from buscai.tools.products import normalize_text
from userintent.schema import UserIntent


class ConstraintUnavailableError(ValueError):
    """Uma exigência não pode ser verificada com os dados do catálogo."""


class ClarificationRequired(ValueError):
    """A intenção pede mais informações antes da busca."""


@dataclass(frozen=True)
class IntentSearchCriteria:
    query: str
    category: str | None
    min_price: Decimal | None
    max_price: Decimal | None
    min_inclusive: bool
    max_inclusive: bool
    price_is_hard: bool
    brands: tuple[str, ...]
    excluded_brands: tuple[str, ...]
    required_features: tuple[str, ...]
    preferred_features: tuple[str, ...]
    excluded_features: tuple[str, ...]
    use_cases: tuple[str, ...]
    profile_context: tuple[str, ...]
    clarification_needed: bool
    clarification_question: str | None


@dataclass(frozen=True)
class IntentSearchResult:
    results: SearchResultPage
    criteria: IntentSearchCriteria
    unapplied_preferences: tuple[str, ...]


def adapt_intent(
    intent: UserIntent, *, category_map: Mapping[str, str] | None = None
) -> IntentSearchCriteria:
    """Preserva as regras do UserIntent sem acoplar o schema ao fornecedor."""
    category = intent.category
    if category is not None and category_map is not None:
        aliases = {normalize_text(key): value for key, value in category_map.items()}
        category = aliases.get(normalize_text(category), category)

    return IntentSearchCriteria(
        query=intent.query,
        category=category,
        min_price=Decimal(str(intent.price.min_price)) if intent.price.min_price is not None else None,
        max_price=Decimal(str(intent.price.max_price)) if intent.price.max_price is not None else None,
        min_inclusive=intent.price.min_inclusive,
        max_inclusive=intent.price.max_inclusive,
        price_is_hard=intent.price.is_hard_constraint,
        brands=tuple(intent.brands),
        excluded_brands=tuple(intent.excluded_brands),
        required_features=tuple(intent.required_features),
        preferred_features=tuple(intent.preferred_features),
        excluded_features=tuple(intent.excluded_features),
        use_cases=tuple(intent.use_cases),
        profile_context=tuple(intent.profile_context),
        clarification_needed=intent.clarification_needed,
        clarification_question=intent.clarification_question,
    )


def _matches_feature(feature: str, searchable: str) -> bool:
    terms = normalize_text(feature).split()
    if not terms:
        raise ConstraintUnavailableError(f"Característica impossível de verificar: {feature!r}.")
    available_terms = set(searchable.split())
    return all(term in available_terms for term in terms)


def _satisfies_constraints(
    product: Product, criteria: IntentSearchCriteria, budget_currency: str | None
) -> bool:
    if criteria.category is not None:
        if product.category is None:
            raise ConstraintUnavailableError("O catálogo não informa a categoria de um produto candidato.")
        if normalize_text(product.category) != normalize_text(criteria.category):
            return False

    if criteria.price_is_hard and (criteria.min_price is not None or criteria.max_price is not None):
        if product.currency is None or product.currency.upper() != budget_currency:
            raise ConstraintUnavailableError("Moeda do produto ausente ou diferente da moeda do orçamento.")
        if criteria.min_price is not None and (
            product.price < criteria.min_price
            or (product.price == criteria.min_price and not criteria.min_inclusive)
        ):
            return False
        if criteria.max_price is not None and (
            product.price > criteria.max_price
            or (product.price == criteria.max_price and not criteria.max_inclusive)
        ):
            return False

    if criteria.excluded_brands:
        brand = product.attributes.get("brand")
        if not isinstance(brand, str) or not brand.strip():
            raise ConstraintUnavailableError("O catálogo não informa a marca de um produto candidato.")
        if normalize_text(brand) in {normalize_text(item) for item in criteria.excluded_brands}:
            return False

    if criteria.required_features:
        description = product.attributes.get("description")
        tags = product.attributes.get("tags")
        fields = [description] if isinstance(description, str) and description.strip() else []
        if isinstance(tags, list):
            fields.extend(tag for tag in tags if isinstance(tag, str) and tag.strip())
        structured_features = product.attributes.get("features")
        if isinstance(structured_features, list):
            fields.extend(feature for feature in structured_features if isinstance(feature, str) and feature.strip())
        if not fields:
            raise ConstraintUnavailableError(
                "O catálogo não traz descrição, tags ou características para verificar exigências."
            )
        if any(
            not any(_matches_feature(feature, normalize_text(field)) for field in fields)
            for feature in criteria.required_features
        ):
            return False

    if criteria.excluded_features:
        features = product.attributes.get("features")
        if product.attributes.get("features_complete") is not True or not isinstance(features, list):
            raise ConstraintUnavailableError(
                "O catálogo não informa uma lista completa de características para aplicar excluded_features."
            )
        if any(not isinstance(feature, str) for feature in features):
            raise ConstraintUnavailableError("A lista de características do catálogo é inválida.")
        if any(
            _matches_feature(excluded, normalize_text(feature))
            for excluded in criteria.excluded_features
            for feature in features
        ):
            return False

    return True


def search_intent(
    intent: UserIntent,
    *,
    catalog: CatalogProvider | None = None,
    category_map: Mapping[str, str] | None = None,
    budget_currency: str | None = None,
    limit: int = 10,
    offset: int = 0,
) -> IntentSearchResult:
    """Aplica exigências verificáveis e explicita preferências ainda sem ranking."""
    criteria = adapt_intent(intent, category_map=category_map)
    if criteria.clarification_needed:
        raise ClarificationRequired(criteria.clarification_question or "A consulta precisa de esclarecimento.")

    hard_price = criteria.price_is_hard and (criteria.min_price is not None or criteria.max_price is not None)
    if hard_price and (
        not isinstance(budget_currency, str)
        or len(budget_currency) != 3
        or not budget_currency.isascii()
        or not budget_currency.isalpha()
    ):
        raise ConstraintUnavailableError("Informe a moeda do orçamento para aplicar a restrição de preço.")
    budget_currency = budget_currency.upper() if hard_price else None

    preferences: list[str] = []
    if criteria.brands:
        preferences.append("brands")
    if criteria.preferred_features:
        preferences.append("preferred_features")
    if criteria.use_cases:
        preferences.append("use_cases")
    if criteria.profile_context:
        preferences.append("profile_context")
    if not criteria.price_is_hard and (criteria.min_price is not None or criteria.max_price is not None):
        preferences.append("price")

    results = search_products(
        criteria.query,
        catalog=catalog,
        limit=limit,
        offset=offset,
        filter_product=lambda product: _satisfies_constraints(product, criteria, budget_currency),
    )
    return IntentSearchResult(results=results, criteria=criteria, unapplied_preferences=tuple(preferences))
