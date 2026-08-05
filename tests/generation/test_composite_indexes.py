from __future__ import annotations

import random

import pytest

from select_fuzz.generation.composite_indexes import (
    CompositeColumn,
    CompositeIndexFamily,
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
