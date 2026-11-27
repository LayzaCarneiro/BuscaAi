"""Construção determinística e extensível de consultas para a eBay Browse API (EBAY_US)."""

from __future__ import annotations

import inspect
import re
import unicodedata
from collections.abc import Mapping, Sequence
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from buscai.catalog_registry import load_default_registry
from buscai.config import Settings
from buscai.pricing import CurrencyConverter, ExchangeRateInfo, FixedEnvExchangeRateProvider
from buscai.search.models import SearchQuery
from userintent.budget import merge_explicit_budget
from userintent.schema import UserIntent

_TWO_DECIMALS = Decimal("0.01")
MAX_QUERY_TEXT_LENGTH = 100


class IntentExtractorLike(Protocol):
    """Protocolo mínimo para extratores de intenção compatíveis com o integrador."""

    def extract(self, user_text: str) -> UserIntent:
        """Extrai UserIntent estruturado a partir de texto em linguagem natural."""
        ...


class CategorySpec(BaseModel):
    """Definição declarativa de uma categoria para mapeamento EN-US na eBay."""

    model_config = ConfigDict(frozen=True)

    canonical_id: str
    primary_term_en: str
    aliases: tuple[str, ...] = ()
    subtype_terms_en: dict[str, str] = Field(default_factory=dict)
    standalone_models: tuple[str, ...] = ()


class AppliedBudget(BaseModel):
    """Resumo transparente da faixa de orçamento aplicada na consulta eBay."""

    model_config = ConfigDict(frozen=True)

    original_currency: str | None = None
    original_min_price: Decimal | None = None
    original_max_price: Decimal | None = None
    min_price_usd: Decimal | None = None
    max_price_usd: Decimal | None = None
    min_inclusive: bool = True
    max_inclusive: bool = True
    is_hard_constraint: bool = False
    exchange_rate: ExchangeRateInfo | None = None


class PlannedQuery(BaseModel):
    """Consulta individual planejada para execução na Browse API."""

    model_config = ConfigDict(frozen=True)

    role: Literal["primary", "alternative"]
    query: SearchQuery
    rationale: str


class SearchPlan(BaseModel):
    """Plano completo de consulta gerado pelo QueryBuilder sem chamadas HTTP."""

    model_config = ConfigDict(frozen=True)

    canonical_category: str | None = None
    is_supported_mvp_category: bool = False
    queries: tuple[PlannedQuery, ...] = ()
    budget: AppliedBudget = Field(default_factory=AppliedBudget)
    ignored_preferences: tuple[str, ...] = ()
    excluded_brands: tuple[str, ...] = ()
    excluded_features: tuple[str, ...] = ()
    clarification_needed: bool = False
    clarification_question: str | None = None

    @property
    def primary_query(self) -> SearchQuery | None:
        return self.queries[0].query if self.queries else None

    @property
    def search_queries(self) -> tuple[SearchQuery, ...]:
        return tuple(item.query for item in self.queries)


class IntentQueryPlanResult(BaseModel):
    """Resultado integrado do fluxo Texto PT-BR -> UserIntent -> SearchPlan."""

    model_config = ConfigDict(frozen=True)

    raw_input: str
    intent: UserIntent
    plan: SearchPlan
    clarification_needed: bool = False
    clarification_question: str | None = None

    @property
    def primary_query(self) -> SearchQuery | None:
        return self.plan.primary_query

    @property
    def search_queries(self) -> tuple[SearchQuery, ...]:
        return self.plan.search_queries


DEFAULT_MVP_CATEGORIES: tuple[CategorySpec, ...] = tuple(
    CategorySpec(
        canonical_id=cat.canonical_id,
        primary_term_en=cat.primary_term_en,
        aliases=cat.aliases,
        subtype_terms_en=cat.subtype_terms_en,
        standalone_models=cat.standalone_models,
    )
    for cat in load_default_registry().categories
)

_MVP_CANONICAL_IDS = frozenset({"notebook", "smartphone", "monitor", "fone de ouvido"})

# Mapeamento oficial da eBay Browse API (`conditions` / `conditionIds`).
_CONDITION_MAP: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "novo": (("NEW",), ()),
    "lacrado": (("NEW",), ()),
    "new": (("NEW",), ()),
    "brand new": (("NEW",), ()),
    "usado": (("USED",), ()),
    "seminovo": (("USED",), ()),
    "semi-novo": (("USED",), ()),
    "pre-owned": (("USED",), ()),
    "used": (("USED",), ()),
    "recondicionado": ((), ("2000", "2010", "2020", "2030")),
    "recondicionados": ((), ("2000", "2010", "2020", "2030")),
    "renovado": ((), ("2000", "2010", "2020", "2030")),
    "refurbished": ((), ("2000", "2010", "2020", "2030")),
    "certified refurbished": ((), ("2000", "2010", "2020", "2030")),
    "open box": ((), ("1500",)),
    "caixa aberta": ((), ("1500",)),
}

_SUBJECTIVE_FEATURES: frozenset[str] = frozenset(
    {
        "bom",
        "boa",
        "bons",
        "boas",
        "otimo",
        "otima",
        "melhor",
        "melhor possivel",
        "barato",
        "barata",
        "baratos",
        "preco acessivel",
        "custo-beneficio",
        "custo beneficio",
        "economico",
        "economica",
        "potente",
        "confortavel",
        "bonito",
        "bonita",
        "legal",
        "grande",
        "tela grande",
        "pequeno",
        "compacto",
        "compacta",
        "leve",
        "portabilidade",
        "interface simples",
        "boa camera",
        "camera boa",
        "bateria longa",
        "bateria de longa duracao",
        "boa bateria",
        "bateria boa",
        "boa autonomia",
        "boa autonomia de bateria",
        "boa qualidade de microfone",
        "microfone bom",
        "rapido",
        "rapida",
        "duravel",
        "resistente",
        "silencioso",
        "alta qualidade",
        "excelente",
    }
)

_FEATURE_PHRASE_TRANSLATIONS: tuple[tuple[str, str], ...] = (
    ("cancelamento ativo de ruido", "ANC"),
    ("cancelamento de ruido ativo", "ANC"),
    ("cancelamento de ruido", "noise cancelling"),
    ("noise cancelling", "noise cancelling"),
    ("noise canceling", "noise cancelling"),
    ("placa de video dedicada", "dedicated GPU"),
    ("gpu dedicada", "dedicated GPU"),
    ("teclado retroiluminado", "backlit keyboard"),
    ("tela sensivel ao toque", "touchscreen"),
    ("tela touch", "touchscreen"),
    ("resistente a agua", "waterproof"),
    ("a prova d agua", "waterproof"),
    ("a prova de agua", "waterproof"),
    ("carregamento rapido", "fast charging"),
    ("resistente a poeira", "dustproof"),
    ("resistente ao suor", "sweatproof"),
    ("leitor de digitais", "fingerprint"),
    ("tela dobravel", "foldable"),
    ("teclado numerico", "numpad"),
    ("tela curva", "curved"),
    ("instalacao de linux", "Linux"),
    ("compatibilidade com linux", "Linux"),
    ("compativel com linux", "Linux"),
    ("sem fio", "wireless"),
    ("com fio", "wired"),
    ("microfone", "microphone"),
    ("caneta", "stylus"),
    ("dobravel", "foldable"),
    ("portatil", "portable"),
    ("monocromatica", "monochrome"),
    ("monocromatico", "monochrome"),
    ("desbloqueado", "unlocked"),
    ("dual chip", "dual SIM"),
    ("dual sim", "dual SIM"),
    ("curvo", "curved"),
    ("curva", "curved"),
    ("ultrawide", "ultrawide"),
    ("ultra wide", "ultrawide"),
    ("usb-c", "USB-C"),
    ("usb c", "USB-C"),
    ("tipo c", "USB-C"),
    ("full hd", "Full HD"),
    ("mini led", "Mini LED"),
    ("true wireless", "TWS"),
    ("bluetooth", "Bluetooth"),
    ("touchscreen", "touchscreen"),
    ("displayport", "DisplayPort"),
    ("thunderbolt", "Thunderbolt"),
    ("freesync", "FreeSync"),
    ("g-sync", "G-Sync"),
    ("backlit", "backlit"),
    ("amoled", "AMOLED"),
    ("oled", "OLED"),
    ("qled", "QLED"),
    ("ips", "IPS"),
    ("hdr", "HDR"),
    ("hdmi", "HDMI"),
    ("wi-fi", "Wi-Fi"),
    ("wifi", "Wi-Fi"),
    ("nvme", "NVMe"),
    ("ssd", "SSD"),
    ("anc", "ANC"),
    ("nfc", "NFC"),
    ("5g", "5G"),
    ("4k", "4K"),
    ("8k", "8K"),
    ("qhd", "QHD"),
    ("uhd", "UHD"),
    ("1080p", "1080p"),
    ("1440p", "1440p"),
    ("2160p", "4K"),
    ("linux", "Linux"),
    ("windows", "Windows"),
    ("macos", "macOS"),
    ("gps", "GPS"),
    ("tws", "TWS"),
    ("rgb", "RGB"),
)

_SYNONYM_EXPANSIONS: dict[str, str] = {
    "noise cancelling": "ANC",
    "anc": "noise cancelling",
    "4K": "UHD",
    "UHD": "4K",
    "Full HD": "1080p",
    "1080p": "Full HD",
    "QHD": "1440p",
    "1440p": "QHD",
}

_RAM_RE = re.compile(
    r"^(?:(?:pelo\s+menos|no\s+minimo|minimo|acima\s+de|de|com)\s+)?(\d+)\s*(gb|mb)\s*(?:de\s+)?(?:memoria\s+)?ram$"
)
_RAM_REV_RE = re.compile(r"^(?:memoria\s+)?ram\s+(?:de\s+)?(\d+)\s*(gb|mb)$")
_SSD_RE_1 = re.compile(
    r"^(?:(?:pelo\s+menos|no\s+minimo|minimo|de|com)\s+)?(nvme\s+ssd|ssd\s+nvme|nvme|ssd)\s*(?:de\s+)?(\d+(?:[.,]\d+)?)\s*(gb|tb)$"
)
_SSD_RE_2 = re.compile(
    r"^(?:(?:pelo\s+menos|no\s+minimo|minimo|de|com)\s+)?(\d+(?:[.,]\d+)?)\s*(gb|tb)\s*(?:de\s+)?(nvme\s+ssd|ssd\s+nvme|nvme|ssd)$"
)
_STORAGE_RE = re.compile(
    r"^(?:(?:pelo\s+menos|no\s+minimo|minimo|acima\s+de|de|com)\s+)?(\d+(?:[.,]\d+)?)\s*(gb|tb)(?:\s+(?:de\s+)?(?:armazenamento|espaco|hd|memoria\s+interna|capacidade)(?:\s+interno)?)?$"
)
_STORAGE_REV_RE = re.compile(
    r"^(?:armazenamento(?:\s+interno)?|hd|capacidade)\s+(?:de\s+)?(?:(?:pelo\s+menos|minimo)\s+)?(\d+(?:[.,]\d+)?)\s*(gb|tb)$"
)
_INCH_RE = re.compile(
    r"^(?:(?:tela|monitor|display|notebook)\s+(?:de\s+)?)?(?:(?:pelo\s+menos|minimo)\s+)?(\d+(?:[.,]\d+)?)\s*(?:polegadas?|pol\.?|inch(?:es)?|\")$"
)
_HZ_RE = re.compile(
    r"^(?:(?:tela|monitor|taxa(?:\s+de\s+atualizacao)?)\s+(?:de\s+)?)?(\d+)\s*hz$"
)
_BATTERY_MAH_RE = re.compile(r"^(?:bateria\s+(?:de\s+)?)?(\d{3,5})\s*mah$")
_WATTS_RE = re.compile(r"^(\d+)\s*w(?:atts?)?$")
_COMPAT_PREFIX_RE = re.compile(r"^compativel\s+com\s+")
_CONTROLLED_TECH_CODE_RE = re.compile(
    r"^(?:"
    r"(?:intel\s+)?core\s+(?:i[3579]|ultra\s+[579])(?:\s+\d{4,5}[a-z]{0,2})?"
    r"|(?:amd\s+)?ryzen\s+[3579](?:\s+\d{4}[a-z]{0,2})?"
    r"|apple\s+m[1-4](?:\s+(?:pro|max|ultra))?|m[1-4](?:\s+(?:pro|max|ultra))?"
    r"|(?:nvidia\s+)?(?:geforce\s+)?(?:rtx|gtx)\s*\d{3,4}(?:\s*(?:ti|super))?"
    r"|(?:amd\s+)?(?:radeon\s+)?rx\s*\d{3,4}(?:\s*xt)?"
    r"|snapdragon(?:\s+[a-z0-9+.-]+){1,3}"
    r"|ip\d{2}"
    r"|wi-?fi\s*(?:5|6e?|7)"
    r"|bluetooth\s+\d(?:\.\d)?"
    r"|lpddr\d[x]?|ddr\d"
    r"|pcie\s*\d(?:\.\d)?"
    r"|hdmi\s*\d(?:\.\d)?|thunderbolt\s*\d|usb\s*[234](?:\.\d)?"
    r"|esim|4g|lte|nfc|5g"
    r")$",
    re.IGNORECASE,
)


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _norm_key(text: str) -> str:
    cleaned = " ".join(text.strip().casefold().split())
    return _strip_accents(cleaned)


def _format_num(raw: str) -> str:
    normalized = raw.replace(",", ".")
    return normalized


def _contains_whole_phrase(haystack_norm: str, phrase_norm: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(phrase_norm)}(?!\w)", haystack_norm))


def _dedupe_tokens(parts: Sequence[str]) -> str:
    tokens: list[str] = []
    seen: set[str] = set()
    for part in parts:
        for token in part.strip().split():
            norm = _norm_key(token)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            tokens.append(token)
    return " ".join(tokens)


def _query_fingerprint(query: SearchQuery) -> tuple[object, ...]:
    return (
        " ".join(query.query_text.casefold().split()),
        query.marketplace_id,
        query.category_ids,
        query.min_price_usd,
        query.max_price_usd,
        query.currency,
        query.conditions,
        query.condition_ids,
        query.fixed_price_only,
    )


def _translate_feature_en(raw_feature: str) -> tuple[str | None, str | None]:
    """Traduz característica PT-BR objetiva para EN-US sem inventar dados.

    Retorna `(termo_en, motivo_ignorado)`. Termos subjetivos retornam `(None, "subjective:...")`.
    """
    cleaned = " ".join(raw_feature.strip().split())
    if not cleaned:
        return None, None
    norm = _norm_key(cleaned)

    if norm in _SUBJECTIVE_FEATURES:
        return None, f"subjective:{cleaned}"

    norm_no_compat = _COMPAT_PREFIX_RE.sub("", norm).strip()

    ram_match = _RAM_RE.match(norm_no_compat) or _RAM_REV_RE.match(norm_no_compat)
    if ram_match:
        num, unit = ram_match.groups()
        return f"{num}{unit.upper()} RAM", None

    ssd_match_1 = _SSD_RE_1.match(norm_no_compat)
    if ssd_match_1:
        kind, num, unit = ssd_match_1.groups()
        label = "NVMe SSD" if "nvme" in kind else "SSD"
        return f"{_format_num(num)}{unit.upper()} {label}", None

    ssd_match_2 = _SSD_RE_2.match(norm_no_compat)
    if ssd_match_2:
        num, unit, kind = ssd_match_2.groups()
        label = "NVMe SSD" if "nvme" in kind else "SSD"
        return f"{_format_num(num)}{unit.upper()} {label}", None

    storage_match = _STORAGE_RE.match(norm_no_compat) or _STORAGE_REV_RE.match(norm_no_compat)
    if storage_match:
        num, unit = storage_match.groups()
        return f"{_format_num(num)}{unit.upper()}", None

    inch_match = _INCH_RE.match(norm_no_compat)
    if inch_match:
        return f"{_format_num(inch_match.group(1))} inch", None

    hz_match = _HZ_RE.match(norm_no_compat)
    if hz_match:
        return f"{hz_match.group(1)}Hz", None

    mah_match = _BATTERY_MAH_RE.match(norm_no_compat)
    if mah_match:
        return f"{mah_match.group(1)}mAh", None

    watts_match = _WATTS_RE.match(norm_no_compat)
    if watts_match:
        return f"{watts_match.group(1)}W", None

    for pt_phrase, en_term in _FEATURE_PHRASE_TRANSLATIONS:
        if norm_no_compat == pt_phrase:
            return en_term, None

    # Verifica combinações conhecidas (ex.: "Bluetooth com cancelamento de ruído" em um único item).
    composite_hits: list[str] = []
    remaining = norm_no_compat
    for pt_phrase, en_term in _FEATURE_PHRASE_TRANSLATIONS:
        if _contains_whole_phrase(remaining, pt_phrase):
            composite_hits.append(en_term)
            remaining = re.sub(rf"(?<!\w){re.escape(pt_phrase)}(?!\w)", " ", remaining)
    leftover_words = [
        word
        for word in re.findall(r"[a-z0-9+.-]+", remaining)
        if word not in {"de", "da", "do", "com", "para", "em", "e", "ou", "a", "o"}
    ]
    if composite_hits and not leftover_words:
        return _dedupe_tokens(composite_hits), None

    # Vocabulário controlado: aceita somente códigos técnicos padronizados explicitamente reconhecidos.
    # Qualquer texto livre em português (com ou sem acentos) é omitido para não zerar recall na eBay.
    if cleaned.isascii() and _CONTROLLED_TECH_CODE_RE.match(norm_no_compat):
        return cleaned, None

    return None, f"unmapped_feature:{cleaned}"


class QueryBuilder:
    """Converte `UserIntent` estruturado em `SearchPlan` compatível com a eBay Browse API."""

    def __init__(
        self,
        *,
        currency_converter: CurrencyConverter,
        marketplace_id: str = "EBAY_US",
        default_budget_currency: str = "BRL",
        max_queries: int = 2,
        max_query_length: int = MAX_QUERY_TEXT_LENGTH,
        extra_categories: Sequence[CategorySpec] | None = None,
        category_id_map: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        if not isinstance(marketplace_id, str) or not marketplace_id.strip():
            raise ValueError("marketplace_id não pode ser vazio.")
        if (
            not isinstance(default_budget_currency, str)
            or len(default_budget_currency.strip()) != 3
            or not default_budget_currency.strip().isalpha()
        ):
            raise ValueError("default_budget_currency deve ser um código ISO-3 válido.")
        if isinstance(max_queries, bool) or not isinstance(max_queries, int) or max_queries < 1:
            raise ValueError("max_queries deve ser um inteiro positivo (>= 1).")
        if isinstance(max_query_length, bool) or not isinstance(max_query_length, int) or max_query_length < 16:
            raise ValueError("max_query_length deve ser um inteiro >= 16.")

        self._currency_converter = currency_converter
        self._marketplace_id = marketplace_id.strip().upper()
        self._default_budget_currency = default_budget_currency.strip().upper()
        self._max_queries = max_queries
        self._max_query_length = max_query_length

        self._categories: dict[str, CategorySpec] = {}
        self._alias_to_canonical: dict[str, str] = {}
        for spec in (*DEFAULT_MVP_CATEGORIES, *(extra_categories or ())):
            canonical_key = _norm_key(spec.canonical_id)
            self._categories[canonical_key] = spec
            self._alias_to_canonical[canonical_key] = canonical_key
            for alias in spec.aliases:
                self._alias_to_canonical[_norm_key(alias)] = canonical_key

        self._category_id_map: dict[str, tuple[str, ...]] = {}
        if category_id_map is not None:
            for key, raw_ids in category_id_map.items():
                cleaned_ids = tuple(str(cid).strip() for cid in raw_ids if str(cid).strip())
                if cleaned_ids:
                    self._category_id_map[_norm_key(key)] = cleaned_ids

    @property
    def currency_converter(self) -> CurrencyConverter:
        """Retorna o conversor cambial configurado para conciliação segura de orçamento."""
        return self._currency_converter

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        max_queries: int = 2,
        max_query_length: int = MAX_QUERY_TEXT_LENGTH,
        extra_categories: Sequence[CategorySpec] | None = None,
        category_id_map: Mapping[str, Sequence[str]] | None = None,
    ) -> QueryBuilder:
        """Constrói QueryBuilder usando Settings e FixedEnvExchangeRateProvider centralizados."""
        converter = CurrencyConverter(FixedEnvExchangeRateProvider.from_settings(settings))
        return cls(
            currency_converter=converter,
            marketplace_id=settings.ebay_marketplace_id,
            max_queries=max_queries,
            max_query_length=max_query_length,
            extra_categories=extra_categories,
            category_id_map=category_id_map,
        )

    def build(self, intent: UserIntent, *, limit: int = 30) -> SearchPlan:
        """Transforma `UserIntent` em `SearchPlan` sem executar chamadas HTTP."""
        if not isinstance(intent, UserIntent):
            raise TypeError("intent deve ser uma instância válida de UserIntent.")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 200:
            raise ValueError("limit deve ser um inteiro entre 1 e 200.")

        spec, canonical_category = self._resolve_category(intent)
        is_mvp = (
            canonical_category is not None
            and _norm_key(canonical_category) in _MVP_CANONICAL_IDS
        )

        budget = self._build_budget(intent)
        ignored: list[str] = []

        # Use cases orientam explicação e futuros perfis (Etapa 5), sem virar filtro rígido no q.
        for use_case in intent.use_cases:
            ignored.append(f"use_case:{use_case}")

        conditions, condition_ids, condition_ignored = self._map_condition(intent.condition)
        if condition_ignored is not None:
            ignored.append(condition_ignored)

        if intent.clarification_needed:
            return SearchPlan(
                canonical_category=canonical_category,
                is_supported_mvp_category=is_mvp,
                queries=(),
                budget=budget,
                ignored_preferences=tuple(ignored),
                excluded_brands=tuple(intent.excluded_brands),
                excluded_features=tuple(intent.excluded_features),
                clarification_needed=True,
                clarification_question=intent.clarification_question
                or "Pode informar mais detalhes sobre o produto desejado?",
            )

        req_terms: list[str] = []
        for feat in intent.required_features:
            translated, reason = _translate_feature_en(feat)
            if translated is not None:
                req_terms.append(translated)
            elif reason is not None:
                ignored.append(reason)

        pref_terms: list[str] = []
        for feat in intent.preferred_features:
            translated, reason = _translate_feature_en(feat)
            if translated is not None:
                pref_terms.append(translated)
                ignored.append(f"deferred_preference:{feat}")
            elif reason is not None:
                ignored.append(reason)

        category_ids: tuple[str, ...] = ()
        if spec is not None:
            category_ids = self._category_id_map.get(_norm_key(spec.canonical_id), ())

        category_term_en = self._pick_category_term(intent, spec)
        primary_q_text = self._compose_query_text(
            brands=intent.brands,
            models=intent.models,
            category_term_en=category_term_en,
            feature_terms=req_terms,
            fallback_query=intent.query,
        )

        # Nota sobre inclusividade de preço: a Browse API interpreta `price:[lo..hi]`
        # com fronteiras inclusivas; preservamos `max_inclusive` / `min_inclusive` em
        # `AppliedBudget` para aplicação exata na etapa de validação determinística.
        primary_query = SearchQuery(
            query_text=primary_q_text,
            marketplace_id=self._marketplace_id,
            category_ids=category_ids,
            min_price_usd=budget.min_price_usd,
            max_price_usd=budget.max_price_usd,
            currency="USD",
            conditions=conditions,
            condition_ids=condition_ids,
            fixed_price_only=True,
            limit=limit,
            offset=0,
        )

        planned: list[PlannedQuery] = [
            PlannedQuery(
                role="primary",
                query=primary_query,
                rationale="Consulta principal preservando marca, modelo, categoria EN-US e exigências técnicas.",
            )
        ]
        seen_fps = {_query_fingerprint(primary_query)}

        if self._max_queries > 1:
            alternatives = self._generate_alternative_candidates(
                intent=intent,
                category_term_en=category_term_en,
                req_terms=req_terms,
                pref_terms=pref_terms,
                base_query=primary_query,
                budget=budget,
            )
            for alt_query, rationale in alternatives:
                fp = _query_fingerprint(alt_query)
                if fp in seen_fps:
                    continue
                seen_fps.add(fp)
                planned.append(
                    PlannedQuery(
                        role="alternative",
                        query=alt_query,
                        rationale=rationale,
                    )
                )
                if len(planned) >= self._max_queries:
                    break

        return SearchPlan(
            canonical_category=canonical_category,
            is_supported_mvp_category=is_mvp,
            queries=tuple(planned),
            budget=budget,
            ignored_preferences=tuple(dict.fromkeys(ignored)),
            excluded_brands=tuple(intent.excluded_brands),
            excluded_features=tuple(intent.excluded_features),
            clarification_needed=False,
            clarification_question=None,
        )

    def _resolve_category(self, intent: UserIntent) -> tuple[CategorySpec | None, str | None]:
        if intent.category is not None:
            key = _norm_key(intent.category)
            canonical_key = self._alias_to_canonical.get(key)
            if canonical_key is not None:
                spec = self._categories[canonical_key]
                return spec, spec.canonical_id
            return None, intent.category

        # Se `category` veio None, só infere categoria caso um `standalone_model` explícito identifique o produto.
        model_norms = [_norm_key(m) for m in intent.models]
        for spec in self._categories.values():
            for standalone in spec.standalone_models:
                s_norm = _norm_key(standalone)
                if any(_contains_whole_phrase(m_norm, s_norm) for m_norm in model_norms):
                    return spec, spec.canonical_id
        return None, None

    def _pick_category_term(self, intent: UserIntent, spec: CategorySpec | None) -> str | None:
        if spec is None:
            return None

        # Modelos auto-suficientes (ex.: iPhone, MacBook, AirPods) dispensam repetir o substantivo genérico.
        model_norms = [_norm_key(m) for m in intent.models]
        for standalone in spec.standalone_models:
            s_norm = _norm_key(standalone)
            if any(_contains_whole_phrase(m_norm, s_norm) for m_norm in model_norms):
                return None

        probes = [intent.category or "", *intent.models, intent.query]
        for probe in probes:
            probe_norm = _norm_key(probe)
            for alias_pt, subtype_en in spec.subtype_terms_en.items():
                if _contains_whole_phrase(probe_norm, _norm_key(alias_pt)):
                    return subtype_en

        return spec.primary_term_en

    def _compose_query_text(
        self,
        *,
        brands: Sequence[str],
        models: Sequence[str],
        category_term_en: str | None,
        feature_terms: Sequence[str],
        fallback_query: str,
    ) -> str:
        clean_features = [f for f in feature_terms if f.strip()]
        has_brand_or_model = bool(brands or models)

        if has_brand_or_model:
            parts: list[str] = [*brands, *models]
            if category_term_en is not None:
                parts.append(category_term_en)
            parts.extend(clean_features)
        elif category_term_en is not None:
            if len(clean_features) <= 2:
                parts = [*clean_features, category_term_en]
            else:
                parts = [category_term_en, *clean_features]
        else:
            parts = [*clean_features] if clean_features else [self._fallback_ascii_query(fallback_query)]

        composed = _dedupe_tokens(parts)
        if not composed:
            composed = self._fallback_ascii_query(fallback_query)
        if len(composed) <= self._max_query_length:
            return composed
        truncated = composed[: self._max_query_length].rsplit(" ", 1)[0].strip()
        return truncated or composed[: self._max_query_length].strip()

    @staticmethod
    def _fallback_ascii_query(fallback_query: str) -> str:
        stopwords = {
            "para",
            "com",
            "sem",
            "ate",
            "de",
            "do",
            "da",
            "dos",
            "das",
            "um",
            "uma",
            "uns",
            "umas",
            "quero",
            "preciso",
            "procuro",
            "gostaria",
            "busco",
            "bom",
            "boa",
            "melhor",
            "barato",
            "barata",
            "resistente",
            "dobravel",
            "numerico",
        }
        tokens = [
            tok
            for tok in fallback_query.strip().split()
            if tok.isascii() and _norm_key(tok) not in stopwords
        ]
        cleaned = " ".join(tokens) if tokens else " ".join(fallback_query.strip().split())
        return cleaned

    def _build_budget(self, intent: UserIntent) -> AppliedBudget:
        price = intent.price
        has_bounds = price.min_price is not None or price.max_price is not None
        if not has_bounds:
            return AppliedBudget(
                min_inclusive=price.min_inclusive,
                max_inclusive=price.max_inclusive,
                is_hard_constraint=price.is_hard_constraint,
            )

        orig_currency = price.currency or self._default_budget_currency
        orig_min = Decimal(str(price.min_price)) if price.min_price is not None else None
        orig_max = Decimal(str(price.max_price)) if price.max_price is not None else None

        if orig_currency == "BRL":
            min_usd = self._currency_converter.brl_to_usd(orig_min) if orig_min is not None else None
            max_usd = self._currency_converter.brl_to_usd(orig_max) if orig_max is not None else None
            rate_info = self._currency_converter.rate_info()
        elif orig_currency == "USD":
            min_usd = orig_min.quantize(_TWO_DECIMALS, rounding=ROUND_HALF_UP) if orig_min is not None else None
            max_usd = orig_max.quantize(_TWO_DECIMALS, rounding=ROUND_HALF_UP) if orig_max is not None else None
            rate_info = None
        else:
            raise ValueError(
                f"Moeda de orçamento não suportada para conversão na eBay Browse API: {orig_currency!r}. "
                "Moedas suportadas: BRL, USD."
            )

        if min_usd is not None and max_usd is not None and max_usd < min_usd:
            raise ValueError("Limite máximo de orçamento convertido é menor que o limite mínimo.")

        return AppliedBudget(
            original_currency=orig_currency,
            original_min_price=orig_min,
            original_max_price=orig_max,
            min_price_usd=min_usd,
            max_price_usd=max_usd,
            min_inclusive=price.min_inclusive,
            max_inclusive=price.max_inclusive,
            is_hard_constraint=price.is_hard_constraint,
            exchange_rate=rate_info,
        )

    @staticmethod
    def _map_condition(
        condition: str | None,
    ) -> tuple[tuple[str, ...], tuple[str, ...], str | None]:
        if condition is None:
            return (), (), None
        norm = _norm_key(condition)
        mapped = _CONDITION_MAP.get(norm)
        if mapped is None:
            return (), (), f"unmapped_condition:{condition}"
        return mapped[0], mapped[1], None

    def _generate_alternative_candidates(
        self,
        *,
        intent: UserIntent,
        category_term_en: str | None,
        req_terms: Sequence[str],
        pref_terms: Sequence[str],
        base_query: SearchQuery,
        budget: AppliedBudget,
    ) -> list[tuple[SearchQuery, str]]:
        candidates: list[tuple[SearchQuery, str]] = []

        # 1) Quando não há preferências relaxáveis, pode expandir incluindo preferências traduzidas.
        if pref_terms:
            q_with_prefs = self._compose_query_text(
                brands=intent.brands,
                models=intent.models,
                category_term_en=category_term_en,
                feature_terms=[*req_terms, *pref_terms],
                fallback_query=intent.query,
            )
            if _norm_key(q_with_prefs) != _norm_key(base_query.query_text):
                candidates.append(
                    (
                        base_query.model_copy(update={"query_text": q_with_prefs}),
                        "Consulta alternativa refinada incluindo preferências opcionais traduzidas.",
                    )
                )

        # 2) Expansão por sinônimo de alta precisão (ex.: 4K <-> UHD, noise cancelling <-> ANC).
        all_features = [*req_terms, *pref_terms]
        synonym_features: list[str] = []
        changed_synonym = False
        for term in all_features:
            updated_term = term
            for src, dst in _SYNONYM_EXPANSIONS.items():
                pattern = re.compile(rf"(?<!\w){re.escape(src)}(?!\w)", flags=re.IGNORECASE)
                if pattern.search(updated_term):
                    updated_term = pattern.sub(dst, updated_term)
                    changed_synonym = True
                    break
            synonym_features.append(updated_term)
        if changed_synonym:
            q_syn = self._compose_query_text(
                brands=intent.brands,
                models=intent.models,
                category_term_en=category_term_en,
                feature_terms=synonym_features,
                fallback_query=intent.query,
            )
            candidates.append(
                (
                    base_query.model_copy(update={"query_text": q_syn}),
                    "Consulta alternativa com sinônimo técnico comum em anúncios EN-US.",
                )
            )

        # 3) Remove especificações de keyword do título para capturar anúncios cujas specs ficam em localizedAspects.
        if req_terms and (intent.brands or intent.models or category_term_en):
            q_broad_specs = self._compose_query_text(
                brands=intent.brands,
                models=intent.models,
                category_term_en=category_term_en,
                feature_terms=(),
                fallback_query=intent.query,
            )
            if _norm_key(q_broad_specs) != _norm_key(base_query.query_text):
                candidates.append(
                    (
                        base_query.model_copy(update={"query_text": q_broad_specs}),
                        "Consulta alternativa mais ampla para recuperar anúncios com specs apenas em localizedAspects.",
                    )
                )

        # 4) Flexibiliza teto de preço SOMENTE quando não for restrição rígida.
        if not budget.is_hard_constraint and base_query.max_price_usd is not None:
            candidates.append(
                (
                    base_query.model_copy(update={"max_price_usd": None}),
                    "Consulta alternativa sem teto de preço pois o orçamento informado não é rígido.",
                )
            )

        return candidates


def _invoke_extractor(
    extractor: IntentExtractorLike,
    user_text: str,
    *,
    conversation_history: Sequence[Mapping[str, str]] | None,
    request_id: str | None,
) -> UserIntent:
    if conversation_history is None and request_id is None:
        return extractor.extract(user_text)

    kwargs: dict[str, object] = {}
    params: Mapping[str, inspect.Parameter]
    try:
        sig = inspect.signature(extractor.extract)
        params = sig.parameters
        accepts_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    except (TypeError, ValueError):
        params = {}
        accepts_varkw = False

    if conversation_history is not None:
        if "conversation_history" in params or accepts_varkw:
            kwargs["conversation_history"] = conversation_history
        else:
            raise TypeError("O extrator informado não suporta o parâmetro conversation_history.")

    if request_id is not None and ("request_id" in params or accepts_varkw):
        kwargs["request_id"] = request_id

    return extractor.extract(user_text, **kwargs)  # type: ignore[call-arg]


def build_search_plan_from_text(
    user_text: str,
    *,
    extractor: IntentExtractorLike,
    query_builder: QueryBuilder,
    budget_brl: Decimal | int | float | str | None = None,
    conversation_history: Sequence[Mapping[str, str]] | None = None,
    request_id: str | None = None,
    limit: int = 30,
) -> IntentQueryPlanResult:
    """Fluxo integrado da Etapa 3: Texto PT-BR -> IntentExtractor -> UserIntent -> QueryBuilder -> SearchPlan."""
    if not isinstance(user_text, str) or not user_text.strip():
        raise ValueError("A consulta do usuário deve conter texto não vazio.")

    cleaned_input = user_text.strip()
    raw_intent = _invoke_extractor(
        extractor,
        cleaned_input,
        conversation_history=conversation_history,
        request_id=request_id,
    )
    if not isinstance(raw_intent, UserIntent):
        raise TypeError("O extrator deve retornar uma instância validada de UserIntent.")

    reconciled_intent = (
        merge_explicit_budget(
            raw_intent,
            budget_brl,
            currency="BRL",
            currency_converter=query_builder.currency_converter,
        )
        if budget_brl is not None
        else raw_intent
    )
    plan = query_builder.build(reconciled_intent, limit=limit)
    return IntentQueryPlanResult(
        raw_input=cleaned_input,
        intent=reconciled_intent,
        plan=plan,
        clarification_needed=plan.clarification_needed,
        clarification_question=plan.clarification_question,
    )
