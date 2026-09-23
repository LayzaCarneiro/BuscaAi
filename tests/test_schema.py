from userintent.schema import UserIntent


def test_schema_accepts_expected_shape():
    intent = UserIntent(
        category="notebook",
        price={"max_price": 4000, "max_inclusive": True, "is_hard_constraint": True},
        brands=["Lenovo"],
        use_cases=["programação", "jogos"],
        profile_context=["estudante de computação"],
        required_features=["16 GB de RAM"],
        query="notebook para programação e jogos",
    )
    assert intent.price.max_price == 4000
    assert intent.brands == ["Lenovo"]


def test_extra_fields_are_rejected():
    try:
        UserIntent(category="notebook", query="notebook", unexpected="x")
    except Exception as exc:
        assert "extra" in str(exc).lower()
    else:
        raise AssertionError("schema should reject extra fields")
