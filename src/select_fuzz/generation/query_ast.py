"""Typed, closed MySQL SELECT AST used by the deterministic query generator."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from enum import StrEnum
import math
import re

from select_fuzz.generation.function_registry import (
    DETERMINISTIC_FUNCTION_SIGNATURES,
    DeterministicFunctionSignature,
)


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_SQLSTATE = re.compile(r"^[0-9A-Z]{5}$")
_OPTIMIZER_INDEX_HINT = re.compile(r"^INDEX\(t (?:PRIMARY|[a-z][a-z0-9_]*)\)$")
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
    NULL_SAFE_EQ = "<=>"
    NE = "<>"
    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="
    ADD = "+"
    SUBTRACT = "-"
    MULTIPLY = "*"
    DIVIDE = "/"
    INTEGER_DIVIDE = "DIV"
    MODULO = "%"
    BIT_AND = "&"
    BIT_OR = "|"
    BIT_XOR = "^"
    SHIFT_LEFT = "<<"
    SHIFT_RIGHT = ">>"
    AND = "AND"
    OR = "OR"
    XOR = "XOR"
    LIKE = "LIKE"


class UnaryOperator(StrEnum):
    NOT = "NOT"
    PLUS = "+"
    MINUS = "-"
    IS_NULL = "IS NULL"
    IS_NOT_NULL = "IS NOT NULL"
    IS_TRUE = "IS TRUE"
    IS_FALSE = "IS FALSE"
    IS_UNKNOWN = "IS UNKNOWN"
    IS_NOT_TRUE = "IS NOT TRUE"
    IS_NOT_FALSE = "IS NOT FALSE"
    IS_NOT_UNKNOWN = "IS NOT UNKNOWN"


class FunctionName(StrEnum):
    ABS = "ABS"
    AVG = "AVG"
    BIT_AND = "BIT_AND"
    BIT_OR = "BIT_OR"
    BIT_XOR = "BIT_XOR"
    COALESCE = "COALESCE"
    CONCAT = "CONCAT"
    COUNT = "COUNT"
    GROUPING = "GROUPING"
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
    REGEXP_LIKE = "REGEXP_LIKE"
    ST_ASBINARY = "ST_AsBinary"
    ST_GEOMFROMTEXT = "ST_GeomFromText"
    ST_ISVALID = "ST_IsValid"
    STDDEV_POP = "STDDEV_POP"
    STDDEV_SAMP = "STDDEV_SAMP"
    SUM = "SUM"
    VAR_POP = "VAR_POP"
    VAR_SAMP = "VAR_SAMP"


class SubqueryOperator(StrEnum):
    SCALAR = "scalar"
    EXISTS = "exists"
    NOT_EXISTS = "not_exists"
    IN = "in"
    NOT_IN = "not_in"
    ANY = "any"
    ALL = "all"


class JoinKind(StrEnum):
    COMMA = ","
    INNER = "INNER JOIN"
    LEFT = "LEFT JOIN"
    RIGHT = "RIGHT JOIN"
    CROSS = "CROSS JOIN"
    STRAIGHT = "STRAIGHT_JOIN"
    NATURAL_INNER = "NATURAL INNER JOIN"
    NATURAL_LEFT = "NATURAL LEFT JOIN"
    NATURAL_RIGHT = "NATURAL RIGHT JOIN"


class IndexHintAction(StrEnum):
    USE = "USE"
    FORCE = "FORCE"
    IGNORE = "IGNORE"


class IndexHintScope(StrEnum):
    JOIN = "JOIN"
    ORDER_BY = "ORDER BY"
    GROUP_BY = "GROUP BY"


class SetOperator(StrEnum):
    UNION = "UNION"
    INTERSECT = "INTERSECT"
    EXCEPT = "EXCEPT"


class SelectModifier(StrEnum):
    ALL = "ALL"
    DISTINCTROW = "DISTINCTROW"
    HIGH_PRIORITY = "HIGH_PRIORITY"
    STRAIGHT_JOIN = "STRAIGHT_JOIN"
    SQL_CALC_FOUND_ROWS = "SQL_CALC_FOUND_ROWS"
    SQL_NO_CACHE = "SQL_NO_CACHE"
    SQL_SMALL_RESULT = "SQL_SMALL_RESULT"
    SQL_BIG_RESULT = "SQL_BIG_RESULT"
    SQL_BUFFER_RESULT = "SQL_BUFFER_RESULT"


class WindowFrameUnit(StrEnum):
    ROWS = "ROWS"
    RANGE = "RANGE"


class WindowFrameBoundKind(StrEnum):
    UNBOUNDED_PRECEDING = "UNBOUNDED PRECEDING"
    PRECEDING = "PRECEDING"
    CURRENT_ROW = "CURRENT ROW"
    FOLLOWING = "FOLLOWING"
    UNBOUNDED_FOLLOWING = "UNBOUNDED FOLLOWING"


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
class BetweenExpression(Expression):
    value: Expression
    lower: Expression
    upper: Expression
    negated: bool = False
    sql_type: SqlType = SqlType.BOOLEAN

    def __post_init__(self) -> None:
        if not isinstance(self.negated, bool):
            raise TypeError("BETWEEN negated flag must be a boolean")


@dataclass(frozen=True, slots=True)
class InListExpression(Expression):
    value: Expression
    options: tuple[Expression, ...]
    negated: bool = False
    sql_type: SqlType = SqlType.BOOLEAN

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", tuple(self.options))
        if not self.options:
            raise ValueError("IN value list must not be empty")
        if not isinstance(self.negated, bool):
            raise TypeError("IN negated flag must be a boolean")


@dataclass(frozen=True, slots=True)
class LikeExpression(Expression):
    value: Expression
    pattern: Expression
    escape: str
    negated: bool = False
    sql_type: SqlType = SqlType.BOOLEAN

    def __post_init__(self) -> None:
        if not isinstance(self.escape, str) or len(self.escape) != 1:
            raise ValueError("LIKE ESCAPE must be exactly one character")
        if not isinstance(self.negated, bool):
            raise TypeError("LIKE negated flag must be a boolean")


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
            FunctionName.AVG: 1,
            FunctionName.BIT_AND: 1,
            FunctionName.BIT_OR: 1,
            FunctionName.BIT_XOR: 1,
            FunctionName.COUNT: 1,
            FunctionName.GROUPING: 1,
            FunctionName.JSON_OVERLAPS: 2,
            FunctionName.JSON_SCHEMA_VALID: 2,
            FunctionName.JSON_TYPE: 1,
            FunctionName.JSON_VALUE: 2,
            FunctionName.LOWER: 1,
            FunctionName.MAX: 1,
            FunctionName.MIN: 1,
            FunctionName.OCTET_LENGTH: 1,
            FunctionName.REGEXP_LIKE: 2,
            FunctionName.ST_ASBINARY: 1,
            FunctionName.ST_GEOMFROMTEXT: 2,
            FunctionName.ST_ISVALID: 1,
            FunctionName.STDDEV_POP: 1,
            FunctionName.STDDEV_SAMP: 1,
            FunctionName.SUM: 1,
            FunctionName.VAR_POP: 1,
            FunctionName.VAR_SAMP: 1,
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
class RegisteredFunctionCall(Expression):
    """A call whose exact signature comes from the deterministic registry."""

    signature: DeterministicFunctionSignature
    args: tuple[Expression, ...]
    sql_type: SqlType

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", tuple(self.args))
        if self.signature not in DETERMINISTIC_FUNCTION_SIGNATURES:
            raise ValueError("function signature is outside the deterministic registry")
        if len(self.args) != len(self.signature.arguments):
            raise ValueError("function arguments do not match the registered arity")
        if not isinstance(self.sql_type, SqlType):
            raise TypeError("registered function sql_type must be a SqlType")


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
class WindowFrameBound:
    kind: WindowFrameBoundKind
    offset: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, WindowFrameBoundKind):
            raise TypeError("window frame bound kind must be a WindowFrameBoundKind")
        offset_kind = self.kind in {
            WindowFrameBoundKind.PRECEDING,
            WindowFrameBoundKind.FOLLOWING,
        }
        if offset_kind:
            if (
                not isinstance(self.offset, int)
                or isinstance(self.offset, bool)
                or self.offset < 0
            ):
                raise ValueError("offset window frame bounds require a nonnegative integer")
        elif self.offset is not None:
            raise ValueError("non-offset window frame bounds cannot carry an offset")


@dataclass(frozen=True, slots=True)
class WindowFrame:
    start: WindowFrameBound
    end: WindowFrameBound

    def __post_init__(self) -> None:
        if not isinstance(self.start, WindowFrameBound) or not isinstance(
            self.end, WindowFrameBound
        ):
            raise TypeError("window frame endpoints must be WindowFrameBound nodes")
        if self.start.kind is WindowFrameBoundKind.UNBOUNDED_FOLLOWING:
            raise ValueError("window frame cannot start with UNBOUNDED FOLLOWING")
        if self.end.kind is WindowFrameBoundKind.UNBOUNDED_PRECEDING:
            raise ValueError("window frame cannot end with UNBOUNDED PRECEDING")
        if self.start.kind is WindowFrameBoundKind.FOLLOWING and self.end.kind in {
            WindowFrameBoundKind.PRECEDING,
            WindowFrameBoundKind.CURRENT_ROW,
        }:
            raise ValueError("window frame start must not follow its end")
        if (
            self.start.kind is WindowFrameBoundKind.CURRENT_ROW
            and self.end.kind is WindowFrameBoundKind.PRECEDING
        ):
            raise ValueError("window frame start must not follow its end")


@dataclass(frozen=True, slots=True)
class WindowSpec:
    partition_by: tuple[Expression, ...]
    order: WindowOrder
    frame: WindowFrame | tuple[int, int] | None = None
    name: str | None = None
    frame_unit: WindowFrameUnit = WindowFrameUnit.ROWS

    def __post_init__(self) -> None:
        object.__setattr__(self, "partition_by", tuple(self.partition_by))
        if any(not isinstance(expression, Expression) for expression in self.partition_by):
            raise TypeError("window partition items must be Expression nodes")
        if not isinstance(self.order, WindowOrder):
            raise TypeError("window order must be a WindowOrder")
        if self.name is not None:
            require_identifier(self.name, "window name")
        if not isinstance(self.frame_unit, WindowFrameUnit):
            raise TypeError("window frame unit must be a WindowFrameUnit")
        if isinstance(self.frame, tuple):
            if len(self.frame) != 2:
                raise ValueError("window frame offset pair must contain two values")
            preceding, following = self.frame
            if any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in (preceding, following)
            ):
                raise ValueError("window frame offsets must be nonnegative")
            object.__setattr__(
                self,
                "frame",
                WindowFrame(
                    WindowFrameBound(WindowFrameBoundKind.PRECEDING, preceding),
                    WindowFrameBound(WindowFrameBoundKind.FOLLOWING, following),
                ),
            )
        elif self.frame is not None and not isinstance(self.frame, WindowFrame):
            raise TypeError("window frame must be a WindowFrame")


@dataclass(frozen=True, slots=True)
class WindowFunction(Expression):
    function: str
    argument: Expression | None
    window: WindowSpec | str
    sql_type: SqlType
    extra_arguments: tuple[Expression, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.function, str):
            raise TypeError("window function name must be a string")
        if self.argument is not None and not isinstance(self.argument, Expression):
            raise TypeError("window function argument must be an Expression node")
        if not isinstance(self.sql_type, SqlType):
            raise TypeError("window function result type must be a SqlType")
        object.__setattr__(self, "extra_arguments", tuple(self.extra_arguments))
        if any(not isinstance(argument, Expression) for argument in self.extra_arguments):
            raise TypeError("window function arguments must be Expression nodes")
        no_argument = {
            "ROW_NUMBER",
            "RANK",
            "DENSE_RANK",
            "CUME_DIST",
            "PERCENT_RANK",
        }
        exact_one = {"SUM", "NTILE", "FIRST_VALUE", "LAST_VALUE"}
        variable = {"LAG", "LEAD"}
        exact_two = {"NTH_VALUE"}
        if self.function not in no_argument | exact_one | variable | exact_two:
            raise ValueError("unsupported deterministic window function")
        arity = (0 if self.argument is None else 1) + len(self.extra_arguments)
        if self.function in no_argument and arity:
            raise ValueError(f"{self.function} does not accept an argument")
        if self.function in exact_one and arity != 1:
            raise ValueError(f"{self.function} window function requires one argument")
        if self.function in exact_two and arity != 2:
            raise ValueError(f"{self.function} window function requires two arguments")
        if self.function in variable and arity not in {1, 2, 3}:
            raise ValueError(f"{self.function} window function requires one to three arguments")
        if isinstance(self.window, str):
            require_identifier(self.window, "window reference")
        elif not isinstance(self.window, WindowSpec):
            raise TypeError("window must be a WindowSpec or named window reference")


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
class IndexHint:
    action: IndexHintAction
    indexes: tuple[str, ...]
    scope: IndexHintScope | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, IndexHintAction):
            raise TypeError("index hint action must be an IndexHintAction")
        if self.scope is not None and not isinstance(self.scope, IndexHintScope):
            raise TypeError("index hint scope must be an IndexHintScope or None")
        object.__setattr__(self, "indexes", tuple(self.indexes))
        if self.action is not IndexHintAction.USE and not self.indexes:
            raise ValueError("FORCE/IGNORE INDEX requires at least one index")
        for index in self.indexes:
            if index != "PRIMARY":
                require_identifier(index, "index hint name")
        if len({index.casefold() for index in self.indexes}) != len(self.indexes):
            raise ValueError("index hint names must be unique")


@dataclass(frozen=True, slots=True)
class TableRelation(Relation):
    table: str
    alias: str
    partitions: tuple[str, ...] = ()
    index_hints: tuple[IndexHint, ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.table, "table name")
        require_identifier(self.alias, "table alias")
        object.__setattr__(self, "partitions", tuple(self.partitions))
        for partition in self.partitions:
            require_identifier(partition, "partition name")
        object.__setattr__(self, "index_hints", tuple(self.index_hints))
        for hint in self.index_hints:
            if not isinstance(hint, IndexHint):
                raise TypeError("table index hints must be IndexHint nodes")
        hint_keys = tuple((hint.action, hint.scope) for hint in self.index_hints)
        if len(set(hint_keys)) != len(hint_keys):
            raise ValueError("duplicate table index hint action/scope")


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
    using_columns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.left, Relation) or not isinstance(self.right, Relation):
            raise TypeError("join operands must be Relation nodes")
        if not isinstance(self.kind, JoinKind):
            raise TypeError("join kind must be a JoinKind")
        object.__setattr__(self, "using_columns", tuple(self.using_columns))
        for column in self.using_columns:
            require_identifier(column, "USING column")
        if len({column.casefold() for column in self.using_columns}) != len(self.using_columns):
            raise ValueError("USING columns must be unique")
        natural = self.kind in {
            JoinKind.NATURAL_INNER,
            JoinKind.NATURAL_LEFT,
            JoinKind.NATURAL_RIGHT,
        }
        if natural and (self.predicate is not None or self.using_columns):
            raise ValueError("NATURAL JOIN cannot have an ON or USING condition")
        if self.predicate is not None and self.using_columns:
            raise ValueError("JOIN cannot combine ON and USING conditions")
        if self.kind is JoinKind.COMMA and self.predicate is not None:
            raise ValueError("comma join cannot have an ON condition")
        if self.using_columns and self.kind not in {
            JoinKind.INNER,
            JoinKind.LEFT,
            JoinKind.RIGHT,
        }:
            raise ValueError("USING is only enabled for INNER/LEFT/RIGHT JOIN")
        conditionless = {
            JoinKind.COMMA,
            JoinKind.INNER,
            JoinKind.CROSS,
            JoinKind.NATURAL_INNER,
            JoinKind.NATURAL_LEFT,
            JoinKind.NATURAL_RIGHT,
        }
        if self.kind not in conditionless and self.predicate is None and not self.using_columns:
            raise ValueError("outer and straight joins require ON or USING")


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


def _relation_aliases(relation: Relation | None) -> frozenset[str]:
    if relation is None:
        return frozenset()
    if isinstance(
        relation,
        (TableRelation, NamedRelation, DerivedRelation, JsonTableRelation),
    ):
        return frozenset({relation.alias})
    if isinstance(relation, JoinRelation):
        return _relation_aliases(relation.left) | _relation_aliases(relation.right)
    return frozenset()


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
    modifiers: tuple[SelectModifier, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "projection", tuple(self.projection))
        object.__setattr__(self, "grouping", tuple(self.grouping))
        object.__setattr__(self, "named_windows", tuple(self.named_windows))
        object.__setattr__(self, "modifiers", tuple(self.modifiers))
        if not self.projection:
            raise ValueError("SELECT projection must not be empty")
        star_projections = tuple(
            projection
            for projection in self.projection
            if isinstance(projection.expression, Star)
        )
        if star_projections:
            if len(self.projection) != 1:
                raise ValueError("star projection must be the only SELECT item")
            if self.source is None:
                raise ValueError("star projection requires a FROM source")
            projection = star_projections[0]
            if projection.alias is not None:
                raise ValueError("star projection cannot have an alias")
            star = projection.expression
            assert isinstance(star, Star)
            if (
                star.table_alias is not None
                and star.table_alias not in _relation_aliases(self.source)
            ):
                raise ValueError("qualified star alias is not visible in the FROM source")
        if any(not isinstance(modifier, SelectModifier) for modifier in self.modifiers):
            raise TypeError("SELECT modifiers must come from the closed modifier set")
        if len(set(self.modifiers)) != len(self.modifiers):
            raise ValueError("SELECT modifiers must be unique")
        row_modes = {
            SelectModifier.ALL,
            SelectModifier.DISTINCTROW,
        }.intersection(self.modifiers)
        if len(row_modes) > 1 or (self.distinct and row_modes):
            raise ValueError("SELECT row multiplicity modifiers are mutually exclusive")
        if {
            SelectModifier.SQL_SMALL_RESULT,
            SelectModifier.SQL_BIG_RESULT,
        } <= set(self.modifiers):
            raise ValueError("SQL_SMALL_RESULT and SQL_BIG_RESULT are mutually exclusive")
        if self.with_rollup and not self.grouping:
            raise ValueError("WITH ROLLUP requires GROUP BY")
        if self.optimizer_hint is not None:
            static_hints = {
                "JOIN_ORDER(t, u)",
                "DERIVED_CONDITION_PUSHDOWN(d)",
                "NO_RANGE_OPTIMIZATION(t)",
                "NO_RANGE_OPTIMIZATION(t PRIMARY)",
            }
            if (
                self.optimizer_hint not in static_hints
                and _OPTIMIZER_INDEX_HINT.fullmatch(self.optimizer_hint) is None
            ):
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
        if not isinstance(self.operator, SetOperator):
            raise TypeError("set operator must be a SetOperator")
        if not isinstance(self.all, bool):
            raise TypeError("set ALL flag must be a boolean")


@dataclass(frozen=True, slots=True)
class SetOperation:
    operator: SetOperator
    query: QueryBody
    all: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.operator, SetOperator):
            raise TypeError("mixed set operator must be a SetOperator")
        if not isinstance(self.query, QueryBody):
            raise TypeError("mixed set operand must be a QueryBody")
        if not isinstance(self.all, bool):
            raise TypeError("mixed set ALL flag must be a boolean")


@dataclass(frozen=True, slots=True)
class MixedSetQuery(QueryBody):
    first: QueryBody
    operations: tuple[SetOperation, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.first, QueryBody):
            raise TypeError("mixed set first operand must be a QueryBody")
        object.__setattr__(self, "operations", tuple(self.operations))
        if len(self.operations) < 2:
            raise ValueError("mixed set query requires at least three operands")
        if any(not isinstance(operation, SetOperation) for operation in self.operations):
            raise TypeError("mixed set operations must be SetOperation nodes")
        if len({operation.operator for operation in self.operations}) < 2:
            raise ValueError("mixed set query requires at least two operator kinds")


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
    if isinstance(query, MixedSetQuery):
        arities = {
            _known_query_arity(query.first),
            *(_known_query_arity(operation.query) for operation in query.operations),
        }
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
    aliases: tuple[str, ...] = ()
    expressions: tuple[Expression, ...] = ()
    projection_ordinals: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "ordinals", tuple(self.ordinals))
        object.__setattr__(self, "aliases", tuple(self.aliases))
        object.__setattr__(self, "expressions", tuple(self.expressions))
        object.__setattr__(self, "projection_ordinals", tuple(self.projection_ordinals))
        forms = sum(bool(values) for values in (self.ordinals, self.aliases, self.expressions))
        if forms != 1:
            raise ValueError("ORDER BY requires exactly one reference form")
        if self.ordinals:
            if len(set(self.ordinals)) != len(self.ordinals):
                raise ValueError("ORDER BY ordinals must be unique")
            if any(
                not isinstance(value, int) or isinstance(value, bool) or value <= 0
                for value in self.ordinals
            ):
                raise ValueError("ORDER BY ordinals must be positive")
            if self.projection_ordinals:
                raise ValueError("ordinal ORDER BY does not accept proof mappings")
        else:
            if self.descending:
                raise ValueError("non-ordinal ORDER BY currently supports ascending order only")
            reference_count = len(self.aliases or self.expressions)
            if len(self.projection_ordinals) != reference_count:
                raise ValueError("ORDER BY proof mappings must match reference count")
            if len(set(self.projection_ordinals)) != len(self.projection_ordinals) or any(
                not isinstance(value, int) or isinstance(value, bool) or value <= 0
                for value in self.projection_ordinals
            ):
                raise ValueError("ORDER BY proof mappings must be positive and unique")
        for alias in self.aliases:
            require_identifier(alias, "ORDER BY alias")
        if len({alias.casefold() for alias in self.aliases}) != len(self.aliases):
            raise ValueError("ORDER BY aliases must be unique")
        if any(not isinstance(expression, Expression) for expression in self.expressions):
            raise TypeError("ORDER BY expressions must be Expression nodes")
        if any(isinstance(expression, Star) for expression in self.expressions):
            raise ValueError("ORDER BY does not accept star expressions")
        if any(not isinstance(value, int) or isinstance(value, bool) for value in self.descending):
            raise TypeError("descending ordinals must be integers")
        if not self.descending <= set(self.ordinals):
            raise ValueError("descending ordinal is not ordered")

    @property
    def proof_ordinals(self) -> tuple[int, ...]:
        return self.ordinals or self.projection_ordinals

    def proves_total_order(self, scope: QueryScope) -> bool:
        if any(value > scope.projection_count for value in self.proof_ordinals):
            return False
        if scope.max_rows is not None and scope.max_rows <= 1:
            return True
        ordered = frozenset(self.proof_ordinals)
        return any(unique <= ordered for unique in scope.unique_projection_sets)


def _value_contains_nested_buffer_result(value: object) -> bool:
    if isinstance(value, QueryBody):
        return _query_contains_illegal_buffer_result(value, outermost=False)
    if isinstance(value, (tuple, list, frozenset)):
        return any(_value_contains_nested_buffer_result(item) for item in value)
    if is_dataclass(value) and not isinstance(value, type):
        return any(
            _value_contains_nested_buffer_result(getattr(value, field.name))
            for field in fields(value)
        )
    return False


def _query_contains_illegal_buffer_result(
    query: QueryBody,
    *,
    outermost: bool,
) -> bool:
    if isinstance(query, SelectQuery):
        if SelectModifier.SQL_BUFFER_RESULT in query.modifiers and not outermost:
            return True
        return any(
            _value_contains_nested_buffer_result(getattr(query, field.name))
            for field in fields(query)
            if field.name != "modifiers"
        )
    if isinstance(query, ParenthesizedQuery):
        if _query_contains_illegal_buffer_result(query.body, outermost=outermost):
            return True
        return any(
            _value_contains_nested_buffer_result(value)
            for value in (
                query.order_by,
                query.descending,
                query.unique_projection_sets,
            )
        )
    if isinstance(query, SetQuery):
        return any(
            _query_contains_illegal_buffer_result(branch, outermost=False)
            for branch in query.branches
        )
    if isinstance(query, MixedSetQuery):
        return _query_contains_illegal_buffer_result(
            query.first,
            outermost=False,
        ) or any(
            _query_contains_illegal_buffer_result(operation.query, outermost=False)
            for operation in query.operations
        )
    return False


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
        if _query_contains_illegal_buffer_result(self.body, outermost=True) or any(
            _query_contains_illegal_buffer_result(cte.query, outermost=False)
            for cte in self.ctes
        ):
            raise ValueError("SQL_BUFFER_RESULT is allowed only in the outermost query block")
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
        if any(value > self.scope.projection_count for value in self.order_by.proof_ordinals):
            raise ValueError("ORDER BY ordinal exceeds projection")
        if self.order_by.aliases:
            if not isinstance(self.body, SelectQuery):
                raise ValueError("ORDER BY alias requires a SELECT query body")
            for alias, ordinal in zip(
                self.order_by.aliases,
                self.order_by.projection_ordinals,
                strict=True,
            ):
                if ordinal > len(self.body.projection):
                    raise ValueError("ORDER BY alias proof exceeds the SELECT projection")
                if (
                    sum(
                        projection.alias is not None
                        and projection.alias.casefold() == alias.casefold()
                        for projection in self.body.projection
                    )
                    != 1
                ):
                    raise ValueError("ORDER BY alias must identify one projection")
                if self.body.projection[ordinal - 1].alias != alias:
                    raise ValueError("ORDER BY alias proof does not match the projection")
        if self.order_by.expressions:
            if not isinstance(self.body, SelectQuery):
                raise ValueError("ORDER BY expression requires a SELECT query body")
            for expression, ordinal in zip(
                self.order_by.expressions,
                self.order_by.projection_ordinals,
                strict=True,
            ):
                if ordinal > len(self.body.projection):
                    raise ValueError("ORDER BY expression proof exceeds the SELECT projection")
                if self.body.projection[ordinal - 1].expression != expression:
                    raise ValueError("ORDER BY expression proof does not match the projection")
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
    expected_errno: int
    expected_sqlstate: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ExpectedErrorKind):
            raise TypeError("kind must be an ExpectedErrorKind")
        if (
            not isinstance(self.expected_errno, int)
            or isinstance(self.expected_errno, bool)
            or not 0 <= self.expected_errno <= 0xFFFF
        ):
            raise ValueError("expected_errno must be an unsigned 16-bit integer")
        if (
            not isinstance(self.expected_sqlstate, str)
            or _SQLSTATE.fullmatch(self.expected_sqlstate) is None
        ):
            raise ValueError(
                "expected_sqlstate must contain five uppercase alphanumeric characters"
            )


__all__ = [
    "BetweenExpression",
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
    "IndexHint",
    "IndexHintAction",
    "IndexHintScope",
    "InListExpression",
    "InvalidFunctionArity",
    "JsonMemberOf",
    "JsonTableRelation",
    "JoinKind",
    "JoinRelation",
    "Literal",
    "LikeExpression",
    "MatchAgainst",
    "MixedSetQuery",
    "NamedRelation",
    "OrderBy",
    "ParenthesizedQuery",
    "Projection",
    "QueryAst",
    "QueryBody",
    "QueryScope",
    "RegisteredFunctionCall",
    "Relation",
    "RowExpression",
    "SelectModifier",
    "SelectQuery",
    "SetOperator",
    "SetOperation",
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
    "WindowFrame",
    "WindowFrameBound",
    "WindowFrameBoundKind",
    "WindowFrameUnit",
    "WindowOrder",
    "WindowSpec",
    "require_identifier",
]
