from __future__ import annotations

import random

import pytest

from select_fuzz.generation.composite_indexes import (
    CompositeColumn,
    CompositeIndexFamily,
    CompositeIndexPartPlan,
    CompositeIndexPlan,
    build_composite_index_candidates,
)


def _columns() -> tuple[CompositeColumn, ...]:
    return (
        CompositeColumn("id", "BIGINT UNSIGNED"),
        CompositeColumn("counter", "INT"),
        CompositeColumn("tenant_id", "BIGINT UNSIGNED"),
        CompositeColumn("payload", "VARCHAR(64)", charset_bytes=4),
        CompositeColumn("body", "TEXT", charset_bytes=4),
        CompositeColumn("token", "VARBINARY(32)"),
    )


def test_planner_builds_every_composite_family_within_budget() -> None:
    candidates = build_composite_index_candidates(
        _columns(),
        rng=random.Random(17),
        index_byte_budget=3072,
    )

    by_family = {candidate.family: candidate for candidate in candidates}
    assert set(by_family) == set(CompositeIndexFamily)
    assert all(len(candidate.parts) >= 2 for candidate in candidates)
    assert all(candidate.estimated_bytes <= 3072 for candidate in candidates)
    assert [part.column_name for part in by_family[CompositeIndexFamily.UNIQUE].parts][
        0
    ] == "id"
    assert {part.descending for part in by_family[CompositeIndexFamily.MIXED_DIRECTION].parts} == {
        False,
        True,
    }
    assert any(
        part.prefix_length is not None
        for part in by_family[CompositeIndexFamily.PREFIX].parts
    )
    assert 3 <= len(by_family[CompositeIndexFamily.WIDE].parts) <= 4


def test_unique_plan_appends_required_columns_once_and_accounts_for_them() -> None:
    candidates = build_composite_index_candidates(
        _columns(),
        rng=random.Random(3),
        index_byte_budget=64,
        unique_required_columns=("tenant_id", "id", "tenant_id"),
    )

    unique = next(
        candidate
        for candidate in candidates
        if candidate.family is CompositeIndexFamily.UNIQUE
    )
    names = tuple(part.column_name for part in unique.parts)
    assert names[0] == "id"
    assert names.count("id") == 1
    assert names.count("tenant_id") == 1
    assert unique.estimated_bytes <= 64


def test_planner_is_seed_deterministic() -> None:
    first = build_composite_index_candidates(
        _columns(),
        rng=random.Random(29),
        index_byte_budget=3072,
    )
    second = build_composite_index_candidates(
        _columns(),
        rng=random.Random(29),
        index_byte_budget=3072,
    )

    assert first == second


def test_planner_omits_unsupported_and_over_budget_shapes() -> None:
    columns = (
        CompositeColumn("id", "BIGINT UNSIGNED"),
        CompositeColumn("document", "JSON"),
        CompositeColumn("location", "POINT"),
    )

    candidates = build_composite_index_candidates(
        columns,
        rng=random.Random(9),
        index_byte_budget=16,
    )

    assert candidates == ()


def test_planner_treats_zero_length_character_and_binary_columns_as_one_byte_minimum() -> None:
    candidates = build_composite_index_candidates(
        (
            CompositeColumn("id", "BIGINT UNSIGNED"),
            CompositeColumn("empty_text", "CHAR(0)", charset_bytes=4),
            CompositeColumn("empty_binary", "BINARY(0)"),
        ),
        rng=random.Random(4),
        index_byte_budget=16,
    )

    assert candidates
    assert all(part.estimated_bytes >= 1 for plan in candidates for part in plan.parts)
    assert all(plan.estimated_bytes <= 16 for plan in candidates)


def test_planner_does_not_underestimate_boolean_alias_storage() -> None:
    candidates = build_composite_index_candidates(
        (
            CompositeColumn("id", "BIGINT UNSIGNED"),
            CompositeColumn("flag", "BOOLEAN"),
        ),
        rng=random.Random(5),
        index_byte_budget=9,
    )

    assert candidates == ()


@pytest.mark.parametrize("budget", (0, -1))
def test_planner_rejects_nonpositive_budget(budget: int) -> None:
    with pytest.raises(ValueError, match="index_byte_budget"):
        build_composite_index_candidates(
            _columns(),
            rng=random.Random(1),
            index_byte_budget=budget,
        )


def test_planner_rejects_duplicate_column_names() -> None:
    with pytest.raises(ValueError, match="column names"):
        build_composite_index_candidates(
            (
                CompositeColumn("id", "BIGINT UNSIGNED"),
                CompositeColumn("id", "INT"),
            ),
            rng=random.Random(1),
            index_byte_budget=3072,
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"name": "not-safe"}, "column name"),
        ({"mysql_type": "  "}, "mysql_type"),
        ({"charset_bytes": 0}, "charset_bytes"),
    ],
)
def test_composite_column_rejects_invalid_physical_metadata(
    kwargs: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "name": "payload",
        "mysql_type": "VARCHAR(32)",
        "charset_bytes": 4,
        **kwargs,
    }

    with pytest.raises(ValueError, match=message):
        CompositeColumn(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        ({"column_name": "not-safe"}, ValueError, "column name"),
        ({"estimated_bytes": 0}, ValueError, "estimated_bytes"),
        ({"prefix_length": 0}, ValueError, "prefix_length"),
        ({"descending": 1}, TypeError, "descending"),
    ],
)
def test_composite_part_rejects_invalid_rendering_metadata(
    kwargs: dict[str, object], error: type[Exception], message: str
) -> None:
    values: dict[str, object] = {
        "column_name": "payload",
        "estimated_bytes": 8,
        **kwargs,
    }

    with pytest.raises(error, match=message):
        CompositeIndexPartPlan(**values)  # type: ignore[arg-type]


def test_composite_plan_rejects_invalid_shape_metadata() -> None:
    first = CompositeIndexPartPlan("first", 4)
    second = CompositeIndexPartPlan("second", 4)

    with pytest.raises(TypeError, match="family"):
        CompositeIndexPlan("ordinary", (first, second))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="between two and four"):
        CompositeIndexPlan(CompositeIndexFamily.ORDINARY, (first,))
    with pytest.raises(ValueError, match="must be unique"):
        CompositeIndexPlan(CompositeIndexFamily.ORDINARY, (first, first))
    with pytest.raises(TypeError, match="unique"):
        CompositeIndexPlan(
            CompositeIndexFamily.ORDINARY,
            (first, second),
            unique=1,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"identity_column": "not-safe"}, "identity_column"),
        ({"unique_required_columns": ("not-safe",)}, "unique_required_columns"),
    ],
)
def test_planner_rejects_invalid_unique_column_identifiers(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        build_composite_index_candidates(
            _columns(),
            rng=random.Random(1),
            index_byte_budget=3072,
            **kwargs,  # type: ignore[arg-type]
        )


def test_planner_omits_unique_family_when_required_shape_is_impossible() -> None:
    missing_required = build_composite_index_candidates(
        _columns(),
        rng=random.Random(7),
        index_byte_budget=3072,
        unique_required_columns=("missing",),
    )
    too_many_required = build_composite_index_candidates(
        (
            CompositeColumn("id", "BIGINT"),
            CompositeColumn("a", "INT"),
            CompositeColumn("b", "INT"),
            CompositeColumn("c", "INT"),
            CompositeColumn("d", "INT"),
        ),
        rng=random.Random(7),
        index_byte_budget=3072,
        unique_required_columns=("a", "b", "c"),
    )

    assert CompositeIndexFamily.UNIQUE not in {plan.family for plan in missing_required}
    assert CompositeIndexFamily.UNIQUE not in {plan.family for plan in too_many_required}


def test_planner_ignores_unrecognized_declared_type() -> None:
    candidates = build_composite_index_candidates(
        (
            CompositeColumn("id", "BIGINT"),
            CompositeColumn("opaque", "UUID(16)"),
        ),
        rng=random.Random(11),
        index_byte_budget=3072,
    )

    assert candidates == ()
