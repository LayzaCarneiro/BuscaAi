"""Validador determinístico por evidências e avaliação de orçamento (T5.2 e T5.3)."""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, ROUND_HALF_UP

from buscai.catalog_registry import ProfileCriterion, ProfileRegistry, load_default_registry
from buscai.pricing import CurrencyConverter, ExchangeRateInfo
from buscai.search.models import Candidate
from buscai.search.query_builder import _CONDITION_MAP, _translate_feature_en
from buscai.validation.evidence import extract_candidate_evidences, match_model_tokens_in_candidate
from buscai.validation.models import (
    BudgetEvaluation,
    CandidateEvidenceMap,
    CandidateValidationResult,
    ConstraintPriority,
    CriterionEvaluation,
    ValidationStatus,
)
from userintent.schema import PriceConstraint, UserIntent

_TWO_DECIMALS = Decimal("0.01")


def _norm_text(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", " ".join(text.strip().casefold().split()))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _convert_decimal(
    amount: Decimal,
    *,
    from_currency: str,
    to_currency: str,
    currency_converter: CurrencyConverter | None,
) -> tuple[Decimal | None, bool, ExchangeRateInfo | None]:
    f_cur = from_currency.strip().upper()
    t_cur = to_currency.strip().upper()
    if f_cur == t_cur:
        return amount.quantize(_TWO_DECIMALS, rounding=ROUND_HALF_UP), False, None
    if currency_converter is None:
        return None, False, None
    if f_cur == "USD" and t_cur == "BRL":
        est = currency_converter.usd_to_brl(amount)
        return est.amount, True, currency_converter.rate_info()
    if f_cur == "BRL" and t_cur == "USD":
        return currency_converter.brl_to_usd(amount), True, currency_converter.rate_info()
    return None, False, None


def _within_bound(
    value: Decimal,
    *,
    bound: Decimal,
    inclusive: bool,
    is_max: bool,
) -> bool:
    if is_max:
        return value <= bound if inclusive else value < bound
    return value >= bound if inclusive else value > bound


def evaluate_budget(
    candidate: Candidate,
    price_constraint: PriceConstraint,
    *,
    currency_converter: CurrencyConverter | None = None,
    default_budget_currency: str = "BRL",
    require_shipping_for_total: bool = True,
) -> BudgetEvaluation:
    """Avalia orçamento com `Decimal`, inclusividade (`max_inclusive`), câmbio e frete."""
    priority: ConstraintPriority = "hard" if price_constraint.is_hard_constraint else "soft"
    has_bounds = price_constraint.min_price is not None or price_constraint.max_price is not None
    if not has_bounds:
        return BudgetEvaluation(
            status="not_applicable",
            priority=priority,
            note="Nenhum limite de orçamento informado.",
        )

    budget_currency = (price_constraint.currency or default_budget_currency).strip().upper()
    min_dec = Decimal(str(price_constraint.min_price)) if price_constraint.min_price is not None else None
    max_dec = Decimal(str(price_constraint.max_price)) if price_constraint.max_price is not None else None

    item_converted, is_est_item, rate_info = _convert_decimal(
        candidate.price,
        from_currency=candidate.currency,
        to_currency=budget_currency,
        currency_converter=currency_converter,
    )
    if item_converted is None:
        return BudgetEvaluation(
            status="unknown",
            priority=priority,
            budget_currency=budget_currency,
            min_price=min_dec,
            max_price=max_dec,
            min_inclusive=price_constraint.min_inclusive,
            max_inclusive=price_constraint.max_inclusive,
            note=f"Não foi possível converter preço de {candidate.currency} para {budget_currency}.",
        )

    # Se o próprio preço do item já excede o teto, não é necessário conhecer o frete para reprovar.
    if max_dec is not None and not _within_bound(
        item_converted, bound=max_dec, inclusive=price_constraint.max_inclusive, is_max=True
    ):
        return BudgetEvaluation(
            status="not_satisfied",
            priority=priority,
            budget_currency=budget_currency,
            min_price=min_dec,
            max_price=max_dec,
            min_inclusive=price_constraint.min_inclusive,
            max_inclusive=price_constraint.max_inclusive,
            item_price_in_budget_currency=item_converted,
            is_estimated_conversion=is_est_item,
            exchange_rate=rate_info,
            note="Preço do produto excede o orçamento máximo permitido.",
        )

    shipping_known = False
    shipping_converted: Decimal | None = None
    is_est_ship = False
    if candidate.shipping is not None:
        if candidate.shipping.free_shipping is True and candidate.shipping.cost is None:
            shipping_known = True
            shipping_converted = Decimal("0.00")
        elif candidate.shipping.cost is not None:
            ship_cur = candidate.shipping.currency or candidate.currency
            shipping_converted, is_est_ship, ship_rate = _convert_decimal(
                candidate.shipping.cost,
                from_currency=ship_cur,
                to_currency=budget_currency,
                currency_converter=currency_converter,
            )
            if shipping_converted is not None:
                shipping_known = True
                if rate_info is None:
                    rate_info = ship_rate

    is_estimated = is_est_item or is_est_ship

    if not shipping_known:
        if require_shipping_for_total and max_dec is not None:
            return BudgetEvaluation(
                status="unknown",
                priority=priority,
                budget_currency=budget_currency,
                min_price=min_dec,
                max_price=max_dec,
                min_inclusive=price_constraint.min_inclusive,
                max_inclusive=price_constraint.max_inclusive,
                item_price_in_budget_currency=item_converted,
                shipping_known=False,
                is_estimated_conversion=is_estimated,
                exchange_rate=rate_info,
                note="Frete desconhecido: orçamento total incluindo frete não pode ser comprovado.",
            )
        total = item_converted
    else:
        assert shipping_converted is not None
        total = (item_converted + shipping_converted).quantize(_TWO_DECIMALS, rounding=ROUND_HALF_UP)

    if max_dec is not None and not _within_bound(
        total, bound=max_dec, inclusive=price_constraint.max_inclusive, is_max=True
    ):
        return BudgetEvaluation(
            status="not_satisfied",
            priority=priority,
            budget_currency=budget_currency,
            min_price=min_dec,
            max_price=max_dec,
            min_inclusive=price_constraint.min_inclusive,
            max_inclusive=price_constraint.max_inclusive,
            item_price_in_budget_currency=item_converted,
            shipping_cost_in_budget_currency=shipping_converted,
            total_price_in_budget_currency=total,
            shipping_known=shipping_known,
            is_estimated_conversion=is_estimated,
            exchange_rate=rate_info,
            note="Preço total com frete excede o teto de orçamento.",
        )

    if min_dec is not None and not _within_bound(
        total, bound=min_dec, inclusive=price_constraint.min_inclusive, is_max=False
    ):
        return BudgetEvaluation(
            status="not_satisfied",
            priority=priority,
            budget_currency=budget_currency,
            min_price=min_dec,
            max_price=max_dec,
            min_inclusive=price_constraint.min_inclusive,
            max_inclusive=price_constraint.max_inclusive,
            item_price_in_budget_currency=item_converted,
            shipping_cost_in_budget_currency=shipping_converted,
            total_price_in_budget_currency=total,
            shipping_known=shipping_known,
            is_estimated_conversion=is_estimated,
            exchange_rate=rate_info,
            note="Preço total abaixo do limite mínimo solicitado.",
        )

    return BudgetEvaluation(
        status="satisfied",
        priority=priority,
        budget_currency=budget_currency,
        min_price=min_dec,
        max_price=max_dec,
        min_inclusive=price_constraint.min_inclusive,
        max_inclusive=price_constraint.max_inclusive,
        item_price_in_budget_currency=item_converted,
        shipping_cost_in_budget_currency=shipping_converted,
        total_price_in_budget_currency=total,
        shipping_known=shipping_known,
        is_estimated_conversion=is_estimated,
        exchange_rate=rate_info,
        note="Conversão estimada aplicada." if is_estimated else "Dentro do orçamento na mesma moeda.",
    )


def _compare_operator(actual: object, operator: str, expected: object) -> bool:
    if operator == "eq":
        return actual == expected
    if operator == "neq":
        return actual != expected
    if operator == "gte":
        return Decimal(str(actual)) >= Decimal(str(expected))
    if operator == "lte":
        return Decimal(str(actual)) <= Decimal(str(expected))
    if operator == "in":
        return actual in expected  # type: ignore[operator]
    if operator == "contains":
        if isinstance(actual, (tuple, list, set, frozenset)):
            return str(expected).casefold() in {str(item).casefold() for item in actual}
        return str(expected).casefold() in str(actual).casefold()
    raise ValueError(f"Operador desconhecido: {operator!r}")


_FEATURE_RAM_RE = re.compile(r"(\d{1,3})\s*gb.*?\bram\b|\bram\b.*?(\d{1,3})\s*gb", re.IGNORECASE)
_FEATURE_STORAGE_RE = re.compile(r"(\d{1,4}(?:[.,]\d+)?)\s*(gb|tb)\b", re.IGNORECASE)
_FEATURE_HZ_RE = re.compile(r"(\d{2,3})\s*hz\b", re.IGNORECASE)
_FEATURE_INCH_RE = re.compile(r"(\d{1,2}(?:[.,]\d{1,2})?)\s*(?:polegadas?|pol\.?|inch(?:es)?|\")", re.IGNORECASE)


def _evaluate_feature_criterion(
    raw_feature: str,
    *,
    priority: ConstraintPriority,
    negated: bool,
    ev_map: CandidateEvidenceMap,
) -> CriterionEvaluation:
    norm = _norm_text(raw_feature)
    crit_id = f"{'excluded' if negated else priority}_feature:{norm}"

    def _finish(
        matched: bool | None,
        *,
        ev_text: str | None = None,
        src: str | None = None,
        note: str | None = None,
    ) -> CriterionEvaluation:
        if matched is None:
            return CriterionEvaluation(
                criterion_id=crit_id,
                criterion_label=raw_feature,
                priority=priority,
                status="unknown",
                evidence=None,
                source_field=None,
                insufficient_information=True,
                note=note or "Informação insuficiente no anúncio.",
            )
        if negated:
            status: ValidationStatus = "not_satisfied" if matched else "satisfied"
        else:
            status = "satisfied" if matched else "not_satisfied"
        return CriterionEvaluation(
            criterion_id=crit_id,
            criterion_label=raw_feature,
            priority=priority,
            status=status,
            evidence=ev_text,
            source_field=src,
            insufficient_information=False,
            note=note,
        )

    # 1. RAM
    ram_m = _FEATURE_RAM_RE.search(norm)
    if ram_m:
        req_ram = int(ram_m.group(1) or ram_m.group(2))
        if "ram_gb" in ev_map.contradictions:
            return _finish(None, note=f"Evidência contraditória de RAM: {ev_map.contradictions['ram_gb']}")
        ev = ev_map.evidences.get("ram_gb")
        if ev is None:
            return _finish(None)
        return _finish(int(ev.value) >= req_ram, ev_text=f"{ev.value} GB RAM", src=ev.source_field)

    # 2. Taxa de atualização (Hz)
    hz_m = _FEATURE_HZ_RE.search(norm)
    if hz_m:
        req_hz = int(hz_m.group(1))
        if "refresh_rate_hz" in ev_map.contradictions:
            return _finish(None, note=f"Contradição de Hz: {ev_map.contradictions['refresh_rate_hz']}")
        ev = ev_map.evidences.get("refresh_rate_hz")
        if ev is None:
            return _finish(None)
        return _finish(int(ev.value) >= req_hz, ev_text=f"{ev.value}Hz", src=ev.source_field)

    # 3. Tamanho de tela (polegadas)
    inch_m = _FEATURE_INCH_RE.search(norm)
    if inch_m:
        req_inch = Decimal(inch_m.group(1).replace(",", "."))
        ev = ev_map.evidences.get("screen_size_inches")
        if ev is None:
            return _finish(None)
        return _finish(Decimal(str(ev.value)) >= req_inch, ev_text=f"{ev.value} inch", src=ev.source_field)

    # 4. Armazenamento
    if any(tok in norm for tok in ("armazenamento", "ssd", "hdd", "nvme", "espaco", "storage", "rom")) or (
        _FEATURE_STORAGE_RE.search(norm) and "ram" not in norm
    ):
        st_m = _FEATURE_STORAGE_RE.search(norm)
        if st_m:
            num = Decimal(st_m.group(1).replace(",", "."))
            mult = 1024 if st_m.group(2).lower() == "tb" else 1
            req_gb = int((num * mult).to_integral_value())
            if "storage_gb" in ev_map.contradictions:
                return _finish(None, note=f"Contradição de armazenamento: {ev_map.contradictions['storage_gb']}")
            ev = ev_map.evidences.get("storage_gb")
            if ev is None:
                return _finish(None)
            return _finish(int(ev.value) >= req_gb, ev_text=f"{ev.value} GB", src=ev.source_field)

    # 5. Resolução
    for res_code, triggers in (
        ("uhd_4k", ("4k", "uhd", "2160p")),
        ("qhd", ("qhd", "wqhd", "1440p")),
        ("fhd", ("full hd", "fhd", "1080p")),
    ):
        if any(tr in norm for tr in triggers):
            if "resolution" in ev_map.contradictions:
                return _finish(None, note=f"Contradição de resolução: {ev_map.contradictions['resolution']}")
            ev = ev_map.evidences.get("resolution")
            if ev is None:
                return _finish(None)
            order = {"hd": 1, "fhd": 2, "qhd": 3, "uhd_4k": 4, "5k": 5, "8k": 6}
            return _finish(
                order.get(str(ev.value), 0) >= order[res_code],
                ev_text=str(ev.value),
                src=ev.source_field,
            )

    # 6. Conectividade / ANC / Form factor / Painel
    conn_rules: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("anc", ("anc", "cancelamento de ruido", "cancelamento ativo de ruido", "noise cancelling")),
        ("bluetooth", ("bluetooth",)),
        ("usb-c", ("usb-c", "usb c", "tipo c", "type-c")),
        ("5g", ("5g",)),
        ("nfc", ("nfc",)),
    )
    for conn_key, conn_triggers in conn_rules:
        if any(tr in norm for tr in conn_triggers):
            ev = ev_map.evidences.get("connectivity_features")
            if ev is not None and conn_key in ev.value:
                return _finish(True, ev_text=conn_key, src=ev.source_field)
            return _finish(None)

    # 7. Termo traduzido ou textual no anúncio (só afirma presença; ausência = unknown)
    searchable_ev = ev_map.evidences.get("searchable_norm")
    searchable = str(searchable_ev.value) if searchable_ev is not None else ""
    translated, _ = _translate_feature_en(raw_feature)
    probes = [norm]
    if translated:
        probes.append(_norm_text(translated))
    for probe in probes:
        if probe and re.search(rf"(?<!\w){re.escape(probe)}(?!\w)", searchable):
            return _finish(True, ev_text=probe, src="title+aspects+short_description")
    return _finish(None)


class EvidenceValidator:
    """Validador determinístico por evidências com separação estrita hard vs. soft."""

    def __init__(
        self,
        *,
        registry: ProfileRegistry | None = None,
        currency_converter: CurrencyConverter | None = None,
        default_budget_currency: str = "BRL",
        require_shipping_for_total: bool = True,
    ) -> None:
        self._registry = registry or load_default_registry()
        self._currency_converter = currency_converter
        self._default_budget_currency = default_budget_currency.strip().upper()
        self._require_shipping_for_total = require_shipping_for_total

    def validate_candidate(
        self,
        candidate: Candidate,
        intent: UserIntent,
        *,
        profile_id: str | None = None,
        relaxed_criteria_ids: frozenset[str] = frozenset(),
        relaxed_preferred_features: frozenset[str] = frozenset(),
        ignore_soft_budget: bool = False,
        ambiguous_item_ids: frozenset[str] = frozenset(),
    ) -> CandidateValidationResult:
        cat_def = self._registry.resolve_category(intent.category)
        canonical_category = cat_def.canonical_id if cat_def is not None else intent.category
        ev_map = extract_candidate_evidences(candidate, category_hint=canonical_category)
        evaluations: list[CriterionEvaluation] = []

        # --- HARD CONSTRAINTS ---
        # 1. Categoria obrigatória
        if canonical_category is not None:
            cat_ev = ev_map.evidences.get("canonical_category")
            if cat_ev is None:
                evaluations.append(
                    CriterionEvaluation(
                        criterion_id="hard:category",
                        criterion_label=f"Categoria: {canonical_category}",
                        priority="hard",
                        status="unknown",
                        insufficient_information=True,
                        note="Categoria não identificada nas evidências do anúncio.",
                    )
                )
            elif _norm_text(str(cat_ev.value)) == _norm_text(canonical_category):
                evaluations.append(
                    CriterionEvaluation(
                        criterion_id="hard:category",
                        criterion_label=f"Categoria: {canonical_category}",
                        priority="hard",
                        status="satisfied",
                        evidence=str(cat_ev.value),
                        source_field=cat_ev.source_field,
                    )
                )
            else:
                evaluations.append(
                    CriterionEvaluation(
                        criterion_id="hard:category",
                        criterion_label=f"Categoria: {canonical_category}",
                        priority="hard",
                        status="not_satisfied",
                        evidence=str(cat_ev.value),
                        source_field=cat_ev.source_field,
                    )
                )

        # 2. Marca exigida
        brand_ev = ev_map.evidences.get("brand")
        if intent.brands:
            wanted_brands = {_norm_text(b) for b in intent.brands}
            if brand_ev is None:
                evaluations.append(
                    CriterionEvaluation(
                        criterion_id="hard:brand",
                        criterion_label=f"Marca: {', '.join(intent.brands)}",
                        priority="hard",
                        status="unknown",
                        insufficient_information=True,
                        note="Marca não informada no anúncio.",
                    )
                )
            elif _norm_text(str(brand_ev.value)) in wanted_brands:
                evaluations.append(
                    CriterionEvaluation(
                        criterion_id="hard:brand",
                        criterion_label=f"Marca: {', '.join(intent.brands)}",
                        priority="hard",
                        status="satisfied",
                        evidence=str(brand_ev.value),
                        source_field=brand_ev.source_field,
                    )
                )
            else:
                evaluations.append(
                    CriterionEvaluation(
                        criterion_id="hard:brand",
                        criterion_label=f"Marca: {', '.join(intent.brands)}",
                        priority="hard",
                        status="not_satisfied",
                        evidence=str(brand_ev.value),
                        source_field=brand_ev.source_field,
                    )
                )

        # 3. Marca excluída
        for ex_brand in intent.excluded_brands:
            if brand_ev is None:
                evaluations.append(
                    CriterionEvaluation(
                        criterion_id=f"hard:excluded_brand:{_norm_text(ex_brand)}",
                        criterion_label=f"Excluir marca: {ex_brand}",
                        priority="hard",
                        status="unknown",
                        insufficient_information=True,
                        note="Marca do candidato desconhecida para comprovar exclusão.",
                    )
                )
            elif _norm_text(str(brand_ev.value)) == _norm_text(ex_brand):
                evaluations.append(
                    CriterionEvaluation(
                        criterion_id=f"hard:excluded_brand:{_norm_text(ex_brand)}",
                        criterion_label=f"Excluir marca: {ex_brand}",
                        priority="hard",
                        status="not_satisfied",
                        evidence=str(brand_ev.value),
                        source_field=brand_ev.source_field,
                    )
                )
            else:
                evaluations.append(
                    CriterionEvaluation(
                        criterion_id=f"hard:excluded_brand:{_norm_text(ex_brand)}",
                        criterion_label=f"Excluir marca: {ex_brand}",
                        priority="hard",
                        status="satisfied",
                        evidence=str(brand_ev.value),
                        source_field=brand_ev.source_field,
                    )
                )

        # 4. Modelo exigido
        for req_model in intent.models:
            matched_model, model_ev_txt = match_model_tokens_in_candidate(req_model, candidate)
            if matched_model:
                model_ev = ev_map.evidences.get("model_tokens")
                evaluations.append(
                    CriterionEvaluation(
                        criterion_id=f"hard:model:{_norm_text(req_model)}",
                        criterion_label=f"Modelo: {req_model}",
                        priority="hard",
                        status="satisfied",
                        evidence=model_ev_txt,
                        source_field=model_ev.source_field if model_ev else "title",
                    )
                )
            elif ev_map.evidences.get("model_tokens") is None:
                evaluations.append(
                    CriterionEvaluation(
                        criterion_id=f"hard:model:{_norm_text(req_model)}",
                        criterion_label=f"Modelo: {req_model}",
                        priority="hard",
                        status="unknown",
                        insufficient_information=True,
                        note="Modelo não identificado no anúncio.",
                    )
                )
            else:
                model_ev = ev_map.evidences["model_tokens"]
                evaluations.append(
                    CriterionEvaluation(
                        criterion_id=f"hard:model:{_norm_text(req_model)}",
                        criterion_label=f"Modelo: {req_model}",
                        priority="hard",
                        status="not_satisfied",
                        evidence=", ".join(model_ev.value),
                        source_field=model_ev.source_field,
                    )
                )

        # 5. Condição explícita
        if intent.condition is not None:
            cond_ev = ev_map.evidences.get("condition_bucket")
            mapped = _CONDITION_MAP.get(_norm_text(intent.condition))
            expected_bucket: str | None = None
            if mapped is not None:
                if "NEW" in mapped[0]:
                    expected_bucket = "new"
                elif "USED" in mapped[0]:
                    expected_bucket = "used"
                elif "1500" in mapped[1]:
                    expected_bucket = "open_box"
                elif mapped[1]:
                    expected_bucket = "refurbished"

            if expected_bucket is not None:
                if cond_ev is None:
                    evaluations.append(
                        CriterionEvaluation(
                            criterion_id="hard:condition",
                            criterion_label=f"Condição: {intent.condition}",
                            priority="hard",
                            status="unknown",
                            insufficient_information=True,
                            note="Condição do produto não informada.",
                        )
                    )
                elif cond_ev.value == expected_bucket:
                    evaluations.append(
                        CriterionEvaluation(
                            criterion_id="hard:condition",
                            criterion_label=f"Condição: {intent.condition}",
                            priority="hard",
                            status="satisfied",
                            evidence=str(cond_ev.value),
                            source_field=cond_ev.source_field,
                        )
                    )
                else:
                    evaluations.append(
                        CriterionEvaluation(
                            criterion_id="hard:condition",
                            criterion_label=f"Condição: {intent.condition}",
                            priority="hard",
                            status="not_satisfied",
                            evidence=str(cond_ev.value),
                            source_field=cond_ev.source_field,
                        )
                    )

        # 6. Características obrigatórias e excluídas da intenção
        for feat in intent.required_features:
            evaluations.append(_evaluate_feature_criterion(feat, priority="hard", negated=False, ev_map=ev_map))
        for feat in intent.excluded_features:
            evaluations.append(_evaluate_feature_criterion(feat, priority="hard", negated=True, ev_map=ev_map))

        # --- SOFT PREFERENCES (da intenção) ---
        for feat in intent.preferred_features:
            if _norm_text(feat) in relaxed_preferred_features:
                continue
            evaluations.append(_evaluate_feature_criterion(feat, priority="soft", negated=False, ev_map=ev_map))

        # --- PERFIL DE UTILIZAÇÃO (critérios hard + soft do registro) ---
        active_profile = self._registry.resolve_profile_for_intent(intent, explicit_profile=profile_id)
        active_profile_id = active_profile.profile_id if active_profile is not None else None
        if active_profile is not None and canonical_category is not None:
            for crit in self._registry.criteria_for(active_profile.profile_id, canonical_category):
                if crit.priority == "soft" and crit.criterion_id in relaxed_criteria_ids:
                    continue
                evaluations.append(self._evaluate_profile_criterion(crit, ev_map, canonical_category))

        # --- ORÇAMENTO ---
        budget_eval: BudgetEvaluation | None = None
        if not (ignore_soft_budget and not intent.price.is_hard_constraint):
            if intent.price.min_price is not None or intent.price.max_price is not None:
                budget_eval = evaluate_budget(
                    candidate,
                    intent.price,
                    currency_converter=self._currency_converter,
                    default_budget_currency=self._default_budget_currency,
                    require_shipping_for_total=self._require_shipping_for_total,
                )
                b_ev_text: str | None = None
                b_src: str | None = None
                if budget_eval.status in ("satisfied", "not_satisfied"):
                    b_ev_text = (
                        f"total={budget_eval.total_price_in_budget_currency} {budget_eval.budget_currency}"
                        if budget_eval.total_price_in_budget_currency is not None
                        else f"item={budget_eval.item_price_in_budget_currency} {budget_eval.budget_currency}"
                    )
                    b_src = "price+shipping" if budget_eval.shipping_known else "price"
                evaluations.append(
                    CriterionEvaluation(
                        criterion_id="budget",
                        criterion_label="Orçamento",
                        priority=budget_eval.priority,
                        status=budget_eval.status,
                        evidence=b_ev_text,
                        source_field=b_src,
                        insufficient_information=(budget_eval.status == "unknown"),
                        note=budget_eval.note,
                    )
                )

        hard_sat = sum(1 for e in evaluations if e.priority == "hard" and e.status == "satisfied")
        hard_not = sum(1 for e in evaluations if e.priority == "hard" and e.status == "not_satisfied")
        hard_unk = sum(1 for e in evaluations if e.priority == "hard" and e.status == "unknown")
        soft_sat = sum(1 for e in evaluations if e.priority == "soft" and e.status == "satisfied")
        soft_not = sum(1 for e in evaluations if e.priority == "soft" and e.status == "not_satisfied")
        soft_unk = sum(1 for e in evaluations if e.priority == "soft" and e.status == "unknown")

        eligible = hard_not == 0
        strictly_verified = eligible and hard_unk == 0
        needs_enrichment = (
            not candidate.details_loaded
            and eligible
            and (
                hard_unk > 0
                or soft_unk > 0
                or candidate.item_id in ambiguous_item_ids
                or bool(ev_map.contradictions)
            )
        )

        return CandidateValidationResult(
            candidate=candidate,
            eligible=eligible,
            strictly_verified=strictly_verified,
            needs_enrichment=needs_enrichment,
            detected_category=ev_map.canonical_category or canonical_category,
            profile_id=active_profile_id,
            evaluations=tuple(evaluations),
            budget_evaluation=budget_eval,
            hard_satisfied_count=hard_sat,
            hard_not_satisfied_count=hard_not,
            hard_unknown_count=hard_unk,
            soft_satisfied_count=soft_sat,
            soft_not_satisfied_count=soft_not,
            soft_unknown_count=soft_unk,
            contradictions=ev_map.contradictions,
        )

    def _evaluate_profile_criterion(
        self,
        crit: ProfileCriterion,
        ev_map: CandidateEvidenceMap,
        category_id: str | None,
    ) -> CriterionEvaluation:
        if not self._registry.is_attribute_applicable(crit.attribute, category_id):
            return CriterionEvaluation(
                criterion_id=crit.criterion_id,
                criterion_label=crit.description_pt or crit.attribute,
                priority=crit.priority,
                status="not_applicable",
                note="Critério não aplicável à categoria.",
            )

        if crit.attribute in ev_map.contradictions:
            return CriterionEvaluation(
                criterion_id=crit.criterion_id,
                criterion_label=crit.description_pt or crit.attribute,
                priority=crit.priority,
                status="unknown",
                insufficient_information=True,
                note=f"Evidência contraditória em {crit.attribute}: {ev_map.contradictions[crit.attribute]}",
            )

        ev = ev_map.evidences.get(crit.attribute)
        if ev is None:
            if crit.missing_behavior == "not_applicable":
                return CriterionEvaluation(
                    criterion_id=crit.criterion_id,
                    criterion_label=crit.description_pt or crit.attribute,
                    priority=crit.priority,
                    status="not_applicable",
                )
            return CriterionEvaluation(
                criterion_id=crit.criterion_id,
                criterion_label=crit.description_pt or crit.attribute,
                priority=crit.priority,
                status="unknown",
                insufficient_information=True,
                note="Informação ausente para validar critério do perfil.",
            )

        passed = _compare_operator(ev.value, crit.operator, crit.expected_value)
        return CriterionEvaluation(
            criterion_id=crit.criterion_id,
            criterion_label=crit.description_pt or crit.attribute,
            priority=crit.priority,
            status="satisfied" if passed else "not_satisfied",
            evidence=str(ev.value),
            source_field=ev.source_field,
            insufficient_information=False,
        )


__all__ = ["EvidenceValidator", "evaluate_budget"]
