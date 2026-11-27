from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from userintent.evaluator import (
    aggregate,
    compute_failed_fields,
    evaluate_case,
    feature_set_f1,
    normalize_feature_tokens,
    normalize_label,
    price_match,
    set_f1,
    token_jaccard,
)
from userintent.prompt import SYSTEM_PROMPT
from userintent.schema import PriceConstraint, UserIntent

ROOT = Path(__file__).resolve().parents[1]


def _load_eval_cases() -> dict[int, dict[str, Any]]:
    path = ROOT / "data" / "intent_eval_50.jsonl"
    return {
        int(row["case_id"]): row
        for row in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    }


def test_good_case_passes():
    expected = {
        "category": "notebook",
        "price": {
            "min_price": None,
            "max_price": 4000,
            "min_inclusive": True,
            "max_inclusive": True,
            "is_hard_constraint": True,
        },
        "brands": ["Lenovo"],
        "excluded_brands": [],
        "use_cases": ["programação"],
        "profile_context": [],
        "required_features": [],
        "preferred_features": [],
        "excluded_features": [],
        "query": "notebook para programação",
        "clarification_needed": False,
    }
    pred = UserIntent(
        category="notebook",
        price={"max_price": 4000, "is_hard_constraint": True},
        brands=["Lenovo"],
        use_cases=["programar"],
        query="notebook programação",
    )
    result = evaluate_case(pred, expected, 1)
    assert result.core_pass is True


def test_evaluator_respects_max_inclusive_false_and_exclusions():
    expected = {
        "category": "monitor",
        "price": {
            "min_price": None,
            "max_price": 2500,
            "min_inclusive": True,
            "max_inclusive": False,
            "is_hard_constraint": True,
        },
        "brands": [],
        "excluded_brands": ["MarcaX"],
        "use_cases": ["jogos"],
        "profile_context": [],
        "required_features": ["165 Hz"],
        "preferred_features": [],
        "excluded_features": ["RGB"],
        "query": "monitor para jogos 165 Hz sem RGB",
        "clarification_needed": False,
    }
    pred_ok = UserIntent(
        category="monitor",
        price={"max_price": 2500, "max_inclusive": False, "is_hard_constraint": True},
        excluded_brands=["MarcaX"],
        use_cases=["games"],
        required_features=["165 Hz"],
        excluded_features=["RGB"],
        query="monitor 165 Hz",
    )
    pred_wrong_inclusive = pred_ok.model_copy(
        update={"price": pred_ok.price.model_copy(update={"max_inclusive": True})}
    )

    assert evaluate_case(pred_ok, expected, 43).core_pass is True
    bad_res = evaluate_case(pred_wrong_inclusive, expected, 43)
    assert bad_res.price_correct is False
    assert bad_res.core_pass is False


def test_evaluator_checks_clarification_question_keywords():
    expected = {
        "category": None,
        "price": {
            "min_price": None,
            "max_price": None,
            "min_inclusive": True,
            "max_inclusive": True,
            "is_hard_constraint": False,
        },
        "brands": [],
        "excluded_brands": [],
        "use_cases": [],
        "profile_context": [],
        "required_features": [],
        "preferred_features": [],
        "excluded_features": [],
        "query": "presente",
        "clarification_needed": True,
        "clarification_question_keywords": ["orçamento", "interesse"],
    }
    good = UserIntent(
        query="presente",
        clarification_needed=True,
        clarification_question="Qual é o seu orçamento máximo para esse presente?",
    )
    too_short_or_offtopic = UserIntent(
        query="presente",
        clarification_needed=True,
        clarification_question="Qual a cor?",
    )

    assert evaluate_case(good, expected, 21).core_pass is True
    off = evaluate_case(too_short_or_offtopic, expected, 21)
    assert off.clarification_question_ok is False
    assert off.core_pass is False


def test_evaluator_aliases_similarity_and_aggregate():
    assert normalize_label("laptop") == "notebook"
    assert normalize_label("celular") == "smartphone"
    assert normalize_label("headset") == "fone de ouvido"
    assert set_f1([], []) == 1.0
    assert set_f1(["a"], []) == 0.0
    assert token_jaccard("notebook para programação", "notebook programação") == 1.0
    assert aggregate([]) == {}

    expected_ok = {
        "category": "notebook",
        "price": {
            "min_price": None,
            "max_price": None,
            "min_inclusive": True,
            "max_inclusive": True,
            "is_hard_constraint": False,
        },
        "query": "notebook",
        "clarification_needed": False,
    }
    res1 = evaluate_case(UserIntent(category="notebook", query="notebook"), expected_ok, 1)
    res2 = evaluate_case(UserIntent(category="smartphone", query="smartphone"), expected_ok, 2)
    summary = aggregate([res1, res2])
    assert summary["cases"] == 2.0
    assert summary["core_pass_rate"] == 0.5
    assert summary["category_accuracy"] == 0.5


def test_price_match_with_empty_bounds_and_non_neutral_flags() -> None:
    assert price_match(
        UserIntent(query="teste", price=PriceConstraint()),
        {"min_price": None, "max_price": None, "min_inclusive": False, "max_inclusive": False, "is_hard_constraint": True},
    )


def test_dataset_single_21_single_3_and_explicit_photography() -> None:
    cases = _load_eval_cases()

    c21 = cases[21]["expected"]
    pred21 = UserIntent(
        category=None,
        price=PriceConstraint(min_price=None, max_price=None, min_inclusive=False, is_hard_constraint=True),
        query="presente para meu pai",
        clarification_needed=True,
        clarification_question="Qual produto ele gosta e qual é o seu orçamento máximo?",
    )
    res21 = evaluate_case(pred21, c21, 21)
    assert res21.price_correct is True
    assert res21.core_pass is True

    c3 = cases[3]["expected"]
    assert c3["use_cases"] == []
    pred3_ok = UserIntent(
        category="smartphone",
        price=PriceConstraint(max_price=2500, is_hard_constraint=True, currency="BRL"),
        brands=["Samsung"],
        use_cases=[],
        preferred_features=["boa câmera", "bateria longa"],
        query="smartphone Samsung boa câmera bateria longa",
    )
    assert evaluate_case(pred3_ok, c3, 3).core_pass is True

    c15 = cases[15]["expected"]
    c30 = cases[30]["expected"]
    assert c15["use_cases"] == ["fotografia", "uso diário"]
    assert c30["use_cases"] == ["fotografia"]
    pred15_missing_photo = UserIntent(
        category="smartphone",
        price=PriceConstraint(max_price=2000, is_hard_constraint=True, currency="BRL"),
        excluded_brands=["Motorola"],
        use_cases=["uso diário"],
        preferred_features=["boa câmera"],
        query="smartphone para fotos sem Motorola",
    )
    assert evaluate_case(pred15_missing_photo, c15, 15).core_pass is False

    pred30_ok = UserIntent(
        category="câmera",
        price=PriceConstraint(max_price=4500, is_hard_constraint=True, currency="BRL"),
        use_cases=["fotografia"],
        query="câmera fotografia profissional",
    )
    assert evaluate_case(pred30_ok, c30, 30).core_pass is True


def test_semantic_distinctions_gamer_normalization_and_hard_rules() -> None:
    cases = _load_eval_cases()

    assert normalize_label("trabalho") != normalize_label("escritório")
    c29 = cases[29]["expected"]
    assert c29["use_cases"] == ["escritório"]

    assert normalize_label("estética gamer") == normalize_label("gamer")
    assert normalize_label("teclado gamer") == normalize_label("estética gamer")
    assert normalize_label("estilo gamer") == normalize_label("estética gamer")
    assert normalize_label("cadeira gamer") == "cadeira gamer"

    pred29 = UserIntent(
        category="teclado",
        use_cases=["escritório"],
        required_features=["sem fio"],
        excluded_features=["RGB", "teclado gamer"],
        query="teclado sem fio escritório sem RGB",
    )
    assert evaluate_case(pred29, c29, 29).core_pass is True

    assert normalize_label("Photoshop") != normalize_label("design")
    c32 = cases[32]["expected"]
    assert c32["use_cases"] == ["Photoshop", "edição de vídeo"]
    assert c32["profile_context"] == ["designer"]

    pred32 = UserIntent(
        category="notebook",
        price=PriceConstraint(max_price=6000, is_hard_constraint=False, currency="BRL"),
        use_cases=["Photoshop", "edição de vídeo"],
        profile_context=["designer"],
        preferred_features=[],
        query="notebook Photoshop edição de vídeo",
    )
    res32 = evaluate_case(pred32, c32, 32)
    assert res32.core_pass is True
    assert res32.profile_f1 == 1.0

    c6 = cases[6]["expected"]
    pred6_bad_inclusive = UserIntent(
        category="tv",
        price=PriceConstraint(max_price=3000, max_inclusive=True, is_hard_constraint=True, currency="BRL"),
        use_cases=["entretenimento"],
        required_features=["55 polegadas"],
        query="TV 55 polegadas",
    )
    res6 = evaluate_case(pred6_bad_inclusive, c6, 6)
    assert res6.price_correct is False
    assert res6.core_pass is False
    assert compute_failed_fields(res6, pred6_bad_inclusive, c6) == [
        {"field": "price_constraint", "category": "price_inclusivity_mismatch"}
    ]


def test_prompt_and_evaluator_single_turn_regressions() -> None:
    cases = _load_eval_cases()

    assert "para trabalhar com design" in SYSTEM_PROMPT
    assert "Photoshop" in SYSTEM_PROMPT
    assert "entretenimento" in SYSTEM_PROMPT
    assert "não precisa ter touchscreen" in SYSTEM_PROMPT
    assert "não faço questão" in SYSTEM_PROMPT

    pred4 = UserIntent(
        category="monitor",
        price=PriceConstraint(max_price=1800, is_hard_constraint=True, currency="BRL"),
        use_cases=["trabalho", "design"],
        profile_context=[],
        required_features=["27 polegadas"],
        query="monitor 27 polegadas trabalho design",
    )
    assert evaluate_case(pred4, cases[4]["expected"], 4).core_pass is True

    for phrase in ("filmes e séries", "assistir filmes e séries", "séries e filmes", "entretenimento"):
        assert normalize_label(phrase) == "entretenimento"
    pred6_ok = UserIntent(
        category="tv",
        price=PriceConstraint(max_price=3000, max_inclusive=False, is_hard_constraint=True, currency="BRL"),
        use_cases=["filmes e séries"],
        required_features=["55 polegadas"],
        query="TV 55 polegadas filmes e séries",
    )
    assert evaluate_case(pred6_ok, cases[6]["expected"], 6).core_pass is True

    assert normalize_feature_tokens("16 GB de RAM") == normalize_feature_tokens("pelo menos 16 GB de RAM")
    assert normalize_feature_tokens("16GB RAM") == normalize_feature_tokens("RAM de 16 GB")
    assert normalize_feature_tokens("8 GB de RAM") != normalize_feature_tokens("16 GB de RAM")
    assert normalize_feature_tokens("512 GB SSD") == normalize_feature_tokens("SSD de 512 GB")
    assert normalize_feature_tokens("SSD 512GB") == normalize_feature_tokens("512 GB de SSD")
    assert normalize_feature_tokens("SSD de 256 GB") != normalize_feature_tokens("SSD de 512 GB")

    pred26_ok = UserIntent(
        category="notebook",
        use_cases=["programação"],
        required_features=["16 GB de RAM", "512GB SSD"],
        query="notebook programação 16 GB RAM SSD 512 GB",
    )
    res26 = evaluate_case(pred26_ok, cases[26]["expected"], 26)
    assert res26.required_features_f1 == 1.0
    assert res26.core_pass is True

    pred26_wrong_ram = UserIntent(
        category="notebook",
        use_cases=["programação"],
        required_features=["8 GB de RAM", "SSD de 512 GB"],
        query="notebook programação 8 GB RAM SSD 512 GB",
    )
    res26_bad = evaluate_case(pred26_wrong_ram, cases[26]["expected"], 26)
    assert res26_bad.core_pass is False
    assert compute_failed_fields(res26_bad, pred26_wrong_ram, cases[26]["expected"]) == [
        {"field": "required_features", "category": "set_mismatch"}
    ]

    assert normalize_feature_tokens("teclado gamer cheio de RGB") == {"rgb", "estetica gamer"}
    assert normalize_feature_tokens("cadeira gamer") == {"cadeira gamer"}

    pred29_combined = UserIntent(
        category="teclado",
        use_cases=["escritório"],
        required_features=["sem fio"],
        excluded_features=["teclado gamer cheio de RGB"],
        query="teclado sem fio escritorio",
    )
    assert evaluate_case(pred29_combined, cases[29]["expected"], 29).core_pass is True

    pred29_incomplete = UserIntent(
        category="teclado",
        use_cases=["escritório"],
        required_features=["sem fio"],
        excluded_features=["RGB"],
        query="teclado sem fio escritorio",
    )
    res29_bad = evaluate_case(pred29_incomplete, cases[29]["expected"], 29)
    assert res29_bad.excluded_features_f1 < 0.75
    assert res29_bad.core_pass is False

    assert normalize_label("boa câmera") != normalize_label("fotografia")
    assert feature_set_f1(["pelo menos 1 TB"], ["1 TB"]) == 1.0

    pred32_ok = UserIntent(
        category="notebook",
        price=PriceConstraint(max_price=6000, is_hard_constraint=False, currency="BRL"),
        use_cases=["Photoshop", "edição de vídeo"],
        profile_context=["designer"],
        query="notebook Photoshop edição de vídeo",
    )
    assert evaluate_case(pred32_ok, cases[32]["expected"], 32).core_pass is True

    pred3_ok = UserIntent(
        category="smartphone",
        price=PriceConstraint(max_price=2500, is_hard_constraint=True, currency="BRL"),
        brands=["Samsung"],
        use_cases=[],
        preferred_features=["boa câmera", "bateria longa"],
        query="smartphone Samsung boa câmera bateria longa",
    )
    assert evaluate_case(pred3_ok, cases[3]["expected"], 3).core_pass is True
