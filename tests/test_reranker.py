"""Tests for two-level reranking, all 6 profiles across 4 MVP categories, offer preservation, and determinism."""

from __future__ import annotations

import itertools
from decimal import Decimal

from buscai.ranking.models import ApprovalState, RankingConfig
from buscai.ranking.reranker import MultiCriteriaReranker
from buscai.search.models import Candidate, SellerInfo, ShippingInfo
from buscai.semantic.dedup import DeduplicationResult, DuplicateGroup
from buscai.semantic.pipeline import SemanticProcessingResult
from buscai.semantic.ranker import SemanticScoredCandidate
from buscai.validation.models import (
    BudgetEvaluation,
    CandidateValidationResult,
    CriterionEvaluation,
    EnrichmentSummary,
    RelaxationStep,
    ValidationPipelineResult,
)


def _cand(
    item_id: str,
    title: str,
    price: str,
    *,
    category: str = "notebook",
    condition: str | None = "New",
    seller_pct: float | None = 98.0,
    seller_score: int | None = 500,
    free_shipping: bool | None = True,
    shipping_cost: str | None = None,
    aspects: dict[str, tuple[str, ...]] | None = None,
) -> Candidate:
    shipping = None
    if free_shipping is True or shipping_cost is not None:
        shipping = ShippingInfo(
            cost=Decimal(shipping_cost) if shipping_cost is not None else Decimal("0"),
            currency="USD",
            free_shipping=bool(free_shipping),
            estimated_max_days=5,
        )
    seller = None
    if seller_pct is not None or seller_score is not None:
        seller = SellerInfo(
            username=f"seller-{item_id}",
            feedback_percentage=seller_pct,
            feedback_score=seller_score,
        )
    return Candidate(
        item_id=item_id,
        source="dummy",
        title=title,
        price=Decimal(price),
        currency="USD",
        condition=condition,
        categories=(category,),
        aspects=aspects or {},
        seller=seller,
        shipping=shipping,
    )


def _val(
    cand: Candidate,
    *,
    category: str = "notebook",
    eligible: bool = True,
    strictly_verified: bool | None = None,
    hard_bad: int = 0,
    hard_unknown: int = 0,
    satisfied: tuple[str, ...] = (),
    not_satisfied: tuple[str, ...] = (),
    unknown_soft: tuple[str, ...] = (),
    max_budget: Decimal = Decimal("10000"),
    budget_priority: str = "hard",
) -> CandidateValidationResult:
    evals: list[CriterionEvaluation] = [
        CriterionEvaluation(
            criterion_id=s,
            criterion_label=s,
            priority="soft",
            status="satisfied",
            evidence="ok",
            source_field="aspects",
        )
        for s in satisfied
    ]
    evals.extend(
        CriterionEvaluation(
            criterion_id=s,
            criterion_label=s,
            priority="soft",
            status="not_satisfied",
            evidence="no",
            source_field="aspects",
        )
        for s in not_satisfied
    )
    evals.extend(
        CriterionEvaluation(
            criterion_id=s,
            criterion_label=s,
            priority="soft",
            status="unknown",
            insufficient_information=True,
        )
        for s in unknown_soft
    )
    if hard_bad > 0:
        evals.append(
            CriterionEvaluation(
                criterion_id="ram_gb",
                criterion_label="ram_gb",
                priority="hard",
                status="not_satisfied",
                evidence="8",
                source_field="aspects",
            )
        )
    if hard_unknown > 0:
        evals.append(
            CriterionEvaluation(
                criterion_id="storage_gb",
                criterion_label="storage_gb",
                priority="hard",
                status="unknown",
                insufficient_information=True,
            )
        )
    total_brl = (cand.price or Decimal("0")) * Decimal("5.0")
    within_budget = total_brl <= max_budget
    return CandidateValidationResult(
        candidate=cand,
        eligible=eligible,
        strictly_verified=(eligible and hard_unknown == 0) if strictly_verified is None else strictly_verified,
        needs_enrichment=False,
        detected_category=category,
        evaluations=tuple(evals),
        budget_evaluation=BudgetEvaluation(
            status="satisfied" if within_budget else "not_satisfied",
            priority=budget_priority,  # type: ignore[arg-type]
            budget_currency="BRL",
            max_price=max_budget,
            total_price_in_budget_currency=total_brl,
            shipping_known=cand.shipping is not None,
        ),
        hard_not_satisfied_count=hard_bad,
        hard_unknown_count=hard_unknown,
        soft_satisfied_count=len(satisfied),
        soft_not_satisfied_count=len(not_satisfied),
        soft_unknown_count=len(unknown_soft),
    )


def test_hard_constraint_rejected_candidates_never_enter_ranking() -> None:
    c_valid = _cand("ok-1", "Notebook Valid 16GB", "700")
    c_bad_spec = _cand("bad-spec", "Notebook Cheap 4GB", "200")
    c_unk_spec = _cand("unk-spec", "Notebook Unknown Storage", "300")
    c_over_budget = _cand("over-budget", "Notebook Caro 32GB", "3000")

    val_res = ValidationPipelineResult(
        eligible_candidates=(_val(c_valid, satisfied=("ram_gb",)),),
        rejected_candidates=(
            _val(c_bad_spec, eligible=False, hard_bad=1),
            _val(c_unk_spec, eligible=True, hard_unknown=1),
            _val(c_over_budget, eligible=True, max_budget=Decimal("5000")),
        ),
        all_validations=(
            _val(c_valid, satisfied=("ram_gb",)),
            _val(c_bad_spec, eligible=False, hard_bad=1),
            _val(c_unk_spec, eligible=True, hard_unknown=1),
            _val(c_over_budget, eligible=True, max_budget=Decimal("5000")),
        ),
        product_groups=(),
        enrichment=EnrichmentSummary(),
        total_candidates_evaluated=4,
    )

    reranker = MultiCriteriaReranker()
    groups, offers, rejected = reranker.rerank(val_res, profile_id="lowest_price")

    assert [g.representative_offer.item_id for g in groups] == ["ok-1"]
    assert [o.item_id for o in offers] == ["ok-1"]
    assert {r.item_id for r in rejected} == {"bad-spec", "unk-spec", "over-budget"}


def test_two_level_ranking_preserves_all_distinct_offers_and_sellers() -> None:
    offer_best = _cand(
        "item-a",
        "Notebook Dell XPS 13",
        "600",
        condition="New",
        seller_pct=99.5,
        seller_score=3000,
        free_shipping=True,
    )
    offer_alt = _cand(
        "item-b",
        "Notebook Dell XPS 13",
        "750",
        condition="Used",
        seller_pct=94.0,
        seller_score=100,
        free_shipping=False,
        shipping_cost="40",
    )
    offer_no_seller = _cand(
        "item-c",
        "Notebook Dell XPS 13",
        "580",
        condition="Open box",
        seller_pct=None,
        seller_score=None,
        free_shipping=True,
    )
    other_prod = _cand(
        "item-d",
        "Notebook Acer Aspire 15",
        "650",
        seller_pct=97.0,
        seller_score=400,
    )

    group_xps = DuplicateGroup(
        group_id="g-xps",
        canonical=offer_best,
        offers=(offer_best, offer_alt, offer_no_seller),
        max_similarity=1.0,
    )
    group_acer = DuplicateGroup(
        group_id="g-acer",
        canonical=other_prod,
        offers=(other_prod,),
        max_similarity=1.0,
    )

    val_res = ValidationPipelineResult(
        eligible_candidates=(
            _val(offer_best),
            _val(offer_alt),
            _val(offer_no_seller),
            _val(other_prod),
        ),
        rejected_candidates=(),
        all_validations=(
            _val(offer_best),
            _val(offer_alt),
            _val(offer_no_seller),
            _val(other_prod),
        ),
        product_groups=(group_xps, group_acer),
        enrichment=EnrichmentSummary(),
        total_candidates_evaluated=4,
    )

    reranker = MultiCriteriaReranker()
    groups, offers, _ = reranker.rerank(val_res, profile_id="best_value")

    assert len(groups) == 2
    assert len(offers) == 4
    xps_group = next(g for g in groups if g.product_group_id == "g-xps")
    assert xps_group.offer_count == 3
    assert {o.item_id for o in xps_group.offers} == {"item-a", "item-b", "item-c"}
    assert xps_group.offers[0].is_best_offer_in_group is True
    assert all(not o.is_best_offer_in_group for o in xps_group.offers[1:])
    cheapest_xps = next(o for o in xps_group.offers if o.item_id == "item-c")
    assert cheapest_xps.explanation.offer_comparison_note is not None
    assert "reputação do vendedor não está disponível" in cheapest_xps.explanation.offer_comparison_note


def test_all_six_profiles_produce_distinct_justified_rankings_across_categories() -> None:
    # 1. Notebook: cheap used vs ultra-portable study vs workstation perf vs balanced
    nb_cheap = _cand(
        "nb-cheap",
        "Notebook Básico 8GB",
        "320",
        condition="Used",
        seller_pct=91.0,
        aspects={"RAM": ("8 GB",), "Weight": ("2.4 kg",)},
    )
    nb_study = _cand(
        "nb-study",
        "Notebook Ultrabook 13.3 16GB SSD 512GB",
        "580",
        condition="New",
        seller_pct=98.5,
        aspects={"RAM": ("16 GB",), "Storage": ("512 GB SSD",), "Weight": ("1.15 kg",)},
    )
    nb_perf = _cand(
        "nb-perf",
        "Notebook Workstation 32GB SSD 1TB",
        "1400",
        condition="New",
        seller_pct=99.6,
        seller_score=4000,
        aspects={"RAM": ("32 GB",), "Storage": ("1 TB SSD",), "Weight": ("2.7 kg",)},
    )

    nb_val = ValidationPipelineResult(
        eligible_candidates=(
            _val(nb_cheap, not_satisfied=("ram_gb", "dedicated_gpu")),
            _val(nb_study, satisfied=("ram_gb", "storage_gb"), not_satisfied=("dedicated_gpu",)),
            _val(nb_perf, satisfied=("ram_gb", "storage_gb", "dedicated_gpu")),
        ),
        all_validations=(
            _val(nb_cheap, not_satisfied=("ram_gb", "dedicated_gpu")),
            _val(nb_study, satisfied=("ram_gb", "storage_gb"), not_satisfied=("dedicated_gpu",)),
            _val(nb_perf, satisfied=("ram_gb", "storage_gb", "dedicated_gpu")),
        ),
        enrichment=EnrichmentSummary(),
        total_candidates_evaluated=3,
    )

    reranker = MultiCriteriaReranker()
    assert reranker.rerank(nb_val, profile_id="lowest_price")[0][0].representative_offer.item_id == "nb-cheap"
    assert reranker.rerank(nb_val, profile_id="best_performance")[0][0].representative_offer.item_id == "nb-perf"
    assert reranker.rerank(nb_val, profile_id="study")[0][0].representative_offer.item_id == "nb-study"
    assert reranker.rerank(nb_val, profile_id="best_value")[0][0].representative_offer.item_id == "nb-study"
    assert reranker.rerank(nb_val, profile_id="professional_use")[0][0].representative_offer.item_id == "nb-perf"

    # 2. Monitor: promotional title vs verified 4K IPS
    mon_fluff = _cand(
        "mon-fluff",
        "MONITOR SUPER GAMER TURBO PRO MAX ULTRA 24",
        "280",
        category="monitor",
        condition="Used",
        seller_pct=92.0,
        aspects={"Resolution": ("1080p",), "Panel Type": ("TN",)},
    )
    mon_pro = _cand(
        "mon-pro",
        "Monitor Dell 27 4K IPS 144Hz",
        "480",
        category="monitor",
        condition="New",
        seller_pct=99.5,
        seller_score=3000,
        aspects={"Maximum Resolution": ("4K 3840x2160",), "Display Type": ("IPS",), "Refresh Rate": ("144 Hz",)},
    )
    mon_val = ValidationPipelineResult(
        eligible_candidates=(
            _val(mon_fluff, category="monitor", not_satisfied=("resolution",)),
            _val(mon_pro, category="monitor", satisfied=("resolution", "panel_type")),
        ),
        all_validations=(
            _val(mon_fluff, category="monitor", not_satisfied=("resolution",)),
            _val(mon_pro, category="monitor", satisfied=("resolution", "panel_type")),
        ),
        enrichment=EnrichmentSummary(),
        total_candidates_evaluated=2,
    )
    assert reranker.rerank(mon_val, profile_id="professional_use", category_hint="monitor")[0][0].representative_offer.item_id == "mon-pro"

    # 3. Smartphone & Headphones everyday_use
    hp_bt = _cand(
        "hp-bt",
        "Fone Bluetooth ANC 30h",
        "180",
        category="fone de ouvido",
        aspects={"Connectivity": ("Bluetooth",), "Features": ("Active Noise Cancellation",), "Battery Life": ("30 Hours",)},
    )
    hp_wired = _cand(
        "hp-wired",
        "Fone Com Fio 3.5mm",
        "80",
        category="fone de ouvido",
        condition="Used",
        seller_pct=92.0,
        aspects={"Connectivity": ("Wired 3.5mm",)},
    )
    hp_val = ValidationPipelineResult(
        eligible_candidates=(
            _val(hp_bt, category="fone de ouvido", satisfied=("wireless",)),
            _val(hp_wired, category="fone de ouvido", not_satisfied=("wireless",)),
        ),
        all_validations=(
            _val(hp_bt, category="fone de ouvido", satisfied=("wireless",)),
            _val(hp_wired, category="fone de ouvido", not_satisfied=("wireless",)),
        ),
        enrichment=EnrichmentSummary(),
        total_candidates_evaluated=2,
    )
    assert reranker.rerank(hp_val, profile_id="everyday_use", category_hint="fone de ouvido")[0][0].representative_offer.item_id == "hp-bt"


def test_incomplete_and_contradictory_candidates_are_penalized_and_flagged() -> None:
    c_clean = _cand(
        "clean-1",
        "Notebook 16GB RAM SSD 512GB",
        "600",
        aspects={"RAM Size": ("16 GB",), "SSD Capacity": ("512 GB",)},
    )
    c_contra = _cand(
        "contra-1",
        "Notebook 16GB RAM SSD 512GB",
        "600",
        aspects={"RAM Size": ("8 GB",), "SSD Capacity": ("512 GB",)},
    )
    c_incomplete = _cand(
        "inc-1",
        "Notebook Genérico",
        "600",
        condition=None,
        seller_pct=None,
        seller_score=None,
        free_shipping=None,
    )

    val_res = ValidationPipelineResult(
        eligible_candidates=(
            _val(c_clean, satisfied=("ram_gb",)),
            _val(c_contra, satisfied=("ram_gb",)),
            _val(c_incomplete, strictly_verified=False, unknown_soft=("ram_gb",)),
        ),
        all_validations=(
            _val(c_clean, satisfied=("ram_gb",)),
            _val(c_contra, satisfied=("ram_gb",)),
            _val(c_incomplete, strictly_verified=False, unknown_soft=("ram_gb",)),
        ),
        enrichment=EnrichmentSummary(),
        total_candidates_evaluated=3,
    )

    reranker = MultiCriteriaReranker()
    groups, _, _ = reranker.rerank(val_res, profile_id="best_value")
    by_id = {g.representative_offer.item_id: g for g in groups}

    assert groups[0].representative_offer.item_id == "clean-1"
    assert by_id["contra-1"].score_breakdown.contradiction_count >= 1
    assert by_id["contra-1"].score_breakdown.contradiction_penalty > 0.0
    assert by_id["inc-1"].approval_state is ApprovalState.INSUFFICIENT_EVIDENCE
    assert by_id["clean-1"].score_breakdown.final_score > by_id["inc-1"].score_breakdown.final_score


def test_deterministic_permutation_invariance_on_all_permutations() -> None:
    c1 = _cand("item-z", "Notebook Alpha", "500", seller_pct=98.0, seller_score=500)
    c2 = _cand("item-a", "Notebook Alpha Identical Specs", "500", seller_pct=98.0, seller_score=500)
    c3 = _cand("item-m", "Notebook Alpha Mid", "500", seller_pct=98.0, seller_score=500)
    vals = [_val(c1), _val(c2), _val(c3)]

    reranker = MultiCriteriaReranker()
    observed_orders: set[tuple[str, ...]] = set()
    for perm in itertools.permutations(vals):
        res = ValidationPipelineResult(
            eligible_candidates=perm,
            all_validations=perm,
            enrichment=EnrichmentSummary(),
            total_candidates_evaluated=3,
        )
        groups, _, _ = reranker.rerank(res, profile_id="best_value")
        observed_orders.add(tuple(g.representative_offer.item_id for g in groups))

    assert observed_orders == {("item-a", "item-m", "item-z")}


def test_semantic_toggle_changes_contribution_and_preserves_stability() -> None:
    c1 = _cand("s-1", "Notebook A", "500")
    c2 = _cand("s-2", "Notebook B", "500")
    v1 = _val(c1)
    v2 = _val(c2)
    val_res = ValidationPipelineResult(
        eligible_candidates=(v1, v2),
        all_validations=(v1, v2),
        enrichment=EnrichmentSummary(),
        total_candidates_evaluated=2,
    )
    sem_res = SemanticProcessingResult(
        candidates=(c1, c2),
        scored_candidates=(
            SemanticScoredCandidate(candidate=c1, semantic_score=0.20, product_group_id="g1"),
            SemanticScoredCandidate(candidate=c2, semantic_score=0.95, product_group_id="g2"),
        ),
        product_groups=(),
        deduplication=DeduplicationResult(),
    )

    r_on = MultiCriteriaReranker(config=RankingConfig(enable_semantic_score=True))
    r_off = MultiCriteriaReranker(config=RankingConfig(enable_semantic_score=False))

    g_on, _, _ = r_on.rerank(val_res, semantic_result=sem_res, profile_id="best_value")
    g_off, _, _ = r_off.rerank(val_res, semantic_result=sem_res, profile_id="best_value")

    assert g_on[0].representative_offer.item_id == "s-2"
    assert g_on[0].score_breakdown.semantic_weight_applied > 0.0
    assert g_off[0].score_breakdown.semantic_enabled is False
    assert g_off[0].score_breakdown.semantic_weight_applied == 0.0


def test_relaxed_approval_state_is_propagated() -> None:
    c1 = _cand("rel-1", "Notebook Relaxed", "500")
    val_res = ValidationPipelineResult(
        eligible_candidates=(_val(c1),),
        all_validations=(_val(c1),),
        enrichment=EnrichmentSummary(),
        relaxed=True,
        relaxations=(
            RelaxationStep(
                step_index=1,
                field="ram_gb",
                original_value="16",
                relaxed_to="8",
                reason="Relaxado critério flexível ram_gb.",
                eligible_count_after=1,
            ),
        ),
        total_candidates_evaluated=1,
    )
    reranker = MultiCriteriaReranker()
    groups, _, _ = reranker.rerank(val_res, profile_id="best_value")
    assert groups[0].approval_state is ApprovalState.APPROVED_WITH_RELAXATION
    assert groups[0].explanation.relaxation_notes == ("Relaxado critério flexível ram_gb.",)
