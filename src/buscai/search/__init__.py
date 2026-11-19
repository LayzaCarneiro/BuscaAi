"""Módulo de busca em marketplace, modelos de candidatos e controle de cota."""

from .backend import SearchBackend
from .ebay_auth import EbayOAuthClient
from .ebay_backend import EbayLiveBackend, build_ebay_search_params
from .ebay_mapper import (
    map_item_detail,
    map_item_summary,
    merge_candidate_details,
    parse_search_response,
)
from .models import Candidate, SearchQuery, SearchResponse, SellerInfo, ShippingInfo
from .query_builder import (
    DEFAULT_MVP_CATEGORIES,
    AppliedBudget,
    CategorySpec,
    IntentQueryPlanResult,
    PlannedQuery,
    QueryBuilder,
    SearchPlan,
    build_search_plan_from_text,
)
from .quota import QuotaGuard, QuotaSnapshot, RequestCallBudget

__all__ = [
    "AppliedBudget",
    "Candidate",
    "CategorySpec",
    "DEFAULT_MVP_CATEGORIES",
    "EbayLiveBackend",
    "EbayOAuthClient",
    "IntentQueryPlanResult",
    "PlannedQuery",
    "QueryBuilder",
    "QuotaGuard",
    "QuotaSnapshot",
    "RequestCallBudget",
    "SearchBackend",
    "SearchPlan",
    "SearchQuery",
    "SearchResponse",
    "SellerInfo",
    "ShippingInfo",
    "build_ebay_search_params",
    "build_search_plan_from_text",
    "map_item_detail",
    "map_item_summary",
    "merge_candidate_details",
    "parse_search_response",
]
