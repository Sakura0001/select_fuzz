"""Grammar-driven, schema-aware MySQL 8.0.22 SELECT candidate generation.

The grammar owns structural randomness.  Semantic hooks are deliberately small:
they bind real tables/columns, maintain nested query scopes, and create deterministic
literal values.  They do not choose whole query shapes except where a SQL construct
needs a dependency-ordered scope transition (derived tables and CTEs).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from importlib import resources
from pathlib import Path
import random
import re
from types import MappingProxyType
from typing import Mapping

from select_fuzz.generation.function_registry import (
    DETERMINISTIC_FUNCTION_SIGNATURES,
    DeterministicFunctionSignature,
    FunctionArgument,
    FunctionResult,
)
from select_fuzz.generation.query_safety import ReadOnlyValidator, UnsafeQuery
from select_fuzz.generation.pq_eligibility import (
    PQ_AGGREGATES,
    PQ_SCALAR_FUNCTIONS,
    PqEligibilityValidator,
    PqIneligible,
    is_pq_supported_type,
)
from select_fuzz.generation.schema import SchemaManifest


_PRODUCTION_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_TOKEN = re.compile(
    r"'(?:''|\\.|[^'])*'|\"(?:\\.|[^\"])*\"|"
    r"<=>|<=|>=|<>|!=|<<|>>|&&|\|\||[(),.]|[^\s(),.]+"
)


class GrammarError(ValueError):
    """The SELECT grammar is malformed or cannot produce a candidate."""


class CandidateRejected(GrammarError):
    """One derivation cannot satisfy its current schema/scope."""

    def __init__(
        self,
        message: str,
        *,
        candidate: CandidateQuery | None = None,
    ) -> None:
        super().__init__(message)
        self.candidate = candidate


class _ExpansionBudgetExceeded(CandidateRejected):
    """One candidate exhausted its bounded grammar-expansion work."""


class Multiplicity(StrEnum):
    ONE = "one"
    OPTIONAL = "optional"
    ZERO_OR_MORE = "zero_or_more"
    ONE_OR_MORE = "one_or_more"


@dataclass(frozen=True, slots=True)
class GrammarSymbol:
    value: str
    multiplicity: Multiplicity = Multiplicity.ONE


@dataclass(frozen=True, slots=True)
class GrammarAlternative:
    symbols: tuple[GrammarSymbol, ...]
    source_line: int


@dataclass(frozen=True, slots=True)
class GrammarProduction:
    name: str
    alternatives: tuple[GrammarAlternative, ...]


def _strip_comment(line: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote is not None:
            escaped = True
            continue
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "#":
            return line[:index]
    return line


def _parse_symbol(raw: str, *, line: int) -> GrammarSymbol:
    multiplicity = Multiplicity.ONE
    if (
        len(raw) > 1
        and raw[-1] in "?*+"
        and (_PRODUCTION_NAME.fullmatch(raw[:-1]) is not None or raw.startswith("_"))
    ):
        suffix = raw[-1]
        raw = raw[:-1]
        multiplicity = {
            "?": Multiplicity.OPTIONAL,
            "*": Multiplicity.ZERO_OR_MORE,
            "+": Multiplicity.ONE_OR_MORE,
        }[suffix]
    if not raw:
        raise GrammarError(f"empty grammar symbol on line {line}")
    return GrammarSymbol(raw, multiplicity)


class SelectGrammar:
    """Parsed `.grammar.yy` productions; duplicate alternatives are weights."""

    def __init__(
        self,
        productions: Mapping[str, GrammarProduction],
        *,
        source_text: str,
        root: str = "query",
    ) -> None:
        normalized = dict(productions)
        if root not in normalized:
            raise GrammarError(f"root production {root!r} does not exist")
        self.productions = MappingProxyType(normalized)
        self.root = root
        self.sha256 = sha256(source_text.encode("utf-8")).hexdigest()
        self._minimum_depth = self._compute_minimum_depths()

    @classmethod
    def from_text(cls, text: str, *, root: str = "query") -> SelectGrammar:
        if not isinstance(text, str) or not text.strip():
            raise GrammarError("grammar text must not be empty")
        alternatives: dict[str, list[GrammarAlternative]] = {}
        current: str | None = None
        for line_number, original in enumerate(text.splitlines(), start=1):
            content = _strip_comment(original).strip()
            if not content:
                continue
            if content.endswith(":") and not content.startswith("|"):
                name = content[:-1].strip()
                if _PRODUCTION_NAME.fullmatch(name) is None:
                    raise GrammarError(f"invalid production name on line {line_number}: {name}")
                if name in alternatives:
                    raise GrammarError(f"duplicate production {name!r} on line {line_number}")
                alternatives[name] = []
                current = name
                continue
            if current is None:
                raise GrammarError(f"alternative without production on line {line_number}")
            if content.startswith("|"):
                content = content[1:].strip()
            if not content:
                raise GrammarError(f"empty alternative on line {line_number}")
            symbols = tuple(
                _parse_symbol(token, line=line_number) for token in _TOKEN.findall(content)
            )
            if not symbols:
                raise GrammarError(f"empty alternative on line {line_number}")
            alternatives[current].append(GrammarAlternative(symbols, line_number))
        missing = [name for name, values in alternatives.items() if not values]
        if missing:
            raise GrammarError(f"productions have no alternatives: {sorted(missing)}")
        productions = {
            name: GrammarProduction(name, tuple(values)) for name, values in alternatives.items()
        }
        return cls(productions, source_text=text, root=root)

    @classmethod
    def from_path(cls, path: str | Path, *, root: str = "query") -> SelectGrammar:
        return cls.from_text(Path(path).read_text(encoding="utf-8"), root=root)

    @classmethod
    def default(cls) -> SelectGrammar:
        packaged = resources.files("select_fuzz").joinpath("data", "mysql-8.0.22-select.grammar.yy")
        if packaged.is_file():
            with resources.as_file(packaged) as grammar_path:
                return cls.from_path(grammar_path)
        checkout = (
            Path(__file__).resolve().parents[3] / "catalog" / "mysql-8.0.22-select.grammar.yy"
        )
        if not checkout.is_file():
            raise GrammarError("canonical MySQL 8.0.22 SELECT grammar is unavailable")
        return cls.from_path(checkout)

    def _compute_minimum_depths(self) -> Mapping[str, int]:
        infinity = 1_000_000
        depths = {name: infinity for name in self.productions}
        changed = True
        while changed:
            changed = False
            for name, production in self.productions.items():
                candidate_depths: list[int] = []
                for alternative in production.alternatives:
                    children = [
                        depths[symbol.value]
                        for symbol in alternative.symbols
                        if symbol.value in self.productions
                    ]
                    if all(depth < infinity for depth in children):
                        candidate_depths.append(1 + (max(children) if children else 0))
                candidate = min(candidate_depths, default=infinity)
                if candidate < depths[name]:
                    depths[name] = candidate
                    changed = True
        unproductive = sorted(name for name, depth in depths.items() if depth >= infinity)
        if unproductive:
            raise GrammarError(f"productions cannot terminate: {unproductive}")
        return MappingProxyType(depths)

    def alternative_minimum_depth(self, alternative: GrammarAlternative) -> int:
        children = [
            self._minimum_depth[symbol.value]
            for symbol in alternative.symbols
            if symbol.value in self.productions
        ]
        return 1 + (max(children) if children else 0)

    def stable_alternative_id(self, trace_entry: str) -> str:
        """Return a line-number-independent coverage identity for one trace entry."""

        production_name, separator, raw_line = trace_entry.rpartition("@")
        if not separator or production_name not in self.productions:
            raise GrammarError(f"invalid production trace entry: {trace_entry!r}")
        try:
            source_line = int(raw_line)
        except ValueError as error:
            raise GrammarError(f"invalid production trace source line: {trace_entry!r}") from error
        alternative = next(
            (
                candidate
                for candidate in self.productions[production_name].alternatives
                if candidate.source_line == source_line
            ),
            None,
        )
        if alternative is None:
            raise GrammarError(f"trace alternative no longer exists: {trace_entry!r}")
        normalized = "\x1f".join(
            f"{symbol.value}\x1e{symbol.multiplicity.value}" for symbol in alternative.symbols
        )
        digest = sha256(f"{production_name}\x00{normalized}".encode("utf-8")).hexdigest()[:16]
        return f"v1:{production_name}:{digest}"


class TypeFamily(StrEnum):
    ANY = "any"
    NUMERIC = "numeric"
    TEXT = "text"
    TEMPORAL = "temporal"
    BINARY = "binary"
    JSON = "json"
    SPATIAL = "spatial"


class FunctionValueProfile(StrEnum):
    """Deterministic value fixtures used to exercise function input domains."""

    NORMAL = "normal"
    BOUNDARY = "boundary"
    SPECIAL = "special"


_NUMERIC_TYPES = frozenset(
    {
        "TINYINT",
        "SMALLINT",
        "MEDIUMINT",
        "INT",
        "INTEGER",
        "BIGINT",
        "BOOL",
        "BOOLEAN",
        "BIT",
        "DECIMAL",
        "NUMERIC",
        "FLOAT",
        "DOUBLE",
        "REAL",
        "YEAR",
    }
)
_TEXT_TYPES = frozenset(
    {
        "CHAR",
        "VARCHAR",
        "NCHAR",
        "NVARCHAR",
        "TINYTEXT",
        "TEXT",
        "MEDIUMTEXT",
        "LONGTEXT",
        "ENUM",
        "SET",
    }
)
_TEMPORAL_TYPES = frozenset({"DATE", "TIME", "DATETIME", "TIMESTAMP"})
_BINARY_TYPES = frozenset({"BINARY", "VARBINARY", "TINYBLOB", "BLOB", "MEDIUMBLOB", "LONGBLOB"})
_SPATIAL_TYPES = frozenset(
    {
        "GEOMETRY",
        "POINT",
        "LINESTRING",
        "POLYGON",
        "MULTIPOINT",
        "MULTILINESTRING",
        "MULTIPOLYGON",
        "GEOMETRYCOLLECTION",
    }
)


def _family(mysql_type: str) -> TypeFamily:
    base = mysql_type.split("(", 1)[0].split(" ", 1)[0].upper()
    if base in _NUMERIC_TYPES:
        return TypeFamily.NUMERIC
    if base in _TEXT_TYPES:
        return TypeFamily.TEXT
    if base in _TEMPORAL_TYPES:
        return TypeFamily.TEMPORAL
    if base in _BINARY_TYPES:
        return TypeFamily.BINARY
    if base == "JSON":
        return TypeFamily.JSON
    if base in _SPATIAL_TYPES:
        return TypeFamily.SPATIAL
    return TypeFamily.ANY


@dataclass(frozen=True, slots=True)
class GrammarColumn:
    name: str
    mysql_type: str
    generated: bool = False

    @property
    def family(self) -> TypeFamily:
        return _family(self.mysql_type)

    @property
    def pq_supported(self) -> bool:
        return is_pq_supported_type(self.mysql_type, generated=self.generated)


@dataclass(frozen=True, slots=True)
class GrammarTable:
    name: str
    columns: tuple[GrammarColumn, ...]
    indexes: tuple[str, ...] = ()
    partitions: tuple[str, ...] = ()
    engine: str = "InnoDB"
    temporary: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "columns", tuple(self.columns))
        object.__setattr__(self, "indexes", tuple(self.indexes))
        object.__setattr__(self, "partitions", tuple(self.partitions))
        if not self.name or not self.columns:
            raise ValueError("grammar tables require a name and columns")
        if any(not name for name in (*self.indexes, *self.partitions)):
            raise ValueError("grammar index and partition names must not be empty")


@dataclass(frozen=True, slots=True)
class GrammarSchema:
    tables: tuple[GrammarTable, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "tables", tuple(self.tables))
        if not self.tables:
            raise ValueError("grammar schema requires at least one table")

    @classmethod
    def from_manifest(cls, manifest: SchemaManifest) -> GrammarSchema:
        return cls(
            tuple(
                GrammarTable(
                    table.name,
                    tuple(
                        GrammarColumn(column.name, column.mysql_type) for column in table.columns
                    ),
                    tuple(
                        index.name
                        for index in table.indexes
                        if index.visible
                        and index.kind.value not in {"fulltext", "spatial", "multivalue"}
                        and all(
                            part.column_name is not None
                            and is_pq_supported_type(table.column(part.column_name).mysql_type)
                            for part in index.parts
                        )
                    ),
                    (
                        ()
                        if table.partition is None
                        else tuple(f"p{index}" for index in range(table.partition.partitions))
                    ),
                    engine=table.engine,
                    temporary=table.temporary,
                )
                for table in manifest.tables
            )
        )


@dataclass(frozen=True, slots=True)
class GrammarQueryConfig:
    compatible_type_percent: int = 80
    max_expansion_depth: int = 12
    max_expansion_steps: int = 1_000
    max_repeat: int = 2
    max_tables_per_query_block: int = 4
    correlated_column_percent: int = 20
    function_value_profile: FunctionValueProfile = FunctionValueProfile.NORMAL

    def __post_init__(self) -> None:
        for name in ("compatible_type_percent", "correlated_column_percent"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100:
                raise ValueError(f"{name} must be an integer from 0 to 100")
        if not isinstance(self.function_value_profile, FunctionValueProfile):
            raise ValueError("function_value_profile must be a FunctionValueProfile")
        for name in (
            "max_expansion_depth",
            "max_expansion_steps",
            "max_repeat",
            "max_tables_per_query_block",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class CandidateQuery:
    sql: str
    seed: int
    grammar_hash: str
    production_trace: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.sql.strip():
            raise ValueError("candidate SQL must not be empty")
        object.__setattr__(self, "production_trace", tuple(self.production_trace))

    @property
    def grammar_sha256(self) -> str:
        """Backward-compatible descriptive alias for the SHA-256 grammar hash."""

        return self.grammar_hash


@dataclass(frozen=True, slots=True)
class _ColumnBinding:
    relation_alias: str
    column: GrammarColumn
    strict_compatible: bool = True

    @property
    def identity(self) -> tuple[str, str]:
        """Stable identity for one column as it crosses nested query scopes."""

        return (self.relation_alias, self.column.name)

    def render(self) -> str:
        return f"{_quote_identifier(self.relation_alias)}.{_quote_identifier(self.column.name)}"


@dataclass(slots=True)
class _QueryScope:
    outer_columns: list[_ColumnBinding] = field(default_factory=list)
    local_columns: list[_ColumnBinding] = field(default_factory=list)
    table_aliases: list[str] = field(default_factory=list)
    table_indexes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    derived_aliases: list[str] = field(default_factory=list)
    projection_columns: list[GrammarColumn] = field(default_factory=list)
    output_columns: list[GrammarColumn] = field(default_factory=list)
    prepared_relation: str | None = None
    group_column: _ColumnBinding | None = None
    group_columns: list[_ColumnBinding] = field(default_factory=list)
    last_value_family: TypeFamily = TypeFamily.ANY
    selected_outer_bindings: set[tuple[str, str]] = field(default_factory=set)
    blocked_outer_bindings: set[tuple[str, str]] = field(default_factory=set)

    @property
    def visible_columns(self) -> list[_ColumnBinding]:
        return [*self.local_columns, *self.outer_columns]


@dataclass(slots=True)
class _PendingCte:
    name: str
    columns: tuple[GrammarColumn, ...]
    body_sql: str | None = None


@dataclass(frozen=True, slots=True)
class _CteBinding:
    name: str
    columns: tuple[GrammarColumn, ...]
    body_sql: str


@dataclass(slots=True)
class _CteFrame:
    bindings: list[_CteBinding] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _QueryResult:
    columns: tuple[GrammarColumn, ...]


@dataclass(frozen=True, slots=True)
class _SetSignature:
    columns: tuple[GrammarColumn, ...]


@dataclass(frozen=True, slots=True)
class _GenerationSnapshot:
    scopes: list[_QueryScope]
    trace: list[str]
    relation_alias_counter: int
    cte_counter: int
    last_completed_scope: _QueryScope | None
    pending_cte: _PendingCte | None
    last_query_result: _QueryResult | None
    set_signatures: list[_SetSignature]
    cte_frames: list[_CteFrame]


def _clone_query_scope(scope: _QueryScope | None) -> _QueryScope | None:
    if scope is None:
        return None
    return _QueryScope(
        outer_columns=list(scope.outer_columns),
        local_columns=list(scope.local_columns),
        table_aliases=list(scope.table_aliases),
        table_indexes=dict(scope.table_indexes),
        derived_aliases=list(scope.derived_aliases),
        projection_columns=list(scope.projection_columns),
        output_columns=list(scope.output_columns),
        prepared_relation=scope.prepared_relation,
        group_column=scope.group_column,
        group_columns=list(scope.group_columns),
        last_value_family=scope.last_value_family,
        selected_outer_bindings=set(scope.selected_outer_bindings),
        blocked_outer_bindings=set(scope.blocked_outer_bindings),
    )


def _clone_pending_cte(pending: _PendingCte | None) -> _PendingCte | None:
    if pending is None:
        return None
    return _PendingCte(pending.name, pending.columns, pending.body_sql)


def _clone_cte_frames(frames: list[_CteFrame]) -> list[_CteFrame]:
    return [_CteFrame(list(frame.bindings)) for frame in frames]


@dataclass(slots=True)
class _GenerationContext:
    schema: GrammarSchema
    rng: random.Random
    config: GrammarQueryConfig
    excluded_families: frozenset[str] = frozenset()
    scopes: list[_QueryScope] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)
    relation_alias_counter: int = 0
    cte_counter: int = 0
    expansion_steps: int = 0
    last_completed_scope: _QueryScope | None = None
    pending_cte: _PendingCte | None = None
    last_query_result: _QueryResult | None = None
    set_signatures: list[_SetSignature] = field(default_factory=list)
    cte_frames: list[_CteFrame] = field(default_factory=list)

    @property
    def scope(self) -> _QueryScope:
        if not self.scopes:
            raise CandidateRejected("semantic token requires an active query scope")
        return self.scopes[-1]

    def snapshot(self) -> _GenerationSnapshot:
        return _GenerationSnapshot(
            scopes=[
                cloned for scope in self.scopes if (cloned := _clone_query_scope(scope)) is not None
            ],
            trace=list(self.trace),
            relation_alias_counter=self.relation_alias_counter,
            cte_counter=self.cte_counter,
            last_completed_scope=_clone_query_scope(self.last_completed_scope),
            pending_cte=_clone_pending_cte(self.pending_cte),
            last_query_result=self.last_query_result,
            set_signatures=list(self.set_signatures),
            cte_frames=_clone_cte_frames(self.cte_frames),
        )

    def restore(self, snapshot: _GenerationSnapshot) -> None:
        self.scopes = [
            cloned for scope in snapshot.scopes if (cloned := _clone_query_scope(scope)) is not None
        ]
        self.trace = list(snapshot.trace)
        self.relation_alias_counter = snapshot.relation_alias_counter
        self.cte_counter = snapshot.cte_counter
        self.last_completed_scope = _clone_query_scope(snapshot.last_completed_scope)
        self.pending_cte = _clone_pending_cte(snapshot.pending_cte)
        self.last_query_result = snapshot.last_query_result
        self.set_signatures = list(snapshot.set_signatures)
        self.cte_frames = _clone_cte_frames(snapshot.cte_frames)


def _quote_identifier(value: str) -> str:
    return "`" + value.replace("`", "``") + "`"


_FUNCTION_CALL_TOKENS = (
    PQ_SCALAR_FUNCTIONS
    | PQ_AGGREGATES
    | frozenset({"CHAR", "VARCHAR", "DECIMAL", "FLOAT", "DOUBLE", "TIME", "DATETIME"})
)

_FUNCTION_ARGUMENT_SQL: Mapping[FunctionArgument, str] = MappingProxyType(
    {
        FunctionArgument.NUMBER: "-2.5",
        FunctionArgument.UNIT_NUMBER: "0.5",
        FunctionArgument.INTEGER: "7",
        FunctionArgument.INTEGER_TWO: "2",
        FunctionArgument.INTEGER_THREE: "3",
        FunctionArgument.BASE_SIXTEEN: "16",
        FunctionArgument.TEXT: "'Alpha beta'",
        FunctionArgument.TEXT_ALT: "'beta'",
        FunctionArgument.SQL_TEXT: "'SELECT 1'",
        FunctionArgument.SEPARATOR: "','",
        FunctionArgument.DATE: "'2024-02-29'",
        FunctionArgument.DATETIME: "'2024-02-29 12:34:56.123456'",
        FunctionArgument.TIME: "'12:34:56.123456'",
        FunctionArgument.PERIOD: "202401",
        FunctionArgument.YEAR_NUMBER: "2024",
        FunctionArgument.DAY_NUMBER: "738945",
        FunctionArgument.SHA_BITS: "256",
        FunctionArgument.BASE64_TEXT: "'YWJj'",
        FunctionArgument.HEX_TEXT: "'616263'",
        FunctionArgument.IPV4_TEXT: "'192.0.2.1'",
        FunctionArgument.IPV4_NUMBER: "3221225985",
        FunctionArgument.IPV6_TEXT: "'2001:db8::1'",
        FunctionArgument.IPV6_BINARY: "X'20010db8000000000000000000000001'",
    }
)

_FUNCTION_ARGUMENT_SQL_BY_PROFILE: Mapping[
    FunctionValueProfile,
    Mapping[FunctionArgument, str],
] = MappingProxyType(
    {
        FunctionValueProfile.NORMAL: _FUNCTION_ARGUMENT_SQL,
        FunctionValueProfile.BOUNDARY: MappingProxyType(
            {
                **_FUNCTION_ARGUMENT_SQL,
                FunctionArgument.NUMBER: "0",
                FunctionArgument.UNIT_NUMBER: "1",
                FunctionArgument.INTEGER: "1",
                FunctionArgument.INTEGER_TWO: "1",
                FunctionArgument.INTEGER_THREE: "1",
                FunctionArgument.BASE_SIXTEEN: "2",
                FunctionArgument.TEXT: "''",
                FunctionArgument.TEXT_ALT: "''",
                FunctionArgument.SQL_TEXT: "'SELECT 0'",
                FunctionArgument.SEPARATOR: "''",
                FunctionArgument.DATE: "'1000-01-01'",
                FunctionArgument.DATETIME: "'1000-01-01 00:00:00.000000'",
                FunctionArgument.TIME: "'00:00:00.000000'",
                FunctionArgument.PERIOD: "190001",
                FunctionArgument.YEAR_NUMBER: "1000",
                FunctionArgument.DAY_NUMBER: "730120",
                FunctionArgument.SHA_BITS: "224",
                FunctionArgument.BASE64_TEXT: "'AA=='",
                FunctionArgument.HEX_TEXT: "'00FF'",
                FunctionArgument.IPV4_TEXT: "'0.0.0.0'",
                FunctionArgument.IPV4_NUMBER: "0",
                FunctionArgument.IPV6_TEXT: "'::'",
                FunctionArgument.IPV6_BINARY: "X'00000000000000000000000000000000'",
            }
        ),
        FunctionValueProfile.SPECIAL: MappingProxyType(
            {
                **_FUNCTION_ARGUMENT_SQL,
                FunctionArgument.TEXT: "CAST(X'6100275C00' AS CHAR CHARACTER SET utf8mb4)",
                FunctionArgument.TEXT_ALT: "CAST(X'CEB1CEB2' AS CHAR CHARACTER SET utf8mb4)",
                FunctionArgument.SQL_TEXT: "CAST(X'53454C4543542030' AS CHAR CHARACTER SET utf8mb4)",
                FunctionArgument.SEPARATOR: "'|'",
                FunctionArgument.DATE: "'9999-12-31'",
                FunctionArgument.DATETIME: "'9999-12-31 23:59:59.999999'",
                FunctionArgument.TIME: "'23:59:59.999999'",
                FunctionArgument.PERIOD: "999912",
                FunctionArgument.YEAR_NUMBER: "9999",
                FunctionArgument.DAY_NUMBER: "3652059",
                FunctionArgument.SHA_BITS: "512",
                FunctionArgument.BASE64_TEXT: "'////'",
                FunctionArgument.HEX_TEXT: "'00FF7F'",
                FunctionArgument.IPV4_TEXT: "'255.255.255.255'",
                FunctionArgument.IPV4_NUMBER: "4294967295",
                FunctionArgument.IPV6_TEXT: "'ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff'",
                FunctionArgument.IPV6_BINARY: "X'FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF'",
            }
        ),
    }
)


def _function_argument_sql(
    argument: FunctionArgument,
    profile: FunctionValueProfile,
) -> str:
    return _FUNCTION_ARGUMENT_SQL_BY_PROFILE[profile][argument]


def _registered_function_symbols() -> dict[
    str,
    tuple[DeterministicFunctionSignature, int | None],
]:
    symbols: dict[str, tuple[DeterministicFunctionSignature, int | None]] = {}
    for signature in DETERMINISTIC_FUNCTION_SIGNATURES:
        symbols[f"_fn_{signature.signature_id}"] = (signature, None)
        for position in sorted(signature.null_argument_positions):
            symbols[f"_fn_{signature.signature_id}_null_{position}"] = (
                signature,
                position,
            )
    return symbols


_REGISTERED_FUNCTION_SYMBOLS = MappingProxyType(_registered_function_symbols())


def _render_tokens(tokens: list[str]) -> str:
    result = ""
    previous = ""
    for token in (value for value in tokens if value):
        if not result:
            result = token
        elif token in {",", ")", ";", "."}:
            result += token
        elif previous in {"(", "."}:
            result += token
        elif token == "(":
            if previous.upper() in _FUNCTION_CALL_TOKENS:
                result += token
            else:
                result += " " + token
        else:
            result += " " + token
        previous = token
    return result.strip()


class GrammarQueryGenerator:
    """Expand a grammar into read-only SQL while keeping all identifiers in scope."""

    def __init__(
        self,
        grammar: SelectGrammar | None = None,
        *,
        config: GrammarQueryConfig | None = None,
        validator: ReadOnlyValidator | None = None,
    ) -> None:
        self.grammar = grammar or SelectGrammar.default()
        self.config = config or GrammarQueryConfig()
        self.validator = validator or ReadOnlyValidator()
        self.pq_validator = PqEligibilityValidator()

    def generate(
        self,
        schema: GrammarSchema | SchemaManifest,
        *,
        seed: int,
        grammar_config: GrammarQueryConfig | None = None,
        excluded_families: frozenset[str] = frozenset(),
    ) -> CandidateQuery:
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise TypeError("seed must be an integer")
        normalized = (
            GrammarSchema.from_manifest(schema) if isinstance(schema, SchemaManifest) else schema
        )
        if not isinstance(normalized, GrammarSchema):
            raise TypeError("schema must be GrammarSchema or SchemaManifest")
        source_schema = normalized
        tables = tuple(
            GrammarTable(
                table.name,
                tuple(column for column in table.columns if column.pq_supported),
                table.indexes if all(column.pq_supported for column in table.columns) else (),
                table.partitions,
                engine=table.engine,
                temporary=table.temporary,
            )
            for table in normalized.tables
            if table.engine.casefold() == "innodb"
            and not table.temporary
            and any(column.pq_supported for column in table.columns)
            and not any(column.mysql_type.upper().startswith("VECTOR") for column in table.columns)
            and "." not in table.name
        )
        if not tables:
            raise CandidateRejected(
                "PQ generation requires an eligible InnoDB base table and column"
            )
        normalized = GrammarSchema(tables)
        if not isinstance(excluded_families, frozenset) or any(
            family not in {"json", "fulltext", "spatial"} for family in excluded_families
        ):
            raise ValueError(
                "excluded_families must be a frozenset containing only json, fulltext, or spatial"
            )
        context = _GenerationContext(
            normalized,
            random.Random(seed),
            grammar_config or self.config,
            excluded_families,
        )
        tokens = self._expand(self.grammar.root, context, depth=1)
        sql = _render_tokens(tokens)
        candidate = CandidateQuery(sql, seed, self.grammar.sha256, tuple(context.trace))
        try:
            self.validator.validate_text(sql)
        except UnsafeQuery as error:
            raise CandidateRejected(
                "candidate failed the read-only safety gate",
                candidate=candidate,
            ) from error
        try:
            self.pq_validator.validate_text(sql)
            self._validate_explicit_schema_references(sql, source_schema)
        except PqIneligible as error:
            raise CandidateRejected(str(error), candidate=candidate) from error
        return candidate

    @staticmethod
    def _validate_explicit_schema_references(sql: str, schema: GrammarSchema) -> None:
        from select_fuzz.generation.query_safety import _masked_sql

        masked = _masked_sql(sql)
        identifiers = {
            match.group().casefold()
            for match in re.finditer(r"\b[A-Za-z_][A-Za-z0-9_]*\b", masked)
            if sql[match.end() : match.end() + 1] != "'"
        }
        identifiers.update(
            match.group()[1:-1].replace("``", "`").casefold()
            for match in re.finditer(r"`(?:``|[^`])+`", sql)
            if masked[match.start()] == "0"
        )
        unsupported = {
            column.name.casefold()
            for table in schema.tables
            for column in table.columns
            if not column.pq_supported
        }
        if identifiers & unsupported:
            raise PqIneligible("PQ query references an unsupported or generated column")
        if unsupported and re.search(
            r"(?:\bSELECT\s+(?:(?:ALL|DISTINCT)\s+)?|\.|,)\s*\*", masked, re.I
        ):
            raise PqIneligible("PQ wildcard projection could expose unsupported columns")
        for table in schema.tables:
            if table.name.casefold() not in identifiers:
                continue
            if (
                table.temporary
                or table.engine.casefold() != "innodb"
                or any(column.mysql_type.upper().startswith("VECTOR") for column in table.columns)
            ):
                raise PqIneligible("PQ query references an ineligible table")
        GrammarQueryGenerator._validate_table_references(sql, masked, schema)

    @staticmethod
    def _validate_table_references(sql: str, masked: str, schema: GrammarSchema) -> None:
        # Keep quoted identifiers distinct from SQL words while parsing FROM
        # lists. Strings and comments are already masked by the read-only lexer.
        tokens: list[tuple[str, str]] = []
        for match in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*|\d+|\S", masked):
            value = match.group()
            if sql[match.start()] == "`":
                quoted = re.match(r"`(?:``|[^`])+`", sql[match.start() :])
                if quoted is None:  # pragma: no cover - read-only lexer checks quotes
                    raise PqIneligible("PQ table identifier is malformed")
                tokens.append((quoted.group()[1:-1].replace("``", "`"), ""))
            else:
                tokens.append((value, value.upper() if value[0].isalpha() else ""))
        ctes: set[str] = set()
        for index, (value, word) in enumerate(tokens[:-2]):
            if word != "WITH" and value != ",":
                continue
            name = tokens[index + 1][0]
            cursor = index + 2
            if tokens[cursor][0] == "(":
                nesting = 1
                cursor += 1
                while cursor < len(tokens) and nesting:
                    nesting += (tokens[cursor][0] == "(") - (tokens[cursor][0] == ")")
                    cursor += 1
            if (
                cursor + 1 < len(tokens)
                and tokens[cursor][1] == "AS"
                and tokens[cursor + 1][0] == "("
            ):
                ctes.add(name.casefold())
        tables = {table.name.casefold(): table for table in schema.tables}
        states = [{"query": False, "from": False, "expect": False}]
        base_references = 0
        for index, (value, word) in enumerate(tokens):
            state = states[-1]
            index_hint_scope = index > 0 and tokens[index - 1][1] == "FOR"
            if value == "(":
                relation_group = state["expect"]
                state["expect"] = False
                states.append({"query": False, "from": relation_group, "expect": relation_group})
                continue
            if value == ")":
                states.pop()
                continue
            if word == "SELECT":
                state.update(query=True, **{"from": False, "expect": False})
                continue
            if (word == "FROM" and state["query"]) or (
                word in {"JOIN", "STRAIGHT_JOIN"} and state["from"] and not index_hint_scope
            ):
                state.update(**{"from": True, "expect": True})
                continue
            if (
                word in {"WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "UNION"}
                and not index_hint_scope
            ):
                state.update(**{"from": False, "expect": False})
            if value == "," and state["from"]:
                state["expect"] = True
                continue
            if not state["expect"]:
                continue
            state["expect"] = False
            name = value.casefold()
            if name in ctes:
                continue
            table = tables.get(name)
            if table is None:
                raise PqIneligible(f"PQ query references a table outside the schema: {value}")
            base_references += 1
            following = tokens[index + 1 :]
            if following and following[0][0] == ".":
                raise PqIneligible("PQ table references must use the bound schema")
            partition = bool(following and following[0][1] == "PARTITION")
            if table.partitions and not partition:
                raise PqIneligible("PQ partitioned table requires an explicit single partition")
            if partition and (
                len(following) < 4
                or following[1][0] != "("
                or following[2][0] not in table.partitions
                or following[3][0] != ")"
            ):
                raise PqIneligible("PQ partition selection is outside the table metadata")
        if base_references == 0:
            raise PqIneligible("PQ query has no bound base table")

    def _expand(
        self,
        production_name: str,
        context: _GenerationContext,
        *,
        depth: int,
    ) -> list[str]:
        excluded_family = {
            "fulltext_query": "fulltext",
            "json_scalar_function": "json",
            "spatial_scalar_function": "spatial",
        }.get(production_name)
        if excluded_family in context.excluded_families:
            raise CandidateRejected(f"{excluded_family} grammar family is excluded from this run")
        if depth > context.config.max_expansion_depth:
            raise CandidateRejected("grammar expansion exceeded the depth limit")
        context.expansion_steps += 1
        if context.expansion_steps > context.config.max_expansion_steps:
            raise _ExpansionBudgetExceeded("grammar expansion exceeded the step limit")
        production = self.grammar.productions[production_name]
        viable = [
            alternative
            for alternative in production.alternatives
            if depth - 1 + self.grammar.alternative_minimum_depth(alternative)
            <= context.config.max_expansion_depth
        ]
        if not viable:
            raise CandidateRejected(f"no terminating alternative for {production_name}")
        remaining = list(viable)
        last_error: CandidateRejected | None = None
        while remaining:
            index = context.rng.randrange(len(remaining))
            alternative = remaining.pop(index)
            snapshot = context.snapshot()
            context.trace.append(f"{production_name}@{alternative.source_line}")
            try:
                output: list[str] = []
                for symbol in self._materialize_symbols(
                    alternative.symbols,
                    context.rng,
                    max_repeat=context.config.max_repeat,
                ):
                    if symbol.value in self.grammar.productions:
                        output.extend(self._expand(symbol.value, context, depth=depth + 1))
                    elif symbol.value.startswith("_"):
                        value = self._semantic(symbol.value, context, depth=depth + 1)
                        if value:
                            output.append(value)
                    else:
                        output.append(symbol.value)
                return output
            except _ExpansionBudgetExceeded:
                raise
            except CandidateRejected as error:
                last_error = error
                context.restore(snapshot)
        raise CandidateRejected(f"all alternatives failed for {production_name}") from last_error

    def _materialize_symbols(
        self,
        symbols: tuple[GrammarSymbol, ...],
        rng: random.Random,
        *,
        max_repeat: int,
    ) -> tuple[GrammarSymbol, ...]:
        materialized: list[GrammarSymbol] = []
        for symbol in symbols:
            count = 1
            if symbol.multiplicity is Multiplicity.OPTIONAL:
                count = rng.randrange(2)
            elif symbol.multiplicity is Multiplicity.ZERO_OR_MORE:
                count = rng.randint(0, max_repeat)
            elif symbol.multiplicity is Multiplicity.ONE_OR_MORE:
                count = rng.randint(1, max_repeat)
            materialized.extend(GrammarSymbol(symbol.value, Multiplicity.ONE) for _ in range(count))
        return tuple(materialized)

    def _semantic(
        self,
        symbol: str,
        context: _GenerationContext,
        *,
        depth: int,
    ) -> str:
        excluded_family = next(
            (
                family
                for prefix, family in (
                    ("_json", "json"),
                    ("_strict_json", "json"),
                    ("_result_json", "json"),
                    ("_spatial", "spatial"),
                    ("_strict_spatial", "spatial"),
                    ("_result_spatial", "spatial"),
                )
                if symbol.startswith(prefix)
            ),
            None,
        )
        if excluded_family in context.excluded_families:
            raise CandidateRejected(f"{excluded_family} semantic family is excluded from this run")
        if symbol == "_scope_begin":
            outer: list[_ColumnBinding] = []
            if context.scopes:
                parent = context.scope
                transitive_outer = {binding.identity for binding in parent.outer_columns}
                outer = [
                    binding
                    for binding in parent.visible_columns
                    if not (
                        binding.identity in transitive_outer
                        and binding.identity in parent.selected_outer_bindings
                    )
                ]
            context.scopes.append(_QueryScope(outer_columns=list(outer)))
            return ""
        if symbol == "_scope_begin_isolated":
            context.scopes.append(_QueryScope())
            return ""
        if symbol == "_scope_end":
            completed = context.scope
            context.scopes.pop()
            if context.scopes:
                parent = context.scope
                transitive_outer = {binding.identity for binding in parent.outer_columns}
                parent.blocked_outer_bindings.update(
                    completed.selected_outer_bindings & transitive_outer
                )
            context.last_completed_scope = completed
            context.last_query_result = _QueryResult(
                tuple(completed.output_columns),
            )
            return ""
        if symbol == "_prepare_relation":
            context.scope.prepared_relation = _render_tokens(
                self._expand("relation", context, depth=depth)
            )
            return ""
        if symbol == "_prepare_base_relation":
            context.scope.prepared_relation = self._bind_table(context)
            return ""
        if symbol == "_emit_relation":
            if context.scope.prepared_relation is None:
                raise CandidateRejected("query scope has no prepared relation")
            return context.scope.prepared_relation
        if symbol == "_prepare_cte_relation":
            pending = context.pending_cte
            if pending is None:
                raise CandidateRejected("no CTE is pending")
            alias = self._next_relation_alias(context)
            context.scope.table_aliases.append(alias)
            context.scope.local_columns.extend(
                _ColumnBinding(alias, column, strict_compatible=False) for column in pending.columns
            )
            context.scope.prepared_relation = (
                f"{_quote_identifier(pending.name)} AS {_quote_identifier(alias)}"
            )
            return ""
        if symbol == "_prepare_group_column":
            context.scope.group_column = self._select_column_binding(
                context,
                TypeFamily.ANY,
            )
            context.scope.group_columns = [context.scope.group_column]
            return ""
        if symbol == "_prepare_group_columns":
            pool = self._visible_column_pool(context)
            if len(pool) < 2:
                raise CandidateRejected("multi-column GROUP BY requires two columns")
            width = context.rng.randint(2, min(3, len(pool)))
            context.scope.group_columns = context.rng.sample(pool, width)
            context.scope.group_column = context.scope.group_columns[0]
            return ""
        if symbol == "_group_column":
            binding = context.scope.group_column
            if binding is None:
                raise CandidateRejected("grouped query has no registered grouping column")
            context.scope.last_value_family = binding.column.family
            return binding.render()
        if symbol == "_group_columns":
            if not context.scope.group_columns:
                raise CandidateRejected("grouped query has no registered grouping columns")
            return ", ".join(binding.render() for binding in context.scope.group_columns)
        if symbol == "_group_expression":
            if not context.scope.group_columns:
                raise CandidateRejected("grouped query has no registered grouping columns")
            binding = context.rng.choice(context.scope.group_columns)
            return f"COALESCE({binding.render()}, {binding.render()})"
        if symbol == "_table":
            return self._bind_table(context)
        if symbol == "_table_implicit_alias":
            return self._bind_table(context, explicit_as=False)
        if symbol == "_table_partition":
            return self._bind_table(context, partitioned=True)
        if symbol == "_table_index_hint":
            return self._bind_table(context, index_hint=True)
        if symbol == "_table_partition_index_hint":
            return self._bind_table(
                context,
                partitioned=True,
                index_hint=True,
            )
        if symbol == "_derived_relation":
            return self._derived_relation(
                context,
                depth=depth,
            )
        if symbol == "_derived_relation_implicit_alias":
            return self._derived_relation(
                context,
                depth=depth,
                explicit_as=False,
            )
        if symbol == "_derived_relation_columns":
            return self._derived_relation(
                context,
                depth=depth,
                explicit_columns=True,
            )
        if symbol == "_derived_query_expression_relation":
            return self._derived_relation(
                context,
                depth=depth,
                explicit_columns=True,
                full_query_expression=True,
            )
        if symbol == "_common_column":
            return self._common_column(context)
        if symbol == "_common_columns":
            return self._common_columns(context)
        if symbol == "_any_column":
            return self._column(context, TypeFamily.ANY)
        if symbol == "_numeric_column":
            return self._column(context, TypeFamily.NUMERIC)
        if symbol == "_strict_numeric_column":
            return self._strict_column(context, TypeFamily.NUMERIC)
        if symbol == "_text_column":
            return self._column(context, TypeFamily.TEXT)
        if symbol == "_strict_text_column":
            return self._strict_column(context, TypeFamily.TEXT)
        if symbol == "_temporal_column":
            return self._column(context, TypeFamily.TEMPORAL)
        if symbol == "_strict_temporal_column":
            return self._strict_column(context, TypeFamily.TEMPORAL)
        if symbol == "_projection_alias":
            alias = f"q{len(context.scope.projection_columns) + 1}"
            column = GrammarColumn(
                alias,
                self._family_type(context.scope.last_value_family),
            )
            context.scope.projection_columns.append(column)
            context.scope.output_columns.append(column)
            return _quote_identifier(alias)
        if symbol == "_order_item":
            if context.scope.projection_columns:
                return _quote_identifier(context.rng.choice(context.scope.projection_columns).name)
            return self._column(context, TypeFamily.ANY)
        if symbol == "_projection_order_item":
            if not context.scope.projection_columns:
                raise CandidateRejected("projection ORDER BY requires a projected alias")
            return _quote_identifier(context.rng.choice(context.scope.projection_columns).name)
        if symbol == "_prepare_cte":
            self._prepare_cte(context, depth=depth)
            return ""
        if symbol == "_prepare_query_expression_cte":
            self._prepare_query_expression_cte(context, depth=depth)
            return ""
        if symbol == "_cte_frame_begin":
            context.cte_frames.append(_CteFrame())
            return ""
        if symbol == "_define_base_cte":
            return self._define_cte(context, depth=depth, dependent=False)
        if symbol == "_define_independent_cte":
            return self._define_cte(context, depth=depth, dependent=False)
        if symbol == "_define_dependent_cte":
            return self._define_cte(context, depth=depth, dependent=True)
        if symbol == "_prepare_latest_cte_relation":
            self._prepare_frame_cte_relation(context, mode="latest")
            return ""
        if symbol == "_prepare_cte_join_relation":
            self._prepare_frame_cte_relation(context, mode="join")
            return ""
        if symbol == "_prepare_cte_reuse_relation":
            self._prepare_frame_cte_relation(context, mode="reuse")
            return ""
        if symbol == "_cte_frame_end":
            if not context.cte_frames:
                raise CandidateRejected("no CTE frame is active")
            context.cte_frames.pop()
            return ""
        if symbol == "_emit_cte_name":
            if context.pending_cte is None:
                raise CandidateRejected("no CTE name is pending")
            return _quote_identifier(context.pending_cte.name)
        if symbol == "_emit_cte_column_list":
            if context.pending_cte is None:
                raise CandidateRejected("no CTE columns are pending")
            return (
                "("
                + ", ".join(
                    _quote_identifier(column.name) for column in context.pending_cte.columns
                )
                + ")"
            )
        if symbol == "_emit_cte_body":
            if context.pending_cte is None or context.pending_cte.body_sql is None:
                raise CandidateRejected("no nonrecursive CTE body is pending")
            return context.pending_cte.body_sql
        if symbol == "_emit_cte_outer":
            if context.pending_cte is None:
                raise CandidateRejected("no CTE is pending")
            return _render_tokens(self._expand("cte_outer_select", context, depth=depth))
        if symbol == "_clear_cte":
            context.pending_cte = None
            return ""
        if symbol.startswith("_prepare_") and symbol.endswith("_set_signature"):
            self._prepare_set_signature(context, symbol)
            return ""
        if symbol == "_set_select_operand":
            return self._set_select_operand(context)
        if symbol == "_set_select_topn_operand":
            return f"({self._set_select_operand(context)} ORDER BY 1 LIMIT 2)"
        if symbol == "_clear_set_signature":
            self._clear_set_signature(context)
            return ""
        if symbol == "_optimizer_hint":
            return self._optimizer_hint(context, kind="random")
        if symbol == "_optimizer_hint_merge":
            return self._optimizer_hint(context, kind="MERGE")
        if symbol == "_optimizer_hint_no_merge":
            return self._optimizer_hint(context, kind="NO_MERGE")
        if symbol == "_optimizer_hint_derived_condition_pushdown":
            return self._optimizer_hint(context, kind="DERIVED_CONDITION_PUSHDOWN")
        if symbol == "_optimizer_hint_join_order":
            return self._optimizer_hint(context, kind="JOIN_ORDER")
        if symbol == "_optimizer_hint_index_primary":
            return self._optimizer_hint(context, kind="INDEX_PRIMARY")
        if symbol == "_optimizer_hint_index_secondary":
            return self._optimizer_hint(context, kind="INDEX_SECONDARY")
        if symbol == "_optimizer_hint_no_range":
            return self._optimizer_hint(context, kind="NO_RANGE_OPTIMIZATION")
        if symbol == "_optimizer_hint_no_icp":
            return self._optimizer_hint(context, kind="NO_ICP")
        if symbol == "_int":
            context.scope.last_value_family = TypeFamily.NUMERIC
            return str(context.rng.choice((-2, -1, 0, 1, 2, 7, 42, 127, 255)))
        if symbol == "_numeric_boundary":
            context.scope.last_value_family = TypeFamily.NUMERIC
            return context.rng.choice(
                (
                    "-9223372036854775808",
                    "9223372036854775807",
                    "18446744073709551615",
                    "1.7976931348623157e308",
                )
            )
        if symbol == "_uint":
            context.scope.last_value_family = TypeFamily.NUMERIC
            return str(context.rng.choice((0, 1, 2, 7, 10, 64, 255)))
        if symbol == "_positive_uint":
            context.scope.last_value_family = TypeFamily.NUMERIC
            return str(context.rng.choice((1, 2, 7, 10, 64, 255)))
        if symbol == "_query_output_ordinal":
            result = context.last_query_result
            if result is None or not result.columns:
                raise CandidateRejected("query expression has no output columns")
            return str(context.rng.randint(1, len(result.columns)))
        if symbol == "_query_output_item":
            result = context.last_query_result
            if result is None or not result.columns:
                raise CandidateRejected("query expression has no output columns")
            aliases = [
                column.name
                for column in result.columns
                if re.fullmatch(r"q[1-9][0-9]*", column.name)
            ]
            if aliases and context.rng.randrange(2) == 0:
                return _quote_identifier(context.rng.choice(aliases))
            return str(context.rng.randint(1, len(result.columns)))
        if symbol == "_limit":
            return str(context.rng.choice((1, 2, 5, 10, 100)))
        if symbol == "_offset":
            return str(context.rng.choice((0, 1, 2, 5, 10)))
        if symbol == "_text":
            context.scope.last_value_family = TypeFamily.TEXT
            return context.rng.choice(("''", "'a'", "'0'", "'abc'", "'A%'", "'_%'"))
        if symbol == "_text_boundary":
            context.scope.last_value_family = TypeFamily.TEXT
            return context.rng.choice(("''", "' '", "'\\0'", "'Alpha beta'"))
        if symbol == "_like_escape_pattern":
            context.scope.last_value_family = TypeFamily.TEXT
            return context.rng.choice(("'a!_%'", "'!%%'", "'abc'"))
        if symbol == "_escape_char":
            context.scope.last_value_family = TypeFamily.TEXT
            return "'!'"
        if symbol == "_temporal":
            context.scope.last_value_family = TypeFamily.TEMPORAL
            return context.rng.choice(
                ("'1970-01-01'", "'2000-01-01'", "'2024-02-29'", "'2038-01-19'")
            )
        if symbol == "_temporal_boundary":
            context.scope.last_value_family = TypeFamily.TEMPORAL
            return context.rng.choice(
                (
                    "CAST('1000-01-01 00:00:00.000000' AS DATETIME(6))",
                    "CAST('9999-12-31 23:59:59.999999' AS DATETIME(6))",
                    "CAST('1970-01-01 00:00:01.000000' AS DATETIME(6))",
                    "CAST('2038-01-19 03:14:07.499999' AS DATETIME(6))",
                )
            )
        if symbol == "_bit_literal":
            context.scope.last_value_family = TypeFamily.NUMERIC
            return context.rng.choice(("b'0'", "b'1'", "b'1010'"))
        if symbol == "_cast_type":
            cast_types = [
                ("SIGNED", TypeFamily.NUMERIC),
                ("UNSIGNED", TypeFamily.NUMERIC),
                ("DECIMAL(20,6)", TypeFamily.NUMERIC),
                ("CHAR(64)", TypeFamily.TEXT),
                ("DATE", TypeFamily.TEMPORAL),
                ("DATETIME", TypeFamily.TEMPORAL),
            ]
            cast_type, family = context.rng.choice(cast_types)
            context.scope.last_value_family = family
            return cast_type
        result_family = {
            "_result_numeric": TypeFamily.NUMERIC,
            "_result_text": TypeFamily.TEXT,
            "_result_temporal": TypeFamily.TEMPORAL,
        }.get(symbol)
        if result_family is not None:
            context.scope.last_value_family = result_family
            return ""
        registered = _REGISTERED_FUNCTION_SYMBOLS.get(symbol)
        if registered is not None:
            signature, null_position = registered
            return self._render_registered_function(
                context,
                signature,
                null_position=null_position,
            )
        raise CandidateRejected(f"PQ does not admit semantic symbol: {symbol}")

    @staticmethod
    def _family_type(family: TypeFamily) -> str:
        return {
            TypeFamily.NUMERIC: "BIGINT",
            TypeFamily.TEXT: "VARCHAR(64)",
            TypeFamily.TEMPORAL: "DATETIME",
            TypeFamily.BINARY: "VARBINARY(64)",
            TypeFamily.JSON: "JSON",
            TypeFamily.SPATIAL: "GEOMETRY",
            TypeFamily.ANY: "VARCHAR(64)",
        }[family]

    @staticmethod
    def _render_registered_function(
        context: _GenerationContext,
        signature: DeterministicFunctionSignature,
        *,
        null_position: int | None,
    ) -> str:
        arguments = [
            _function_argument_sql(argument, context.config.function_value_profile)
            for argument in signature.arguments
        ]
        if (
            context.config.function_value_profile is FunctionValueProfile.BOUNDARY
            and signature.sql_name == "LOG"
            and len(arguments) == 2
        ):
            # LOG(X, B) rejects a base of 1 with warning 3020; use the
            # smallest valid base and argument for the boundary witness.
            arguments = ["2", "2"]
        if null_position is not None:
            arguments[null_position] = "NULL"
        context.scope.last_value_family = {
            FunctionResult.NUMERIC: TypeFamily.NUMERIC,
            FunctionResult.BOOLEAN: TypeFamily.NUMERIC,
            FunctionResult.TEXT: TypeFamily.TEXT,
            FunctionResult.BINARY: TypeFamily.BINARY,
            FunctionResult.TEMPORAL: TypeFamily.TEMPORAL,
        }[signature.result]
        return f"{signature.sql_name}({', '.join(arguments)})"

    def _next_relation_alias(self, context: _GenerationContext) -> str:
        context.relation_alias_counter += 1
        return f"r{context.relation_alias_counter}"

    def _prepare_set_signature(
        self,
        context: _GenerationContext,
        symbol: str,
    ) -> None:
        match = re.fullmatch(
            r"_prepare_(numeric|text|temporal)_([12])_set_signature",
            symbol,
        )
        if match is None:
            raise CandidateRejected(f"PQ does not admit set signature: {symbol}")
        family = TypeFamily(match.group(1))
        arity = int(match.group(2))
        context.set_signatures.append(
            _SetSignature(
                tuple(
                    GrammarColumn(
                        f"s{index}",
                        self._family_type(family),
                    )
                    for index in range(1, arity + 1)
                )
            )
        )

    @staticmethod
    def _active_set_signature(context: _GenerationContext) -> _SetSignature:
        if not context.set_signatures:
            raise CandidateRejected("no set signature is active")
        return context.set_signatures[-1]

    def _signature_bindings(
        self,
        context: _GenerationContext,
        signature: _SetSignature,
    ) -> tuple[GrammarTable, str, list[_ColumnBinding]]:
        table = context.rng.choice(context.schema.tables)
        alias = self._next_relation_alias(context)
        available = [
            _ColumnBinding(alias, column)
            for column in table.columns
            if column.family.value not in context.excluded_families
        ]
        if not available:
            raise CandidateRejected("set operand has no in-scope columns")
        selected: list[_ColumnBinding] = []
        for expected in signature.columns:
            desired = expected.family
            compatible = [binding for binding in available if binding.column.family is desired]
            incompatible = [
                binding for binding in available if binding.column.family is not desired
            ]
            use_compatible = (
                compatible and context.rng.randrange(100) < context.config.compatible_type_percent
            )
            selected.append(
                context.rng.choice(compatible if use_compatible else (incompatible or available))
            )
        return table, alias, selected

    def _set_select_operand(self, context: _GenerationContext) -> str:
        signature = self._active_set_signature(context)
        table, alias, bindings = self._signature_bindings(context, signature)
        projection = ", ".join(
            f"{binding.render()} AS {_quote_identifier(f'q{index}')}"
            for index, binding in enumerate(bindings, start=1)
        )
        return (
            f"SELECT {projection} FROM {_quote_identifier(table.name)}{self._partition_clause(table, context)} "
            f"AS {_quote_identifier(alias)}"
        )

    @staticmethod
    def _clear_set_signature(context: _GenerationContext) -> None:
        signature = GrammarQueryGenerator._active_set_signature(context)
        context.set_signatures.pop()
        context.last_query_result = _QueryResult(signature.columns)

    def _bind_table(
        self,
        context: _GenerationContext,
        *,
        explicit_as: bool = True,
        partitioned: bool = False,
        index_hint: bool = False,
    ) -> str:
        if len(context.scope.table_aliases) >= context.config.max_tables_per_query_block:
            raise CandidateRejected("query block exceeded its table budget")
        tables = [
            table
            for table in context.schema.tables
            if (not partitioned or table.partitions) and (not index_hint or table.indexes)
        ]
        if not tables:
            requirement = "partition" if partitioned else "index"
            raise CandidateRejected(f"no table exposes required {requirement} metadata")
        table = context.rng.choice(tables)
        alias = self._next_relation_alias(context)
        context.scope.table_aliases.append(alias)
        context.scope.local_columns.extend(
            _ColumnBinding(alias, column) for column in table.columns
        )
        context.scope.table_indexes[alias] = tuple(table.indexes)
        partition_clause = self._partition_clause(table, context)
        alias_clause = " AS " if explicit_as else " "
        hint_clause = ""
        if index_hint:
            width = context.rng.randint(1, min(2, len(table.indexes)))
            selected = context.rng.sample(list(table.indexes), width)
            action = context.rng.choice(("USE", "FORCE", "IGNORE"))
            scope = context.rng.choice(("", " FOR JOIN", " FOR ORDER BY", " FOR GROUP BY"))
            hint_clause = (
                f" {action} INDEX{scope} ("
                + ", ".join(_quote_identifier(name) for name in selected)
                + ")"
            )
        return (
            f"{_quote_identifier(table.name)}{partition_clause}"
            f"{alias_clause}{_quote_identifier(alias)}{hint_clause}"
        )

    @staticmethod
    def _partition_clause(table: GrammarTable, context: _GenerationContext) -> str:
        if not table.partitions:
            return ""
        name = context.rng.choice(table.partitions)
        return f" PARTITION ({_quote_identifier(name)})"

    def _optimizer_hint(self, context: _GenerationContext, *, kind: str) -> str:
        aliases = context.scope.table_aliases
        derived_aliases = context.scope.derived_aliases
        if kind in {"MERGE", "NO_MERGE", "DERIVED_CONDITION_PUSHDOWN"}:
            if not derived_aliases:
                raise CandidateRejected(f"{kind} requires a derived relation")
            alias = _quote_identifier(context.rng.choice(derived_aliases))
            return f"/*+ {kind}({alias}) */"
        if kind == "random" and derived_aliases and context.rng.randrange(2) == 0:
            return self._optimizer_hint(
                context,
                kind=context.rng.choice(("MERGE", "NO_MERGE", "DERIVED_CONDITION_PUSHDOWN")),
            )
        if kind in {"INDEX_PRIMARY", "INDEX_SECONDARY", "NO_RANGE_OPTIMIZATION"}:
            index_candidates = [
                (alias, index)
                for alias in aliases
                for index in context.scope.table_indexes.get(alias, ())
                if (
                    kind == "NO_RANGE_OPTIMIZATION"
                    or (kind == "INDEX_PRIMARY" and index == "PRIMARY")
                    or (kind == "INDEX_SECONDARY" and index != "PRIMARY")
                )
            ]
            if not index_candidates:
                raise CandidateRejected(f"{kind} requires a matching visible index")
            alias, index = context.rng.choice(index_candidates)
            hint_name = {
                "INDEX_PRIMARY": "INDEX",
                "INDEX_SECONDARY": "INDEX",
                "NO_RANGE_OPTIMIZATION": "NO_RANGE_OPTIMIZATION",
            }[kind]
            return f"/*+ {hint_name}({_quote_identifier(alias)} {_quote_identifier(index)}) */"
        if kind == "random":
            index_candidates = [
                (alias, index)
                for alias in aliases
                for index in context.scope.table_indexes.get(alias, ())
            ]
            if index_candidates and context.rng.randrange(3) == 0:
                return self._optimizer_hint(
                    context,
                    kind=context.rng.choice(
                        ("INDEX_PRIMARY", "INDEX_SECONDARY", "NO_RANGE_OPTIMIZATION")
                    ),
                )
            if len(aliases) >= 2:
                return self._optimizer_hint(context, kind="JOIN_ORDER")
            return self._optimizer_hint(context, kind="NO_ICP")
        if kind == "JOIN_ORDER":
            if len(aliases) < 2:
                raise CandidateRejected("JOIN_ORDER requires at least two relation aliases")
            rendered = ", ".join(_quote_identifier(alias) for alias in aliases)
            return f"/*+ JOIN_ORDER({rendered}) */"
        if kind == "NO_ICP":
            if not aliases:
                raise CandidateRejected("NO_ICP requires a relation alias")
            return f"/*+ NO_ICP({_quote_identifier(aliases[0])}) */"
        raise CandidateRejected(f"unsupported optimizer hint kind: {kind}")

    def _visible_column_pool(self, context: _GenerationContext) -> list[_ColumnBinding]:
        local = [
            binding
            for binding in context.scope.local_columns
            if binding.column.family.value not in context.excluded_families
        ]
        outer = [
            binding
            for binding in context.scope.outer_columns
            if binding.identity not in context.scope.blocked_outer_bindings
            and binding.column.family.value not in context.excluded_families
        ]
        if not local:
            return list(outer)
        if outer and context.rng.randrange(100) < context.config.correlated_column_percent:
            return [*local, *outer]
        return list(local)

    def _select_column_binding(
        self,
        context: _GenerationContext,
        desired: TypeFamily,
    ) -> _ColumnBinding:
        columns = self._visible_column_pool(context)
        if not columns:
            raise CandidateRejected("no visible columns are available")
        compatible = [binding for binding in columns if binding.column.family is desired]
        use_compatible = (
            desired is not TypeFamily.ANY
            and compatible
            and context.rng.randrange(100) < context.config.compatible_type_percent
        )
        incompatible = [binding for binding in columns if binding.column.family is not desired]
        selected = context.rng.choice(compatible if use_compatible else (incompatible or columns))
        if selected in context.scope.outer_columns:
            context.scope.selected_outer_bindings.add(selected.identity)
        return selected

    def _column(self, context: _GenerationContext, desired: TypeFamily) -> str:
        selected = self._select_column_binding(context, desired)
        context.scope.last_value_family = selected.column.family
        return selected.render()

    def _strict_column(self, context: _GenerationContext, desired: TypeFamily) -> str:
        return self._strict_binding(context, desired).render()

    def _strict_binding(
        self,
        context: _GenerationContext,
        desired: TypeFamily,
    ) -> _ColumnBinding:
        columns = self._visible_column_pool(context)
        compatible = [
            binding
            for binding in columns
            if binding.strict_compatible and binding.column.family is desired
        ]
        if not compatible:
            raise CandidateRejected(f"no {desired.value} column is visible")
        selected = context.rng.choice(compatible)
        if selected in context.scope.outer_columns:
            context.scope.selected_outer_bindings.add(selected.identity)
        context.scope.last_value_family = selected.column.family
        return selected

    def _common_column(self, context: _GenerationContext) -> str:
        return self._common_columns(context, maximum=1)

    def _common_columns(
        self,
        context: _GenerationContext,
        *,
        maximum: int = 3,
    ) -> str:
        aliases = context.scope.table_aliases
        if len(aliases) < 2:
            raise CandidateRejected("USING requires two relation aliases")
        left, right = aliases[-2:]
        left_names = {
            binding.column.name: binding.column.family
            for binding in context.scope.local_columns
            if binding.relation_alias == left
        }
        right_names = {
            binding.column.name: binding.column.family
            for binding in context.scope.local_columns
            if binding.relation_alias == right
        }
        common = sorted(
            name
            for name in left_names.keys() & right_names.keys()
            if left_names[name].value not in context.excluded_families
            and right_names[name].value not in context.excluded_families
        )
        minimum = 1 if maximum == 1 else 2
        if len(common) < minimum:
            raise CandidateRejected("joined relations have no common column")
        width = context.rng.randint(minimum, min(maximum, len(common)))
        return ", ".join(_quote_identifier(name) for name in context.rng.sample(common, width))

    def _derived_relation(
        self,
        context: _GenerationContext,
        *,
        depth: int,
        explicit_columns: bool = False,
        explicit_as: bool = True,
        full_query_expression: bool = False,
    ) -> str:
        parent_scope_depth = len(context.scopes)
        production = "derived_query_expression" if full_query_expression else "derived_select"
        context.last_completed_scope = None
        context.last_query_result = None
        sql = _render_tokens(self._expand(production, context, depth=depth))
        completed = context.last_completed_scope
        result = context.last_query_result
        if result is None or not result.columns:
            raise CandidateRejected("derived query did not expose named columns")
        if len(context.scopes) != parent_scope_depth:
            raise CandidateRejected("derived query did not restore its parent scope")
        parent = context.scope
        alias = self._next_relation_alias(context)
        parent.table_aliases.append(alias)
        parent.derived_aliases.append(alias)
        output_columns = (
            tuple(completed.projection_columns)
            if completed is not None and completed.projection_columns
            else tuple(result.columns)
        )
        column_clause = ""
        if explicit_columns:
            output_columns = tuple(
                GrammarColumn(f"d{index}", column.mysql_type)
                for index, column in enumerate(output_columns, start=1)
            )
            column_clause = (
                " (" + ", ".join(_quote_identifier(column.name) for column in output_columns) + ")"
            )
        parent.local_columns.extend(
            _ColumnBinding(alias, column, strict_compatible=False) for column in output_columns
        )
        alias_clause = " AS " if explicit_as else " "
        return f"({sql}){alias_clause}{_quote_identifier(alias)}{column_clause}"

    def _prepare_cte(self, context: _GenerationContext, *, depth: int) -> None:
        body = _render_tokens(self._expand("derived_select", context, depth=depth))
        completed = context.last_completed_scope
        if completed is None or not completed.projection_columns:
            raise CandidateRejected("CTE body did not expose named columns")
        context.cte_counter += 1
        name = f"cte{context.cte_counter}"
        context.pending_cte = _PendingCte(
            name,
            tuple(completed.projection_columns),
            body,
        )

    def _prepare_query_expression_cte(
        self,
        context: _GenerationContext,
        *,
        depth: int,
    ) -> None:
        context.last_completed_scope = None
        context.last_query_result = None
        body = _render_tokens(self._expand("derived_query_expression", context, depth=depth))
        result = context.last_query_result
        if result is None or not result.columns:
            raise CandidateRejected("CTE query expression has no output columns")
        context.cte_counter += 1
        columns = tuple(
            GrammarColumn(f"c{index}", column.mysql_type)
            for index, column in enumerate(result.columns, start=1)
        )
        context.pending_cte = _PendingCte(
            f"cte{context.cte_counter}",
            columns,
            body,
        )

    @staticmethod
    def _active_cte_frame(context: _GenerationContext) -> _CteFrame:
        if not context.cte_frames:
            raise CandidateRejected("no CTE frame is active")
        return context.cte_frames[-1]

    def _define_cte(
        self,
        context: _GenerationContext,
        *,
        depth: int,
        dependent: bool,
    ) -> str:
        frame = self._active_cte_frame(context)
        if dependent:
            if not frame.bindings:
                raise CandidateRejected("dependent CTE requires an earlier binding")
            source = frame.bindings[-1]
            alias = self._next_relation_alias(context)
            columns = tuple(
                GrammarColumn(f"q{index}", column.mysql_type)
                for index, column in enumerate(source.columns, start=1)
            )
            projection = ", ".join(
                f"{_quote_identifier(alias)}.{_quote_identifier(column.name)} "
                f"AS {_quote_identifier(output.name)}"
                for column, output in zip(source.columns, columns, strict=True)
            )
            body = (
                f"SELECT {projection} FROM {_quote_identifier(source.name)} "
                f"AS {_quote_identifier(alias)}"
            )
        else:
            body = _render_tokens(self._expand("derived_select", context, depth=depth))
            completed = context.last_completed_scope
            if completed is None or not completed.projection_columns:
                raise CandidateRejected("CTE body did not expose named columns")
            columns = tuple(completed.projection_columns)
        context.cte_counter += 1
        binding = _CteBinding(f"cte{context.cte_counter}", columns, body)
        frame.bindings.append(binding)
        return f"{_quote_identifier(binding.name)} AS ({binding.body_sql})"

    def _prepare_frame_cte_relation(
        self,
        context: _GenerationContext,
        *,
        mode: str,
    ) -> None:
        frame = self._active_cte_frame(context)
        if not frame.bindings:
            raise CandidateRejected("CTE relation requires a defined binding")
        bindings: tuple[_CteBinding, ...]
        if mode == "latest":
            bindings = (frame.bindings[-1],)
        elif mode == "join":
            if len(frame.bindings) < 2:
                raise CandidateRejected("CTE join requires two defined bindings")
            bindings = tuple(frame.bindings[-2:])
        elif mode == "reuse":
            bindings = (frame.bindings[-1], frame.bindings[-1])
        else:  # pragma: no cover - closed semantic dispatch
            raise GrammarError(f"unknown CTE relation mode: {mode}")
        aliases = tuple(self._next_relation_alias(context) for _ in bindings)
        for binding, alias in zip(bindings, aliases, strict=True):
            context.scope.table_aliases.append(alias)
            context.scope.derived_aliases.append(alias)
            context.scope.local_columns.extend(
                _ColumnBinding(alias, column, strict_compatible=False) for column in binding.columns
            )
        relations = [
            f"{_quote_identifier(binding.name)} AS {_quote_identifier(alias)}"
            for binding, alias in zip(bindings, aliases, strict=True)
        ]
        if len(relations) == 1:
            context.scope.prepared_relation = relations[0]
            return
        left_binding = bindings[0]
        right_binding = bindings[1]
        if not left_binding.columns or not right_binding.columns:
            raise CandidateRejected("CTE join requires visible output columns")
        left_column = left_binding.columns[0]
        right_column = right_binding.columns[0]
        condition = (
            f"{_quote_identifier(aliases[0])}.{_quote_identifier(left_column.name)} "
            f"<=> {_quote_identifier(aliases[1])}.{_quote_identifier(right_column.name)}"
        )
        context.scope.prepared_relation = f"{relations[0]} JOIN {relations[1]} ON {condition}"


__all__ = [
    "CandidateQuery",
    "CandidateRejected",
    "GrammarAlternative",
    "GrammarColumn",
    "GrammarError",
    "GrammarProduction",
    "GrammarQueryConfig",
    "GrammarQueryGenerator",
    "GrammarSchema",
    "GrammarSymbol",
    "GrammarTable",
    "FunctionValueProfile",
    "Multiplicity",
    "SelectGrammar",
    "TypeFamily",
]
