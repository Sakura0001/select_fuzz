"""Explicit MySQL 8.0.41 schema compatibility predicates."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import ClassVar, NoReturn

from select_fuzz.generation.schema import (
    ColumnDef,
    IndexDef,
    IndexExpressionKind,
    IndexKind,
    IndexPart,
    SchemaLimits,
    SchemaManifest,
    SchemaProfile,
    TableDef,
)


_LARGE_OBJECT_TYPES = frozenset(
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
_TEXT_TYPES = frozenset({"CHAR", "VARCHAR", "TINYTEXT", "TEXT", "MEDIUMTEXT", "LONGTEXT"})
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
_TYPE_LENGTH = re.compile(r"^[A-Z]+\(([0-9]+)(?:,([0-9]+))?\)")
_PROFILE_ALIASES = {
    "partitioned": SchemaProfile.PARTITIONED_INNODB,
    "temporary": SchemaProfile.TEMPORARY_INNODB,
    "foreign_key_graph": SchemaProfile.FOREIGN_KEY_GRAPH,
    "json_multivalue": SchemaProfile.JSON_MULTIVALUE_INNODB,
}


class SchemaRuleViolation(ValueError):
    """A stable machine rule ID plus a human diagnostic."""

    def __init__(self, rule_id: str, detail: str) -> None:
        self.rule_id = rule_id
        self.detail = detail
        super().__init__(f"{rule_id}: {detail}")


@dataclass(frozen=True, slots=True)
class SchemaRules:
    """The intentionally small, auditable compatibility matrix for 8.0.41."""

    version: tuple[int, int, int]

    INDEX_COMPATIBILITY: ClassVar[MappingProxyType[SchemaProfile, frozenset[IndexKind]]] = (
        MappingProxyType(
            {
                SchemaProfile.REGULAR_INNODB: frozenset(
                    {IndexKind.BTREE, IndexKind.FUNCTIONAL}
                ),
                SchemaProfile.PARTITIONED_INNODB: frozenset(
                    {IndexKind.BTREE, IndexKind.FUNCTIONAL}
                ),
                SchemaProfile.TEMPORARY_INNODB: frozenset(
                    {IndexKind.BTREE, IndexKind.FUNCTIONAL}
                ),
                SchemaProfile.FOREIGN_KEY_GRAPH: frozenset(
                    {IndexKind.BTREE, IndexKind.FUNCTIONAL}
                ),
                SchemaProfile.FULLTEXT_INNODB: frozenset(
                    {IndexKind.BTREE, IndexKind.FULLTEXT}
                ),
                SchemaProfile.SPATIAL_INNODB: frozenset(
                    {IndexKind.BTREE, IndexKind.SPATIAL}
                ),
                SchemaProfile.JSON_MULTIVALUE_INNODB: frozenset(
                    {IndexKind.BTREE, IndexKind.MULTIVALUE}
                ),
            }
        )
    )

    @classmethod
    def mysql_8041(cls) -> SchemaRules:
        return cls((8, 0, 41))

    @staticmethod
    def _profile(profile: SchemaProfile | str) -> SchemaProfile | None:
        if isinstance(profile, SchemaProfile):
            return profile
        try:
            return SchemaProfile(profile)
        except ValueError:
            return _PROFILE_ALIASES.get(profile)

    def allows(self, profile: SchemaProfile | str, features: Collection[str]) -> bool:
        """Return whether feature tags belong to one isolated scene profile."""

        normalized = self._profile(profile)
        if normalized is None:
            return False
        tags = frozenset(features)
        special_tags = {
            "partitioned",
            "partition",
            "temporary",
            "foreign_key",
            "composite_fk",
            "fulltext",
            "spatial",
            "multivalue",
            "json_multivalue",
            "unique_multivalue",
        }
        allowed_special: dict[SchemaProfile, frozenset[str]] = {
            SchemaProfile.REGULAR_INNODB: frozenset(),
            SchemaProfile.PARTITIONED_INNODB: frozenset({"partitioned", "partition"}),
            SchemaProfile.TEMPORARY_INNODB: frozenset({"temporary"}),
            SchemaProfile.FOREIGN_KEY_GRAPH: frozenset({"foreign_key", "composite_fk"}),
            SchemaProfile.FULLTEXT_INNODB: frozenset({"fulltext"}),
            SchemaProfile.SPATIAL_INNODB: frozenset({"spatial"}),
            SchemaProfile.JSON_MULTIVALUE_INNODB: frozenset(
                {"multivalue", "json_multivalue", "unique_multivalue"}
            ),
        }
        return not (tags & special_tags) - allowed_special[normalized]

    def allows_index(self, profile: SchemaProfile | str, kind: IndexKind) -> bool:
        normalized = self._profile(profile)
        return normalized is not None and kind in self.INDEX_COMPATIBILITY[normalized]

    def validate(self, manifest: SchemaManifest, *, limits: SchemaLimits) -> None:
        self._validate_manifest_shape(manifest, limits)
        self._validate_profile(manifest)
        tables = {table.name: table for table in manifest.tables}
        foreign_key_names = [
            foreign_key.name
            for table in manifest.tables
            for foreign_key in table.foreign_keys
        ]
        if len(set(foreign_key_names)) != len(foreign_key_names):
            self._fail(
                "foreign_key_name_global_unique",
                "foreign key constraint names must be unique within a database",
            )
        positions = {table.name: position for position, table in enumerate(manifest.tables)}
        for position, table in enumerate(manifest.tables):
            if any(
                foreign_key.referenced_table != table.name
                and foreign_key.referenced_table in positions
                and positions[foreign_key.referenced_table] >= position
                for foreign_key in table.foreign_keys
            ):
                self._fail(
                    "foreign_key_parent_precedes_child",
                    "referenced tables must render before child tables",
                )
        for table in manifest.tables:
            self._validate_table(table, manifest.profile, limits)
        for table in manifest.tables:
            self._validate_foreign_keys(table, tables)

    @staticmethod
    def _fail(rule_id: str, detail: str) -> NoReturn:
        raise SchemaRuleViolation(rule_id, detail)

    def _validate_manifest_shape(self, manifest: SchemaManifest, limits: SchemaLimits) -> None:
        if not limits.min_tables <= len(manifest.tables) <= limits.max_tables:
            self._fail("schema_table_count", "table count is outside SchemaLimits")
        for table in manifest.tables:
            if not limits.min_columns <= len(table.columns) <= limits.max_columns:
                self._fail(
                    "table_column_count",
                    f"{table.name} column count is outside SchemaLimits",
                )
            if len(table.indexes) > limits.max_indexes_per_table:
                self._fail(
                    "table_index_count",
                    f"{table.name} exceeds max_indexes_per_table",
                )
            if sum(not index.primary for index in table.indexes) > 64:
                self._fail(
                    "innodb_secondary_index_limit",
                    f"{table.name} exceeds 64 secondary indexes",
                )
            if table.row_format != limits.row_format:
                self._fail("table_row_format", f"{table.name} row format differs from limits")

    def _validate_profile(self, manifest: SchemaManifest) -> None:
        profile = manifest.profile
        tables = manifest.tables
        if profile is SchemaProfile.TEMPORARY_INNODB:
            if not manifest.requires_same_session:
                self._fail("temporary_same_session", "temporary manifests require a pinned session")
            if any(not table.temporary for table in tables):
                self._fail("temporary_tables_only", "temporary profile contains a persistent table")
            if any(table.row_format == "COMPRESSED" for table in tables):
                self._fail(
                    "temporary_no_compressed_row_format",
                    "InnoDB temporary tables reject ROW_FORMAT=COMPRESSED in strict mode",
                )
            if any(table.partition is not None for table in tables):
                self._fail("temporary_no_partition", "temporary tables cannot be partitioned")
            if any(table.foreign_keys for table in tables):
                self._fail("temporary_no_foreign_key", "temporary tables cannot participate in FKs")
            if any(
                index.kind in {IndexKind.FULLTEXT, IndexKind.SPATIAL, IndexKind.MULTIVALUE}
                for table in tables
                for index in table.indexes
            ):
                self._fail(
                    "temporary_no_special_index",
                    "temporary scene excludes FULLTEXT, SPATIAL, and MVI",
                )
            return
        if manifest.requires_same_session:
            self._fail("same_session_only_for_temporary", "persistent profiles are not session scoped")
        if any(table.temporary for table in tables):
            self._fail("persistent_tables_only", "persistent profile contains a temporary table")
        has_partition = any(table.partition is not None for table in tables)
        has_fk = any(table.foreign_keys for table in tables)
        kinds = {index.kind for table in tables for index in table.indexes}
        if profile is SchemaProfile.PARTITIONED_INNODB:
            if not has_partition:
                self._fail("partition_profile_required", "partition profile has no partitioned table")
            if has_fk:
                self._fail("partition_no_foreign_key", "partitioned InnoDB excludes foreign keys")
            if kinds & {IndexKind.FULLTEXT, IndexKind.SPATIAL, IndexKind.MULTIVALUE}:
                self._fail("partition_no_special_index", "partition scene excludes special indexes")
        elif profile is SchemaProfile.FOREIGN_KEY_GRAPH:
            if len(tables) < 2 or not has_fk:
                self._fail("foreign_key_graph_required", "FK profile requires an edge and two tables")
            if has_partition:
                self._fail("foreign_key_no_partition", "FK graph excludes partitions")
        elif profile is SchemaProfile.FULLTEXT_INNODB:
            if IndexKind.FULLTEXT not in kinds:
                self._fail("fulltext_profile_required", "FULLTEXT profile has no FULLTEXT index")
            self._reject_unexpected(profile, has_partition, has_fk, kinds, IndexKind.FULLTEXT)
        elif profile is SchemaProfile.SPATIAL_INNODB:
            if IndexKind.SPATIAL not in kinds:
                self._fail("spatial_profile_required", "SPATIAL profile has no SPATIAL index")
            self._reject_unexpected(profile, has_partition, has_fk, kinds, IndexKind.SPATIAL)
        elif profile is SchemaProfile.JSON_MULTIVALUE_INNODB:
            if IndexKind.MULTIVALUE not in kinds:
                self._fail("multivalue_profile_required", "MVI profile has no multivalue index")
            self._reject_unexpected(profile, has_partition, has_fk, kinds, IndexKind.MULTIVALUE)
        elif profile is SchemaProfile.REGULAR_INNODB:
            if has_partition or has_fk or kinds & {
                IndexKind.FULLTEXT,
                IndexKind.SPATIAL,
                IndexKind.MULTIVALUE,
            }:
                self._fail("regular_profile_isolation", "regular scene contains a special structure")

    def _reject_unexpected(
        self,
        profile: SchemaProfile,
        has_partition: bool,
        has_fk: bool,
        kinds: set[IndexKind],
        expected: IndexKind,
    ) -> None:
        unexpected = kinds & {
            IndexKind.FULLTEXT,
            IndexKind.SPATIAL,
            IndexKind.MULTIVALUE,
        } - {expected}
        if has_partition or has_fk or unexpected:
            self._fail(
                "special_profile_isolation",
                f"{profile.value} contains an incompatible special structure",
            )

    def _validate_table(
        self,
        table: TableDef,
        profile: SchemaProfile,
        limits: SchemaLimits,
    ) -> None:
        columns = {column.name: column for column in table.columns}
        if self._row_bytes(table) > limits.row_byte_budget:
            self._fail("row_size_budget", f"{table.name} exceeds the configured row budget")
        local_bytes = _minimum_local_row_bytes(table, limits.row_format)
        if local_bytes > _local_row_limit(limits):
            self._fail(
                "innodb_local_row_size",
                f"{table.name} requires at least {local_bytes} inline bytes",
            )
        if table.partition is not None:
            if table.partition.partitions > limits.max_partitions:
                self._fail("partition_count", f"{table.name} exceeds max_partitions")
            for column_name in table.partition.columns:
                if column_name not in columns:
                    self._fail("partition_column_exists", f"unknown partition column {column_name}")
            if table.partition.method == "HASH" and any(
                columns[name].base_type
                not in {"TINYINT", "SMALLINT", "MEDIUMINT", "INT", "BIGINT"}
                for name in table.partition.columns
            ):
                self._fail("hash_partition_integer", "HASH partition expressions must be integer")
            if table.partition.method == "HASH" and len(table.partition.columns) != 1:
                self._fail(
                    "hash_partition_single_column",
                    "HASH partition rendering supports one integer expression",
                )
            if table.partition.method in {"RANGE", "LIST"} and (
                len(table.partition.columns) != 1
                or columns[table.partition.columns[0]].base_type
                not in {"TINYINT", "SMALLINT", "MEDIUMINT", "INT", "BIGINT"}
            ):
                self._fail(
                    "range_list_partition_integer",
                    "RANGE and LIST expression profiles require one integer column",
                )
            if any(column.base_type in _SPATIAL_TYPES for column in table.columns):
                self._fail(
                    "partition_no_spatial_columns",
                    "partitioned tables cannot contain spatial columns",
                )
            if table.partition.method == "KEY" and any(
                columns[name].base_type in _LARGE_OBJECT_TYPES | {"JSON"}
                for name in table.partition.columns
            ):
                self._fail(
                    "key_partition_column_type",
                    "KEY partition columns cannot be BLOB, TEXT, or JSON",
                )
            if table.partition.method in {"RANGE COLUMNS", "LIST COLUMNS"} and any(
                columns[name].base_type
                not in {
                    "TINYINT",
                    "SMALLINT",
                    "MEDIUMINT",
                    "INT",
                    "BIGINT",
                    "DATE",
                    "DATETIME",
                    "CHAR",
                    "VARCHAR",
                    "BINARY",
                    "VARBINARY",
                }
                for name in table.partition.columns
            ):
                self._fail(
                    "columns_partition_column_type",
                    "COLUMNS partitioning uses an unsupported column type",
                )
        functional_parts = sum(
            sum(part.expression is not None for part in index.parts)
            for index in table.indexes
            if index.kind in {IndexKind.FUNCTIONAL, IndexKind.MULTIVALUE}
        )
        if len(table.columns) + functional_parts > 1017:
            self._fail(
                "innodb_hidden_column_limit",
                f"{table.name} functional hidden columns exceed the InnoDB column limit",
            )
        for index in table.indexes:
            self._validate_index(table, index, columns, profile, limits)
            if table.partition is not None and index.unique:
                if not set(table.partition.columns) <= set(index.column_names):
                    self._fail(
                        "partition_unique_key_contains_partition_key",
                        f"{table.name}.{index.name} omits a partition column",
                    )

    def _validate_index(
        self,
        table: TableDef,
        index: IndexDef,
        columns: dict[str, ColumnDef],
        profile: SchemaProfile,
        limits: SchemaLimits,
    ) -> None:
        if not self.allows_index(profile, index.kind):
            self._fail(
                "profile_index_compatibility",
                f"{profile.value} does not allow {index.kind.value}",
            )
        for part in index.parts:
            if part.column_name is not None and part.column_name not in columns:
                self._fail("index_column_exists", f"{table.name}.{index.name} has an unknown column")
            if part.expression is not None:
                if part.expression.column_name not in columns:
                    self._fail(
                        "index_expression_columns_exist",
                        f"{table.name}.{index.name} expression has unknown columns",
                    )
        if index.kind in {IndexKind.FULLTEXT, IndexKind.SPATIAL} and index.unique:
            self._fail("special_index_not_unique", f"{index.name} cannot be unique")
        if index.primary:
            for part in index.parts:
                if part.column_name is None or part.prefix_length is not None:
                    self._fail("primary_whole_columns", "primary keys require whole columns")
                if columns[part.column_name].nullable:
                    self._fail("primary_not_null", "primary key columns must be NOT NULL")
        if index.kind in {IndexKind.FULLTEXT, IndexKind.SPATIAL, IndexKind.MULTIVALUE} and any(
            part.direction.value != "ASC" for part in index.parts
        ):
            self._fail("special_index_no_direction", "special index parts cannot be DESC")
        if index.kind is IndexKind.FULLTEXT:
            if any(
                part.column_name is None
                or columns[part.column_name].base_type not in _TEXT_TYPES
                or part.prefix_length is not None
                for part in index.parts
            ):
                self._fail("fulltext_text_columns", f"{index.name} requires whole text columns")
            text_columns = [columns[part.column_name or ""] for part in index.parts]
            if len(
                {
                    (
                        column.charset or table.default_charset,
                        column.collation or table.default_collation,
                    )
                    for column in text_columns
                }
            ) != 1:
                self._fail(
                    "fulltext_column_collation",
                    f"{index.name} columns require one charset and collation",
                )
        elif index.kind is IndexKind.SPATIAL:
            if len(index.parts) != 1 or index.parts[0].column_name is None:
                self._fail("spatial_single_column", f"{index.name} requires one spatial column")
            column = columns[index.parts[0].column_name or ""]
            if column.base_type not in _SPATIAL_TYPES:
                self._fail("spatial_column_type", f"{index.name} does not reference geometry")
            if column.nullable:
                self._fail("spatial_not_null", f"{column.name} must be NOT NULL")
            if column.srid is None:
                self._fail("spatial_srid_required", f"{column.name} must declare an SRID")
            if index.parts[0].prefix_length is not None:
                self._fail("spatial_no_prefix", f"{index.name} cannot use a prefix")
        elif index.kind is IndexKind.MULTIVALUE:
            array_parts = [
                part
                for part in index.parts
                if part.expression is not None
                and part.expression.kind is IndexExpressionKind.JSON_UNSIGNED_ARRAY
            ]
            if len(array_parts) != 1 or sum(
                part.expression is not None for part in index.parts
            ) != 1:
                self._fail(
                    "multivalue_single_array_part",
                    f"{index.name} requires exactly one typed ARRAY expression",
                )
            expression = array_parts[0].expression
            assert expression is not None
            if columns[expression.column_name].base_type != "JSON":
                self._fail("multivalue_json_array", f"{index.name} must cast a JSON array")
            if any(part.prefix_length is not None for part in index.parts):
                self._fail("multivalue_no_prefix", f"{index.name} cannot use prefixes")
        elif index.kind is IndexKind.FUNCTIONAL:
            expressions = [part.expression for part in index.parts if part.expression is not None]
            if not expressions or any(
                expression.kind is not IndexExpressionKind.LOWER_CHAR
                for expression in expressions
            ):
                self._fail(
                    "functional_typed_expression",
                    f"{index.name} requires a supported functional template",
                )
        elif any(part.expression is not None for part in index.parts):
            self._fail("btree_column_parts", f"{index.name} BTREE parts must be columns")

        for part in index.parts:
            if part.column_name is None:
                continue
            column = columns[part.column_name]
            if column.base_type == "JSON":
                self._fail(
                    "json_requires_expression_index",
                    f"{table.name}.{index.name} directly indexes JSON",
                )
            if part.prefix_length is not None and column.base_type not in {
                *_TEXT_TYPES,
                *_BINARY_TYPES,
            }:
                self._fail(
                    "index_prefix_string_type",
                    f"{table.name}.{index.name} prefixes a non-string column",
                )
            if (
                column.base_type in _LARGE_OBJECT_TYPES
                and part.prefix_length is None
                and index.kind is not IndexKind.FULLTEXT
            ):
                self._fail(
                    "lob_index_requires_prefix",
                    f"{table.name}.{index.name} indexes {column.name} without a prefix",
                )
            maximum = _declared_length(column)
            if part.prefix_length is not None and maximum is not None and part.prefix_length > maximum:
                self._fail(
                    "index_prefix_within_column",
                    f"{table.name}.{index.name} prefix exceeds {column.name}",
                )
        if index.kind not in {IndexKind.FULLTEXT, IndexKind.SPATIAL}:
            key_bytes = sum(
                self._index_part_bytes(part, columns, table) for part in index.parts
            )
            if key_bytes > _physical_index_budget(limits):
                self._fail(
                    "index_key_byte_budget",
                    f"{table.name}.{index.name} uses {key_bytes} key bytes",
                )
        if profile is SchemaProfile.TEMPORARY_INNODB and index.kind in {
            IndexKind.FULLTEXT,
            IndexKind.SPATIAL,
            IndexKind.MULTIVALUE,
        }:
            self._fail("temporary_no_special_index", f"temporary table contains {index.kind.value}")

    def _validate_foreign_keys(
        self,
        child: TableDef,
        tables: dict[str, TableDef],
    ) -> None:
        child_columns = {column.name: column for column in child.columns}
        for foreign_key in child.foreign_keys:
            parent = tables.get(foreign_key.referenced_table)
            if parent is None:
                self._fail("foreign_key_parent_exists", "referenced table does not exist")
            assert parent is not None
            parent_columns = {column.name: column for column in parent.columns}
            if any(name not in child_columns for name in foreign_key.columns) or any(
                name not in parent_columns for name in foreign_key.referenced_columns
            ):
                self._fail("foreign_key_columns_exist", "foreign key references an unknown column")
            if child.name == parent.name and any(
                child_name == parent_name
                for child_name, parent_name in zip(
                    foreign_key.columns,
                    foreign_key.referenced_columns,
                    strict=True,
                )
            ):
                self._fail(
                    "foreign_key_column_not_self_reference",
                    "a column cannot have a foreign key reference to itself",
                )
            if not all(
                _foreign_key_columns_compatible(
                    child_columns[child_name],
                    parent_columns[parent_name],
                    child,
                    parent,
                )
                for child_name, parent_name in zip(
                    foreign_key.columns,
                    foreign_key.referenced_columns,
                    strict=True,
                )
            ):
                self._fail(
                    "foreign_key_column_compatible",
                    "FK storage type, sign, charset, or collation differs",
                )
            if not any(
                _index_starts_with_columns(index, foreign_key.referenced_columns)
                for index in parent.indexes
            ):
                self._fail(
                    "foreign_key_reference_index_left_prefix",
                    "referenced columns are not a complete index left prefix",
                )
            if not all(child_columns[name].nullable for name in foreign_key.columns) and (
                foreign_key.on_delete == "SET NULL" or foreign_key.on_update == "SET NULL"
            ):
                self._fail("foreign_key_set_null_nullable", "SET NULL requires nullable child columns")

    @staticmethod
    def _row_bytes(table: TableDef) -> int:
        return 5 + (len(table.columns) + 7) // 8 + sum(
            _column_storage_bytes(column, table.default_charset) for column in table.columns
        )

    @staticmethod
    def _index_part_bytes(
        part: IndexPart,
        columns: dict[str, ColumnDef],
        table: TableDef,
    ) -> int:
        if part.expression is not None:
            if part.expression.kind is IndexExpressionKind.JSON_UNSIGNED_ARRAY:
                return 8
            assert part.expression.cast_length is not None
            return part.expression.cast_length * 4
        column = columns[part.column_name or ""]
        length = part.prefix_length
        if length is None:
            length = _declared_length(column)
        if column.base_type in _TEXT_TYPES:
            return (length or 1) * _charset_width(
                column.charset or table.default_charset
            )
        if column.base_type in _BINARY_TYPES:
            return length or 1
        return _column_storage_bytes(column, table.default_charset)


def _index_starts_with_columns(index: IndexDef, names: tuple[str, ...]) -> bool:
    return index.kind is IndexKind.BTREE and len(index.parts) >= len(names) and all(
        part.column_name == name and part.expression is None and part.prefix_length is None
        for part, name in zip(index.parts[: len(names)], names, strict=True)
    )


def _foreign_key_columns_compatible(
    child: ColumnDef,
    parent: ColumnDef,
    child_table: TableDef,
    parent_table: TableDef,
) -> bool:
    if child.base_type != parent.base_type:
        return False
    if child.base_type in {"TINYINT", "SMALLINT", "MEDIUMINT", "INT", "BIGINT", "DECIMAL"}:
        return child.mysql_type == parent.mysql_type
    if child.base_type in {"CHAR", "VARCHAR"}:
        return (
            child.charset or child_table.default_charset,
            child.collation or child_table.default_collation,
        ) == (
            parent.charset or parent_table.default_charset,
            parent.collation or parent_table.default_collation,
        )
    if child.base_type in {"BINARY", "VARBINARY"}:
        return True
    return (
        child.mysql_type,
        child.charset or child_table.default_charset,
        child.collation or child_table.default_collation,
    ) == (
        parent.mysql_type,
        parent.charset or parent_table.default_charset,
        parent.collation or parent_table.default_collation,
    )


def _charset_width(charset: str | None) -> int:
    return {
        None: 4,
        "ascii": 1,
        "binary": 1,
        "latin1": 1,
        "utf8mb3": 3,
        "utf8mb4": 4,
    }.get(charset, 4)


def _physical_index_budget(limits: SchemaLimits) -> int:
    if limits.row_format in {"COMPACT", "REDUNDANT"}:
        physical = 767
    else:
        physical = min(3072, limits.page_size * 3 // 16)
    return min(limits.index_byte_budget, physical)


def _local_row_limit(limits: SchemaLimits) -> int:
    page_fraction = 4 if limits.row_format == "COMPRESSED" else 2
    return max(512, min(16_000, limits.page_size // page_fraction - 256))


def _minimum_local_row_bytes(table: TableDef, row_format: str) -> int:
    return 5 + (len(table.columns) + 7) // 8 + sum(
        _minimum_local_column_bytes(column, row_format, table.default_charset)
        for column in table.columns
    )


def _minimum_local_column_bytes(
    column: ColumnDef,
    row_format: str,
    default_charset: str,
) -> int:
    base = column.base_type
    full_size = _column_storage_bytes(column, default_charset)
    variable = base in {
        "VARCHAR",
        "VARBINARY",
        *_LARGE_OBJECT_TYPES,
        "JSON",
        *_SPATIAL_TYPES,
    } or (base == "CHAR" and full_size >= 768)
    if not variable:
        return full_size
    if row_format in {"DYNAMIC", "COMPRESSED"}:
        return min(full_size, 20)
    return min(full_size, 788)


def _declared_length(column: ColumnDef) -> int | None:
    match = _TYPE_LENGTH.match(column.mysql_type)
    if match is not None:
        return int(match.group(1))
    return {
        "TINYTEXT": 255,
        "TINYBLOB": 255,
        "TEXT": 65_535,
        "BLOB": 65_535,
        "MEDIUMTEXT": 16_777_215,
        "MEDIUMBLOB": 16_777_215,
        "LONGTEXT": 4_294_967_295,
        "LONGBLOB": 4_294_967_295,
    }.get(column.base_type)


def _column_storage_bytes(column: ColumnDef, default_charset: str | None = None) -> int:
    base = column.base_type
    length = _declared_length(column)
    if base == "TINYINT":
        return 1
    if base == "SMALLINT":
        return 2
    if base == "MEDIUMINT":
        return 3
    if base in {"INT", "FLOAT"}:
        return 4
    if base in {"BIGINT", "DOUBLE"}:
        return 8
    if base == "BIT":
        return ((length or 1) + 7) // 8
    if base == "DECIMAL":
        return ((length or 1) + 1) // 2 + 1
    if base in _TEXT_TYPES:
        if base in _LARGE_OBJECT_TYPES:
            return 20
        return (length or 1) * _charset_width(column.charset or default_charset) + 2
    if base in _BINARY_TYPES:
        if base in _LARGE_OBJECT_TYPES:
            return 20
        return (length or 1) + 2
    if base == "DATE":
        return 3
    if base in {"TIME", "TIMESTAMP"}:
        return 7
    if base == "DATETIME":
        return 8
    if base == "YEAR":
        return 1
    if base in {"JSON", *_SPATIAL_TYPES}:
        return 20
    return 16


__all__ = ["SchemaRuleViolation", "SchemaRules"]
