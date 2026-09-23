from userintent.evaluator import evaluate_case
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
