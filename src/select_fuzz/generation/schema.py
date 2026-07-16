"""Deterministic MySQL 8.0.41 schema profiles and SQL rendering."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import json
import random
import re
from typing import TYPE_CHECKING

from select_fuzz.domain import SeedTree
from select_fuzz.generation.catalog import FeatureSpec

if TYPE_CHECKING:
    from select_fuzz.generation.schema_rules import SchemaRules


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_TYPE_DECLARATION = re.compile(
    r"^(?:[A-Z][A-Z0-9]*(?:\([0-9]+(?:,[0-9]+)?\))?(?: UNSIGNED)?"
    r"|(?:ENUM|SET)\('[a-z0-9_]+'(?:,'[a-z0-9_]+')*\))$"
)
_GEOMETRY_TYPES = frozenset(
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
_INTEGER_TYPES = frozenset({"TINYINT", "SMALLINT", "MEDIUMINT", "INT", "BIGINT"})
_LOB_TYPES = frozenset(
    {
        "TINYTEXT",
        "TEXT",
        "MEDIUMTEXT",
        "LONGTEXT",
        "TINYBLOB",
        "BLOB",
        "MEDIUMBLOB",
        "LONGBLOB",
    }
)
_SUPPORTED_COLLATIONS = {
    "ascii": frozenset({"ascii_general_ci", "ascii_bin"}),
    "latin1": frozenset({"latin1_swedish_ci", "latin1_bin"}),
    "utf8mb3": frozenset({"utf8mb3_general_ci", "utf8mb3_bin"}),
    "utf8mb4": frozenset(
        {
            "utf8mb4_0900_ai_ci",
            "utf8mb4_0900_as_cs",
            "utf8mb4_bin",
        }
    ),
}


def _require_identifier(value: str, label: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a snake_case identifier")


def _quote(value: str) -> str:
    _require_identifier(value, "SQL identifier")
    return f"`{value}`"


def _is_mysql_8041_type(declaration: str) -> bool:
    if declaration.startswith(("ENUM(", "SET(")):
        kind = declaration.split("(", 1)[0]
        values = re.findall(r"'[a-z0-9_]+'", declaration)
        maximum = 65_535 if kind == "ENUM" else 64
        return 1 <= len(values) <= maximum
    unsigned = declaration.endswith(" UNSIGNED")
    core = declaration.removesuffix(" UNSIGNED")
    match = re.fullmatch(r"([A-Z][A-Z0-9]*)(?:\(([0-9]+)(?:,([0-9]+))?\))?", core)
    if match is None:
        return False
    base, first_raw, second_raw = match.groups()
    if base in _INTEGER_TYPES:
        return first_raw is None and second_raw is None
    if base in {"FLOAT", "DOUBLE"}:
        return first_raw is None and second_raw is None
    if base == "DECIMAL":
        if first_raw is None or second_raw is None:
            return False
        precision, scale = int(first_raw), int(second_raw)
        return 1 <= precision <= 65 and 0 <= scale <= min(30, precision)
    if unsigned:
        return False
    if base == "BIT":
        return first_raw is not None and second_raw is None and 1 <= int(first_raw) <= 64
    if base in {"CHAR", "BINARY"}:
        return first_raw is not None and second_raw is None and 0 <= int(first_raw) <= 255
    if base in {"VARCHAR", "VARBINARY"}:
        return first_raw is not None and second_raw is None and 0 <= int(first_raw) <= 65_535
    if base in {"TIME", "DATETIME", "TIMESTAMP"}:
        return second_raw is None and (first_raw is None or 0 <= int(first_raw) <= 6)
    return (
        first_raw is None
        and second_raw is None
        and base in {"DATE", "YEAR", "JSON", *_LOB_TYPES, *_GEOMETRY_TYPES}
    )


class SchemaProfile(StrEnum):
    REGULAR_INNODB = "regular_innodb"
    PARTITIONED_INNODB = "partitioned_innodb"
    TEMPORARY_INNODB = "temporary_innodb"
    FOREIGN_KEY_GRAPH = "foreign_key_graph"
    FULLTEXT_INNODB = "fulltext_innodb"
    SPATIAL_INNODB = "spatial_innodb"
    JSON_MULTIVALUE_INNODB = "json_multivalue_innodb"


class BoundaryDeclarationId(StrEnum):
    """Stable machine IDs for non-JSON, non-spatial type boundaries."""

    TINYINT_SIGNED = "tinyint_signed"
    TINYINT_UNSIGNED = "tinyint_unsigned"
    SMALLINT_SIGNED = "smallint_signed"
    SMALLINT_UNSIGNED = "smallint_unsigned"
    MEDIUMINT_SIGNED = "mediumint_signed"
    MEDIUMINT_UNSIGNED = "mediumint_unsigned"
    INT_SIGNED = "int_signed"
    INT_UNSIGNED = "int_unsigned"
    BIGINT_SIGNED = "bigint_signed"
    BIGINT_UNSIGNED = "bigint_unsigned"
    BIT_LENGTH_1 = "bit_length_1"
    BIT_LENGTH_64 = "bit_length_64"
    DECIMAL_P1_S0 = "decimal_p1_s0"
    DECIMAL_P1_S1 = "decimal_p1_s1"
    DECIMAL_P30_S30 = "decimal_p30_s30"
    DECIMAL_P31_S30 = "decimal_p31_s30"
    DECIMAL_P65_S0 = "decimal_p65_s0"
    DECIMAL_P65_S30 = "decimal_p65_s30"
    FLOAT_SIGNED = "float_signed"
    FLOAT_UNSIGNED = "float_unsigned"
    DOUBLE_SIGNED = "double_signed"
    DOUBLE_UNSIGNED = "double_unsigned"
    CHAR_LENGTH_0 = "char_length_0"
    CHAR_LENGTH_1 = "char_length_1"
    CHAR_LENGTH_MAX = "char_length_max"
    VARCHAR_LENGTH_0 = "varchar_length_0"
    VARCHAR_LENGTH_1 = "varchar_length_1"
    VARCHAR_LENGTH_MAX = "varchar_length_max"
    BINARY_LENGTH_0 = "binary_length_0"
    BINARY_LENGTH_1 = "binary_length_1"
    BINARY_LENGTH_MAX = "binary_length_max"
    VARBINARY_LENGTH_0 = "varbinary_length_0"
    VARBINARY_LENGTH_1 = "varbinary_length_1"
    VARBINARY_LENGTH_MAX = "varbinary_length_max"
    DATE = "date"
    TIME_FSP_0 = "time_fsp_0"
    TIME_FSP_6 = "time_fsp_6"
    DATETIME_FSP_0 = "datetime_fsp_0"
    DATETIME_FSP_6 = "datetime_fsp_6"
    TIMESTAMP_FSP_0 = "timestamp_fsp_0"
    TIMESTAMP_FSP_6 = "timestamp_fsp_6"
    YEAR = "year"
    TINYTEXT = "tinytext"
    TEXT = "text"
    MEDIUMTEXT = "mediumtext"
    LONGTEXT = "longtext"
    TINYBLOB = "tinyblob"
    BLOB = "blob"
    MEDIUMBLOB = "mediumblob"
    LONGBLOB = "longblob"
    ENUM = "enum"
    SET = "set"


class IndexKind(StrEnum):
    BTREE = "btree"
    FUNCTIONAL = "functional"
    FULLTEXT = "fulltext"
    SPATIAL = "spatial"
    MULTIVALUE = "multivalue"


class IndexExpressionKind(StrEnum):
    LOWER_CHAR = "lower_char"
    JSON_UNSIGNED_ARRAY = "json_unsigned_array"


class SortDirection(StrEnum):
    ASC = "ASC"
    DESC = "DESC"


@dataclass(frozen=True, slots=True)
class SchemaLimits:
    """Complexity and physical limits used by both generation and validation."""

    min_tables: int = 1
    max_tables: int = 8
    min_columns: int = 2
    max_columns: int = 16
    max_indexes_per_table: int = 8
    index_byte_budget: int = 3072
    row_byte_budget: int = 65_535
    page_size: int = 16_384
    row_format: str = "DYNAMIC"
    max_varchar_characters: int = 16_383
    max_varbinary_bytes: int = 65_535
    max_partitions: int = 16

    def __post_init__(self) -> None:
        integer_fields = (
            "min_tables",
            "max_tables",
            "min_columns",
            "max_columns",
            "max_indexes_per_table",
            "index_byte_budget",
            "row_byte_budget",
            "page_size",
            "max_varchar_characters",
            "max_varbinary_bytes",
            "max_partitions",
        )
        for field_name in integer_fields:
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        if self.min_tables > self.max_tables:
            raise ValueError("min_tables must not exceed max_tables")
        if self.min_columns > self.max_columns:
            raise ValueError("min_columns must not exceed max_columns")
        if self.max_columns > 1017:
            raise ValueError("max_columns exceeds the InnoDB limit of 1017")
        if self.max_indexes_per_table > 65:
            raise ValueError("max_indexes_per_table exceeds 64 secondary indexes plus PRIMARY")
        if self.index_byte_budget < 8:
            raise ValueError("index_byte_budget must fit the generated BIGINT primary key")
        if self.row_byte_budget > 65_535:
            raise ValueError("row_byte_budget exceeds the MySQL logical row-size limit")
        if self.index_byte_budget > 3072:
            raise ValueError("index_byte_budget exceeds the DYNAMIC row-format limit")
        if self.page_size not in {4096, 8192, 16_384, 32_768, 65_536}:
            raise ValueError("page_size must be a supported InnoDB page size")
        if self.row_format not in {"DYNAMIC", "COMPACT", "REDUNDANT", "COMPRESSED"}:
            raise ValueError("row_format is not supported by MySQL 8.0.41")
        if self.row_format == "COMPRESSED" and self.page_size > 16_384:
            raise ValueError("COMPRESSED row format requires an InnoDB page size at most 16KB")
        if self.max_varchar_characters > 65_535:
            raise ValueError("max_varchar_characters exceeds the MySQL field limit")
        if self.max_varbinary_bytes > 65_535:
            raise ValueError("max_varbinary_bytes exceeds the MySQL field limit")
        if self.max_partitions > 8192:
            raise ValueError("max_partitions exceeds the MySQL partition limit")

    def identity(self) -> str:
        return ":".join(str(getattr(self, name)) for name in self.__dataclass_fields__)


@dataclass(frozen=True, slots=True)
class ColumnDef:
    name: str
    mysql_type: str
    nullable: bool
    charset: str | None = None
    collation: str | None = None
    srid: int | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.name, "column name")
        if not _TYPE_DECLARATION.fullmatch(self.mysql_type):
            raise ValueError("mysql_type must be a normalized MySQL type declaration")
        if not _is_mysql_8041_type(self.mysql_type):
            raise ValueError("mysql_type is outside the supported MySQL 8.0.41 type domain")
        if not isinstance(self.nullable, bool):
            raise TypeError("nullable must be a boolean")
        if (self.charset is None) != (self.collation is None):
            raise ValueError("charset and collation must be supplied together")
        if self.charset is not None:
            _require_identifier(self.charset, "charset")
            _require_identifier(self.collation or "", "collation")
            if self.base_type not in {
                "CHAR",
                "VARCHAR",
                "TINYTEXT",
                "TEXT",
                "MEDIUMTEXT",
                "LONGTEXT",
                "ENUM",
                "SET",
            }:
                raise ValueError("charset and collation require a nonbinary string type")
            if self.collation not in _SUPPORTED_COLLATIONS.get(self.charset, frozenset()):
                raise ValueError("charset and collation are not a supported compatible pair")
        if self.srid is not None and (
            not isinstance(self.srid, int) or isinstance(self.srid, bool) or self.srid < 0
        ):
            raise ValueError("srid must be a nonnegative integer")
        if self.srid is not None and self.srid > 2**32 - 1:
            raise ValueError("srid exceeds the MySQL unsigned 32-bit range")
        if self.srid is not None and self.base_type not in _GEOMETRY_TYPES:
            raise ValueError("SRID is only valid for spatial columns")

    @property
    def base_type(self) -> str:
        return self.mysql_type.split("(", 1)[0].split(" ", 1)[0]

    @property
    def compatibility_key(self) -> tuple[str, str | None, str | None]:
        """Attributes MySQL requires to match across a foreign key edge."""

        return (self.mysql_type, self.charset, self.collation)

    def render(self) -> str:
        pieces = [_quote(self.name), self.mysql_type]
        if self.charset is not None:
            pieces.extend(("CHARACTER SET", self.charset, "COLLATE", self.collation or ""))
        if self.srid is not None:
            pieces.extend(("SRID", str(self.srid)))
        pieces.append("NULL" if self.nullable else "NOT NULL")
        return " ".join(pieces)


@dataclass(frozen=True, slots=True)
class BoundaryDeclaration:
    """One model-valid type declaration with a stable coverage identity."""

    boundary_id: BoundaryDeclarationId
    declaration: str
    tags: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.boundary_id, BoundaryDeclarationId):
            raise TypeError("boundary_id must be a BoundaryDeclarationId")
        if not isinstance(self.declaration, str):
            raise TypeError("declaration must be a string")
        if not isinstance(self.tags, frozenset) or any(
            not isinstance(tag, str) for tag in self.tags
        ):
            raise TypeError("tags must be a frozenset of strings")
        if not self.tags <= {"deprecated"}:
            raise ValueError("boundary declaration has an unsupported tag")
        ColumnDef("boundary", self.declaration, True)


@dataclass(frozen=True, slots=True)
class IndexExpression:
    """A closed set of deterministic MySQL 8.0.41 functional-key templates."""

    kind: IndexExpressionKind
    column_name: str
    cast_length: int | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.column_name, "expression column")
        if self.kind is IndexExpressionKind.LOWER_CHAR:
            if (
                not isinstance(self.cast_length, int)
                or isinstance(self.cast_length, bool)
                or self.cast_length <= 0
            ):
                raise ValueError("LOWER_CHAR requires a positive cast length")
        elif self.cast_length is not None:
            raise ValueError("JSON_UNSIGNED_ARRAY does not accept a cast length")

    @classmethod
    def lower_char(cls, column_name: str, cast_length: int) -> IndexExpression:
        return cls(IndexExpressionKind.LOWER_CHAR, column_name, cast_length)

    @classmethod
    def json_unsigned_array(cls, column_name: str) -> IndexExpression:
        return cls(IndexExpressionKind.JSON_UNSIGNED_ARRAY, column_name)

    def render(self) -> str:
        column = _quote(self.column_name)
        if self.kind is IndexExpressionKind.LOWER_CHAR:
            return (
                f"CAST(LOWER({column}) AS CHAR({self.cast_length}) CHARACTER SET utf8mb4) "
                "COLLATE utf8mb4_0900_ai_ci"
            )
        return f"CAST(JSON_EXTRACT({column}, '$[*]') AS UNSIGNED ARRAY)"


@dataclass(frozen=True, slots=True)
class IndexPart:
    column_name: str | None = None
    expression: IndexExpression | None = None
    prefix_length: int | None = None
    direction: SortDirection = SortDirection.ASC

    def __post_init__(self) -> None:
        if (self.column_name is None) == (self.expression is None):
            raise ValueError("an index part requires exactly one column or expression")
        if self.column_name is not None:
            _require_identifier(self.column_name, "index column")
        if self.expression is not None and not isinstance(self.expression, IndexExpression):
            raise TypeError("expression must be an IndexExpression typed template")
        if self.prefix_length is not None and (
            not isinstance(self.prefix_length, int)
            or isinstance(self.prefix_length, bool)
            or self.prefix_length <= 0
        ):
            raise ValueError("prefix_length must be a positive integer")
        if self.expression is not None and self.prefix_length is not None:
            raise ValueError("functional index parts cannot have a prefix length")

    def render(self) -> str:
        if self.column_name is not None:
            rendered = _quote(self.column_name)
            if self.prefix_length is not None:
                rendered += f"({self.prefix_length})"
        else:
            assert self.expression is not None
            rendered = f"({self.expression.render()})"
        if self.direction is SortDirection.DESC:
            rendered += " DESC"
        return rendered


@dataclass(frozen=True, slots=True)
class IndexDef:
    name: str
    parts: tuple[IndexPart, ...]
    unique: bool = False
    primary: bool = False
    kind: IndexKind = IndexKind.BTREE
    visible: bool = True

    def __post_init__(self) -> None:
        if self.name != "PRIMARY":
            _require_identifier(self.name, "index name")
        object.__setattr__(self, "parts", tuple(self.parts))
        if not isinstance(self.unique, bool) or not isinstance(self.primary, bool):
            raise TypeError("unique and primary must be booleans")
        if not self.parts or len(self.parts) > 16:
            raise ValueError("an index must contain between 1 and 16 parts")
        if self.primary and (self.name != "PRIMARY" or not self.unique):
            raise ValueError("a primary index must be named PRIMARY and be unique")
        if self.primary and self.kind is not IndexKind.BTREE:
            raise ValueError("a primary index must use BTREE")
        if not isinstance(self.visible, bool):
            raise TypeError("visible must be a boolean")
        if self.primary and not self.visible:
            raise ValueError("a primary index cannot be invisible")
        if (
            self.kind in {IndexKind.FULLTEXT, IndexKind.SPATIAL, IndexKind.MULTIVALUE}
            and self.primary
        ):
            raise ValueError("special indexes cannot be primary")

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(part.column_name for part in self.parts if part.column_name is not None)

    def render(self) -> str:
        rendered_parts = ", ".join(part.render() for part in self.parts)
        if self.primary:
            return f"PRIMARY KEY ({rendered_parts})"
        if self.kind is IndexKind.FULLTEXT:
            prefix = "FULLTEXT KEY"
        elif self.kind is IndexKind.SPATIAL:
            prefix = "SPATIAL KEY"
        elif self.unique:
            prefix = "UNIQUE KEY"
        else:
            prefix = "KEY"
        visibility = "" if self.visible else " INVISIBLE"
        return f"{prefix} {_quote(self.name)} ({rendered_parts}){visibility}"


@dataclass(frozen=True, slots=True)
class PartitionDef:
    method: str
    columns: tuple[str, ...]
    partitions: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "columns", tuple(self.columns))
        if self.method not in {
            "HASH",
            "KEY",
            "RANGE",
            "LIST",
            "RANGE COLUMNS",
            "LIST COLUMNS",
        }:
            raise ValueError("unsupported MySQL 8.0.41 partition method")
        if not self.columns:
            raise ValueError("partition columns must not be empty")
        for name in self.columns:
            _require_identifier(name, "partition column")
        if not isinstance(self.partitions, int) or isinstance(self.partitions, bool):
            raise ValueError("partitions must be an integer")
        if not 1 <= self.partitions <= 8192:
            raise ValueError("partitions must be between 1 and 8192")

    def render(self) -> str:
        columns = ", ".join(_quote(name) for name in self.columns)
        if self.method == "HASH":
            return f"PARTITION BY HASH ({columns}) PARTITIONS {self.partitions}"
        if self.method == "KEY":
            return f"PARTITION BY KEY ({columns}) PARTITIONS {self.partitions}"
        definitions: list[str] = []
        for index in range(self.partitions):
            if self.method.startswith("RANGE"):
                boundary = "MAXVALUE" if index == self.partitions - 1 else str((index + 1) * 1000)
                clause = f"VALUES LESS THAN ({boundary})"
            else:
                clause = f"VALUES IN ({index})"
            definitions.append(f"PARTITION {_quote(f'p{index}')} {clause}")
        body = ",\n  ".join(definitions)
        expression = (
            f"MOD({_quote(self.columns[0])}, {self.partitions})"
            if self.method == "LIST"
            else columns
        )
        return f"PARTITION BY {self.method} ({expression}) (\n  {body}\n)"

    def bucket_for_identity(self, identity: int) -> int:
        if self.method not in {"LIST", "LIST COLUMNS"}:
            raise ValueError("bucket routing is only defined for LIST profiles")
        if not isinstance(identity, int) or isinstance(identity, bool):
            raise TypeError("identity must be an integer")
        return identity % self.partitions


@dataclass(frozen=True, slots=True)
class ForeignKeyDef:
    name: str
    columns: tuple[str, ...]
    referenced_table: str
    referenced_columns: tuple[str, ...]
    on_delete: str = "RESTRICT"
    on_update: str = "RESTRICT"

    def __post_init__(self) -> None:
        _require_identifier(self.name, "foreign key name")
        _require_identifier(self.referenced_table, "referenced table")
        object.__setattr__(self, "columns", tuple(self.columns))
        object.__setattr__(self, "referenced_columns", tuple(self.referenced_columns))
        if not self.columns or len(self.columns) != len(self.referenced_columns):
            raise ValueError("foreign key columns must be nonempty and have equal arity")
        for name in (*self.columns, *self.referenced_columns):
            _require_identifier(name, "foreign key column")
        actions = {"RESTRICT", "CASCADE", "SET NULL", "NO ACTION"}
        if self.on_delete not in actions or self.on_update not in actions:
            raise ValueError("unsupported foreign key action")

    def render(self) -> str:
        columns = ", ".join(_quote(name) for name in self.columns)
        referenced = ", ".join(_quote(name) for name in self.referenced_columns)
        return (
            f"CONSTRAINT {_quote(self.name)} FOREIGN KEY ({columns}) "
            f"REFERENCES {_quote(self.referenced_table)} ({referenced}) "
            f"ON DELETE {self.on_delete} ON UPDATE {self.on_update}"
        )


@dataclass(frozen=True, slots=True)
class TableDef:
    name: str
    temporary: bool
    columns: tuple[ColumnDef, ...]
    indexes: tuple[IndexDef, ...]
    partition: PartitionDef | None = None
    foreign_keys: tuple[ForeignKeyDef, ...] = ()
    engine: str = "InnoDB"
    row_format: str = "DYNAMIC"
    default_charset: str = "utf8mb4"
    default_collation: str = "utf8mb4_0900_ai_ci"

    def __post_init__(self) -> None:
        _require_identifier(self.name, "table name")
        if not isinstance(self.temporary, bool):
            raise TypeError("temporary must be a boolean")
        object.__setattr__(self, "columns", tuple(self.columns))
        object.__setattr__(self, "indexes", tuple(self.indexes))
        object.__setattr__(self, "foreign_keys", tuple(self.foreign_keys))
        if not self.columns:
            raise ValueError("a table must have columns")
        if len({column.name for column in self.columns}) != len(self.columns):
            raise ValueError("column names must be unique within a table")
        if len({index.name for index in self.indexes}) != len(self.indexes):
            raise ValueError("index names must be unique within a table")
        if len({foreign_key.name for foreign_key in self.foreign_keys}) != len(self.foreign_keys):
            raise ValueError("foreign key names must be unique within a table")
        if self.engine != "InnoDB":
            raise ValueError("schema profiles require InnoDB")
        if self.row_format not in {"DYNAMIC", "COMPACT", "REDUNDANT", "COMPRESSED"}:
            raise ValueError("row_format is not supported by MySQL 8.0.41")
        _require_identifier(self.default_charset, "default charset")
        _require_identifier(self.default_collation, "default collation")
        if self.default_collation not in _SUPPORTED_COLLATIONS.get(
            self.default_charset, frozenset()
        ):
            raise ValueError("default charset and collation are not a supported pair")

    def column(self, name: str) -> ColumnDef:
        for column in self.columns:
            if column.name == name:
                return column
        raise KeyError(name)

    def render(self) -> str:
        definitions = [column.render() for column in self.columns]
        definitions.extend(index.render() for index in self.indexes)
        definitions.extend(foreign_key.render() for foreign_key in self.foreign_keys)
        temporary = "TEMPORARY " if self.temporary else ""
        sql = (
            f"CREATE {temporary}TABLE {_quote(self.name)} (\n  "
            + ",\n  ".join(definitions)
            + f"\n) ENGINE={self.engine} ROW_FORMAT={self.row_format} "
            + f"DEFAULT CHARSET={self.default_charset} COLLATE={self.default_collation}"
        )
        if self.partition is not None:
            sql += "\n" + self.partition.render()
        return sql + ";"


@dataclass(frozen=True, slots=True)
class SchemaManifest:
    profile: SchemaProfile
    target_feature_id: str
    seed: int
    tables: tuple[TableDef, ...]
    requires_same_session: bool = False
    limits_identity: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.profile, SchemaProfile):
            raise TypeError("profile must be a SchemaProfile")
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise TypeError("seed must be an integer")
        if not isinstance(self.requires_same_session, bool):
            raise TypeError("requires_same_session must be a boolean")
        _require_identifier(self.target_feature_id, "target feature ID")
        object.__setattr__(self, "tables", tuple(self.tables))
        if not self.tables:
            raise ValueError("schema manifest must contain tables")
        if len({table.name for table in self.tables}) != len(self.tables):
            raise ValueError("table names must be unique")

    def render_setup_sql(self) -> str:
        return "\n\n".join(table.render() for table in self.tables) + "\n"

    def canonical_bytes(self) -> bytes:
        payload = {
            "limits_identity": self.limits_identity,
            "profile": self.profile.value,
            "requires_same_session": self.requires_same_session,
            "seed": self.seed,
            "tables": [_table_payload(table) for table in self.tables],
            "target_feature_id": self.target_feature_id,
        }
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


def _table_payload(table: TableDef) -> dict[str, object]:
    return {
        "columns": [
            {
                "charset": column.charset,
                "collation": column.collation,
                "mysql_type": column.mysql_type,
                "name": column.name,
                "nullable": column.nullable,
                "srid": column.srid,
            }
            for column in table.columns
        ],
        "default_charset": table.default_charset,
        "default_collation": table.default_collation,
        "engine": table.engine,
        "foreign_keys": [
            {
                "columns": foreign_key.columns,
                "name": foreign_key.name,
                "on_delete": foreign_key.on_delete,
                "on_update": foreign_key.on_update,
                "referenced_columns": foreign_key.referenced_columns,
                "referenced_table": foreign_key.referenced_table,
            }
            for foreign_key in table.foreign_keys
        ],
        "indexes": [
            {
                "kind": index.kind.value,
                "name": index.name,
                "parts": [
                    {
                        "column_name": part.column_name,
                        "direction": part.direction.value,
                        "expression": (
                            None
                            if part.expression is None
                            else {
                                "cast_length": part.expression.cast_length,
                                "column_name": part.expression.column_name,
                                "kind": part.expression.kind.value,
                            }
                        ),
                        "prefix_length": part.prefix_length,
                    }
                    for part in index.parts
                ],
                "primary": index.primary,
                "unique": index.unique,
                "visible": index.visible,
            }
            for index in table.indexes
        ],
        "name": table.name,
        "partition": (
            None
            if table.partition is None
            else {
                "columns": table.partition.columns,
                "method": table.partition.method,
                "partitions": table.partition.partitions,
            }
        ),
        "row_format": table.row_format,
        "temporary": table.temporary,
    }


class SchemaGenerator:
    """Build profile-isolated schemas using independent hierarchical seed paths."""

    def __init__(self, rules: SchemaRules | None = None) -> None:
        if rules is None:
            from select_fuzz.generation.schema_rules import SchemaRules

            rules = SchemaRules.mysql_8041()
        self.rules = rules

    @staticmethod
    def boundary_declarations(
        limits: SchemaLimits,
    ) -> tuple[BoundaryDeclaration, ...]:
        """Enumerate model-valid non-JSON, non-spatial declaration boundaries."""

        varchar_max = min(16_383, limits.max_varchar_characters)
        varbinary_max = min(65_535, limits.max_varbinary_bytes)

        def boundary(
            boundary_id: BoundaryDeclarationId,
            declaration: str,
            *,
            deprecated: bool = False,
        ) -> BoundaryDeclaration:
            tags = frozenset({"deprecated"}) if deprecated else frozenset()
            return BoundaryDeclaration(boundary_id, declaration, tags)

        return (
            boundary(BoundaryDeclarationId.TINYINT_SIGNED, "TINYINT"),
            boundary(BoundaryDeclarationId.TINYINT_UNSIGNED, "TINYINT UNSIGNED"),
            boundary(BoundaryDeclarationId.SMALLINT_SIGNED, "SMALLINT"),
            boundary(BoundaryDeclarationId.SMALLINT_UNSIGNED, "SMALLINT UNSIGNED"),
            boundary(BoundaryDeclarationId.MEDIUMINT_SIGNED, "MEDIUMINT"),
            boundary(BoundaryDeclarationId.MEDIUMINT_UNSIGNED, "MEDIUMINT UNSIGNED"),
            boundary(BoundaryDeclarationId.INT_SIGNED, "INT"),
            boundary(BoundaryDeclarationId.INT_UNSIGNED, "INT UNSIGNED"),
            boundary(BoundaryDeclarationId.BIGINT_SIGNED, "BIGINT"),
            boundary(BoundaryDeclarationId.BIGINT_UNSIGNED, "BIGINT UNSIGNED"),
            boundary(BoundaryDeclarationId.BIT_LENGTH_1, "BIT(1)"),
            boundary(BoundaryDeclarationId.BIT_LENGTH_64, "BIT(64)"),
            boundary(BoundaryDeclarationId.DECIMAL_P1_S0, "DECIMAL(1,0)"),
            boundary(BoundaryDeclarationId.DECIMAL_P1_S1, "DECIMAL(1,1)"),
            boundary(BoundaryDeclarationId.DECIMAL_P30_S30, "DECIMAL(30,30)"),
            boundary(BoundaryDeclarationId.DECIMAL_P31_S30, "DECIMAL(31,30)"),
            boundary(BoundaryDeclarationId.DECIMAL_P65_S0, "DECIMAL(65,0)"),
            boundary(BoundaryDeclarationId.DECIMAL_P65_S30, "DECIMAL(65,30)"),
            boundary(BoundaryDeclarationId.FLOAT_SIGNED, "FLOAT"),
            boundary(
                BoundaryDeclarationId.FLOAT_UNSIGNED,
                "FLOAT UNSIGNED",
                deprecated=True,
            ),
            boundary(BoundaryDeclarationId.DOUBLE_SIGNED, "DOUBLE"),
            boundary(
                BoundaryDeclarationId.DOUBLE_UNSIGNED,
                "DOUBLE UNSIGNED",
                deprecated=True,
            ),
            boundary(BoundaryDeclarationId.CHAR_LENGTH_0, "CHAR(0)"),
            boundary(BoundaryDeclarationId.CHAR_LENGTH_1, "CHAR(1)"),
            boundary(BoundaryDeclarationId.CHAR_LENGTH_MAX, "CHAR(255)"),
            boundary(BoundaryDeclarationId.VARCHAR_LENGTH_0, "VARCHAR(0)"),
            boundary(BoundaryDeclarationId.VARCHAR_LENGTH_1, "VARCHAR(1)"),
            boundary(
                BoundaryDeclarationId.VARCHAR_LENGTH_MAX,
                f"VARCHAR({varchar_max})",
            ),
            boundary(BoundaryDeclarationId.BINARY_LENGTH_0, "BINARY(0)"),
            boundary(BoundaryDeclarationId.BINARY_LENGTH_1, "BINARY(1)"),
            boundary(BoundaryDeclarationId.BINARY_LENGTH_MAX, "BINARY(255)"),
            boundary(BoundaryDeclarationId.VARBINARY_LENGTH_0, "VARBINARY(0)"),
            boundary(BoundaryDeclarationId.VARBINARY_LENGTH_1, "VARBINARY(1)"),
            boundary(
                BoundaryDeclarationId.VARBINARY_LENGTH_MAX,
                f"VARBINARY({varbinary_max})",
            ),
            boundary(BoundaryDeclarationId.DATE, "DATE"),
            boundary(BoundaryDeclarationId.TIME_FSP_0, "TIME(0)"),
            boundary(BoundaryDeclarationId.TIME_FSP_6, "TIME(6)"),
            boundary(BoundaryDeclarationId.DATETIME_FSP_0, "DATETIME(0)"),
            boundary(BoundaryDeclarationId.DATETIME_FSP_6, "DATETIME(6)"),
            boundary(BoundaryDeclarationId.TIMESTAMP_FSP_0, "TIMESTAMP(0)"),
            boundary(BoundaryDeclarationId.TIMESTAMP_FSP_6, "TIMESTAMP(6)"),
            boundary(BoundaryDeclarationId.YEAR, "YEAR"),
            boundary(BoundaryDeclarationId.TINYTEXT, "TINYTEXT"),
            boundary(BoundaryDeclarationId.TEXT, "TEXT"),
            boundary(BoundaryDeclarationId.MEDIUMTEXT, "MEDIUMTEXT"),
            boundary(BoundaryDeclarationId.LONGTEXT, "LONGTEXT"),
            boundary(BoundaryDeclarationId.TINYBLOB, "TINYBLOB"),
            boundary(BoundaryDeclarationId.BLOB, "BLOB"),
            boundary(BoundaryDeclarationId.MEDIUMBLOB, "MEDIUMBLOB"),
            boundary(BoundaryDeclarationId.LONGBLOB, "LONGBLOB"),
            boundary(BoundaryDeclarationId.ENUM, "ENUM('a','z')"),
            boundary(BoundaryDeclarationId.SET, "SET('a','b','c')"),
        )

    @classmethod
    def declaration_pool(cls, limits: SchemaLimits) -> tuple[str, ...]:
        """Expose isolated grammar boundaries, including legacy special types."""

        non_special = tuple(boundary.declaration for boundary in cls.boundary_declarations(limits))
        return non_special + (
            "JSON",
            "GEOMETRY",
            "POINT",
            "LINESTRING",
            "POLYGON",
            "MULTIPOINT",
            "MULTILINESTRING",
            "MULTIPOLYGON",
            "GEOMETRYCOLLECTION",
        )

    @classmethod
    def executable_boundary_declarations(
        cls, limits: SchemaLimits
    ) -> tuple[BoundaryDeclaration, ...]:
        """Return typed non-special boundaries that fit beside required columns."""

        available = min(limits.row_byte_budget, 65_535) - 2048
        if available <= 0:
            raise ValueError("boundary lane requires at least 2049 row bytes")
        varchar_max = min(16_383, limits.max_varchar_characters, available // 4)
        varbinary_max = min(65_535, limits.max_varbinary_bytes, available)
        grammar_varchar_max = f"VARCHAR({min(16_383, limits.max_varchar_characters)})"
        grammar_varbinary_max = f"VARBINARY({min(65_535, limits.max_varbinary_bytes)})"
        executable: list[BoundaryDeclaration] = []
        for boundary in cls.boundary_declarations(limits):
            declaration = boundary.declaration
            if declaration == grammar_varchar_max and declaration not in {
                "VARCHAR(0)",
                "VARCHAR(1)",
            }:
                declaration = f"VARCHAR({max(1, varchar_max)})"
            elif declaration == grammar_varbinary_max and declaration not in {
                "VARBINARY(0)",
                "VARBINARY(1)",
            }:
                declaration = f"VARBINARY({max(1, varbinary_max)})"
            executable.append(replace(boundary, declaration=declaration))
        return tuple(executable)

    @classmethod
    def executable_boundary_pool(cls, limits: SchemaLimits) -> tuple[str, ...]:
        """Return boundaries that fit beside the mandatory columns in a real table."""

        non_special = tuple(
            boundary.declaration for boundary in cls.executable_boundary_declarations(limits)
        )
        return non_special + cls.declaration_pool(limits)[len(cls.boundary_declarations(limits)) :]

    @classmethod
    def boundary_column(
        cls,
        *,
        name: str,
        ordinal: int,
        limits: SchemaLimits,
    ) -> ColumnDef:
        """Select every grammar boundary through an explicit directed lane."""

        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
            raise ValueError("boundary ordinal must be a nonnegative integer")
        pool = cls.executable_boundary_pool(limits)
        declaration = pool[ordinal % len(pool)]
        base_type = declaration.split("(", 1)[0].split(" ", 1)[0]
        if base_type in {
            "CHAR",
            "VARCHAR",
            "TINYTEXT",
            "TEXT",
            "MEDIUMTEXT",
            "LONGTEXT",
            "ENUM",
            "SET",
        }:
            return ColumnDef(
                name,
                declaration,
                True,
                "utf8mb4",
                "utf8mb4_0900_ai_ci",
            )
        if base_type in _GEOMETRY_TYPES:
            return ColumnDef(name, declaration, False, srid=4326)
        return ColumnDef(name, declaration, True)

    @classmethod
    def typed_boundary_column(
        cls,
        *,
        name: str,
        boundary_id: BoundaryDeclarationId,
        limits: SchemaLimits,
    ) -> ColumnDef:
        """Build a production boundary column without JSON or spatial types."""

        if not isinstance(boundary_id, BoundaryDeclarationId):
            raise TypeError("boundary_id must be a BoundaryDeclarationId")
        boundary = next(
            item
            for item in cls.executable_boundary_declarations(limits)
            if item.boundary_id is boundary_id
        )
        declaration = boundary.declaration
        base_type = declaration.split("(", 1)[0].split(" ", 1)[0]
        if base_type in {
            "CHAR",
            "VARCHAR",
            "TINYTEXT",
            "TEXT",
            "MEDIUMTEXT",
            "LONGTEXT",
            "ENUM",
            "SET",
        }:
            return ColumnDef(
                name,
                declaration,
                True,
                "utf8mb4",
                "utf8mb4_0900_ai_ci",
            )
        return ColumnDef(name, declaration, True)

    def generate(
        self,
        target: FeatureSpec,
        *,
        seed: int,
        limits: SchemaLimits,
        boundary_ordinal: int | None = None,
        typed_boundary_id: BoundaryDeclarationId | None = None,
    ) -> SchemaManifest:
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise TypeError("seed must be an integer")
        if limits.max_columns < 2:
            raise ValueError("schema profiles require at least two columns per table")
        supported = {profile.value: profile for profile in SchemaProfile}
        compatible = sorted(set(target.compatible_profiles) & supported.keys())
        if not compatible:
            raise ValueError("target has no compatible implemented schema profile")
        tree = SeedTree(seed)
        if boundary_ordinal is not None and (
            not isinstance(boundary_ordinal, int)
            or isinstance(boundary_ordinal, bool)
            or boundary_ordinal < 0
        ):
            raise ValueError("boundary ordinal must be a nonnegative integer")
        if typed_boundary_id is not None and not isinstance(
            typed_boundary_id, BoundaryDeclarationId
        ):
            raise TypeError("typed_boundary_id must be a BoundaryDeclarationId")
        if boundary_ordinal is not None and typed_boundary_id is not None:
            raise ValueError("only one boundary lane may be selected")
        boundary_selected = boundary_ordinal is not None or typed_boundary_id is not None
        if boundary_selected:
            if SchemaProfile.REGULAR_INNODB.value not in compatible:
                raise ValueError("boundary lane requires a regular_innodb target")
            if limits.max_columns < 3:
                raise ValueError("boundary lane requires at least three columns")
            profile = SchemaProfile.REGULAR_INNODB
        else:
            profile_rng = random.Random(
                tree.derive(target.feature_id, limits.identity(), "profile")
            )
            profile = supported[profile_rng.choice(compatible)]
        boundary_identity = ""
        if boundary_ordinal is not None:
            boundary_identity = f":boundary={boundary_ordinal}"
        elif typed_boundary_id is not None:
            boundary_identity = f":typed_boundary={typed_boundary_id.value}"
        identity = limits.identity() + boundary_identity
        if profile is SchemaProfile.TEMPORARY_INNODB and limits.row_format == "COMPRESSED":
            raise ValueError("temporary_innodb does not support COMPRESSED row format")
        minimum_tables = max(
            limits.min_tables, 2 if profile is SchemaProfile.FOREIGN_KEY_GRAPH else 1
        )
        if minimum_tables > limits.max_tables:
            raise ValueError("foreign_key_graph requires at least two tables")
        if (
            profile
            in {
                SchemaProfile.FOREIGN_KEY_GRAPH,
                SchemaProfile.FULLTEXT_INNODB,
                SchemaProfile.SPATIAL_INNODB,
                SchemaProfile.JSON_MULTIVALUE_INNODB,
            }
            and limits.max_indexes_per_table < 2
        ):
            raise ValueError(f"{profile.value} requires a primary and a special index")
        count_rng = random.Random(
            tree.derive(target.feature_id, identity, profile.value, "table_count")
        )
        table_count = count_rng.randint(minimum_tables, limits.max_tables)
        tables = tuple(
            self._build_table(
                profile=profile,
                table_index=index,
                table_count=table_count,
                tree=tree,
                path=(target.feature_id, identity, profile.value),
                limits=limits,
                boundary_ordinal=boundary_ordinal if index == 0 else None,
                typed_boundary_id=typed_boundary_id if index == 0 else None,
            )
            for index in range(table_count)
        )
        manifest = SchemaManifest(
            profile=profile,
            target_feature_id=target.feature_id,
            seed=seed,
            tables=tables,
            requires_same_session=profile is SchemaProfile.TEMPORARY_INNODB,
            limits_identity=identity,
        )
        self.rules.validate(manifest, limits=limits)
        return manifest

    def _build_table(
        self,
        *,
        profile: SchemaProfile,
        table_index: int,
        table_count: int,
        tree: SeedTree,
        path: tuple[str, ...],
        limits: SchemaLimits,
        boundary_ordinal: int | None,
        typed_boundary_id: BoundaryDeclarationId | None,
    ) -> TableDef:
        name = f"t{table_index}"
        partition = None
        if profile is SchemaProfile.PARTITIONED_INNODB:
            partition_rng = random.Random(tree.derive(*path, "table", table_index, "partition"))
            methods = ["HASH", "KEY", "RANGE", "LIST", "RANGE COLUMNS"]
            if limits.max_columns >= 3 and _effective_index_budget(limits) >= 9:
                methods.append("LIST COLUMNS")
            method = partition_rng.choice(methods)
            partition = PartitionDef(
                method=method,
                columns=("partition_bucket",) if method == "LIST COLUMNS" else ("id",),
                partitions=partition_rng.randint(1, min(16, limits.max_partitions)),
            )
        column_rng = random.Random(tree.derive(*path, "table", table_index, "column_count"))
        boundary_selected = boundary_ordinal is not None or typed_boundary_id is not None
        column_count = (
            max(3, limits.min_columns)
            if boundary_selected
            else column_rng.randint(limits.min_columns, limits.max_columns)
        )
        columns = self._required_columns(
            profile,
            table_index,
            table_count,
            limits,
            partition.method if partition is not None else None,
        )
        if boundary_ordinal is not None:
            columns.append(
                self.boundary_column(
                    name="boundary_col",
                    ordinal=boundary_ordinal,
                    limits=limits,
                )
            )
        elif typed_boundary_id is not None:
            columns.append(
                self.typed_boundary_column(
                    name="boundary_col",
                    boundary_id=typed_boundary_id,
                    limits=limits,
                )
            )
        random_column_count = max(1, column_count - len(columns))
        logical_column_share = max(
            8,
            (limits.row_byte_budget - 2048) // random_column_count,
        )
        while len(columns) < column_count:
            column_index = len(columns)
            columns.append(
                self._random_column(
                    name=f"c{column_index}",
                    rng=random.Random(
                        tree.derive(*path, "table", table_index, "column", column_index)
                    ),
                    limits=limits,
                    logical_byte_share=logical_column_share,
                )
            )

        primary_mode = "single"
        if profile is SchemaProfile.REGULAR_INNODB:
            payload = next(column for column in columns if column.name == "payload")
            payload_match = re.search(r"\(([0-9]+)\)", payload.mysql_type)
            assert payload_match is not None
            composite_bytes = 8 + int(payload_match.group(1)) * 4
            primary_modes = ["none", "single"]
            if composite_bytes <= _effective_index_budget(limits):
                primary_modes.append("composite")
            primary_rng = random.Random(tree.derive(*path, "table", table_index, "primary_key"))
            primary_mode = primary_rng.choice(primary_modes)
            if primary_mode == "composite":
                columns = [
                    replace(column, nullable=False) if column.name == "payload" else column
                    for column in columns
                ]

        indexes = self._indexes_for(
            profile=profile,
            table_index=table_index,
            columns=tuple(columns),
            rng=random.Random(tree.derive(*path, "table", table_index, "indexes")),
            limits=limits,
            primary_mode=primary_mode,
            partition_columns=partition.columns if partition is not None else (),
        )
        foreign_keys: tuple[ForeignKeyDef, ...] = ()
        if profile is SchemaProfile.FOREIGN_KEY_GRAPH and table_index > 0:
            composite = any(column.name == "parent_tenant_id" for column in columns)
            edges = [
                ForeignKeyDef(
                    name=f"fk_t{table_index}_parent",
                    columns=("parent_id", "parent_tenant_id") if composite else ("parent_id",),
                    referenced_table="t0",
                    referenced_columns=("id", "tenant_id") if composite else ("id",),
                )
            ]
            if any(column.name == "other_parent_id" for column in columns):
                edges.append(
                    ForeignKeyDef(
                        name=f"fk_t{table_index}_other_parent",
                        columns=("other_parent_id",),
                        referenced_table="t1",
                        referenced_columns=("id",),
                    )
                )
            foreign_keys = tuple(edges)
        return TableDef(
            name=name,
            temporary=profile is SchemaProfile.TEMPORARY_INNODB,
            columns=tuple(columns),
            indexes=indexes,
            partition=partition,
            foreign_keys=foreign_keys,
            row_format=limits.row_format,
        )

    @staticmethod
    def _required_columns(
        profile: SchemaProfile,
        table_index: int,
        table_count: int,
        limits: SchemaLimits,
        partition_method: str | None,
    ) -> list[ColumnDef]:
        identifier = ColumnDef("id", "BIGINT UNSIGNED", False)
        payload_length = 255
        if limits.row_format in {"COMPACT", "REDUNDANT"}:
            payload_length = min(payload_length, max(1, _inline_column_share(limits) // 4))
        payload = ColumnDef(
            "payload",
            f"VARCHAR({payload_length})",
            True,
            "utf8mb4",
            "utf8mb4_0900_ai_ci",
        )
        if profile is SchemaProfile.FOREIGN_KEY_GRAPH:
            if table_index > 0:
                nullable_fk = table_index % 2 == 0
                columns = [
                    identifier,
                    ColumnDef("parent_id", "BIGINT UNSIGNED", nullable_fk),
                ]
                if limits.max_columns >= 3 and _effective_index_budget(limits) >= 16:
                    columns.append(ColumnDef("parent_tenant_id", "BIGINT UNSIGNED", nullable_fk))
                if (
                    table_index == 2
                    and table_count >= 3
                    and limits.max_columns >= 4
                    and limits.max_indexes_per_table >= 3
                ):
                    columns.append(ColumnDef("other_parent_id", "BIGINT UNSIGNED", True))
                return columns
            columns = [identifier, payload]
            if limits.max_columns >= 3 and _effective_index_budget(limits) >= 16:
                columns.append(ColumnDef("tenant_id", "BIGINT UNSIGNED", False))
            return columns
        if profile is SchemaProfile.FULLTEXT_INNODB:
            return [
                identifier,
                ColumnDef("body", "LONGTEXT", False, "utf8mb4", "utf8mb4_0900_ai_ci"),
            ]
        if profile is SchemaProfile.SPATIAL_INNODB:
            return [identifier, ColumnDef("location", "POINT", False, srid=4326)]
        if profile is SchemaProfile.JSON_MULTIVALUE_INNODB:
            return [identifier, ColumnDef("tags", "JSON", False)]
        columns = [identifier, payload]
        if partition_method == "LIST COLUMNS":
            columns.append(ColumnDef("partition_bucket", "TINYINT UNSIGNED", False))
        return columns

    @staticmethod
    def _random_column(
        *,
        name: str,
        rng: random.Random,
        limits: SchemaLimits,
        logical_byte_share: int,
    ) -> ColumnDef:
        families = list(range(16))
        if limits.row_format in {"COMPACT", "REDUNDANT"}:
            families.remove(12)
            families.remove(13)
        family = rng.choice(families)
        nullable = bool(rng.randrange(2))
        inline_share = _inline_column_share(limits)
        if family == 0:
            base = rng.choice(("TINYINT", "SMALLINT", "MEDIUMINT", "INT", "BIGINT"))
            declaration = base + (" UNSIGNED" if rng.randrange(2) else "")
        elif family == 1:
            declaration = f"BIT({rng.randint(1, 64)})"
        elif family == 2:
            precision = rng.randint(1, 65)
            declaration = f"DECIMAL({precision},{rng.randint(0, min(30, precision))})"
        elif family == 3:
            declaration = rng.choice(("FLOAT", "DOUBLE"))
        elif family == 4:
            declaration = f"CHAR({rng.randint(1, min(255, max(1, inline_share // 4)))})"
            return ColumnDef(name, declaration, nullable, "utf8mb4", "utf8mb4_0900_ai_ci")
        elif family == 5:
            varchar_cap = min(
                limits.max_varchar_characters,
                max(1, logical_byte_share // 4),
            )
            if limits.row_format in {"COMPACT", "REDUNDANT"}:
                varchar_cap = min(varchar_cap, max(1, inline_share // 4))
            length = rng.randint(1, varchar_cap)
            declaration = f"VARCHAR({length})"
            return ColumnDef(name, declaration, nullable, "utf8mb4", "utf8mb4_0900_ai_ci")
        elif family == 6:
            declaration = f"BINARY({rng.randint(1, min(255, inline_share))})"
        elif family == 7:
            varbinary_cap = min(limits.max_varbinary_bytes, logical_byte_share)
            if limits.row_format in {"COMPACT", "REDUNDANT"}:
                varbinary_cap = min(varbinary_cap, inline_share)
            declaration = f"VARBINARY({rng.randint(1, varbinary_cap)})"
        elif family == 8:
            declaration = rng.choice(("DATE", "YEAR"))
        elif family == 9:
            declaration = f"TIME({rng.randint(0, 6)})"
        elif family == 10:
            declaration = f"DATETIME({rng.randint(0, 6)})"
        elif family == 11:
            declaration = f"TIMESTAMP({rng.randint(0, 6)})"
        elif family == 12:
            declaration = rng.choice(("TINYTEXT", "TEXT", "MEDIUMTEXT", "LONGTEXT"))
            return ColumnDef(name, declaration, nullable, "utf8mb4", "utf8mb4_0900_ai_ci")
        elif family == 13:
            # JSON has a dedicated opt-in profile. Keep default fuzz focused on
            # ordinary scalar/LOB types until JSON expansion is explicitly enabled.
            declaration = rng.choice(("TINYBLOB", "BLOB", "MEDIUMBLOB", "LONGBLOB"))
        elif family == 14:
            return ColumnDef(
                name,
                "ENUM('a','z')",
                nullable,
                "utf8mb4",
                "utf8mb4_0900_ai_ci",
            )
        else:
            return ColumnDef(
                name,
                "SET('a','b','c')",
                nullable,
                "utf8mb4",
                "utf8mb4_0900_ai_ci",
            )
        return ColumnDef(name, declaration, nullable)

    @staticmethod
    def _indexes_for(
        *,
        profile: SchemaProfile,
        table_index: int,
        columns: tuple[ColumnDef, ...],
        rng: random.Random,
        limits: SchemaLimits,
        primary_mode: str,
        partition_columns: tuple[str, ...],
    ) -> tuple[IndexDef, ...]:
        indexes: list[IndexDef] = []
        if primary_mode == "single":
            primary_columns = ["id"]
            primary_columns.extend(
                name for name in partition_columns if name not in primary_columns
            )
            indexes.append(
                IndexDef(
                    "PRIMARY",
                    tuple(IndexPart(column_name=name) for name in primary_columns),
                    unique=True,
                    primary=True,
                )
            )
        elif primary_mode == "composite":
            indexes.append(
                IndexDef(
                    "PRIMARY",
                    (IndexPart(column_name="id"), IndexPart(column_name="payload")),
                    unique=True,
                    primary=True,
                )
            )
        if profile is SchemaProfile.FOREIGN_KEY_GRAPH:
            if table_index == 0:
                if any(column.name == "tenant_id" for column in columns):
                    indexes.append(
                        IndexDef(
                            "ix_parent_ref_target",
                            (
                                IndexPart(column_name="id"),
                                IndexPart(column_name="tenant_id"),
                            ),
                            unique=bool(rng.randrange(2)),
                        )
                    )
            else:
                child_parts = [IndexPart(column_name="parent_id")]
                if any(column.name == "parent_tenant_id" for column in columns):
                    child_parts.append(IndexPart(column_name="parent_tenant_id"))
                indexes.append(
                    IndexDef(
                        "ix_parent_ref",
                        tuple(child_parts),
                        unique=table_index == 1,
                    )
                )
                if any(column.name == "other_parent_id" for column in columns):
                    indexes.append(
                        IndexDef(
                            "ix_other_parent",
                            (IndexPart(column_name="other_parent_id"),),
                        )
                    )
        elif profile is SchemaProfile.FULLTEXT_INNODB:
            indexes.append(
                IndexDef("ft_body", (IndexPart(column_name="body"),), kind=IndexKind.FULLTEXT)
            )
        elif profile is SchemaProfile.SPATIAL_INNODB:
            indexes.append(
                IndexDef(
                    "sx_location",
                    (IndexPart(column_name="location"),),
                    kind=IndexKind.SPATIAL,
                )
            )
        elif profile is SchemaProfile.JSON_MULTIVALUE_INNODB:
            multivalue_parts = []
            if _effective_index_budget(limits) >= 16:
                multivalue_parts.append(IndexPart(column_name="id"))
            multivalue_parts.append(
                IndexPart(expression=IndexExpression.json_unsigned_array("tags"))
            )
            indexes.append(
                IndexDef(
                    "mx_tags",
                    tuple(multivalue_parts),
                    unique=bool(rng.randrange(2)),
                    kind=IndexKind.MULTIVALUE,
                )
            )
        else:
            # The suite spans single, composite, descending, unique, prefix, and
            # functional forms. A deterministic random subset keeps each table small.
            physical_budget = _effective_index_budget(limits)
            payload_column = next(column for column in columns if column.name == "payload")
            payload_match = re.search(r"\(([0-9]+)\)", payload_column.mysql_type)
            assert payload_match is not None
            payload_characters = int(payload_match.group(1))
            payload_prefix = min(payload_characters, physical_budget // 4)
            composite_prefix = min(
                payload_characters,
                32,
                max(1, (physical_budget - 8) // 4),
            )
            candidates = [
                IndexDef(
                    "ix_payload",
                    (
                        IndexPart(
                            column_name="payload",
                            prefix_length=None if payload_prefix == 255 else payload_prefix,
                        ),
                    ),
                ),
                IndexDef(
                    "ix_id_desc",
                    (IndexPart(column_name="id", direction=SortDirection.DESC),),
                    visible=False,
                ),
                IndexDef(
                    "ix_payload_prefix",
                    (IndexPart(column_name="payload", prefix_length=min(16, payload_prefix)),),
                ),
                IndexDef(
                    "ix_payload_lower",
                    (
                        IndexPart(
                            expression=IndexExpression.lower_char(
                                "payload", min(191, payload_prefix)
                            )
                        ),
                    ),
                    kind=IndexKind.FUNCTIONAL,
                ),
            ]
            if physical_budget >= 12:
                candidates.append(
                    IndexDef(
                        "ix_composite",
                        (
                            IndexPart(
                                column_name="payload",
                                prefix_length=composite_prefix,
                            ),
                            IndexPart(column_name="id"),
                        ),
                    )
                )
            unique_suffix = (
                ("id",) + tuple(name for name in partition_columns if name != "id")
                if profile is SchemaProfile.PARTITIONED_INNODB
                else ()
            )
            by_name = {column.name: column for column in columns}
            unique_suffix_bytes = sum(_fixed_index_bytes(by_name[name]) for name in unique_suffix)
            unique_prefix = min(
                payload_characters,
                32,
                max(0, (physical_budget - unique_suffix_bytes) // 4),
            )
            if unique_prefix >= 1:
                candidates.append(
                    IndexDef(
                        "uq_id_payload",
                        (
                            IndexPart(
                                column_name="payload",
                                prefix_length=unique_prefix,
                            ),
                        )
                        + tuple(IndexPart(column_name=name) for name in unique_suffix),
                        unique=True,
                    )
                )
            random_columns = [
                column
                for column in columns
                if column.name.startswith("c")
                and column.base_type
                in {
                    "TINYINT",
                    "SMALLINT",
                    "MEDIUMINT",
                    "INT",
                    "BIGINT",
                    "BIT",
                    "DECIMAL",
                    "FLOAT",
                    "DOUBLE",
                    "DATE",
                    "TIME",
                    "DATETIME",
                    "TIMESTAMP",
                    "YEAR",
                }
                and _fixed_index_bytes(column) <= physical_budget
            ]
            if random_columns:
                random_column = rng.choice(random_columns)
                candidates.append(
                    IndexDef(
                        "ix_random_col",
                        (
                            IndexPart(
                                column_name=random_column.name,
                                direction=rng.choice((SortDirection.ASC, SortDirection.DESC)),
                            ),
                        ),
                    )
                )
            rng.shuffle(candidates)
            capacity = max(0, limits.max_indexes_per_table - len(indexes))
            selected_count = rng.randint(0, min(len(candidates), capacity))
            indexes.extend(candidates[:selected_count])
        return tuple(indexes[: limits.max_indexes_per_table])


def _inline_row_limit(limits: SchemaLimits) -> int:
    page_fraction = 4 if limits.row_format == "COMPRESSED" else 2
    return max(512, min(16_000, limits.page_size // page_fraction - 256))


def _fixed_index_bytes(column: ColumnDef) -> int:
    base = column.base_type
    sizes = {
        "TINYINT": 1,
        "SMALLINT": 2,
        "MEDIUMINT": 3,
        "INT": 4,
        "BIGINT": 8,
        "FLOAT": 4,
        "DOUBLE": 8,
        "DATE": 3,
        "TIME": 7,
        "DATETIME": 8,
        "TIMESTAMP": 7,
        "YEAR": 1,
    }
    if base in sizes:
        return sizes[base]
    match = re.search(r"\(([0-9]+)", column.mysql_type)
    length = int(match.group(1)) if match is not None else 1
    if base == "BIT":
        return (length + 7) // 8
    if base == "DECIMAL":
        return (length + 1) // 2 + 1
    return 16


def _effective_index_budget(limits: SchemaLimits) -> int:
    physical = (
        767
        if limits.row_format in {"COMPACT", "REDUNDANT"}
        else min(3072, limits.page_size * 3 // 16)
    )
    return min(limits.index_byte_budget, physical)


def _inline_column_share(limits: SchemaLimits) -> int:
    # Reserve enough for headers and one mandatory off-page-capable scene column
    # (for example LONGTEXT in the FULLTEXT profile).
    return max(8, (_inline_row_limit(limits) - 1024) // limits.max_columns)


__all__ = [
    "BoundaryDeclaration",
    "BoundaryDeclarationId",
    "ColumnDef",
    "ForeignKeyDef",
    "IndexDef",
    "IndexExpression",
    "IndexExpressionKind",
    "IndexKind",
    "IndexPart",
    "PartitionDef",
    "SchemaGenerator",
    "SchemaLimits",
    "SchemaManifest",
    "SchemaProfile",
    "SortDirection",
    "TableDef",
]
