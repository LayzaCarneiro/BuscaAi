"""Deterministic, evidence-grounded PT-BR explanation builder for Stage 6."""

from __future__ import annotations

from buscai.ranking.models import (
    ApprovalState,
    CriterionName,
    Explanation,
    RankedOffer,
    ScoreBreakdown,
)
from buscai.search.models import Candidate
from buscai.validation.models import CandidateValidationResult, RelaxationStep

_PROFILE_LABELS: dict[str, str] = {
    "best_value": "custo-benefício",
    "best_performance": "melhor desempenho",
    "lowest_price": "menor preço",
    "professional_use": "uso profissional",
    "study": "estudos",
    "everyday_use": "uso cotidiano",
}


def _format_currency_brl(breakdown: ScoreBreakdown, candidate: Candidate) -> str:
    if breakdown.total_cost_brl is not None:
        if breakdown.shipping_known:
            return f"custo total verificado de R$ {breakdown.total_cost_brl:.2f}"
        return f"preço do item de R$ {breakdown.total_cost_brl:.2f} (frete não informado)"
    if candidate.price is not None:
        return f"preço anunciado de {candidate.price} {candidate.currency}"
    return "preço não informado"


def _build_sibling_comparison_note(
    *,
    item_id: str,
    breakdown: ScoreBreakdown,
    sibling_breakdowns: dict[str, ScoreBreakdown] | None,
) -> str | None:
    if not sibling_breakdowns or len(sibling_breakdowns) <= 1 or breakdown.total_cost_brl is None:
        return None
    other_costs = [
        sb.total_cost_brl
        for oid, sb in sibling_breakdowns.items()
        if oid != item_id and sb.total_cost_brl is not None
    ]
    if not other_costs:
        return None
    seller_crit = breakdown.by_name(CriterionName.SELLER_RELIABILITY)
    seller_missing = seller_crit is None or not seller_crit.available
    if breakdown.total_cost_brl < min(other_costs):
        if not breakdown.shipping_known:
            return (
                "Esta oferta possui menor preço anunciado entre as ofertas do mesmo modelo, "
                "mas o frete não foi informado."
            )
        if seller_missing:
            return (
                "Esta oferta possui menor custo total entre as ofertas do mesmo modelo, "
                "mas a reputação do vendedor não está disponível."
            )
        return "Esta oferta apresenta o menor custo total verificado entre as ofertas do mesmo modelo."
    return None


def build_offer_explanation(
    *,
    candidate: Candidate,
    validation: CandidateValidationResult,
    breakdown: ScoreBreakdown,
    approval_state: ApprovalState,
    profile_id: str | None,
    rank: int = 0,
    relaxations: tuple[RelaxationStep, ...] = (),
    sibling_breakdowns: dict[str, ScoreBreakdown] | None = None,
) -> Explanation:
    """Build an auditable PT-BR explanation strictly from verified evidences."""
    reasons: list[str] = []
    tradeoffs: list[str] = []
    warnings: list[str] = []
    missing_data: list[str] = []
    penalties: list[str] = []
    relaxation_notes: list[str] = []
    citations: list[str] = []

    cost_crit = breakdown.by_name(CriterionName.COST_VALUE)
    if cost_crit and cost_crit.available:
        price_desc = _format_currency_brl(breakdown, candidate)
        if breakdown.shipping_known and (cost_crit.normalized_score or 0.0) >= 0.75:
            reasons.append(f"Destaque em {price_desc}.")
        else:
            reasons.append(f"Oferta com {price_desc}.")
        if not breakdown.shipping_known:
            msg = "Frete não informado; a comparação de custo total permanece incompleta."
            warnings.append(msg)
            missing_data.append(msg)
        citations.extend(cost_crit.evidence_sources)

    tech_crit = breakdown.by_name(CriterionName.TECHNICAL_FIT)
    satisfied_evals = [
        e for e in validation.evaluations if e.status == "satisfied"
    ]
    not_satisfied_evals = [
        e for e in validation.evaluations if e.status == "not_satisfied"
    ]
    unknown_evals = [
        e for e in validation.evaluations if e.status == "unknown"
    ]
    if satisfied_evals:
        names = ", ".join(e.criterion_label or e.criterion_id for e in satisfied_evals[:3])
        reasons.append(f"Atende aos critérios técnicos verificados: {names}.")
        for e in satisfied_evals[:3]:
            if e.source_field:
                citations.append(e.source_field)
    elif tech_crit and tech_crit.available and (tech_crit.normalized_score or 0.0) >= 0.7:
        reasons.append(tech_crit.note)
        citations.extend(tech_crit.evidence_sources)

    for u in unknown_evals:
        missing_data.append(f"Não foi possível confirmar {u.criterion_label or u.criterion_id}.")

    cond_crit = breakdown.by_name(CriterionName.CONDITION_QUALITY)
    if cond_crit and cond_crit.available:
        score = cond_crit.normalized_score or 0.0
        if score >= 0.85:
            reasons.append(f"Condição verificada: {cond_crit.note}.")
        else:
            tradeoffs.append(f"Condição {cond_crit.note.lower()} em troca de menor preço ou disponibilidade.")
        citations.extend(cond_crit.evidence_sources)
    else:
        missing_data.append("Condição do produto não informada.")

    seller_crit = breakdown.by_name(CriterionName.SELLER_RELIABILITY)
    if seller_crit and seller_crit.available:
        score = seller_crit.normalized_score or 0.0
        if score >= 0.75:
            reasons.append(f"Vendedor com boa reputação ({seller_crit.note}).")
        else:
            tradeoffs.append(f"Reputação moderada do vendedor ({seller_crit.note}).")
        citations.extend(seller_crit.evidence_sources)
    else:
        missing_data.append("Reputação do vendedor não informada.")

    port_crit = breakdown.by_name(CriterionName.PORTABILITY)
    if port_crit and port_crit.available and (port_crit.normalized_score or 0.0) >= 0.75:
        reasons.append(f"Boa portabilidade ({port_crit.note}).")
        citations.extend(port_crit.evidence_sources)

    if not_satisfied_evals:
        missed = ", ".join(e.criterion_label or e.criterion_id for e in not_satisfied_evals[:3])
        tradeoffs.append(f"Preferências não atendidas: {missed}.")

    if breakdown.contradiction_count > 0:
        msg = (
            f"Foram detectadas {breakdown.contradiction_count} contradição(ões) entre título e atributos do anúncio."
        )
        warnings.append(msg)
        penalties.append(f"Penalidade por contradição (-{breakdown.contradiction_penalty:.2f}).")

    if breakdown.evidence_coverage < 0.70:
        msg = f"Cobertura parcial de evidências ({breakdown.evidence_coverage * 100:.0f}% dos critérios aplicáveis)."
        warnings.append(msg)
        penalties.append(f"Fator de confiança reduzido para {breakdown.confidence_factor:.2f}.")

    if approval_state is ApprovalState.APPROVED_WITH_RELAXATION:
        penalties.append("Ajuste de pontuação por aprovação mediante relaxamento controlado.")
        if relaxations:
            for step in relaxations:
                relaxation_notes.append(step.reason)
        else:
            relaxation_notes.append(
                "Aprovado após relaxamento controlado de preferências flexíveis."
            )

    offer_note = _build_sibling_comparison_note(
        item_id=candidate.item_id,
        breakdown=breakdown,
        sibling_breakdowns=sibling_breakdowns,
    )

    profile_label = _PROFILE_LABELS.get((profile_id or "").lower(), profile_id or "padrão")
    if approval_state is ApprovalState.APPROVED_WITH_RELAXATION:
        prefix = f"Recomendado com relaxamento para o perfil {profile_label}"
    elif approval_state is ApprovalState.INSUFFICIENT_EVIDENCE:
        prefix = f"Aprovado com evidências parciais para o perfil {profile_label}"
    else:
        prefix = f"Recomendado para o perfil {profile_label}"

    lead_reason = reasons[0] if reasons else "Candidato validado pelos critérios obrigatórios."
    summary = f"{prefix} (score {breakdown.final_score:.2f}): {lead_reason}"

    dedup_citations = tuple(dict.fromkeys(c for c in citations if c))
    dedup_missing = tuple(dict.fromkeys(missing_data))
    return Explanation(
        summary=summary,
        rank=rank,
        total_score=breakdown.final_score,
        reasons=tuple(reasons),
        tradeoffs=tuple(tradeoffs),
        warnings=tuple(warnings),
        missing_data=dedup_missing,
        penalties=tuple(penalties),
        relaxation_notes=tuple(relaxation_notes),
        evidence_citations=dedup_citations,
        offer_comparison_note=offer_note,
        profile_id=profile_id,
        approval_state=approval_state,
    )


def build_group_explanation(
    *,
    canonical_title: str,
    representative_offer: RankedOffer,
    offer_count: int,
    rank: int = 0,
) -> Explanation:
    """Build Level-1 group explanation grounded in the representative offer."""
    base = representative_offer.explanation
    reasons = list(base.reasons)
    if offer_count > 1:
        reasons.append(
            f"Grupo com {offer_count} ofertas distintas preservadas; melhor oferta selecionada: {representative_offer.item_id}."
        )
    return Explanation(
        summary=f"{canonical_title} — {base.summary}",
        rank=rank or base.rank,
        total_score=base.total_score,
        reasons=tuple(reasons),
        tradeoffs=base.tradeoffs,
        warnings=base.warnings,
        missing_data=base.missing_data,
        penalties=base.penalties,
        relaxation_notes=base.relaxation_notes,
        evidence_citations=base.evidence_citations,
        offer_comparison_note=base.offer_comparison_note,
        profile_id=base.profile_id,
        approval_state=base.approval_state,
    )
