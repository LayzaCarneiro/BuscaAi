from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, asdict
from typing import Any

from .schema import UserIntent


_RAW_ALIASES = {
    "laptop": "notebook",
    "pc": "computador",
    "smartphone": "smartphone",
    "celular": "smartphone",
    "telefone": "smartphone",
    "smart tv": "tv",
    "televisao": "tv",
    "televisão": "tv",
    "headset": "fone de ouvido",
    "fone": "fone de ouvido",
    "fones": "fone de ouvido",
    "programar": "programação",
    "programação": "programação",
    "desenvolvimento": "programação",
    "desenvolver": "programação",
    "codar": "programação",
    "jogar": "jogos",
    "games": "jogos",
    "game": "jogos",
    "jogos": "jogos",
    "estudar": "estudos",
    "estudo": "estudos",
    "fotografar": "fotografia",
    "fotos": "fotografia",
    "filmes": "entretenimento",
    "series": "entretenimento",
    "séries": "entretenimento",
}


ALIASES: dict[str, str] = {}


def normalize_text(value: str) -> str:
    value = value.strip().casefold()
    value = "".join(
        c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c)
    )
    value = re.sub(r"\s+", " ", value)
    return value


def normalize_label(value: str) -> str:
    value = normalize_text(value)
    if not ALIASES:
        ALIASES.update({normalize_text(k): normalize_text(v) for k, v in _RAW_ALIASES.items()})
    return ALIASES.get(value, value)


def normalize_set(values: list[str]) -> set[str]:
    return {normalize_label(v) for v in values if v and v.strip()}


def safe_equal(a: Any, b: Any) -> bool:
    return normalize_text(str(a)) == normalize_text(str(b))


def set_f1(pred: list[str], expected: list[str]) -> float:
    p = normalize_set(pred)
    e = normalize_set(expected)
    if not p and not e:
        return 1.0
    if not p or not e:
        return 0.0
    tp = len(p & e)
    precision = tp / len(p)
    recall = tp / len(e)
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def token_jaccard(a: str, b: str) -> float:
    stop = {
        "de", "da", "do", "das", "dos", "para", "por", "e", "um", "uma",
        "com", "sem", "ate", "até", "quero", "preciso", "algo", "em", "no", "na",
    }
    ta = {t for t in re.findall(r"[\wÀ-ÿ]+", normalize_text(a)) if t not in stop}
    tb = {t for t in re.findall(r"[\wÀ-ÿ]+", normalize_text(b)) if t not in stop}
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def price_match(pred: UserIntent, exp: dict[str, Any]) -> bool:
    p = pred.price
    keys = ["min_price", "max_price", "min_inclusive", "max_inclusive", "is_hard_constraint"]
    return all(getattr(p, key) == exp.get(key) for key in keys)


@dataclass
class CaseResult:
    case_id: int
    category_correct: bool
    price_correct: bool
    brands_f1: float
    excluded_brands_f1: float
    use_cases_f1: float
    profile_f1: float
    required_features_f1: float
    preferred_features_f1: float
    excluded_features_f1: float
    clarification_correct: bool
    clarification_question_ok: bool
    query_similarity: float
    core_pass: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_case(pred: UserIntent, expected: dict[str, Any], case_id: int) -> CaseResult:
    category_correct = safe_equal(pred.category or "", expected.get("category") or "")
    price_correct_flag = price_match(pred, expected.get("price", {}))
    brands_f1 = set_f1(pred.brands, expected.get("brands", []))
    excluded_brands_f1 = set_f1(pred.excluded_brands, expected.get("excluded_brands", []))
    use_cases_f1 = set_f1(pred.use_cases, expected.get("use_cases", []))
    profile_f1 = set_f1(pred.profile_context, expected.get("profile_context", []))
    required_features_f1 = set_f1(pred.required_features, expected.get("required_features", []))
    preferred_features_f1 = set_f1(pred.preferred_features, expected.get("preferred_features", []))
    excluded_features_f1 = set_f1(pred.excluded_features, expected.get("excluded_features", []))
    clarification_expected = expected.get("clarification_needed", False)
    clarification_correct = pred.clarification_needed == clarification_expected
    question = (pred.clarification_question or "").strip()
    if clarification_expected:
        keywords = [normalize_text(k) for k in expected.get("clarification_question_keywords", [])]
        qn = normalize_text(question)
        clarification_question_ok = len(qn) >= 12 and (not keywords or any(k in qn for k in keywords))
    else:
        clarification_question_ok = not question
    query_similarity = token_jaccard(pred.query, expected.get("query", ""))

    # Core correctness is intentionally stricter than average score:
    # hard constraints and clarification behavior cannot be traded away by good text similarity.
    core_pass = (
        category_correct
        and price_correct_flag
        and brands_f1 >= 0.99
        and excluded_brands_f1 >= 0.99
        and use_cases_f1 >= 0.75
        and required_features_f1 >= 0.75
        and excluded_features_f1 >= 0.75
        and clarification_correct
        and clarification_question_ok
    )

    return CaseResult(
        case_id=case_id,
        category_correct=category_correct,
        price_correct=price_correct_flag,
        brands_f1=brands_f1,
        excluded_brands_f1=excluded_brands_f1,
        use_cases_f1=use_cases_f1,
        profile_f1=profile_f1,
        required_features_f1=required_features_f1,
        preferred_features_f1=preferred_features_f1,
        excluded_features_f1=excluded_features_f1,
        clarification_correct=clarification_correct,
        clarification_question_ok=clarification_question_ok,
        query_similarity=query_similarity,
        core_pass=core_pass,
    )


def aggregate(results: list[CaseResult]) -> dict[str, float]:
    if not results:
        return {}

    def mean(field: str) -> float:
        return sum(float(getattr(r, field)) for r in results) / len(results)

    return {
        "cases": float(len(results)),
        "core_pass_rate": mean("core_pass"),
        "category_accuracy": mean("category_correct"),
        "price_accuracy": mean("price_correct"),
        "brands_f1": mean("brands_f1"),
        "excluded_brands_f1": mean("excluded_brands_f1"),
        "use_cases_f1": mean("use_cases_f1"),
        "profile_f1": mean("profile_f1"),
        "required_features_f1": mean("required_features_f1"),
        "preferred_features_f1": mean("preferred_features_f1"),
        "excluded_features_f1": mean("excluded_features_f1"),
        "clarification_accuracy": mean("clarification_correct"),
        "clarification_question_ok_rate": mean("clarification_question_ok"),
        "mean_query_similarity": mean("query_similarity"),
    }
