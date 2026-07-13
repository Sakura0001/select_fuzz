from __future__ import annotations

from collections.abc import Callable

import pytest

from select_fuzz.generation.query_ast import (
    CaseExpression,
    CastExpression,
    ColumnRef,
    Cte,
    DerivedRelation,
    FunctionCall,
    FunctionName,
    FunctionalLowerExpression,
    InvalidFunctionArity,
    JoinKind,
    JoinRelation,
    JsonMemberOf,
    Literal,
    MatchAgainst,
    OrderBy,
    Projection,
    QueryAst,
    QueryScope,
    RowExpression,
    SelectQuery,
    SetOperator,
    SetQuery,
    SqlType,
    Star,
    SubqueryExpression,
    SubqueryOperator,
    TableRelation,
    ValuesQuery,
    WindowFunction,
    WindowOrder,
    WindowSpec,
    require_identifier,
)


NUMBER = Literal(1, SqlType.NUMERIC)
TEXT = Literal("x", SqlType.TEXT)
COLUMN = ColumnRef("t", "id", SqlType.NUMERIC)
SELECT = SelectQuery((Projection(NUMBER),))
TABLE = TableRelation("items", "t")


@pytest.mark.parametrize(
    ("factory", "error"),
    [
        (lambda: require_identifier("Bad-Name"), ValueError),
        (lambda: Literal(True, SqlType.NUMERIC), TypeError),
        (lambda: Literal(float("inf"), SqlType.NUMERIC), ValueError),
        (lambda: Star("Bad"), ValueError),
        (lambda: FunctionCall("ABS", (NUMBER,), SqlType.NUMERIC), TypeError),
        (lambda: FunctionCall(FunctionName.ABS, (NUMBER,), SqlType.NUMERIC, distinct=1), TypeError),
        (lambda: FunctionCall(FunctionName.ABS, (), SqlType.NUMERIC), ValueError),
        (lambda: FunctionCall(FunctionName.COALESCE, (NUMBER,), SqlType.NUMERIC), ValueError),
        (lambda: FunctionCall(FunctionName.CONCAT, (), SqlType.TEXT), ValueError),
        (lambda: FunctionCall(FunctionName.JSON_EXTRACT, (TEXT,), SqlType.JSON), ValueError),
        (lambda: FunctionCall(FunctionName.JSON_OBJECT, (TEXT,), SqlType.JSON), ValueError),
        (lambda: InvalidFunctionArity(FunctionName.COUNT), ValueError),
        (lambda: InvalidFunctionArity(FunctionName.ABS, (NUMBER,)), ValueError),
        (lambda: FunctionalLowerExpression(COLUMN, 0), ValueError),
        (lambda: FunctionalLowerExpression(COLUMN, True), ValueError),
        (lambda: CastExpression(NUMBER, "FLOAT", SqlType.NUMERIC), ValueError),
        (lambda: RowExpression(()), ValueError),
        (lambda: CaseExpression(None, (), NUMBER, SqlType.NUMERIC), ValueError),
        (
            lambda: SubqueryExpression(SubqueryOperator.IN, SELECT, left=None),
            ValueError,
        ),
        (
            lambda: SubqueryExpression(SubqueryOperator.EXISTS, SELECT, left=NUMBER),
            ValueError,
        ),
        (
            lambda: SubqueryExpression(SubqueryOperator.SCALAR, SELECT),
            ValueError,
        ),
        (lambda: JsonMemberOf(NUMBER, TEXT, "$.items"), ValueError),
        (lambda: MatchAgainst((), "term"), ValueError),
        (lambda: MatchAgainst((COLUMN,), "term", boolean_mode=1), TypeError),
        (lambda: WindowOrder((), False), ValueError),
        (lambda: WindowOrder((NUMBER,), 1), TypeError),
        (lambda: WindowOrder((NUMBER,), False, -1), ValueError),
        (lambda: WindowOrder((NUMBER,), False, True), ValueError),
        (lambda: WindowSpec((), WindowOrder((NUMBER,), True), frame=(0, -1)), ValueError),
        (lambda: WindowSpec((), WindowOrder((NUMBER,), True), frame=(True, 0)), ValueError),
        (
            lambda: WindowFunction("AVG", NUMBER, WindowOrder((NUMBER,), True), SqlType.NUMERIC),
            ValueError,
        ),
        (
            lambda: WindowFunction("SUM", None, WindowOrder((NUMBER,), True), SqlType.NUMERIC),
            ValueError,
        ),
        (
            lambda: WindowFunction(
                "ROW_NUMBER", NUMBER, WindowOrder((NUMBER,), True), SqlType.NUMERIC
            ),
            ValueError,
        ),
        (
            lambda: JoinRelation(TABLE, TABLE, JoinKind.NATURAL_LEFT, NUMBER),
            ValueError,
        ),
        (lambda: JoinRelation(TABLE, TABLE, JoinKind.INNER), ValueError),
        (lambda: DerivedRelation(SELECT, "d", lateral=1), TypeError),
        (lambda: SelectQuery(()), ValueError),
        (lambda: SelectQuery((Projection(NUMBER),), with_rollup=True), ValueError),
        (
            lambda: SelectQuery((Projection(NUMBER),), optimizer_hint="HASH_JOIN(t, u)"),
            ValueError,
        ),
        (lambda: SetQuery((SELECT,), SetOperator.UNION), ValueError),
        (lambda: SetQuery((SELECT, SELECT), SetOperator.INTERSECT, all=True), ValueError),
        (lambda: SetQuery((SELECT, SELECT), SetOperator.EXCEPT, all=True), ValueError),
        (lambda: ValuesQuery(()), ValueError),
        (lambda: ValuesQuery(((),)), ValueError),
        (lambda: ValuesQuery(((NUMBER,), (NUMBER, TEXT))), ValueError),
        (lambda: QueryScope(0), ValueError),
        (lambda: QueryScope(True), ValueError),
        (lambda: QueryScope(1, frozenset({frozenset()})), ValueError),
        (lambda: QueryScope(1, frozenset({frozenset({2})})), ValueError),
        (lambda: QueryScope(1, max_rows=-1), ValueError),
        (lambda: QueryScope(1, max_rows=True), ValueError),
        (lambda: OrderBy(()), ValueError),
        (lambda: OrderBy((1, 1)), ValueError),
        (lambda: OrderBy((0,)), ValueError),
        (lambda: OrderBy((True,)), ValueError),
        (lambda: OrderBy((1,), frozenset({"1"})), TypeError),
        (lambda: OrderBy((1,), frozenset({2})), ValueError),
    ],
)
def test_closed_ast_rejects_invalid_boundary_values(
    factory: Callable[[], object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        factory()


def test_order_proofs_cover_cardinality_uniqueness_and_out_of_scope_paths() -> None:
    assert WindowOrder((NUMBER,), False, max_rows=1).proves_total_order()
    assert WindowOrder((NUMBER,), True).proves_total_order()
    assert not WindowOrder((NUMBER,), False, max_rows=2).proves_total_order()

    assert not OrderBy((2,)).proves_total_order(QueryScope(1))
    assert OrderBy((1,)).proves_total_order(QueryScope(1, max_rows=1))
    assert OrderBy((1, 2)).proves_total_order(
        QueryScope(2, frozenset({frozenset({1, 2})}))
    )
    assert not OrderBy((1,)).proves_total_order(
        QueryScope(2, frozenset({frozenset({1, 2})}))
    )


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"recursive": 1}, TypeError),
        ({"recursive": True}, ValueError),
        ({"limit": 0}, ValueError),
        ({"limit": True}, ValueError),
        ({"limit": 1}, ValueError),
    ],
)
def test_query_ast_rejects_invalid_global_contracts(
    kwargs: dict[str, object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        QueryAst(SELECT, OrderBy((1,)), QueryScope(1), **kwargs)


def test_query_ast_rejects_out_of_scope_and_nondeterministic_window_orders() -> None:
    with pytest.raises(ValueError, match="ordinal exceeds projection"):
        QueryAst(SELECT, OrderBy((2,)), QueryScope(1))

    with pytest.raises(ValueError, match="window ordering"):
        QueryAst(
            SELECT,
            OrderBy((1,)),
            QueryScope(1),
            window_orders=(WindowOrder((NUMBER,), False, max_rows=2),),
        )


def test_valid_recursive_limited_window_query_reports_window_contract() -> None:
    order = WindowOrder((NUMBER,), True)
    query = QueryAst(
        SELECT,
        OrderBy((1,)),
        QueryScope(1, max_rows=1),
        ctes=(Cte("seed", ("value",), SELECT),),
        recursive=True,
        limit=1,
        window_orders=(order,),
    )

    assert query.has_window
    assert query.window_orders_are_total()
