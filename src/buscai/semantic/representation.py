"""Extração de atributos estruturados e representação textual enxuta de candidatos (sem dados proibidos)."""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict

from buscai.catalog_registry import (
    build_category_detection_keywords,
    build_category_priority_aspects,
)
from buscai.search.models import Candidate

_FORBIDDEN_ASPECT_FRAGMENTS: tuple[str, ...] = (
    "price",
    "url",
    "link",
    "image",
    "thumbnail",
    "seller",
    "username",
    "shipping",
    "delivery",
    "item_id",
    "itemid",
    "token",
    "secret",
    "password",
    "fee",
    "tax",
)

_SKIP_NOISE_VALUES: frozenset[str] = frozenset(
    {
        "n/a",
        "na",
        "does not apply",
        "not applicable",
        "unknown",
        "generic",
        "unbranded",
        "none",
        "null",
    }
)

_KNOWN_BRANDS: tuple[tuple[str, str], ...] = (
    ("audio-technica", "audio-technica"),
    ("hewlett-packard", "hp"),
    ("hewlett packard", "hp"),
    ("apple", "apple"),
    ("lenovo", "lenovo"),
    ("dell", "dell"),
    ("hp", "hp"),
    ("asus", "asus"),
    ("acer", "acer"),
    ("samsung", "samsung"),
    ("lg", "lg"),
    ("sony", "sony"),
    ("jbl", "jbl"),
    ("bose", "bose"),
    ("beats", "beats"),
    ("sennheiser", "sennheiser"),
    ("logitech", "logitech"),
    ("razer", "razer"),
    ("hyperx", "hyperx"),
    ("msi", "msi"),
    ("gigabyte", "gigabyte"),
    ("benq", "benq"),
    ("aoc", "aoc"),
    ("philips", "philips"),
    ("viewsonic", "viewsonic"),
    ("xiaomi", "xiaomi"),
    ("redmi", "xiaomi"),
    ("poco", "xiaomi"),
    ("motorola", "motorola"),
    ("google", "google"),
    ("microsoft", "microsoft"),
    ("anker", "anker"),
    ("soundcore", "soundcore"),
    ("jabra", "jabra"),
    ("skullcandy", "skullcandy"),
    ("oneplus", "oneplus"),
    ("realme", "realme"),
)

_APPLE_EXCLUSIVE_LINES = ("iphone", "macbook", "airpods")

_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = build_category_detection_keywords()

_ACCESSORY_MARKERS = ("case", "cover", "bag", "sleeve", "charger", "adapter", "cable", "stand", "screen protector", "skin")

_NOISE_MODEL_WORDS: frozenset[str] = frozenset(
    {
        "new",
        "brand",
        "sealed",
        "open",
        "box",
        "used",
        "refurbished",
        "renewed",
        "certified",
        "excellent",
        "very",
        "good",
        "mint",
        "condition",
        "unlocked",
        "factory",
        "carrier",
        "gsm",
        "cdma",
        "fast",
        "free",
        "shipping",
        "warranty",
        "edition",
        "series",
        "computer",
        "laptop",
        "notebook",
        "pc",
        "ultrabook",
        "smartphone",
        "phone",
        "cell",
        "mobile",
        "monitor",
        "display",
        "screen",
        "led",
        "lcd",
        "headphones",
        "headphone",
        "headset",
        "earbuds",
        "earphones",
        "audio",
        "wireless",
        "wired",
        "bluetooth",
        "noise",
        "cancelling",
        "canceling",
        "active",
        "anc",
        "over",
        "overear",
        "inear",
        "onear",
        "ear",
        "on",
        "tws",
        "black",
        "white",
        "silver",
        "gray",
        "grey",
        "space",
        "blue",
        "red",
        "pink",
        "green",
        "gold",
        "graphite",
        "midnight",
        "starlight",
        "titanium",
        "matte",
        "glossy",
        "natural",
        "obsidian",
        "porcelain",
        "preto",
        "branco",
        "prata",
        "cinza",
        "azul",
        "novo",
        "usado",
        "lacrado",
        "com",
        "sem",
        "para",
        "and",
        "with",
        "the",
        "for",
        "de",
        "business",
        "gaming",
        "portable",
        "touchscreen",
        "touch",
        "convertible",
        "2in1",
        "ram",
        "ssd",
        "nvme",
        "hdd",
        "rom",
        "storage",
        "memory",
        "ips",
        "oled",
        "amoled",
        "qled",
        "va",
        "tn",
        "hdr",
        "uhd",
        "qhd",
        "wqhd",
        "fhd",
        "full",
        "hd",
        "4k",
        "5k",
        "8k",
        "720p",
        "1080p",
        "1440p",
        "2160p",
        "usb",
        "usbc",
        "typec",
        "hdmi",
        "displayport",
        "nfc",
        "5g",
        "wifi",
        "wi",
        "fi",
        "inch",
        "inches",
        "polegadas",
        "hz",
        "windows",
        "macos",
        "linux",
        "chrome",
        "os",
        "intel",
        "core",
        "amd",
        "ryzen",
        "nvidia",
        "geforce",
        "rtx",
        "gtx",
        "radeon",
        "mpn",
        "upc",
        "ean",
        "gtin",
    }
)

VARIANT_TIER_TOKENS: frozenset[str] = frozenset(
    {"pro", "max", "plus", "ultra", "mini", "air", "se", "lite", "fe", "ti", "super", "xl", "fold", "flip"}
)
_VARIANT_TIER_TOKENS: frozenset[str] = VARIANT_TIER_TOKENS

DEFAULT_MAX_EMBEDDING_CHARS = 1600
_MAX_SHORT_DESCRIPTION_CHARS = 320

_COMMON_PRIORITY_ASPECT_KEYS: tuple[str, ...] = (
    "model",
    "modelo",
    "mpn",
    "manufacturer part number",
    "part number",
    "brand",
    "marca",
    "condition",
)

_CATEGORY_PRIORITY_ASPECT_KEYS: dict[str, tuple[str, ...]] = build_category_priority_aspects()

_BUDGET_CLAUSE_RE = re.compile(
    r"(?:,?\s*\b(?:ate|até|abaixo\s+de|menos\s+de|no\s+maximo|no\s+máximo|sem\s+ultrapassar|por\s+ate|por\s+até|entre)\s+"
    r"(?:r\$|rs|us\$|usd|brl|\$|€|eur)?\s*\d[\d.,]*(?:\s*(?:e|a)\s*(?:r\$|rs|us\$|usd|brl|\$|€|eur)?\s*\d[\d.,]*)?"
    r"(?:\s*(?:reais|real|dolares|dólares|dolar|dólar|brl|usd))?)",
    re.IGNORECASE,
)

_SLASH_MEM_STORAGE_RE = re.compile(r"\b(\d{1,3})\s*gb\s*/\s*(\d{1,4}(?:[.,]\d+)?)\s*(gb|tb)\b", re.IGNORECASE)
_RAM_RE = re.compile(
    r"\b(\d{1,3})\s*gb\s*(?:of\s+)?(?:ddr[345x]?|lpddr\d?[x]?|unified\s+memory|memory|ram)\b"
    r"|\b(?:ram|system\s+memory)\s*[:\-]?\s*(\d{1,3})\s*gb\b",
    re.IGNORECASE,
)
_EXPLICIT_STORAGE_RE = re.compile(
    r"\b(\d{1,4}(?:[.,]\d+)?)\s*(gb|tb)\s*(?:nvme\s+)?(?:m\.2\s+)?(?:pcie\s+)?(?:ssd|hdd|emmc|ufs|rom|storage|hard\s+drive)\b"
    r"|\b(?:ssd|nvme|storage|hdd|hard\s+drive|rom)\s*(?:capacity)?\s*[:\-]?\s*(\d{1,4}(?:[.,]\d+)?)\s*(gb|tb)\b",
    re.IGNORECASE,
)
_STANDALONE_CAPACITY_RE = re.compile(r"\b(\d{1,4}(?:[.,]\d+)?)\s*(gb|tb)\b", re.IGNORECASE)
_SCREEN_INCH_RE = re.compile(
    r'(?<!\w)(\d{1,2}(?:[.,]\d{1,2})?)\s*(?:"|\'\'|inch(?:es)?|-inch|polegadas?|pol\.?)(?!\w)',
    re.IGNORECASE,
)
_HZ_RE = re.compile(r"\b(\d{2,3})\s*hz\b", re.IGNORECASE)
_TAGGED_ID_RE = re.compile(
    r"\b(?:mpn|p/n|part\s*(?:no\.?|number)|upc|ean|gtin)\s*[:#]\s*([a-z0-9._-]{4,32})\b",
    re.IGNORECASE,
)


def _strip_accents(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))


def _norm_text(text: str) -> str:
    return _strip_accents(" ".join(text.strip().casefold().split()))


def _aspect_lookup(aspects: dict[str, tuple[str, ...]], candidate_keys: tuple[str, ...]) -> tuple[str, ...]:
    targets = {key.casefold() for key in candidate_keys}
    collected: list[str] = []
    for raw_key, values in aspects.items():
        if raw_key.strip().casefold() in targets:
            for val in values:
                cleaned = val.strip()
                if cleaned and cleaned.casefold() not in _SKIP_NOISE_VALUES:
                    collected.append(cleaned)
    return tuple(collected)


def _to_gb(raw_num: str, unit: str) -> int | None:
    try:
        dec = Decimal(raw_num.replace(",", "."))
    except InvalidOperation:
        return None
    if not dec.is_finite() or dec <= 0:
        return None
    multiplier = 1024 if unit.lower() == "tb" else 1
    gb_val = int((dec * multiplier).to_integral_value())
    return gb_val if gb_val > 0 else None


def _is_forbidden_aspect_key(key: str) -> bool:
    norm = key.strip().casefold()
    return any(fragment in norm for fragment in _FORBIDDEN_ASPECT_FRAGMENTS)


def build_query_embedding_text(query_text: str) -> str:
    """Valida e normaliza a consulta textual para geração do vetor de consulta."""
    if not isinstance(query_text, str) or not query_text.strip():
        raise ValueError("query_text para embedding não pode ser vazio.")
    return " ".join(query_text.strip().split())


def build_semantic_query_text(query_text: str, *, intent: object | None = None) -> str:
    """Contrato explícito de consulta semântica (A14): texto descritivo limpo sem ruído transacional.

    Quando `intent` estruturado (ex.: `UserIntent`) for fornecido, compõe a consulta semântica a
    partir de marca, modelo, categoria, características técnicas e casos de uso, excluindo limites
    numéricos de orçamento/moeda. Caso contrário, normaliza `query_text` removendo cláusulas
    monetárias explícitas quando restar conteúdo descritivo útil.
    """
    cleaned_raw = build_query_embedding_text(query_text)
    if intent is not None:
        parts: list[str] = []
        seen: set[str] = set()

        def _append_unique(value: object) -> None:
            if not isinstance(value, str):
                return
            cleaned = " ".join(value.strip().split())
            if not cleaned:
                return
            key = _norm_text(cleaned)
            if key and key not in seen:
                seen.add(key)
                parts.append(cleaned)

        for field_name in ("brands", "models"):
            vals = getattr(intent, field_name, ()) or ()
            for val in vals:
                _append_unique(val)
        _append_unique(getattr(intent, "category", None))
        for field_name in ("required_features", "preferred_features", "use_cases"):
            vals = getattr(intent, field_name, ()) or ()
            for val in vals:
                _append_unique(val)
        if parts:
            return " ".join(parts)
        intent_query = getattr(intent, "query", None)
        if isinstance(intent_query, str) and intent_query.strip():
            cleaned_raw = build_query_embedding_text(intent_query)

    stripped_budget = " ".join(_BUDGET_CLAUSE_RE.sub(" ", cleaned_raw).split()).strip(" ,.-;")
    return stripped_budget if stripped_budget else cleaned_raw


def _aspect_priority_rank(key: str, canonical_category: str | None) -> tuple[int, int, str]:
    norm_key = key.strip().casefold()
    cat_keys = _CATEGORY_PRIORITY_ASPECT_KEYS.get(canonical_category or "", ())
    for idx, target in enumerate(cat_keys):
        if norm_key == target:
            return (0, idx, norm_key)
    for idx, target in enumerate(_COMMON_PRIORITY_ASPECT_KEYS):
        if norm_key == target:
            return (1, idx, norm_key)
    return (2, 0, norm_key)


def build_candidate_embedding_text(
    candidate: Candidate,
    *,
    category_hint: str | None = None,
    max_chars: int = DEFAULT_MAX_EMBEDDING_CHARS,
) -> str:
    """Constrói representação textual concisa priorizando atributos técnicos críticos por categoria.

    Exclui explicitamente preço, moeda, URLs, vendedor, frete, opções de compra e `item_id`.
    """
    if not isinstance(candidate, Candidate):
        raise TypeError("candidate deve ser uma instância válida de Candidate.")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 64:
        raise ValueError("max_chars deve ser um inteiro >= 64.")

    title_clean = " ".join(candidate.title.strip().split())
    parts: list[str] = [title_clean]
    title_norm = _norm_text(title_clean)
    canonical_category = _detect_category(candidate, title_norm, category_hint)

    if candidate.brand and _norm_text(candidate.brand) not in title_norm:
        parts.append(f"Brand: {candidate.brand}")

    if candidate.model and _norm_text(candidate.model) not in title_norm:
        parts.append(f"Model: {candidate.model}")

    if candidate.categories:
        parts.append(f"Category: {' > '.join(candidate.categories)}")

    if candidate.condition:
        parts.append(f"Condition: {candidate.condition}")

    sorted_aspects = sorted(
        candidate.aspects.items(),
        key=lambda item: _aspect_priority_rank(item[0], canonical_category),
    )
    for key, values in sorted_aspects:
        if _is_forbidden_aspect_key(key):
            continue
        valid_vals = [v.strip() for v in values if v.strip() and v.strip().casefold() not in _SKIP_NOISE_VALUES]
        if valid_vals:
            parts.append(f"{key}: {', '.join(valid_vals)}")

    if candidate.short_description:
        desc_clean = " ".join(candidate.short_description.strip().split())
        if desc_clean:
            if len(desc_clean) > _MAX_SHORT_DESCRIPTION_CHARS:
                desc_clean = desc_clean[:_MAX_SHORT_DESCRIPTION_CHARS].rsplit(" ", 1)[0].rstrip(" ,.;:")
            if desc_clean:
                parts.append(desc_clean)

    # Monta segmentos respeitando `max_chars` sem cortar atributos prioritários.
    selected: list[str] = []
    current_len = 0
    for idx, segment in enumerate(parts):
        sep_len = 3 if idx > 0 else 0
        if idx == 0:
            if len(segment) > max_chars:
                return segment[:max_chars].rstrip()
            selected.append(segment)
            current_len = len(segment)
            continue
        if current_len + sep_len + len(segment) <= max_chars:
            selected.append(segment)
            current_len += sep_len + len(segment)

    return " | ".join(selected)


class ProductStructuredAttributes(BaseModel):
    """Atributos estruturados extraídos de `Candidate` para deduplicação híbrida."""

    model_config = ConfigDict(frozen=True)

    canonical_category: str | None = None
    brand: str | None = None
    model_tokens: tuple[str, ...] = ()
    manufacturer_ids: tuple[str, ...] = ()
    storage_gb: int | None = None
    ram_gb: int | None = None
    screen_size_inches: Decimal | None = None
    resolution: str | None = None
    refresh_rate_hz: int | None = None
    panel_type: str | None = None
    connectivity_features: tuple[str, ...] = ()
    form_factor: str | None = None
    condition_bucket: str | None = None
    normalized_title: str = ""


def _detect_category(candidate: Candidate, title_norm: str, category_hint: str | None) -> str | None:
    cat_norms = [_norm_text(cat) for cat in candidate.categories]
    combined = " ".join([*cat_norms, title_norm])

    is_accessory = any(re.search(rf"\b{word}\b", title_norm) for word in _ACCESSORY_MARKERS)

    for canonical, keywords in _CATEGORY_KEYWORDS:
        for kw in keywords:
            if any(re.search(rf"\b{re.escape(kw)}\b", cn) for cn in cat_norms):
                return canonical

    if not is_accessory:
        for canonical, keywords in _CATEGORY_KEYWORDS:
            for kw in keywords:
                if re.search(rf"\b{re.escape(kw)}\b", combined):
                    return canonical

    if category_hint:
        hint_norm = _norm_text(category_hint)
        for canonical, keywords in _CATEGORY_KEYWORDS:
            if hint_norm == _norm_text(canonical) or any(hint_norm == kw for kw in keywords):
                return canonical
    return None


def _detect_brand(candidate: Candidate, title_norm: str) -> str | None:
    if candidate.brand:
        cleaned = _norm_text(candidate.brand)
        if cleaned and cleaned not in _SKIP_NOISE_VALUES:
            for alias, canonical in _KNOWN_BRANDS:
                if cleaned == alias:
                    return canonical
            return cleaned

    aspect_brands = _aspect_lookup(candidate.aspects, ("brand", "marca", "manufacturer"))
    if aspect_brands:
        cleaned = _norm_text(aspect_brands[0])
        for alias, canonical in _KNOWN_BRANDS:
            if cleaned == alias:
                return canonical
        return cleaned

    for alias, canonical in _KNOWN_BRANDS:
        if re.search(rf"\b{re.escape(alias)}\b", title_norm):
            return canonical

    for line in _APPLE_EXCLUSIVE_LINES:
        if re.search(rf"\b{line}\b", title_norm) and not re.search(rf"\bfor\s+{line}\b", title_norm):
            return "apple"

    return None


def _extract_manufacturer_ids(candidate: Candidate, combined_text: str) -> tuple[str, ...]:
    ids: list[str] = []
    seen: set[str] = set()

    raw_aspect_ids = _aspect_lookup(
        candidate.aspects,
        ("mpn", "manufacturer part number", "part number", "upc", "ean", "gtin", "model number"),
    )
    for raw in raw_aspect_ids:
        for part in re.split(r"[,;/\s]+", raw):
            token = re.sub(r"[^A-Z0-9._-]", "", part.strip().upper())
            if len(token) >= 4 and token.casefold() not in _SKIP_NOISE_VALUES and token not in seen:
                seen.add(token)
                ids.append(token)

    for match in _TAGGED_ID_RE.finditer(combined_text):
        token = match.group(1).strip().upper()
        if len(token) >= 4 and token.casefold() not in _SKIP_NOISE_VALUES and token not in seen:
            seen.add(token)
            ids.append(token)

    return tuple(ids)


def _extract_ram_and_storage(
    candidate: Candidate,
    title_norm: str,
    desc_norm: str,
    canonical_category: str | None,
) -> tuple[int | None, int | None]:
    ram_gb: int | None = None
    storage_gb: int | None = None

    ram_aspects = _aspect_lookup(candidate.aspects, ("ram size", "ram", "memory", "system memory", "memória ram"))
    for val in ram_aspects:
        m = _STANDALONE_CAPACITY_RE.search(val)
        if m:
            parsed = _to_gb(m.group(1), m.group(2))
            if parsed is not None:
                ram_gb = parsed
                break

    storage_aspects = _aspect_lookup(
        candidate.aspects,
        ("ssd capacity", "storage capacity", "hard drive capacity", "capacity", "internal storage", "storage"),
    )
    for val in storage_aspects:
        m = _STANDALONE_CAPACITY_RE.search(val)
        if m:
            parsed = _to_gb(m.group(1), m.group(2))
            if parsed is not None:
                storage_gb = parsed
                break

    probe = f"{title_norm} {desc_norm}".strip()
    slash_match = _SLASH_MEM_STORAGE_RE.search(probe)
    if slash_match:
        if ram_gb is None:
            ram_gb = _to_gb(slash_match.group(1), "gb")
        if storage_gb is None:
            storage_gb = _to_gb(slash_match.group(2), slash_match.group(3))

    if ram_gb is None:
        ram_match = _RAM_RE.search(probe)
        if ram_match:
            raw_ram = ram_match.group(1) or ram_match.group(2)
            if raw_ram:
                ram_gb = _to_gb(raw_ram, "gb")

    if storage_gb is None:
        storage_match = _EXPLICIT_STORAGE_RE.search(probe)
        if storage_match:
            num = storage_match.group(1) or storage_match.group(3)
            unit = storage_match.group(2) or storage_match.group(4)
            if num and unit:
                storage_gb = _to_gb(num, unit)

    if storage_gb is None:
        # Remove correspondências já consumidas como RAM para não confundir RAM com armazenamento.
        probe_no_ram = _RAM_RE.sub(" ", _SLASH_MEM_STORAGE_RE.sub(" ", probe))
        candidates_gb: list[tuple[int, str]] = []
        for cap_match in _STANDALONE_CAPACITY_RE.finditer(probe_no_ram):
            gb = _to_gb(cap_match.group(1), cap_match.group(2))
            if gb is not None:
                candidates_gb.append((gb, cap_match.group(2).lower()))
        for gb, unit in candidates_gb:
            if unit == "tb" or gb >= 128 or (canonical_category == "smartphone" and gb >= 32):
                storage_gb = gb
                break

    return ram_gb, storage_gb


def _extract_screen_size(candidate: Candidate, probe: str) -> Decimal | None:
    aspect_sizes = _aspect_lookup(candidate.aspects, ("screen size", "display size", "tamanho da tela"))
    for raw in [*aspect_sizes, probe]:
        m = _SCREEN_INCH_RE.search(raw)
        if m:
            try:
                val = Decimal(m.group(1).replace(",", "."))
            except InvalidOperation:
                continue
            if Decimal("4.0") <= val <= Decimal("85.0"):
                return val
        if raw in aspect_sizes:
            num_m = re.search(r"(\d{1,2}(?:[.,]\d{1,2})?)", raw)
            if num_m:
                try:
                    val = Decimal(num_m.group(1).replace(",", "."))
                    if Decimal("4.0") <= val <= Decimal("85.0"):
                        return val
                except InvalidOperation:
                    continue
    return None


def _extract_resolution(candidate: Candidate, probe: str) -> str | None:
    aspect_res = _aspect_lookup(candidate.aspects, ("maximum resolution", "resolution", "display resolution"))
    text = _norm_text(" ".join([*aspect_res, probe]))

    patterns: tuple[tuple[str, str], ...] = (
        ("8k", r"\b(?:8k|7680\s*[x×]\s*4320)\b"),
        ("5k", r"\b(?:5k|5120\s*[x×]\s*2880)\b"),
        ("uhd_4k", r"\b(?:4k|uhd|ultra\s*hd|3840\s*[x×]\s*2160|2160p)\b"),
        ("qhd", r"\b(?:wqhd|qhd\+|qhd|2560\s*[x×]\s*1440|3440\s*[x×]\s*1440|1440p)\b"),
        ("fhd", r"\b(?:full\s*hd|fhd|1920\s*[x×]\s*1080|1080p)\b"),
        ("hd", r"\b(?:1280\s*[x×]\s*720|720p)\b"),
    )
    for canonical, regex in patterns:
        if re.search(regex, text):
            return canonical
    return None


def _extract_refresh_rate(candidate: Candidate, probe: str) -> int | None:
    aspect_hz = _aspect_lookup(candidate.aspects, ("refresh rate", "taxa de atualização"))
    for src in [*aspect_hz, probe]:
        m = _HZ_RE.search(src)
        if m:
            hz = int(m.group(1))
            if 48 <= hz <= 540:
                return hz
    return None


def _extract_panel_type(candidate: Candidate, probe: str) -> str | None:
    aspect_panel = _aspect_lookup(candidate.aspects, ("display type", "panel type", "screen type"))
    text = _norm_text(" ".join([*aspect_panel, probe]))
    for panel, pat in (
        ("qd-oled", r"\bqd[\s-]?oled\b"),
        ("amoled", r"\bamoled\b"),
        ("oled", r"\boled\b"),
        ("mini-led", r"\bmini[\s-]?led\b"),
        ("qled", r"\bqled\b"),
        ("ips", r"\bips\b"),
        ("va", r"\bva\b"),
        ("tn", r"\btn\b"),
    ):
        if re.search(pat, text):
            return panel
    return None


def _extract_connectivity(candidate: Candidate, probe: str) -> tuple[str, ...]:
    aspect_conn = _aspect_lookup(candidate.aspects, ("connectivity", "features", "wireless technology"))
    text = _norm_text(" ".join([*aspect_conn, probe]))
    found: list[str] = []
    checks = (
        ("anc", r"\b(?:anc|active\s+noise\s+cancell?ing|noise\s+cancell?ing|cancelamento(?:\s+ativo)?\s+de\s+ruido)\b"),
        ("bluetooth", r"\bbluetooth\b"),
        ("wireless", r"\b(?:wireless|sem\s+fio)\b"),
        ("wired", r"\b(?:wired|com\s+fio)\b"),
        ("usb-c", r"\busb[\s-]?c\b"),
        ("5g", r"\b5g\b"),
        ("nfc", r"\bnfc\b"),
    )
    for label, pat in checks:
        if re.search(pat, text):
            found.append(label)
    return tuple(found)


def _extract_form_factor(candidate: Candidate, probe: str) -> str | None:
    aspect_ff = _aspect_lookup(candidate.aspects, ("form factor", "type", "fit design"))
    text = _norm_text(" ".join([*aspect_ff, probe]))
    for ff, pat in (
        ("in-ear", r"\b(?:in[\s-]?ear|earbuds?|tws|intra[\s-]?auricular)\b"),
        ("over-ear", r"\b(?:over[\s-]?ear|circumaural)\b"),
        ("on-ear", r"\bon[\s-]?ear\b"),
        ("headset", r"\bheadset\b"),
    ):
        if re.search(pat, text):
            return ff
    return None


def _extract_condition_bucket(candidate: Candidate, title_norm: str) -> str | None:
    raw = _norm_text(candidate.condition or "")
    for probe in (raw, title_norm):
        if not probe:
            continue
        if any(marker in probe for marker in ("for parts", "not working", "parts only", "para pecas")):
            return "parts"
        if any(marker in probe for marker in ("open box", "new other", "caixa aberta")):
            return "open_box"
        if any(marker in probe for marker in ("refurbished", "recondicionado", "renewed")):
            return "refurbished"
        if any(marker in probe for marker in ("used", "pre-owned", "pre owned", "usado", "seminovo")):
            return "used"
        if probe in {"new", "novo", "brand new", "sealed", "lacrado"} or (
            probe == raw and "new" in probe
        ):
            return "new"
    return None


def _extract_model_tokens(
    candidate: Candidate,
    title_norm: str,
    brand: str | None,
) -> tuple[str, ...]:
    model_sources: list[str] = []
    if candidate.model:
        model_sources.append(_norm_text(candidate.model))
    aspect_models = _aspect_lookup(candidate.aspects, ("model", "modelo", "series"))
    model_sources.extend(_norm_text(v) for v in aspect_models)

    cleaned_title = _SLASH_MEM_STORAGE_RE.sub(" ", title_norm)
    cleaned_title = _RAM_RE.sub(" ", cleaned_title)
    cleaned_title = _EXPLICIT_STORAGE_RE.sub(" ", cleaned_title)
    cleaned_title = _STANDALONE_CAPACITY_RE.sub(" ", cleaned_title)
    cleaned_title = _SCREEN_INCH_RE.sub(" ", cleaned_title)
    cleaned_title = _HZ_RE.sub(" ", cleaned_title)
    cleaned_title = _TAGGED_ID_RE.sub(" ", cleaned_title)
    cleaned_title = re.sub(r"\bmini[\s-]?led\b", " ", cleaned_title, flags=re.IGNORECASE)
    cleaned_title = re.sub(r"\b(?:wqhd|qhd\+|qhd|uhd|fhd|full\s*hd|4k|5k|8k|720p|1080p|1440p|2160p)\b", " ", cleaned_title, flags=re.IGNORECASE)
    model_sources.append(cleaned_title)

    brand_tokens = set(brand.split()) if brand else set()
    tokens: list[str] = []
    seen: set[str] = set()

    def _emit_token(tok: str) -> None:
        if tok and tok not in seen:
            seen.add(tok)
            tokens.append(tok)

    for source in model_sources:
        for raw_tok in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*\+?", source):
            has_plus = raw_tok.endswith("+")
            base_raw = raw_tok[:-1] if has_plus else raw_tok
            norm_tok = base_raw.replace("-", "")
            if not norm_tok:
                continue
            if norm_tok in _NOISE_MODEL_WORDS or base_raw in _NOISE_MODEL_WORDS:
                continue
            if norm_tok in brand_tokens or base_raw in brand_tokens:
                continue
            if norm_tok.isdigit() and len(norm_tok) > 2:
                if len(norm_tok) >= 5:
                    continue
            _emit_token(norm_tok)
            if has_plus:
                _emit_token("plus")

    return tuple(tokens)


def extract_product_attributes(
    candidate: Candidate,
    *,
    category_hint: str | None = None,
) -> ProductStructuredAttributes:
    """Extrai sinais estruturados objetivos para deduplicação híbrida sem inventar campos."""
    if not isinstance(candidate, Candidate):
        raise TypeError("candidate deve ser uma instância válida de Candidate.")

    title_norm = _norm_text(candidate.title)
    desc_norm = _norm_text(candidate.short_description or "")
    probe = f"{title_norm} {desc_norm}".strip()

    canonical_category = _detect_category(candidate, title_norm, category_hint)
    brand = _detect_brand(candidate, title_norm)
    manufacturer_ids = _extract_manufacturer_ids(candidate, f"{title_norm} {desc_norm}")
    ram_gb, storage_gb = _extract_ram_and_storage(candidate, title_norm, desc_norm, canonical_category)
    screen_size = _extract_screen_size(candidate, probe)
    resolution = _extract_resolution(candidate, probe)
    refresh_rate = _extract_refresh_rate(candidate, probe)
    panel_type = _extract_panel_type(candidate, probe)
    connectivity = _extract_connectivity(candidate, probe)
    form_factor = _extract_form_factor(candidate, probe)
    condition_bucket = _extract_condition_bucket(candidate, title_norm)
    model_tokens = _extract_model_tokens(candidate, title_norm, brand)

    return ProductStructuredAttributes(
        canonical_category=canonical_category,
        brand=brand,
        model_tokens=model_tokens,
        manufacturer_ids=manufacturer_ids,
        storage_gb=storage_gb,
        ram_gb=ram_gb,
        screen_size_inches=screen_size,
        resolution=resolution,
        refresh_rate_hz=refresh_rate,
        panel_type=panel_type,
        connectivity_features=connectivity,
        form_factor=form_factor,
        condition_bucket=condition_bucket,
        normalized_title=title_norm,
    )


__all__ = [
    "DEFAULT_MAX_EMBEDDING_CHARS",
    "ProductStructuredAttributes",
    "VARIANT_TIER_TOKENS",
    "build_candidate_embedding_text",
    "build_query_embedding_text",
    "build_semantic_query_text",
    "extract_product_attributes",
    "_VARIANT_TIER_TOKENS",
]
