from __future__ import annotations

from collections.abc import Callable

import pytest

from select_fuzz.generation.query import (
    GeneratedQuery,
    QueryBudget,
    QueryComplexity,
    QueryLane,
    QueryMix,
)
from select_fuzz.generation.query_ast import (
    Literal,
    OrderBy,
    Projection,
    QueryAst,
    QueryScope,
    SelectQuery,
    SqlType,
)


COMPLEXITY = QueryComplexity(1, 1, 0, 1, 1, 0, 1, 1, 1)
AST = QueryAst(
    SelectQuery((Projection(Literal(1, SqlType.NUMERIC)),)),
    OrderBy((1,)),
    QueryScope(1, max_rows=1),
)


@pytest.mark.parametrize(
    ("factory", "error"),
    [
        (lambda: QueryMix(valid_percent=True, free_random_percent=0, negative_percent=0), TypeError),
        (lambda: QueryMix(valid_percent=90, free_random_percent=5, negative_percent=4), ValueError),
        (lambda: QueryMix(valid_percent=101, free_random_percent=0, negative_percent=-1), ValueError),
        (lambda: QueryBudget(max_tables=0), ValueError),
        (lambda: QueryBudget(max_tables=True), ValueError),
        (lambda: QueryComplexity(-1, 1, 0, 1, 1, 0, 0, 0, 0), ValueError),
        (lambda: QueryComplexity(0, 0, 0, 1, 1, 0, 0, 0, 0), ValueError),
        (lambda: QueryComplexity(0, 1, 0, 0, 1, 0, 0, 0, 0), ValueError),
        (lambda: QueryComplexity(0, 1, 0, 1, 0, 0, 0, 0, 0), ValueError),
        (lambda: QueryMix().choose(seed=1, case_ordinal=-1), ValueError),
        (lambda: QueryMix().choose(seed=1, case_ordinal=True), ValueError),
    ],
)
def test_query_model_contracts_reject_invalid_states(
    factory: Callable[[], object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        factory()


def test_query_mix_reaches_all_lanes_and_has_stable_identity() -> None:
    mix = QueryMix(34, 33, 33)
    lanes = {mix.choose(seed=7, case_ordinal=ordinal) for ordinal in range(100)}
    assert lanes == {QueryLane.VALID, QueryLane.FREE_RANDOM, QueryLane.NEGATIVE}
    assert mix.identity() == "34:33:33"


def test_query_complexity_reports_every_budget_violation() -> None:
    complexity = QueryComplexity(5, 4, 3, 4, 13, 13, 1, 100_001, 10_001)
    assert complexity.violations(QueryBudget()) == (
        "tables",
        "depth",
        "ctes",
        "set_branches",
        "projection",
        "predicates",
        "estimated_intermediate_rows",
        "estimated_output_rows",
    )
    assert not complexity.within(QueryBudget())
    assert COMPLEXITY.within(QueryBudget())


def test_generated_query_validates_lane_coverage_flag_and_error_serialization() -> None:
    with pytest.raises(TypeError, match="lane"):
        GeneratedQuery(AST, "SELECT 1 ORDER BY 1", "feature", frozenset(), "valid", 0, 1, COMPLEXITY)
    with pytest.raises(TypeError, match="coverage_eligible"):
        GeneratedQuery(
            AST,
            "SELECT 1 ORDER BY 1",
            "feature",
            frozenset(),
            QueryLane.VALID,
            0,
            1,
            COMPLEXITY,
            coverage_eligible=1,
        )
    generated = GeneratedQuery(
        AST,
        "SELECT 1 ORDER BY 1",
        "feature",
        frozenset({"tag"}),
        QueryLane.VALID,
        0,
        1,
        COMPLEXITY,
    )
    assert generated.canonical_bytes()
