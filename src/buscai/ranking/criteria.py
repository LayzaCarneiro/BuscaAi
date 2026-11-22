"""Deterministic, evidence-based criterion evaluators for Stage 6 reranking."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal

from buscai.pricing.currency import CurrencyConverter, FixedEnvExchangeRateProvider
from buscai.ranking.models import CriterionName
from buscai.search.models import Candidate
from buscai.validation.evidence import extract_candidate_evidences
from buscai.validation.models import (
    CandidateEvidenceMap,
    CandidateValidationResult,
)

_CONDITION_SCORES: dict[str, tuple[float, str]] = {
    "new": (1.0, "Novo"),
    "brand new": (1.0, "Novo"),
    "novo": (1.0, "Novo"),
    "open box": (0.88, "Open box"),
    "open_box": (0.88, "Open box"),
    "like new": (0.88, "Como novo"),
    "refurbished": (0.72, "Recondicionado"),
    "certified - refurbished": (0.76, "Recondicionado certificado"),
    "excellent - refurbished": (0.76, "Recondicionado excelente"),
    "very good - refurbished": (0.70, "Recondicionado muito bom"),
    "good - refurbished": (0.65, "Recondicionado bom"),
    "recondicionado": (0.72, "Recondicionado"),
    "used": (0.50, "Usado"),
    "pre-owned": (0.50, "Usado"),
    "usado": (0.50, "Usado"),
    "for parts": (0.10, "Para peças"),
    "for parts or not working": (0.10, "Para peças"),
}


@dataclass(frozen=True)
class RawCriterionEvaluation:
    """Intermediate criterion evaluation before cohort normalization and weighting."""

    name: CriterionName
    raw_value: float | str | None
    normalized_score: float | None
    available: bool
    not_applicable: bool = False
    evidence_sources: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class CandidateCostInfo:
    """Computed BRL cost metadata for a candidate."""

    total_cost_brl: Decimal | None
    item_price_brl: Decimal | None
    shipping_brl: Decimal | None
    shipping_known: bool


def default_currency_converter() -> CurrencyConverter:
    """Return a deterministic default CurrencyConverter for offline/fallback use."""
    return CurrencyConverter(FixedEnvExchangeRateProvider(rate=Decimal("5.00")))


def compute_candidate_cost_brl(
    candidate: Candidate,
    converter: CurrencyConverter | None = None,
) -> CandidateCostInfo:
    """Compute item and total BRL cost using Decimal arithmetic without inventing shipping."""
    active_converter = converter or default_currency_converter()

    def _to_brl(amount: Decimal, currency: str) -> Decimal:
        cur = (currency or "USD").strip().upper()
        if cur == "BRL":
            return amount
        if cur == "USD":
            return active_converter.usd_to_brl(amount).amount
        # Deterministic fallback via USD ratio if an unexpected currency appears.
        return active_converter.usd_to_brl(amount).amount

    item_price_brl: Decimal | None = None
    if candidate.price is not None:
        item_price_brl = _to_brl(candidate.price, candidate.currency)

    shipping = candidate.shipping
    shipping_known = False
    shipping_brl: Decimal | None = None
    if shipping is not None:
        if shipping.free_shipping:
            shipping_known = True
            shipping_brl = Decimal("0.00")
        elif shipping.cost is not None:
            shipping_known = True
            shipping_brl = _to_brl(shipping.cost, shipping.currency or candidate.currency)

    if item_price_brl is None:
        return CandidateCostInfo(
            total_cost_brl=None,
            item_price_brl=None,
            shipping_brl=shipping_brl,
            shipping_known=shipping_known,
        )

    total_cost_brl = (
        item_price_brl + shipping_brl
        if shipping_brl is not None
        else item_price_brl
    )
    return CandidateCostInfo(
        total_cost_brl=total_cost_brl,
        item_price_brl=item_price_brl,
        shipping_brl=shipping_brl,
        shipping_known=shipping_known,
    )


def evaluate_budget_fit(validation: CandidateValidationResult) -> RawCriterionEvaluation:
    """Evaluate how comfortably a candidate fits within the user's budget."""
    budget = validation.budget_evaluation
    if budget is None or budget.max_price is None or budget.status == "not_applicable":
        return RawCriterionEvaluation(
            name=CriterionName.BUDGET_FIT,
            raw_value=None,
            normalized_score=None,
            available=False,
            not_applicable=True,
            note="Orçamento máximo não informado.",
        )

    total_cost = budget.total_price_in_budget_currency or budget.item_price_in_budget_currency
    if budget.status == "not_satisfied":
        return RawCriterionEvaluation(
            name=CriterionName.BUDGET_FIT,
            raw_value=float(total_cost) if total_cost is not None else None,
            normalized_score=0.0,
            available=True,
            evidence_sources=("price", "budget"),
            note="Acima do orçamento máximo.",
        )

    if budget.status != "satisfied" or total_cost is None or budget.max_price <= 0:
        return RawCriterionEvaluation(
            name=CriterionName.BUDGET_FIT,
            raw_value=None,
            normalized_score=None,
            available=False,
            note="Custo ou orçamento indisponível para comparação.",
        )

    ratio = float(total_cost / budget.max_price)
    ratio = max(0.0, min(1.0, ratio))
    score = max(0.70, min(1.0, 1.0 - 0.30 * ratio))
    return RawCriterionEvaluation(
        name=CriterionName.BUDGET_FIT,
        raw_value=float(total_cost),
        normalized_score=score,
        available=True,
        evidence_sources=("price", "budget"),
        note=f"Dentro do orçamento ({ratio * 100:.0f}% do teto).",
    )


def evaluate_seller_reliability(candidate: Candidate) -> RawCriterionEvaluation:
    """Evaluate seller feedback percentage and feedback score without inventing missing data."""
    seller = candidate.seller
    if seller is None or (seller.feedback_percentage is None and seller.feedback_score is None):
        return RawCriterionEvaluation(
            name=CriterionName.SELLER_RELIABILITY,
            raw_value=None,
            normalized_score=None,
            available=False,
            note="Reputação do vendedor não informada.",
        )

    parts: list[tuple[float, float]] = []
    sources: list[str] = []
    notes: list[str] = []

    if seller.feedback_percentage is not None:
        pct = float(seller.feedback_percentage)
        pct_norm = max(0.0, min(1.0, (pct - 80.0) / 20.0))
        parts.append((0.75, pct_norm))
        sources.append("seller.feedback_percentage")
        notes.append(f"{pct:.1f}% positivo")

    if seller.feedback_score is not None:
        score_val = max(0, int(seller.feedback_score))
        score_norm = min(1.0, math.log10(max(score_val, 1)) / 4.0) if score_val > 0 else 0.0
        parts.append((0.25, score_norm))
        sources.append("seller.feedback_score")
        notes.append(f"{score_val} avaliações")

    total_w = sum(w for w, _ in parts)
    combined = sum(w * s for w, s in parts) / total_w
    raw_val = (
        float(seller.feedback_percentage)
        if seller.feedback_percentage is not None
        else float(seller.feedback_score or 0)
    )
    return RawCriterionEvaluation(
        name=CriterionName.SELLER_RELIABILITY,
        raw_value=raw_val,
        normalized_score=max(0.0, min(1.0, combined)),
        available=True,
        evidence_sources=tuple(sources),
        note=", ".join(notes),
    )


def evaluate_condition_quality(candidate: Candidate) -> RawCriterionEvaluation:
    """Evaluate product condition quality from explicit listing condition."""
    raw_cond = (candidate.condition or "").strip()
    if not raw_cond:
        return RawCriterionEvaluation(
            name=CriterionName.CONDITION_QUALITY,
            raw_value=None,
            normalized_score=None,
            available=False,
            note="Condição do produto não informada.",
        )

    key = raw_cond.lower()
    if key in _CONDITION_SCORES:
        score, label = _CONDITION_SCORES[key]
    elif "refurb" in key or "recond" in key:
        score, label = 0.72, raw_cond
    elif "open" in key:
        score, label = 0.88, raw_cond
    elif "new" in key or "novo" in key:
        score, label = 1.0, raw_cond
    elif "used" in key or "usado" in key or "pre-owned" in key:
        score, label = 0.50, raw_cond
    else:
        return RawCriterionEvaluation(
            name=CriterionName.CONDITION_QUALITY,
            raw_value=raw_cond,
            normalized_score=None,
            available=False,
            note=f"Condição não mapeada: {raw_cond}",
        )

    return RawCriterionEvaluation(
        name=CriterionName.CONDITION_QUALITY,
        raw_value=raw_cond,
        normalized_score=score,
        available=True,
        evidence_sources=("condition",),
        note=label,
    )


_WEIGHT_KG_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*kg\b", re.IGNORECASE)
_WEIGHT_G_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*g\b", re.IGNORECASE)
_BATTERY_H_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:h|hr|hrs|hour|hours|horas)\b", re.IGNORECASE)


def _extract_weight_kg(candidate: Candidate) -> tuple[float | None, str]:
    for key, vals in candidate.aspects.items():
        if key.strip().lower() in ("weight", "peso", "item weight"):
            joined = " ".join(vals)
            m_kg = _WEIGHT_KG_RE.search(joined)
            if m_kg:
                return float(m_kg.group(1).replace(",", ".")), f"aspects.{key}"
            m_g = _WEIGHT_G_RE.search(joined)
            if m_g:
                return float(m_g.group(1).replace(",", ".")) / 1000.0, f"aspects.{key}"
    m_kg = _WEIGHT_KG_RE.search(candidate.title)
    if m_kg:
        return float(m_kg.group(1).replace(",", ".")), "title"
    return None, ""


def _extract_battery_hours(candidate: Candidate) -> tuple[float | None, str]:
    for key, vals in candidate.aspects.items():
        if "battery" in key.strip().lower() or "bateria" in key.strip().lower():
            m = _BATTERY_H_RE.search(" ".join(vals))
            if m:
                return float(m.group(1).replace(",", ".")), f"aspects.{key}"
    m = _BATTERY_H_RE.search(candidate.title)
    if m:
        return float(m.group(1).replace(",", ".")), "title"
    return None, ""


def _verified_hardware_tier_score(
    ev_map: CandidateEvidenceMap,
) -> tuple[float | None, tuple[str, ...], str]:
    """Score verified technical specs strictly from extracted evidences (ignoring promotional fluff)."""
    ev = ev_map.evidences
    sub_scores: list[float] = []
    sources: list[str] = []
    notes: list[str] = []

    ram = ev.get("ram_gb")
    if ram is not None and isinstance(ram.value, (int, float)):
        gb = float(ram.value)
        sub_scores.append(1.0 if gb >= 32 else (0.85 if gb >= 16 else (0.55 if gb >= 8 else 0.25)))
        sources.append(ram.source_field)
        notes.append(f"{int(gb)}GB RAM")

    storage = ev.get("storage_gb")
    if storage is not None and isinstance(storage.value, (int, float)):
        st = float(storage.value)
        sub_scores.append(1.0 if st >= 1000 else (0.82 if st >= 512 else (0.60 if st >= 256 else 0.35)))
        sources.append(storage.source_field)
        notes.append(f"{int(st)}GB")

    res = ev.get("resolution")
    if res is not None and isinstance(res.value, str):
        r = res.value.lower()
        sub_scores.append(1.0 if ("4k" in r or "2160" in r) else (0.80 if ("1440" in r or "qhd" in r) else 0.55))
        sources.append(res.source_field)
        notes.append(res.value)

    hz = ev.get("refresh_rate_hz")
    if hz is not None and isinstance(hz.value, (int, float)):
        rate = float(hz.value)
        sub_scores.append(1.0 if rate >= 144 else (0.82 if rate >= 120 else (0.68 if rate >= 75 else 0.50)))
        sources.append(hz.source_field)
        notes.append(f"{int(rate)}Hz")

    panel = ev.get("panel_type")
    if panel is not None and isinstance(panel.value, str):
        p = panel.value.lower()
        sub_scores.append(1.0 if "oled" in p else (0.85 if "ips" in p else (0.68 if "va" in p else 0.45)))
        sources.append(panel.source_field)
        notes.append(panel.value)

    conn = ev.get("connectivity_features")
    if conn is not None and conn.value:
        feats = [str(f).lower() for f in conn.value]
        has_anc = any("anc" in f or "noise" in f for f in feats)
        has_bt = any("bluetooth" in f or "wireless" in f for f in feats)
        sub_scores.append(0.95 if (has_anc and has_bt) else (0.80 if (has_anc or has_bt) else 0.60))
        sources.append(conn.source_field)
        notes.append(", ".join(str(f) for f in conn.value))

    if not sub_scores:
        return None, (), ""
    return sum(sub_scores) / len(sub_scores), tuple(dict.fromkeys(sources)), ", ".join(notes)


def evaluate_technical_fit(
    validation: CandidateValidationResult,
    *,
    category_hint: str | None = None,
    evidences: CandidateEvidenceMap | None = None,
) -> RawCriterionEvaluation:
    """Evaluate verified technical criteria and hardware spec tier without promotional title inflation."""
    evals = validation.evaluations
    ev_map = evidences or extract_candidate_evidences(
        validation.candidate, category_hint=category_hint or validation.detected_category
    )
    hw_score, hw_sources, hw_note = _verified_hardware_tier_score(ev_map)
    known = [e for e in evals if e.status in ("satisfied", "not_satisfied")]

    if not evals and hw_score is None:
        return RawCriterionEvaluation(
            name=CriterionName.TECHNICAL_FIT,
            raw_value=None,
            normalized_score=None,
            available=False,
            not_applicable=True,
            note="Sem critérios técnicos aplicáveis.",
        )

    if not known and hw_score is None:
        return RawCriterionEvaluation(
            name=CriterionName.TECHNICAL_FIT,
            raw_value=None,
            normalized_score=None,
            available=False,
            note="Critérios técnicos sem evidência suficiente.",
        )

    sources: list[str] = list(hw_sources)
    if known:
        satisfied = sum(1 for e in known if e.status == "satisfied")
        known_ratio = len(known) / len(evals)
        match_ratio = satisfied / len(known)
        req_score = match_ratio * (0.85 + 0.15 * known_ratio)
        sources.extend(e.source_field for e in known if e.source_field)
        combined = (0.70 * req_score + 0.30 * hw_score) if hw_score is not None else req_score
        note = f"{satisfied} de {len(known)} critérios técnicos verificados atendidos"
        if hw_note:
            note += f" ({hw_note})"
        note += "."
        raw_val = f"{satisfied}/{len(evals)}"
    else:
        combined = hw_score if hw_score is not None else 0.0
        note = f"Especificações verificadas: {hw_note}."
        raw_val = hw_note

    dedup_sources = tuple(dict.fromkeys(s for s in sources if s)) or ("validation.evaluations",)
    return RawCriterionEvaluation(
        name=CriterionName.TECHNICAL_FIT,
        raw_value=raw_val,
        normalized_score=max(0.0, min(1.0, combined)),
        available=True,
        evidence_sources=dedup_sources,
        note=note,
    )


def evaluate_portability(
    candidate: Candidate,
    *,
    category_hint: str | None,
    evidences: CandidateEvidenceMap | None = None,
) -> RawCriterionEvaluation:
    """Evaluate portability for portable MVP categories; not applicable to monitors."""
    cat = (category_hint or "").strip().lower()
    if cat == "monitor":
        return RawCriterionEvaluation(
            name=CriterionName.PORTABILITY,
            raw_value=None,
            normalized_score=None,
            available=False,
            not_applicable=True,
            note="Portabilidade não aplicável para monitores.",
        )

    ev_map = evidences or extract_candidate_evidences(candidate, category_hint=category_hint)
    ev = ev_map.evidences

    if cat == "fone de ouvido":
        conn = ev.get("connectivity_features")
        form_factor = ev.get("form_factor")
        battery_hrs, battery_src = _extract_battery_hours(candidate)
        feats = [str(f).lower() for f in conn.value] if (conn is not None and conn.value) else []
        has_wireless = any(t in f for f in feats for t in ("bluetooth", "wireless", "tws"))
        has_wired = any("wired" in f or "3.5mm" in f or "fio" in f for f in feats)

        if not feats and form_factor is None and battery_hrs is None:
            return RawCriterionEvaluation(
                name=CriterionName.PORTABILITY,
                raw_value=None,
                normalized_score=None,
                available=False,
                note="Sem evidência de portabilidade para fone.",
            )

        score = 0.65
        sources: list[str] = []
        notes: list[str] = []
        if conn is not None and feats:
            sources.append(conn.source_field)
            if has_wireless:
                score = 0.90
                notes.append("Sem fio/Bluetooth")
            elif has_wired:
                score = 0.45
                notes.append("Com fio")
        if form_factor is not None:
            sources.append(form_factor.source_field)
            ff = str(form_factor.value).lower()
            if "in-ear" in ff or "earbud" in ff:
                score = min(1.0, score + 0.08)
            notes.append(str(form_factor.value))
        if battery_hrs is not None:
            sources.append(battery_src)
            if battery_hrs >= 25:
                score = min(1.0, score + 0.08)
            notes.append(f"Bateria {battery_hrs:.0f}h")
        return RawCriterionEvaluation(
            name=CriterionName.PORTABILITY,
            raw_value=", ".join(notes),
            normalized_score=max(0.0, min(1.0, score)),
            available=True,
            evidence_sources=tuple(dict.fromkeys(sources)),
            note=", ".join(notes),
        )

    weight_kg, weight_src = _extract_weight_kg(candidate)
    screen = ev.get("screen_size_inches")
    if weight_kg is not None:
        if cat == "smartphone":
            score = 0.95 if weight_kg <= 0.18 else (0.75 if weight_kg <= 0.21 else 0.55)
        else:
            score = 1.0 if weight_kg <= 1.4 else (0.80 if weight_kg <= 1.8 else (0.55 if weight_kg <= 2.2 else 0.25))
        return RawCriterionEvaluation(
            name=CriterionName.PORTABILITY,
            raw_value=weight_kg,
            normalized_score=score,
            available=True,
            evidence_sources=(weight_src,),
            note=f"Peso {weight_kg:.2f} kg",
        )

    if screen is not None and isinstance(screen.value, (int, float)):
        inches = float(screen.value)
        if cat == "smartphone":
            score = 0.90 if inches <= 6.2 else (0.70 if inches <= 6.6 else 0.50)
        else:
            score = 0.85 if inches <= 14.2 else (0.55 if inches <= 15.6 else 0.25)
        return RawCriterionEvaluation(
            name=CriterionName.PORTABILITY,
            raw_value=inches,
            normalized_score=score,
            available=True,
            evidence_sources=(screen.source_field,),
            note=f'Tela {inches:.1f}"',
        )

    return RawCriterionEvaluation(
        name=CriterionName.PORTABILITY,
        raw_value=None,
        normalized_score=None,
        available=False,
        note="Peso e tamanho de tela não informados.",
    )


def evaluate_logistics(candidate: Candidate) -> RawCriterionEvaluation:
    """Evaluate shipping cost/free-shipping and delivery estimate when available."""
    shipping = candidate.shipping
    if shipping is None:
        return RawCriterionEvaluation(
            name=CriterionName.LOGISTICS,
            raw_value=None,
            normalized_score=None,
            available=False,
            note="Frete e prazo não informados.",
        )

    signals: list[float] = []
    sources: list[str] = []
    notes: list[str] = []

    if shipping.free_shipping:
        signals.append(1.0)
        sources.append("shipping.free_shipping")
        notes.append("Frete grátis")
    elif shipping.cost is not None:
        signals.append(0.45)
        sources.append("shipping.cost")
        notes.append(f"Frete {shipping.cost} {shipping.currency or candidate.currency}")

    if shipping.estimated_max_days is not None:
        days = int(shipping.estimated_max_days)
        day_score = 1.0 if days <= 3 else (0.80 if days <= 7 else (0.55 if days <= 14 else 0.30))
        signals.append(day_score)
        sources.append("shipping.estimated_max_days")
        notes.append(f"Entrega em até {days} dias")

    if not signals:
        return RawCriterionEvaluation(
            name=CriterionName.LOGISTICS,
            raw_value=None,
            normalized_score=None,
            available=False,
            note="Dados de logística insuficientes.",
        )

    return RawCriterionEvaluation(
        name=CriterionName.LOGISTICS,
        raw_value=", ".join(notes),
        normalized_score=sum(signals) / len(signals),
        available=True,
        evidence_sources=tuple(sources),
        note=", ".join(notes),
    )


def evaluate_semantic_relevance(
    semantic_score: float | None,
    *,
    enable_semantic_score: bool,
) -> RawCriterionEvaluation:
    """Evaluate optional semantic relevance score with explicit disable toggle."""
    if not enable_semantic_score:
        return RawCriterionEvaluation(
            name=CriterionName.SEMANTIC_RELEVANCE,
            raw_value=None,
            normalized_score=None,
            available=False,
            not_applicable=True,
            note="Sinal semântico desabilitado por configuração.",
        )
    if semantic_score is None:
        return RawCriterionEvaluation(
            name=CriterionName.SEMANTIC_RELEVANCE,
            raw_value=None,
            normalized_score=None,
            available=False,
            note="Score semântico indisponível.",
        )
    clamped = max(0.0, min(1.0, float(semantic_score)))
    return RawCriterionEvaluation(
        name=CriterionName.SEMANTIC_RELEVANCE,
        raw_value=clamped,
        normalized_score=clamped,
        available=True,
        evidence_sources=("semantic_score",),
        note=f"Similaridade semântica {clamped:.2f}",
    )
