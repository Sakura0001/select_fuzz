"""Typed, closed MySQL SELECT AST used by the deterministic query generator."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
import re


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_UNSIGNED_BIGINT_MAX = 2**64 - 1


def require_identifier(value: str, label: str = "identifier") -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} must be a snake_case identifier")


class SqlType(StrEnum):
    NUMERIC = "numeric"
    TEXT = "text"
    BINARY = "binary"
    TEMPORAL = "temporal"
    JSON = "json"
    SPATIAL = "spatial"
    BOOLEAN = "boolean"
    UNKNOWN = "unknown"


class BinaryOperator(StrEnum):
    EQ = "="
    NE = "<>"
    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="
    ADD = "+"
    SUBTRACT = "-"
    MULTIPLY = "*"
    MODULO = "%"
    AND = "AND"
    OR = "OR"
    LIKE = "LIKE"


class UnaryOperator(StrEnum):
    NOT = "NOT"
    IS_NULL = "IS NULL"
    IS_NOT_NULL = "IS NOT NULL"


class FunctionName(StrEnum):
    ABS = "ABS"
    COALESCE = "COALESCE"
    CONCAT = "CONCAT"
    COUNT = "COUNT"
    JSON_EXTRACT = "JSON_EXTRACT"
    JSON_OBJECT = "JSON_OBJECT"
    JSON_OVERLAPS = "JSON_OVERLAPS"
    JSON_SCHEMA_VALID = "JSON_SCHEMA_VALID"
    JSON_TYPE = "JSON_TYPE"
    JSON_VALUE = "JSON_VALUE"
    LOWER = "LOWER"
    MAX = "MAX"
    MIN = "MIN"
    OCTET_LENGTH = "OCTET_LENGTH"
    ST_ASBINARY = "ST_AsBinary"
    ST_GEOMFROMTEXT = "ST_GeomFromText"
    ST_ISVALID = "ST_IsValid"


class SubqueryOperator(StrEnum):
    SCALAR = "scalar"
    EXISTS = "exists"
    NOT_EXISTS = "not_exists"
    IN = "in"
    NOT_IN = "not_in"
    ANY = "any"
    ALL = "all"


class JoinKind(StrEnum):
    INNER = "INNER JOIN"
    LEFT = "LEFT JOIN"
    RIGHT = "RIGHT JOIN"
    CROSS = "CROSS JOIN"
    STRAIGHT = "STRAIGHT_JOIN"
    NATURAL_LEFT = "NATURAL LEFT JOIN"
    NATURAL_RIGHT = "NATURAL RIGHT JOIN"


class SetOperator(StrEnum):
    UNION = "UNION"
    INTERSECT = "INTERSECT"
    EXCEPT = "EXCEPT"


class ExpectedErrorKind(StrEnum):
    UNKNOWN_COLUMN = "unknown_column"
    SET_ARITY_MISMATCH = "set_arity_mismatch"
    INVALID_FUNCTION_ARITY = "invalid_function_arity"


class QueryBody:
    """Marker base for closed query-expression nodes."""


class Expression:
    """Marker base for closed expression nodes."""

    sql_type: SqlType


@dataclass(frozen=True, slots=True)
class ColumnRef(Expression):
    table_alias: str
    name: str
    sql_type: SqlType

    def __post_init__(self) -> None:
        require_identifier(self.table_alias, "column table alias")
        require_identifier(self.name, "column name")


@dataclass(frozen=True, slots=True)
class Literal(Expression):
    value: int | float | str | bytes | None
    sql_type: SqlType

    def __post_init__(self) -> None:
        if isinstance(self.value, bool):
            raise TypeError("boolean literals must be represented as integer 0 or 1")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("floating SQL literals must be finite")


@dataclass(frozen=True, slots=True)
class Star(Expression):
    table_alias: str | None = None
    sql_type: SqlType = SqlType.UNKNOWN

    def __post_init__(self) -> None:
        if self.table_alias is not None:
            require_identifier(self.table_alias, "star table alias")


@dataclass(frozen=True, slots=True)
class BinaryExpression(Expression):
    left: Expression
    operator: BinaryOperator
    right: Expression
    sql_type: SqlType


@dataclass(frozen=True, slots=True)
class UnaryExpression(Expression):
    operator: UnaryOperator
    operand: Expression
    sql_type: SqlType = SqlType.BOOLEAN


@dataclass(frozen=True, slots=True)
class FunctionCall(Expression):
    name: FunctionName
    args: tuple[Expression, ...]
    sql_type: SqlType
    distinct: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", tuple(self.args))
        if not isinstance(self.name, FunctionName):
            raise TypeError("function name must come from the deterministic allowlist")
        if not isinstance(self.distinct, bool):
            raise TypeError("distinct must be a boolean")
        arity = len(self.args)
        exact_arities = {
            FunctionName.ABS: 1,
            FunctionName.COUNT: 1,
            FunctionName.JSON_OVERLAPS: 2,
            FunctionName.JSON_SCHEMA_VALID: 2,
            FunctionName.JSON_TYPE: 1,
            FunctionName.JSON_VALUE: 2,
            FunctionName.LOWER: 1,
            FunctionName.MAX: 1,
            FunctionName.MIN: 1,
            FunctionName.OCTET_LENGTH: 1,
            FunctionName.ST_ASBINARY: 1,
            FunctionName.ST_GEOMFROMTEXT: 2,
            FunctionName.ST_ISVALID: 1,
        }
        if self.name in exact_arities and arity != exact_arities[self.name]:
            raise ValueError(f"{self.name.value} requires {exact_arities[self.name]} arguments")
        if self.name is FunctionName.COALESCE and arity < 2:
            raise ValueError("COALESCE requires at least two arguments")
        if self.name is FunctionName.CONCAT and arity < 1:
            raise ValueError("CONCAT requires at least one argument")
        if self.name is FunctionName.JSON_EXTRACT and arity < 2:
            raise ValueError("JSON_EXTRACT requires a document and at least one path")
        if self.name is FunctionName.JSON_OBJECT and arity % 2:
            raise ValueError("JSON_OBJECT requires key/value pairs")


@dataclass(frozen=True, slots=True)
class InvalidFunctionArity(Expression):
    """Closed negative-lane node; never used by the valid renderer path."""

    name: FunctionName
    args: tuple[Expression, ...] = ()
    sql_type: SqlType = SqlType.UNKNOWN

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", tuple(self.args))
        if self.name is not FunctionName.ABS or self.args:
            raise ValueError("the verified negative mutation is ABS() with zero arguments")


@dataclass(frozen=True, slots=True)
class FunctionalLowerExpression(Expression):
    """Exact expression used by the schema generator's LOWER functional key."""

    operand: ColumnRef
    cast_length: int
    sql_type: SqlType = SqlType.TEXT

    def __post_init__(self) -> None:
        if (
            not isinstance(self.cast_length, int)
            or isinstance(self.cast_length, bool)
            or self.cast_length <= 0
        ):
            raise ValueError("functional LOWER cast length must be positive")


@dataclass(frozen=True, slots=True)
class CastExpression(Expression):
    operand: Expression
    target: str
    sql_type: SqlType

    def __post_init__(self) -> None:
        if self.target not in {
            "SIGNED",
            "UNSIGNED",
            "CHAR(64)",
            "DECIMAL(65,30)",
            "DATETIME(6)",
            "JSON",
        }:
            raise ValueError("cast target is outside the closed MySQL 8.0.41 set")


@dataclass(frozen=True, slots=True)
class RowExpression(Expression):
    values: tuple[Expression, ...]
    sql_type: SqlType = SqlType.UNKNOWN

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", tuple(self.values))
        if not self.values:
            raise ValueError("row expression must not be empty")


@dataclass(frozen=True, slots=True)
class CaseExpression(Expression):
    base: Expression | None
    branches: tuple[tuple[Expression, Expression], ...]
    otherwise: Expression
    sql_type: SqlType

    def __post_init__(self) -> None:
        object.__setattr__(self, "branches", tuple(self.branches))
        if not self.branches:
            raise ValueError("CASE requires at least one branch")


@dataclass(frozen=True, slots=True)
class SubqueryExpression(Expression):
    operator: SubqueryOperator
    query: QueryBody
    left: Expression | None = None
    sql_type: SqlType = SqlType.BOOLEAN

    def __post_init__(self) -> None:
        needs_left = self.operator in {
            SubqueryOperator.IN,
            SubqueryOperator.NOT_IN,
            SubqueryOperator.ANY,
            SubqueryOperator.ALL,
        }
        if needs_left != (self.left is not None):
            raise ValueError("subquery operator has an invalid left operand")
        if self.operator is SubqueryOperator.SCALAR and self.sql_type is SqlType.BOOLEAN:
            raise ValueError("scalar subquery requires its projected SQL type")


@dataclass(frozen=True, slots=True)
class JsonMemberOf(Expression):
    member: Expression
    document: Expression
    path: str = "$[*]"
    sql_type: SqlType = SqlType.BOOLEAN

    def __post_init__(self) -> None:
        if self.path != "$[*]":
            raise ValueError("multivalue queries use the verified array wildcard path")


@dataclass(frozen=True, slots=True)
class MatchAgainst(Expression):
    columns: tuple[ColumnRef, ...]
    search: str
    boolean_mode: bool = True
    sql_type: SqlType = SqlType.NUMERIC

    def __post_init__(self) -> None:
        object.__setattr__(self, "columns", tuple(self.columns))
        if not self.columns:
            raise ValueError("MATCH requires at least one column")
        if not isinstance(self.boolean_mode, bool):
            raise TypeError("boolean_mode must be a boolean")


@dataclass(frozen=True, slots=True)
class WindowOrder:
    expressions: tuple[Expression, ...]
    proven_unique: bool
    max_rows: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "expressions", tuple(self.expressions))
        if not self.expressions:
            raise ValueError("window ordering must not be empty")
        if not isinstance(self.proven_unique, bool):
            raise TypeError("proven_unique must be a boolean")
        if self.max_rows is not None and (
            not isinstance(self.max_rows, int)
            or isinstance(self.max_rows, bool)
            or self.max_rows < 0
        ):
            raise ValueError("window max_rows must be a nonnegative integer")

    def proves_total_order(self) -> bool:
        return self.max_rows is not None and self.max_rows <= 1 or self.proven_unique


@dataclass(frozen=True, slots=True)
class WindowSpec:
    partition_by: tuple[Expression, ...]
    order: WindowOrder
    frame: tuple[int, int] | None = None
    name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "partition_by", tuple(self.partition_by))
        if self.name is not None:
            require_identifier(self.name, "window name")
        if self.frame is not None:
            preceding, following = self.frame
            if any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in (preceding, following)
            ):
                raise ValueError("window frame offsets must be nonnegative")


@dataclass(frozen=True, slots=True)
class WindowFunction(Expression):
    function: str
    argument: Expression | None
    window: WindowSpec | str
    sql_type: SqlType

    def __post_init__(self) -> None:
        if self.function not in {"ROW_NUMBER", "SUM"}:
            raise ValueError("unsupported deterministic window function")
        if self.function == "SUM" and self.argument is None:
            raise ValueError("SUM window function requires an argument")
        if self.function == "ROW_NUMBER" and self.argument is not None:
            raise ValueError("ROW_NUMBER does not accept an argument")
        if isinstance(self.window, str):
            require_identifier(self.window, "window reference")


@dataclass(frozen=True, slots=True)
class Projection:
    expression: Expression
    alias: str | None = None

    def __post_init__(self) -> None:
        if self.alias is not None:
            require_identifier(self.alias, "projection alias")


class Relation:
    """Marker base for closed relation nodes."""


@dataclass(frozen=True, slots=True)
class TableRelation(Relation):
    table: str
    alias: str
    partitions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.table, "table name")
        require_identifier(self.alias, "table alias")
        object.__setattr__(self, "partitions", tuple(self.partitions))
        for partition in self.partitions:
            require_identifier(partition, "partition name")


@dataclass(frozen=True, slots=True)
class NamedRelation(Relation):
    name: str
    alias: str

    def __post_init__(self) -> None:
        require_identifier(self.name, "named relation")
        require_identifier(self.alias, "named relation alias")


@dataclass(frozen=True, slots=True)
class JoinRelation(Relation):
    left: Relation
    right: Relation
    kind: JoinKind
    predicate: Expression | None = None

    def __post_init__(self) -> None:
        natural = self.kind in {JoinKind.NATURAL_LEFT, JoinKind.NATURAL_RIGHT}
        if natural and self.predicate is not None:
            raise ValueError("NATURAL JOIN cannot have an ON predicate")
        if self.kind not in {JoinKind.CROSS, JoinKind.NATURAL_LEFT, JoinKind.NATURAL_RIGHT}:
            if self.predicate is None:
                raise ValueError("non-natural, non-cross joins require an ON predicate")


@dataclass(frozen=True, slots=True)
class DerivedRelation(Relation):
    query: QueryBody
    alias: str
    lateral: bool = False
    columns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.alias, "derived table alias")
        if not isinstance(self.lateral, bool):
            raise TypeError("lateral must be a boolean")
        object.__setattr__(self, "columns", tuple(self.columns))
        for column in self.columns:
            require_identifier(column, "derived table column")
        if len({column.casefold() for column in self.columns}) != len(self.columns):
            raise ValueError("derived table columns must be unique")
        if self.columns:
            arity = _known_query_arity(self.query)
            if arity is None:
                raise ValueError("explicit derived columns require a known query arity")
            if len(self.columns) != arity:
                raise ValueError("derived table column count must match query arity")


@dataclass(frozen=True, slots=True)
class JsonTableRelation(Relation):
    source: Expression
    alias: str
    value_column: str = "json_value"
    ordinal_column: str = "json_ordinal"

    def __post_init__(self) -> None:
        require_identifier(self.alias, "JSON_TABLE alias")
        require_identifier(self.value_column, "JSON_TABLE value column")
        require_identifier(self.ordinal_column, "JSON_TABLE ordinality column")


@dataclass(frozen=True, slots=True)
class SelectQuery(QueryBody):
    projection: tuple[Projection, ...]
    source: Relation | None = None
    predicate: Expression | None = None
    grouping: tuple[Expression, ...] = ()
    with_rollup: bool = False
    having: Expression | None = None
    named_windows: tuple[WindowSpec, ...] = ()
    distinct: bool = False
    optimizer_hint: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "projection", tuple(self.projection))
        object.__setattr__(self, "grouping", tuple(self.grouping))
        object.__setattr__(self, "named_windows", tuple(self.named_windows))
        if not self.projection:
            raise ValueError("SELECT projection must not be empty")
        if self.with_rollup and not self.grouping:
            raise ValueError("WITH ROLLUP requires GROUP BY")
        if self.optimizer_hint is not None and self.optimizer_hint not in {
            "JOIN_ORDER(t, u)",
            "INDEX(t ix_id_desc)",
            "DERIVED_CONDITION_PUSHDOWN(d)",
            "NO_RANGE_OPTIMIZATION(t)",
            "NO_RANGE_OPTIMIZATION(t PRIMARY)",
        }:
            raise ValueError("optimizer hint is outside the closed verified set")


@dataclass(frozen=True, slots=True)
class TableQuery(QueryBody):
    """MySQL 8.0.19+ explicit TABLE query block."""

    table: str

    def __post_init__(self) -> None:
        require_identifier(self.table, "explicit table name")


@dataclass(frozen=True, slots=True)
class SetQuery(QueryBody):
    branches: tuple[QueryBody, ...]
    operator: SetOperator
    all: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "branches", tuple(self.branches))
        if len(self.branches) < 2:
            raise ValueError("set operation requires at least two branches")
        if self.operator in {SetOperator.INTERSECT, SetOperator.EXCEPT} and self.all:
            raise ValueError("MySQL 8.0.41 renderer uses DISTINCT INTERSECT/EXCEPT")


@dataclass(frozen=True, slots=True)
class ValuesQuery(QueryBody):
    rows: tuple[tuple[Expression, ...], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", tuple(tuple(row) for row in self.rows))
        if not self.rows or not self.rows[0]:
            raise ValueError("VALUES requires at least one nonempty row")
        arity = len(self.rows[0])
        if any(len(row) != arity for row in self.rows):
            raise ValueError("VALUES rows must have equal arity")


@dataclass(frozen=True, slots=True)
class ParenthesizedQuery(QueryBody):
    body: QueryBody
    order_by: tuple[int, ...] = ()
    descending: frozenset[int] = frozenset()
    limit: int | None = None
    offset: int | None = None
    unique_projection_sets: frozenset[frozenset[int]] = frozenset()
    max_rows: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "order_by", tuple(self.order_by))
        if self.order_by and self.limit is None:
            raise ValueError("local ORDER BY requires LIMIT")
        if len(set(self.order_by)) != len(self.order_by) or any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
            for value in self.order_by
        ):
            raise ValueError("local ORDER BY ordinals must be positive and unique")
        if any(not isinstance(value, int) or isinstance(value, bool) for value in self.descending):
            raise TypeError("local descending ordinals must be integers")
        if not self.descending <= set(self.order_by):
            raise ValueError("local descending ordinal is not ordered")
        arity = _known_query_arity(self.body)
        if self.order_by and (arity is None or any(value > arity for value in self.order_by)):
            raise ValueError("local ORDER BY ordinal exceeds known projection")
        legal = set() if arity is None else set(range(1, arity + 1))
        if any(
            not unique
            or any(not isinstance(value, int) or isinstance(value, bool) for value in unique)
            or not set(unique) <= legal
            for unique in self.unique_projection_sets
        ):
            raise ValueError("local unique projection set contains an invalid ordinal")
        if self.max_rows is not None and (
            not isinstance(self.max_rows, int)
            or isinstance(self.max_rows, bool)
            or self.max_rows < 0
        ):
            raise ValueError("local max_rows must be a nonnegative integer")
        if self.limit is not None and (
            not isinstance(self.limit, int) or isinstance(self.limit, bool) or self.limit < 0
        ):
            raise ValueError("local LIMIT must be a nonnegative integer")
        if self.limit is not None and self.limit > _UNSIGNED_BIGINT_MAX:
            raise ValueError("local LIMIT must fit an unsigned BIGINT")
        if self.limit is not None and self.limit > 0:
            ordered = frozenset(self.order_by)
            proven = self.max_rows is not None and self.max_rows <= 1
            proven = proven or any(unique <= ordered for unique in self.unique_projection_sets)
            if not proven:
                raise ValueError("local LIMIT requires a proven total order")
        if self.offset is not None:
            if not isinstance(self.offset, int) or isinstance(self.offset, bool):
                raise TypeError("local OFFSET must be an integer")
            if self.offset < 0:
                raise ValueError("local OFFSET must be nonnegative")
            if self.offset > _UNSIGNED_BIGINT_MAX:
                raise ValueError("local OFFSET must fit an unsigned BIGINT")
            if self.limit is None:
                raise ValueError("local OFFSET requires LIMIT")


def _known_query_arity(query: QueryBody) -> int | None:
    """Return an arity only when the closed AST proves it without schema lookup."""

    if isinstance(query, SelectQuery):
        return len(query.projection)
    if isinstance(query, ValuesQuery):
        return len(query.rows[0])
    if isinstance(query, ParenthesizedQuery):
        return _known_query_arity(query.body)
    if isinstance(query, SetQuery):
        arities = {_known_query_arity(branch) for branch in query.branches}
        if len(arities) == 1:
            return next(iter(arities))
    return None


@dataclass(frozen=True, slots=True)
class Cte:
    name: str
    columns: tuple[str, ...]
    query: QueryBody

    def __post_init__(self) -> None:
        require_identifier(self.name, "CTE name")
        object.__setattr__(self, "columns", tuple(self.columns))
        for column in self.columns:
            require_identifier(column, "CTE column")


@dataclass(frozen=True, slots=True)
class QueryScope:
    projection_count: int
    unique_projection_sets: frozenset[frozenset[int]] = frozenset()
    max_rows: int | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.projection_count, int)
            or isinstance(self.projection_count, bool)
            or self.projection_count <= 0
        ):
            raise ValueError("projection_count must be positive")
        legal = set(range(1, self.projection_count + 1))
        if any(
            not unique
            or any(not isinstance(value, int) or isinstance(value, bool) for value in unique)
            or not set(unique) <= legal
            for unique in self.unique_projection_sets
        ):
            raise ValueError("unique projection set contains an invalid ordinal")
        if self.max_rows is not None and (
            not isinstance(self.max_rows, int)
            or isinstance(self.max_rows, bool)
            or self.max_rows < 0
        ):
            raise ValueError("max_rows must be a nonnegative integer")


@dataclass(frozen=True, slots=True)
class OrderBy:
    ordinals: tuple[int, ...]
    descending: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "ordinals", tuple(self.ordinals))
        if not self.ordinals or len(set(self.ordinals)) != len(self.ordinals):
            raise ValueError("ORDER BY ordinals must be nonempty and unique")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
            for value in self.ordinals
        ):
            raise ValueError("ORDER BY ordinals must be positive")
        if any(not isinstance(value, int) or isinstance(value, bool) for value in self.descending):
            raise TypeError("descending ordinals must be integers")
        if not self.descending <= set(self.ordinals):
            raise ValueError("descending ordinal is not ordered")

    def proves_total_order(self, scope: QueryScope) -> bool:
        if any(value > scope.projection_count for value in self.ordinals):
            return False
        if scope.max_rows is not None and scope.max_rows <= 1:
            return True
        ordered = frozenset(self.ordinals)
        return any(unique <= ordered for unique in scope.unique_projection_sets)


@dataclass(frozen=True, slots=True)
class QueryAst:
    body: QueryBody
    order_by: OrderBy
    scope: QueryScope
    ctes: tuple[Cte, ...] = ()
    recursive: bool = False
    limit: int | None = None
    window_orders: tuple[WindowOrder, ...] = ()
    offset: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ctes", tuple(self.ctes))
        object.__setattr__(self, "window_orders", tuple(self.window_orders))
        if not isinstance(self.recursive, bool):
            raise TypeError("recursive must be a boolean")
        if self.recursive and not self.ctes:
            raise ValueError("WITH RECURSIVE requires a CTE")
        if self.limit is not None and (
            not isinstance(self.limit, int) or isinstance(self.limit, bool) or self.limit < 0
        ):
            raise ValueError("LIMIT must be a nonnegative integer")
        if self.limit is not None and self.limit > _UNSIGNED_BIGINT_MAX:
            raise ValueError("LIMIT must fit an unsigned BIGINT")
        if self.offset is not None:
            if not isinstance(self.offset, int) or isinstance(self.offset, bool):
                raise TypeError("OFFSET must be an integer")
            if self.offset < 0:
                raise ValueError("OFFSET must be nonnegative")
            if self.offset > _UNSIGNED_BIGINT_MAX:
                raise ValueError("OFFSET must fit an unsigned BIGINT")
            if self.limit is None:
                raise ValueError("OFFSET requires LIMIT")
        if (
            self.limit is not None
            and self.limit > 0
            and not self.order_by.proves_total_order(self.scope)
        ):
            raise ValueError("LIMIT requires a proven total order")
        if any(value > self.scope.projection_count for value in self.order_by.ordinals):
            raise ValueError("ORDER BY ordinal exceeds projection")
        if any(not order.proves_total_order() for order in self.window_orders):
            raise ValueError("window ordering requires a unique tie-breaker")

    @property
    def has_window(self) -> bool:
        return bool(self.window_orders)

    def window_orders_are_total(self) -> bool:
        return all(order.proves_total_order() for order in self.window_orders)


@dataclass(frozen=True, slots=True)
class ExpectedError:
    kind: ExpectedErrorKind
    expected_errno: int | None = None
    expected_sqlstate: str | None = None


__all__ = [
    "BinaryExpression",
    "BinaryOperator",
    "CaseExpression",
    "CastExpression",
    "ColumnRef",
    "Cte",
    "DerivedRelation",
    "ExpectedError",
    "ExpectedErrorKind",
    "Expression",
    "FunctionCall",
    "FunctionName",
    "FunctionalLowerExpression",
    "InvalidFunctionArity",
    "JsonMemberOf",
    "JsonTableRelation",
    "JoinKind",
    "JoinRelation",
    "Literal",
    "MatchAgainst",
    "NamedRelation",
    "OrderBy",
    "ParenthesizedQuery",
    "Projection",
    "QueryAst",
    "QueryBody",
    "QueryScope",
    "Relation",
    "RowExpression",
    "SelectQuery",
    "SetOperator",
    "SetQuery",
    "SqlType",
    "Star",
    "SubqueryExpression",
    "SubqueryOperator",
    "TableQuery",
    "TableRelation",
    "UnaryExpression",
    "UnaryOperator",
    "ValuesQuery",
    "WindowFunction",
    "WindowOrder",
    "WindowSpec",
    "require_identifier",
]
