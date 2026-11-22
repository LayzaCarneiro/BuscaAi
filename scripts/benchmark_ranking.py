"""Offline synthetic benchmark for Stage 6 multi-criteria reranking (T6.10)."""

from __future__ import annotations

import json
import math
from decimal import Decimal
from pathlib import Path
from typing import Any

from buscai.ranking.models import RankingConfig
from buscai.ranking.pipeline import RankingPipeline
from buscai.search.models import Candidate, SellerInfo, ShippingInfo
from buscai.semantic.dedup import DeduplicationResult, DuplicateGroup
from buscai.semantic.pipeline import SemanticProcessingResult
from buscai.semantic.ranker import SemanticScoredCandidate
from buscai.validation.models import (
    BudgetEvaluation,
    CandidateValidationResult,
    CriterionEvaluation,
    EnrichmentSummary,
    ValidationPipelineResult,
)

DEFAULT_DATASET_PATH = Path(__file__).resolve().parents[1] / "data" / "eval_ranking_synthetic.jsonl"


def _dcg(grades: list[int], k: int) -> float:
    total = 0.0
    for i, grade in enumerate(grades[:k], start=1):
        total += (2**grade - 1) / math.log2(i + 1)
    return total


def _ndcg_at_k(ranked_grades: list[int], ideal_grades: list[int], k: int = 3) -> float:
    ideal = _dcg(sorted(ideal_grades, reverse=True), k)
    if ideal <= 0.0:
        return 1.0
    return _dcg(ranked_grades, k) / ideal


def _build_inputs_from_row(
    row: dict[str, Any],
) -> tuple[ValidationPipelineResult, SemanticProcessingResult, dict[str, int], int]:
    max_budget = Decimal(str(row["max_budget_brl"]))
    category = str(row["category"])
    profile_id = str(row["profile_id"])

    validations: list[CandidateValidationResult] = []
    scored_candidates: list[SemanticScoredCandidate] = []
    groups_map: dict[str, list[Candidate]] = {}
    grade_by_group: dict[str, int] = {}
    valid_offer_count = 0

    for item in row["candidates"]:
        cand = Candidate(
            item_id=str(item["item_id"]),
            source="synthetic",
            title=str(item["title"]),
            price=Decimal(str(item["price"])),
            currency=str(item.get("currency", "USD")),
            condition=item.get("condition"),
            categories=(category,),
            aspects={
                str(k): (str(v),) for k, v in item.get("aspects", {}).items()
            },
            seller=SellerInfo(
                username=f"seller-{item['item_id']}",
                feedback_percentage=item.get("seller_pct"),
                feedback_score=item.get("seller_score"),
            ),
            shipping=ShippingInfo(
                cost=Decimal(str(item.get("shipping_cost", 0.0))),
                currency=str(item.get("currency", "USD")),
                free_shipping=bool(item.get("free_shipping", False)),
                estimated_max_days=5,
            ),
        )
        evals: list[CriterionEvaluation] = []
        for spec in item.get("satisfied_specs", []):
            evals.append(
                CriterionEvaluation(
                    criterion_id=str(spec),
                    criterion_label=str(spec),
                    priority="soft",
                    status="satisfied",
                    evidence="present",
                    source_field="aspects",
                )
            )
        for spec in item.get("not_satisfied_specs", []):
            evals.append(
                CriterionEvaluation(
                    criterion_id=str(spec),
                    criterion_label=str(spec),
                    priority="soft",
                    status="not_satisfied",
                    evidence="absent",
                    source_field="aspects",
                )
            )
        hard_valid = bool(item.get("hard_valid", True))
        if not hard_valid:
            evals.append(
                CriterionEvaluation(
                    criterion_id="hard_requirement",
                    criterion_label="hard_requirement",
                    priority="hard",
                    status="not_satisfied",
                    evidence="missing",
                    source_field="aspects",
                )
            )

        total_brl = (cand.price or Decimal("0")) * Decimal("5.0")
        within_budget = total_brl <= max_budget
        val = CandidateValidationResult(
            candidate=cand,
            eligible=hard_valid,
            strictly_verified=hard_valid,
            needs_enrichment=False,
            detected_category=category,
            profile_id=profile_id,
            evaluations=tuple(evals),
            budget_evaluation=BudgetEvaluation(
                status="satisfied" if within_budget else "not_satisfied",
                priority="hard",
                budget_currency="BRL",
                max_price=max_budget,
                total_price_in_budget_currency=total_brl,
                shipping_known=True,
            ),
            hard_satisfied_count=1 if hard_valid else 0,
            hard_not_satisfied_count=0 if hard_valid else 1,
            soft_satisfied_count=len(item.get("satisfied_specs", [])),
            soft_not_satisfied_count=len(item.get("not_satisfied_specs", [])),
        )
        validations.append(val)

        gid = str(item["group_id"])
        scored_candidates.append(
            SemanticScoredCandidate(
                candidate=cand,
                semantic_score=float(item.get("semantic_score", 0.5)),
                product_group_id=gid,
            )
        )
        if hard_valid:
            valid_offer_count += 1
        groups_map.setdefault(gid, []).append(cand)
        grade_by_group[gid] = max(
            grade_by_group.get(gid, 0), int(item.get("relevance_grade", 0))
        )

    dup_groups = tuple(
        DuplicateGroup(
            group_id=gid,
            canonical=members[0],
            offers=tuple(members),
            max_similarity=1.0,
        )
        for gid, members in groups_map.items()
    )

    val_res = ValidationPipelineResult(
        eligible_candidates=tuple(v for v in validations if v.eligible),
        rejected_candidates=tuple(v for v in validations if not v.eligible),
        all_validations=tuple(validations),
        product_groups=dup_groups,
        active_profile_id=profile_id,
        enrichment=EnrichmentSummary(),
        total_candidates_evaluated=len(validations),
    )
    sem_res = SemanticProcessingResult(
        candidates=tuple(sc.candidate for sc in scored_candidates),
        scored_candidates=tuple(scored_candidates),
        product_groups=dup_groups,
        deduplication=DeduplicationResult(
            groups=dup_groups,
            deduplicated_candidates=tuple(g.canonical for g in dup_groups),
            input_count=len(scored_candidates),
            output_count=len(dup_groups),
            removed_duplicates_count=len(scored_candidates) - len(dup_groups),
        ),
        input_count=len(scored_candidates),
        output_count=len(scored_candidates),
        unique_products_count=len(dup_groups),
        removed_duplicates_count=len(scored_candidates) - len(dup_groups),
    )
    return val_res, sem_res, grade_by_group, valid_offer_count


def run_benchmark(
    dataset_path: Path = DEFAULT_DATASET_PATH,
    *,
    enable_semantic_score: bool = True,
) -> dict[str, Any]:
    """Run offline Stage 6 ranking benchmark and return aggregate-only metrics."""
    rows = [
        json.loads(line)
        for line in dataset_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    pipeline = RankingPipeline(
        config=RankingConfig(enable_semantic_score=enable_semantic_score)
    )

    ndcg_scores: list[float] = []
    mrr_scores: list[float] = []
    p3_scores: list[float] = []
    compliant_scenarios = 0
    preserved_offers_total = 0
    expected_offers_total = 0

    for row in rows:
        val_res, sem_res, grade_by_group, valid_offers = _build_inputs_from_row(row)
        out = pipeline.run(
            val_res,
            semantic_result=sem_res,
            profile_id=row["profile_id"],
            category_hint=row["category"],
            request_id=f"bench-{row['scenario_id']}",
        )

        ranked_grades = [
            grade_by_group.get(g.product_group_id, 0) for g in out.ranked_groups
        ]
        ideal_grades = [
            grade for gid, grade in grade_by_group.items() if grade > 0
        ]
        ndcg_scores.append(_ndcg_at_k(ranked_grades, ideal_grades, k=3))

        rr = 0.0
        for idx, g in enumerate(ranked_grades, start=1):
            if g >= 3:
                rr = 1.0 / idx
                break
        mrr_scores.append(rr)

        top_k = ranked_grades[:3]
        p3_scores.append(sum(1 for g in top_k if g >= 2) / max(1, len(top_k)))

        # Hard constraint compliance: zero rejected items leaked into ranked output.
        ranked_ids = {o.item_id for o in out.ranked_offers}
        rejected_ids = {r.item_id for r in out.rejected_candidates}
        if not (ranked_ids & rejected_ids) and out.hard_constraints_preserved:
            compliant_scenarios += 1

        preserved_offers_total += out.eligible_offers_count
        expected_offers_total += valid_offers

    n = max(1, len(rows))
    return {
        "dataset_origin": "synthetic_offline_stage6",
        "scenarios_evaluated": len(rows),
        "semantic_enabled": enable_semantic_score,
        "ndcg_at_3": round(sum(ndcg_scores) / n, 4),
        "mrr": round(sum(mrr_scores) / n, 4),
        "precision_at_3": round(sum(p3_scores) / n, 4),
        "hard_constraint_compliance_rate": round(compliant_scenarios / n, 4),
        "offer_preservation_rate": round(
            preserved_offers_total / max(1, expected_offers_total), 4
        ),
        "limitations": [
            "Avaliado sobre dataset sintético offline (data/eval_ranking_synthetic.jsonl), não sobre tráfego real do eBay.",
            "Validação empírica com dados reais e pesos de embeddings (A2) permanece pendente.",
        ],
    }


def main() -> None:
    on_metrics = run_benchmark(enable_semantic_score=True)
    off_metrics = run_benchmark(enable_semantic_score=False)
    report = {
        "semantic_on": on_metrics,
        "semantic_off": off_metrics,
        "semantic_on_vs_off_ndcg_delta": round(
            on_metrics["ndcg_at_3"] - off_metrics["ndcg_at_3"], 4
        ),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
