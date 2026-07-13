"""Rendering for the closed typed query AST; no caller-supplied SQL fragments."""

from __future__ import annotations

from select_fuzz.generation.query_ast import (
    BinaryExpression,
    CaseExpression,
    CastExpression,
    ColumnRef,
    DerivedRelation,
    Expression,
    FunctionCall,
    FunctionalLowerExpression,
    InvalidFunctionArity,
    JsonMemberOf,
    JsonTableRelation,
    JoinRelation,
    Literal,
    MatchAgainst,
    NamedRelation,
    ParenthesizedQuery,
    Projection,
    QueryAst,
    QueryBody,
    Relation,
    RowExpression,
    SelectQuery,
    SetQuery,
    Star,
    SubqueryExpression,
    SubqueryOperator,
    TableQuery,
    TableRelation,
    UnaryExpression,
    UnaryOperator,
    ValuesQuery,
    WindowFunction,
    WindowSpec,
    require_identifier,
)


def quote_identifier(value: str) -> str:
    require_identifier(value, "SQL identifier")
    return f"`{value}`"


def quote_text(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def render_expression(expression: Expression) -> str:
    if isinstance(expression, ColumnRef):
        return f"{quote_identifier(expression.table_alias)}.{quote_identifier(expression.name)}"
    if isinstance(expression, Literal):
        if expression.value is None:
            return "NULL"
        if isinstance(expression.value, bytes):
            return f"X'{expression.value.hex().upper()}'"
        if isinstance(expression.value, str):
            return quote_text(expression.value)
        return repr(expression.value)
    if isinstance(expression, Star):
        if expression.table_alias is None:
            return "*"
        return f"{quote_identifier(expression.table_alias)}.*"
    if isinstance(expression, BinaryExpression):
        return (
            f"({render_expression(expression.left)} {expression.operator.value} "
            f"{render_expression(expression.right)})"
        )
    if isinstance(expression, UnaryExpression):
        operand = render_expression(expression.operand)
        if expression.operator is UnaryOperator.NOT:
            return f"(NOT {operand})"
        return f"({operand} {expression.operator.value})"
    if isinstance(expression, FunctionCall):
        arguments = ", ".join(render_expression(argument) for argument in expression.args)
        if expression.distinct:
            arguments = "DISTINCT " + arguments
        return f"{expression.name.value}({arguments})"
    if isinstance(expression, FunctionalLowerExpression):
        return (
            f"CAST(LOWER({render_expression(expression.operand)}) AS "
            f"CHAR({expression.cast_length}) CHARACTER SET utf8mb4) "
            "COLLATE utf8mb4_0900_ai_ci"
        )
    if isinstance(expression, InvalidFunctionArity):
        return f"{expression.name.value}()"
    if isinstance(expression, CastExpression):
        return f"CAST({render_expression(expression.operand)} AS {expression.target})"
    if isinstance(expression, RowExpression):
        return "ROW(" + ", ".join(render_expression(value) for value in expression.values) + ")"
    if isinstance(expression, CaseExpression):
        pieces = ["CASE"]
        if expression.base is not None:
            pieces.append(render_expression(expression.base))
        for condition, result in expression.branches:
            pieces.extend(
                ("WHEN", render_expression(condition), "THEN", render_expression(result))
            )
        pieces.extend(("ELSE", render_expression(expression.otherwise), "END"))
        return " ".join(pieces)
    if isinstance(expression, SubqueryExpression):
        query = f"({render_query_body(expression.query)})"
        if expression.operator is SubqueryOperator.SCALAR:
            return query
        if expression.operator is SubqueryOperator.EXISTS:
            return f"EXISTS {query}"
        if expression.operator is SubqueryOperator.NOT_EXISTS:
            return f"NOT EXISTS {query}"
        assert expression.left is not None
        left = render_expression(expression.left)
        if expression.operator is SubqueryOperator.IN:
            return f"{left} IN {query}"
        if expression.operator is SubqueryOperator.NOT_IN:
            return f"{left} NOT IN {query}"
        comparison = "ANY" if expression.operator is SubqueryOperator.ANY else "ALL"
        return f"{left} = {comparison} {query}"
    if isinstance(expression, JsonMemberOf):
        document = render_expression(expression.document)
        return (
            f"{render_expression(expression.member)} MEMBER OF "
            f"(JSON_EXTRACT({document}, {quote_text(expression.path)}))"
        )
    if isinstance(expression, MatchAgainst):
        columns = ", ".join(render_expression(column) for column in expression.columns)
        mode = " IN BOOLEAN MODE" if expression.boolean_mode else ""
        return f"MATCH({columns}) AGAINST({quote_text(expression.search)}{mode})"
    if isinstance(expression, WindowFunction):
        argument = "" if expression.argument is None else render_expression(expression.argument)
        window = (
            quote_identifier(expression.window)
            if isinstance(expression.window, str)
            else _render_window_spec(expression.window)
        )
        return f"{expression.function}({argument}) OVER {window}"
    raise TypeError(f"unsupported expression node: {type(expression).__name__}")


def _render_projection(projection: Projection) -> str:
    rendered = render_expression(projection.expression)
    if projection.alias is not None:
        rendered += f" AS {quote_identifier(projection.alias)}"
    return rendered


def _render_window_spec(window: WindowSpec) -> str:
    pieces: list[str] = []
    if window.partition_by:
        pieces.append(
            "PARTITION BY "
            + ", ".join(render_expression(item) for item in window.partition_by)
        )
    pieces.append(
        "ORDER BY " + ", ".join(render_expression(item) for item in window.order.expressions)
    )
    if window.frame is not None:
        preceding, following = window.frame
        pieces.append(
            "ROWS BETWEEN "
            f"{preceding} PRECEDING AND {following} FOLLOWING"
        )
    return "(" + " ".join(pieces) + ")"


def render_relation(relation: Relation) -> str:
    if isinstance(relation, TableRelation):
        partitions = ""
        if relation.partitions:
            partitions = " PARTITION (" + ", ".join(
                quote_identifier(item) for item in relation.partitions
            ) + ")"
        return (
            f"{quote_identifier(relation.table)}{partitions} "
            f"AS {quote_identifier(relation.alias)}"
        )
    if isinstance(relation, NamedRelation):
        return f"{quote_identifier(relation.name)} AS {quote_identifier(relation.alias)}"
    if isinstance(relation, DerivedRelation):
        lateral = "LATERAL " if relation.lateral else ""
        columns = ""
        if relation.columns:
            columns = " (" + ", ".join(
                quote_identifier(column) for column in relation.columns
            ) + ")"
        return (
            f"{lateral}({render_query_body(relation.query)}) "
            f"AS {quote_identifier(relation.alias)}{columns}"
        )
    if isinstance(relation, JsonTableRelation):
        return (
            "JSON_TABLE("
            f"{render_expression(relation.source)}, '$[*]' COLUMNS ("
            f"{quote_identifier(relation.ordinal_column)} FOR ORDINALITY, "
            f"{quote_identifier(relation.value_column)} BIGINT PATH '$')) "
            f"AS {quote_identifier(relation.alias)}"
        )
    if isinstance(relation, JoinRelation):
        rendered = (
            f"{render_relation(relation.left)} {relation.kind.value} "
            f"{render_relation(relation.right)}"
        )
        if relation.predicate is not None:
            rendered += f" ON {render_expression(relation.predicate)}"
        return rendered
    raise TypeError(f"unsupported relation node: {type(relation).__name__}")


def render_query_body(query: QueryBody) -> str:
    if isinstance(query, SelectQuery):
        hint = ""
        if query.optimizer_hint is not None:
            hint = f" /*+ {query.optimizer_hint} */"
        distinct = " DISTINCT" if query.distinct else ""
        rendered = "SELECT" + hint + distinct + " " + ", ".join(
            _render_projection(item) for item in query.projection
        )
        if query.source is not None:
            rendered += " FROM " + render_relation(query.source)
        if query.predicate is not None:
            rendered += " WHERE " + render_expression(query.predicate)
        if query.grouping:
            rendered += " GROUP BY " + ", ".join(
                render_expression(item) for item in query.grouping
            )
            if query.with_rollup:
                rendered += " WITH ROLLUP"
        if query.having is not None:
            rendered += " HAVING " + render_expression(query.having)
        if query.named_windows:
            rendered += " WINDOW " + ", ".join(
                f"{quote_identifier(window.name or '')} AS {_render_window_spec(window)}"
                for window in query.named_windows
            )
        return rendered
    if isinstance(query, TableQuery):
        return f"TABLE {quote_identifier(query.table)}"
    if isinstance(query, SetQuery):
        operator = query.operator.value + (" ALL" if query.all else "")
        return f" {operator} ".join(_render_set_branch(branch) for branch in query.branches)
    if isinstance(query, ValuesQuery):
        return "VALUES " + ", ".join(
            "ROW(" + ", ".join(render_expression(item) for item in row) + ")"
            for row in query.rows
        )
    if isinstance(query, ParenthesizedQuery):
        return f"({render_query_body(query.body)})"
    raise TypeError(f"unsupported query body: {type(query).__name__}")


def _render_set_branch(query: QueryBody) -> str:
    rendered = render_query_body(query)
    if isinstance(query, SetQuery):
        return f"({rendered})"
    return rendered


def render_query_ast(query: QueryAst) -> str:
    prefix = ""
    if query.ctes:
        recursive = " RECURSIVE" if query.recursive else ""
        ctes = []
        for cte in query.ctes:
            columns = ""
            if cte.columns:
                columns = " (" + ", ".join(quote_identifier(item) for item in cte.columns) + ")"
            ctes.append(
                f"{quote_identifier(cte.name)}{columns} AS ({render_query_body(cte.query)})"
            )
        prefix = f"WITH{recursive} " + ", ".join(ctes) + " "
    rendered = prefix + render_query_body(query.body)
    order = []
    for ordinal in query.order_by.ordinals:
        suffix = " DESC" if ordinal in query.order_by.descending else ""
        order.append(f"{ordinal}{suffix}")
    rendered += " ORDER BY " + ", ".join(order)
    if query.limit is not None:
        rendered += f" LIMIT {query.limit}"
        if query.offset is not None:
            rendered += f" OFFSET {query.offset}"
    return rendered


__all__ = [
    "quote_identifier",
    "quote_text",
    "render_expression",
    "render_query_ast",
    "render_query_body",
    "render_relation",
]
