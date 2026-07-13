from __future__ import annotations

from collections.abc import Callable

import pytest

from select_fuzz.generation import query_ast as ast_nodes
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
    ParenthesizedQuery,
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
from select_fuzz.generation.query_render import render_query_ast


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
        (lambda: DerivedRelation(SELECT, "d", columns=("Bad-Name",)), ValueError),
        (lambda: DerivedRelation(SELECT, "d", columns=("renamed", "renamed")), ValueError),
        (lambda: DerivedRelation(SELECT, "d", columns=("renamed", "extra")), ValueError),
        (
            lambda: DerivedRelation(
                getattr(ast_nodes, "TableQuery")("items"),
                "d",
                columns=("renamed",),
            ),
            ValueError,
        ),
        (lambda: getattr(ast_nodes, "TableQuery")("Bad-Name"), ValueError),
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
    assert OrderBy((1, 2)).proves_total_order(QueryScope(2, frozenset({frozenset({1, 2})})))
    assert not OrderBy((1,)).proves_total_order(QueryScope(2, frozenset({frozenset({1, 2})})))


def test_derived_relation_accepts_an_exact_explicit_column_list() -> None:
    relation = DerivedRelation(SELECT, "d", columns=["renamed"])  # type: ignore[arg-type]

    assert relation.columns == ("renamed",)


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"recursive": 1}, TypeError),
        ({"recursive": True}, ValueError),
        ({"limit": True}, ValueError),
        ({"limit": 1}, ValueError),
    ],
)
def test_query_ast_rejects_invalid_global_contracts(
    kwargs: dict[str, object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        QueryAst(SELECT, OrderBy((1,)), QueryScope(1), **kwargs)


def test_query_ast_accepts_limit_zero_and_validates_offset_contract() -> None:
    empty = QueryAst(SELECT, OrderBy((1,)), QueryScope(1), limit=0)
    paged = QueryAst(
        SELECT,
        OrderBy((1,)),
        QueryScope(1, frozenset({frozenset({1})})),
        limit=1,
        offset=0,
    )

    assert empty.limit == 0
    assert paged.offset == 0
    with pytest.raises(ValueError, match="OFFSET requires LIMIT"):
        QueryAst(SELECT, OrderBy((1,)), QueryScope(1), offset=1)
    with pytest.raises(ValueError, match="nonnegative"):
        QueryAst(SELECT, OrderBy((1,)), QueryScope(1), limit=0, offset=-1)
    with pytest.raises(TypeError, match="integer"):
        QueryAst(SELECT, OrderBy((1,)), QueryScope(1), limit=0, offset=True)


def test_query_ast_caps_limit_and_offset_at_mysql_unsigned_bigint() -> None:
    maximum = 2**64 - 1
    scope = QueryScope(1, max_rows=1)

    assert QueryAst(SELECT, OrderBy((1,)), scope, limit=maximum).limit == maximum
    assert (
        QueryAst(
            SELECT,
            OrderBy((1,)),
            scope,
            limit=1,
            offset=maximum,
        ).offset
        == maximum
    )
    with pytest.raises(ValueError, match="unsigned BIGINT"):
        QueryAst(SELECT, OrderBy((1,)), scope, limit=maximum + 1)
    with pytest.raises(ValueError, match="unsigned BIGINT"):
        QueryAst(SELECT, OrderBy((1,)), scope, limit=1, offset=maximum + 1)


def test_parenthesized_query_owns_a_bounded_local_order_and_limit() -> None:
    branch = ParenthesizedQuery(
        SELECT,
        order_by=(1,),
        limit=1,
        offset=0,
        max_rows=1,
    )
    query = QueryAst(
        SetQuery((branch, SELECT), SetOperator.UNION),
        OrderBy((1,)),
        QueryScope(1, frozenset({frozenset({1})}), 2),
    )

    assert render_query_ast(query) == (
        "(SELECT 1 ORDER BY 1 LIMIT 1 OFFSET 0) UNION SELECT 1 ORDER BY 1"
    )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: ParenthesizedQuery(SELECT, order_by=(1,)),
        lambda: ParenthesizedQuery(SELECT, limit=1),
        lambda: ParenthesizedQuery(SELECT, order_by=(2,), limit=1),
        lambda: ParenthesizedQuery(SELECT, order_by=(1,), offset=1),
        lambda: ParenthesizedQuery(SELECT, order_by=(1,), limit=True),
        lambda: ParenthesizedQuery(SELECT, order_by=(1,), limit=2**64),
        lambda: ParenthesizedQuery(SELECT, order_by=(1,), limit=1),
        lambda: ParenthesizedQuery(
            SELECT,
            order_by=(1,),
            limit=1,
            unique_projection_sets=frozenset({frozenset({True})}),
            max_rows=2,
        ),
        lambda: ParenthesizedQuery(
            SELECT,
            order_by=(1,),
            limit=1,
            unique_projection_sets=frozenset({frozenset({1.0})}),
            max_rows=2,
        ),
        lambda: QueryScope(1, frozenset({frozenset({True})}), 2),
        lambda: QueryScope(1, frozenset({frozenset({1.0})}), 2),
    ],
)
def test_parenthesized_query_rejects_unsafe_local_top_n_contracts(
    factory: Callable[[], object],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        factory()


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
