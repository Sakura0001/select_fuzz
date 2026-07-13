from __future__ import annotations

import pytest

from select_fuzz.performance.tree import (
    Family,
    PlanParseError,
    ShapeBoundary,
    parse_tree,
)


TREE = """-> Sort: k (cost=1.2e+3 rows=2.5e+4) (actual time=1.25e-4..6.02e+3 rows=25000 loops=1)
    -> Table scan on t (cost=1 rows=25000) (actual time=5..7e+3 rows=25000 loops=1)"""


def test_parser_extracts_scientific_actual_time_rows_and_loops() -> None:
    plan = parse_tree(TREE, completed=True)

    assert plan.root.start_ms == pytest.approx(0.000125)
    assert plan.root.end_ms == pytest.approx(6020.0)
    assert plan.root.actual_rows == 25_000
    assert plan.root.loops == 1
    assert plan.nodes[1].family is Family.SCAN


def test_shape_boundary_accepts_equivalent_scan_families_per_role() -> None:
    boundary = ShapeBoundary(required=frozenset({Family.SCAN}))

    boundary.validate(parse_tree("-> Table scan on a (cost=1 rows=10)"), "baseline")
    boundary.validate(parse_tree("-> Index range scan on b (cost=1 rows=12)"), "custom_off")


@pytest.mark.parametrize(
    "tree,completed",
    [
        ("", False),
        ("-> Scan (cost=1 rows=1)", True),
        (
            "-> A (actual time=1..2 rows=1 loops=1)\n-> B (actual time=1..2 rows=1 loops=1)",
            True,
        ),
    ],
)
def test_parser_rejects_empty_incomplete_or_ambiguous_plans(
    tree: str, completed: bool
) -> None:
    with pytest.raises(PlanParseError):
        parse_tree(tree, completed=completed)


def test_shape_boundary_rejects_a_non_target_operator_family() -> None:
    boundary = ShapeBoundary(required=frozenset({Family.JOIN}))

    with pytest.raises(PlanParseError, match="baseline"):
        boundary.validate(parse_tree("-> Table scan on a (cost=1 rows=10)"), "baseline")


def test_completed_tree_rejects_root_iterator_with_multiple_loops() -> None:
    with pytest.raises(PlanParseError, match="root iterator loops"):
        parse_tree(
            "-> Aggregate (actual time=1..2 rows=1 loops=2)\n"
            "    -> Table scan (actual time=0.1..1 rows=2 loops=2)",
            completed=True,
        )
