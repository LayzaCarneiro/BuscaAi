from userintent.evaluator import (
    aggregate,
    evaluate_case,
    normalize_label,
    set_f1,
    token_jaccard,
)
from userintent.schema import UserIntent


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
