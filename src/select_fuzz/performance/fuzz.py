"""Seeded, scalable random workloads for MySQL performance comparison.

The schema and query are frozen by the case seed.  Only row-count knobs change
during calibration.  Large payloads are produced by deterministic, set-based
stored procedures, so replay SQL grows with schema complexity rather than data
volume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import random
import re

from select_fuzz.domain import SeedTree
from select_fuzz.generation.composite_indexes import (
    CompositeColumn,
    CompositeIndexFamily,
    CompositeIndexPlan,
    build_composite_index_candidates,
)
from select_fuzz.generation.schema import (
    ColumnDef,
    IndexDef,
    IndexExpression,
    IndexKind,
    IndexPart,
    SchemaLimits,
    SchemaManifest,
    SchemaProfile,
    SortDirection,
    TableDef,
)
from select_fuzz.generation.schema_rules import SchemaRules
from select_fuzz.performance.models import ScaleKnobs
from select_fuzz.performance.tree import Family, ShapeBoundary


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_TEXT_TYPES = frozenset({"CHAR", "VARCHAR", "TINYTEXT", "TEXT", "ENUM", "SET"})
_INDEXABLE_TYPES = frozenset(
    {
        "TINYINT",
        "SMALLINT",
        "MEDIUMINT",
        "INT",
        "BIGINT",
        "DECIMAL",
        "FLOAT",
        "DOUBLE",
        "BIT",
        "CHAR",
        "VARCHAR",
        "BINARY",
        "VARBINARY",
        "DATE",
        "TIME",
        "DATETIME",
        "TIMESTAMP",
        "YEAR",
        "ENUM",
        "SET",
    }
)
_QUERY_SHAPES = (
    "aggregate_scan",
    "range_sort",
    "join_aggregate",
    "group_sort",
    "window_sort",
    "filtered_scan",
)
_INNODB_INDEX_BYTE_BUDGET = 3072
_INTEGER_BOUNDS: dict[str, tuple[int, int]] = {
    "TINYINT": (-(2**7), 2**7 - 1),
    "SMALLINT": (-(2**15), 2**15 - 1),
    "MEDIUMINT": (-(2**23), 2**23 - 1),
    "INT": (-(2**31), 2**31 - 1),
    # Keep arithmetic comfortably inside MySQL signed BIGINT while still
    # exercising the declaration itself.
    "BIGINT": (-(2**40), 2**40 - 1),
}


@dataclass(frozen=True, slots=True)
class ScalableFuzzSetupManifest:
    """Executable DDL/DML needed to rebuild one scalable performance case."""

    template_id: str
    seed: int
    schema: SchemaManifest
    expected_rows: dict[str, int]
    setup_statements: tuple[str, ...]
    batch_rows: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "expected_rows", dict(self.expected_rows))
        object.__setattr__(self, "setup_statements", tuple(self.setup_statements))
        if set(self.expected_rows) != {table.name for table in self.schema.tables}:
            raise ValueError("expected_rows must cover every generated table")
        if any(rows <= 0 for rows in self.expected_rows.values()):
            raise ValueError("expected row counts must be positive")

    @property
    def table_names(self) -> tuple[str, ...]:
        return tuple(table.name for table in self.schema.tables)

    @property
    def expected_row_count(self) -> int:
        """Compatibility total for materializers that expose one count."""

        return sum(self.expected_rows.values())


@dataclass(frozen=True, slots=True)
class _ColumnPlan:
    definition: ColumnDef
    value_sql: str


@dataclass(frozen=True, slots=True)
class _TablePlan:
    table: TableDef
    values: tuple[str, ...]
    procedure_name: str


def _positive_integer(name: str, value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _case_id(case_id: str, round_number: int, query_number: int) -> str:
    _positive_integer("round_number", round_number)
    _positive_integer("query_number", query_number)
    return f"{case_id}_r{round_number}_q{query_number}"


def _nullable(column: ColumnDef, expression: str, *, salt: int) -> str:
    if not column.nullable:
        return expression
    return f"IF(MOD(v_offset + nums.n + p_seed + {salt}, 11) = 0, NULL, {expression})"


def _column_plan(rng: random.Random, ordinal: int) -> _ColumnPlan:
    """Pick a non-special declaration plus a valid deterministic SQL value."""

    family = rng.choice(
        (
            "integer",
            "decimal",
            "float",
            "bit",
            "character",
            "binary",
            "temporal",
            "year",
            "enum_set",
            "text_blob",
        )
    )
    name = f"c{ordinal}"
    nullable = rng.random() < 0.45
    salt = rng.randrange(1, 1_000_000)
    x = f"(v_offset + nums.n + p_seed + {salt})"

    if family == "integer":
        base = rng.choice(tuple(_INTEGER_BOUNDS))
        unsigned = rng.random() < 0.35
        low, high = _INTEGER_BOUNDS[base]
        if unsigned:
            low = 0
            high = min(high * 2 + 1, 2**41 - 1)
        declaration = base + (" UNSIGNED" if unsigned else "")
        column = ColumnDef(name, declaration, nullable)
        span = high - low + 1
        expression = f"(CAST(MOD(MOD({x}, 1000003) * 1103515245, {span}) AS SIGNED) + ({low}))"
    elif family == "decimal":
        precision = rng.randint(1, 18)
        scale = rng.randint(0, min(6, precision))
        column = ColumnDef(name, f"DECIMAL({precision},{scale})", nullable)
        expression = f"(MOD({x}, {10**precision}) / {10**scale})"
    elif family == "float":
        column = ColumnDef(name, rng.choice(("FLOAT", "DOUBLE")), nullable)
        expression = (
            f"(CAST(MOD(MOD({x}, 1000003) * 2654435761, 2000001) AS SIGNED) - 1000000) / 100.0"
        )
    elif family == "bit":
        width = rng.randint(1, 64)
        column = ColumnDef(name, f"BIT({width})", nullable)
        expression = f"CAST(MOD({x}, {2 ** min(width, 30)}) AS UNSIGNED)"
    elif family == "character":
        base = rng.choice(("CHAR", "VARCHAR"))
        length = rng.randint(1, 96)
        charset, collation = rng.choice(
            (
                ("ascii", "ascii_bin"),
                ("latin1", "latin1_swedish_ci"),
                ("utf8mb4", "utf8mb4_0900_ai_ci"),
                ("utf8mb4", "utf8mb4_0900_as_cs"),
            )
        )
        column = ColumnDef(
            name,
            f"{base}({length})",
            nullable,
            charset,
            collation,
        )
        expression = f"RIGHT(CONCAT(REPEAT('0', {length}), CONV({x}, 10, 36)), {length})"
    elif family == "binary":
        base = rng.choice(("BINARY", "VARBINARY"))
        length = rng.randint(1, 64)
        column = ColumnDef(name, f"{base}({length})", nullable)
        expression = (
            f"UNHEX(RIGHT(CONCAT(REPEAT('0', {length * 2}), HEX(MOD({x}, 4294967295))), "
            f"{length * 2}))"
        )
    elif family == "temporal":
        base = rng.choice(("DATE", "TIME", "DATETIME", "TIMESTAMP"))
        fsp = rng.randint(0, 6)
        declaration = base if base == "DATE" else f"{base}({fsp})"
        column = ColumnDef(name, declaration, nullable)
        if base == "DATE":
            expression = f"DATE_ADD('2000-01-01', INTERVAL MOD({x}, 9000) DAY)"
        elif base == "TIME":
            expression = f"SEC_TO_TIME(MOD({x}, 86400))"
        else:
            expression = f"TIMESTAMPADD(SECOND, MOD({x}, 1000000000), '2000-01-01')"
    elif family == "year":
        column = ColumnDef(name, "YEAR", nullable)
        expression = f"(1901 + MOD({x}, 255))"
    elif family == "enum_set":
        if rng.random() < 0.5:
            column = ColumnDef(
                name,
                "ENUM('a','b','c')",
                nullable,
                "utf8mb4",
                "utf8mb4_0900_ai_ci",
            )
            expression = f"ELT(MOD({x}, 3) + 1, 'a', 'b', 'c')"
        else:
            column = ColumnDef(
                name,
                "SET('a','b','c')",
                nullable,
                "utf8mb4",
                "utf8mb4_0900_ai_ci",
            )
            expression = f"ELT(MOD({x}, 4) + 1, 'a', 'b', 'c', 'a,b')"
    else:
        base = rng.choice(("TINYTEXT", "TEXT", "TINYBLOB", "BLOB"))
        if base.endswith("TEXT"):
            column = ColumnDef(
                name,
                base,
                nullable,
                "utf8mb4",
                "utf8mb4_0900_ai_ci",
            )
            expression = f"CONCAT('text_', CONV({x}, 10, 36))"
        else:
            column = ColumnDef(name, base, nullable)
            expression = f"UNHEX(LPAD(HEX(MOD({x}, 4294967295)), 8, '0'))"
    return _ColumnPlan(column, _nullable(column, expression, salt=salt))


def _index_parts(
    rng: random.Random,
    column: ColumnDef,
    *,
    unique: bool,
) -> tuple[IndexPart, ...]:
    direction = rng.choice(tuple(SortDirection))
    column_part = IndexPart(column_name=column.name, direction=direction)
    if unique:
        return (IndexPart(column_name="id"), column_part)
    return (column_part,)


def _composite_column(column: ColumnDef) -> CompositeColumn:
    charset_bytes = 1
    if column.base_type in _TEXT_TYPES:
        charset_bytes = 1 if column.charset in {"ascii", "latin1"} else 4
    return CompositeColumn(column.name, column.mysql_type, charset_bytes=charset_bytes)


def _composite_index(plan: CompositeIndexPlan, *, visible: bool) -> IndexDef:
    name = (
        "uq_comp_unique"
        if plan.family is CompositeIndexFamily.UNIQUE
        else f"idx_comp_{plan.family.value}"
    )
    return IndexDef(
        name,
        tuple(
            IndexPart(
                column_name=part.column_name,
                prefix_length=part.prefix_length,
                direction=(SortDirection.DESC if part.descending else SortDirection.ASC),
            )
            for part in plan.parts
        ),
        unique=plan.unique,
        visible=visible,
    )


def _table_plan(
    seed_tree: SeedTree,
    seed: int,
    table_ordinal: int,
    *,
    min_columns: int,
    max_columns: int,
    max_indexes_per_table: int,
) -> _TablePlan:
    rng = random.Random(seed_tree.derive("performance_fuzz", "table", table_ordinal))
    column_count = rng.randint(min_columns, max_columns)
    generated = tuple(_column_plan(rng, ordinal) for ordinal in range(1, column_count))
    columns = (ColumnDef("id", "BIGINT UNSIGNED", False),) + tuple(
        plan.definition for plan in generated
    )
    indexes: list[IndexDef] = [
        IndexDef(
            "PRIMARY",
            (IndexPart(column_name="id"),),
            unique=True,
            primary=True,
        )
    ]
    indexable_columns = [
        column for column in columns[1:] if column.base_type in _INDEXABLE_TYPES
    ]
    secondary_candidates: list[IndexDef] = []
    for index_ordinal, column in enumerate(rng.sample(indexable_columns, len(indexable_columns))):
        unique = rng.random() < 0.25
        if column.base_type in _TEXT_TYPES and rng.random() < 0.25:
            secondary_candidates.append(
                IndexDef(
                    f"idx_{table_ordinal}_{index_ordinal}",
                    (IndexPart(expression=IndexExpression.lower_char(column.name, 255)),),
                    kind=IndexKind.FUNCTIONAL,
                    visible=rng.random() >= 0.15,
                )
            )
        else:
            secondary_candidates.append(
                IndexDef(
                    f"idx_{table_ordinal}_{index_ordinal}",
                    _index_parts(rng, column, unique=unique),
                    unique=unique,
                    visible=rng.random() >= 0.15,
                )
            )
    composite_rng = random.Random(
        seed_tree.derive("performance_fuzz", "table", table_ordinal, "composite_indexes")
    )
    secondary_candidates.extend(
        _composite_index(plan, visible=composite_rng.random() >= 0.15)
        for plan in build_composite_index_candidates(
            tuple(_composite_column(column) for column in columns),
            rng=composite_rng,
            index_byte_budget=_INNODB_INDEX_BYTE_BUDGET,
        )
    )
    rng.shuffle(secondary_candidates)
    secondary_count = rng.randint(
        0,
        min(max_indexes_per_table - 1, len(secondary_candidates)),
    )
    indexes.extend(secondary_candidates[:secondary_count])
    table = TableDef(
        name=f"pf_t{table_ordinal}",
        temporary=False,
        columns=columns,
        indexes=tuple(indexes),
    )
    normalized_seed = seed & ((1 << 63) - 1)
    return _TablePlan(
        table=table,
        values=("(v_offset + nums.n + 1)",) + tuple(plan.value_sql for plan in generated),
        procedure_name=f"sf_fill_{normalized_seed:x}_{table_ordinal}",
    )


def _helper_insert(helper_name: str) -> str:
    digits = " UNION ALL ".join(
        f"SELECT {number}" + (" AS n" if number == 0 else "") for number in range(10)
    )
    return (
        f"INSERT INTO `{helper_name}` (`n`) "
        "SELECT d0.n + 10 * d1.n + 100 * d2.n + 1000 * d3.n "
        f"FROM ({digits}) AS d0 CROSS JOIN ({digits}) AS d1 "
        f"CROSS JOIN ({digits}) AS d2 CROSS JOIN ({digits}) AS d3 ORDER BY 1"
    )


def _fill_procedure(plan: _TablePlan, helper_name: str) -> str:
    columns = ", ".join(f"`{column.name}`" for column in plan.table.columns)
    values = ", ".join(plan.values)
    return (
        f"CREATE PROCEDURE `{plan.procedure_name}`("
        "IN p_target BIGINT UNSIGNED, IN p_seed BIGINT UNSIGNED, "
        "IN p_batch INT UNSIGNED) "
        "BEGIN "
        "DECLARE v_offset BIGINT UNSIGNED DEFAULT 0; "
        "WHILE v_offset < p_target DO "
        f"INSERT INTO `{plan.table.name}` ({columns}) "
        f"SELECT {values} "
        f"FROM `{helper_name}` AS nums "
        "WHERE nums.n < LEAST(p_batch, p_target - v_offset) "
        "ORDER BY nums.n; "
        "SET v_offset = v_offset + p_batch; "
        "END WHILE; "
        "END"
    )


@dataclass(frozen=True, slots=True)
class PerformanceFuzzTemplate:
    """A frozen random schema/query implementing the performance template API."""

    seed: int
    case_id: str
    schema_seed: int | None = None
    min_initial_rows: int = 100_000
    max_initial_rows: int = 1_000_000
    max_table_rows: int = 50_000_000
    max_total_rows: int = 100_000_000
    batch_rows: int = 10_000
    min_tables: int = 1
    max_tables: int = 3
    min_columns: int = 3
    max_columns: int = 8
    max_indexes_per_table: int = 4
    max_query_tables: int = 4
    max_query_depth: int = 3
    schema: SchemaManifest = field(init=False)
    initial_scale: ScaleKnobs = field(init=False)
    template_id: str = field(init=False)
    boundary: ShapeBoundary = field(init=False)
    driver_family: Family = field(init=False)
    _query_shape: str = field(init=False, repr=False)
    _query_salt: int = field(init=False, repr=False)
    _plans: tuple[_TablePlan, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise TypeError("seed must be an integer")
        if self.schema_seed is not None and (
            not isinstance(self.schema_seed, int) or isinstance(self.schema_seed, bool)
        ):
            raise TypeError("schema_seed must be an integer when supplied")
        if not isinstance(self.case_id, str) or not _IDENTIFIER.fullmatch(self.case_id):
            raise ValueError("case_id must be a snake_case identifier")
        for name in (
            "min_initial_rows",
            "max_initial_rows",
            "max_table_rows",
            "max_total_rows",
            "batch_rows",
            "min_tables",
            "max_tables",
            "min_columns",
            "max_columns",
            "max_indexes_per_table",
            "max_query_tables",
            "max_query_depth",
        ):
            _positive_integer(name, getattr(self, name))
        if self.max_initial_rows < self.min_initial_rows:
            raise ValueError("max_initial_rows must not be less than min_initial_rows")
        if self.max_table_rows < self.max_initial_rows:
            raise ValueError("max_table_rows must not be less than max_initial_rows")
        if self.max_total_rows < self.max_table_rows:
            raise ValueError("max_total_rows must not be less than max_table_rows")
        if self.min_tables > self.max_tables:
            raise ValueError("min_tables must not exceed max_tables")
        if self.min_columns > self.max_columns:
            raise ValueError("min_columns must not exceed max_columns")
        if self.batch_rows > 10_000:
            raise ValueError("batch_rows must not exceed the 10,000-row helper table")

        materialization_seed = self.seed if self.schema_seed is None else self.schema_seed
        schema_tree = SeedTree(materialization_seed)
        query_tree = SeedTree(self.seed)
        shape_rng = random.Random(query_tree.derive("performance_fuzz", "query"))
        query_shapes = list(_QUERY_SHAPES)
        if self.max_query_tables < 2 or self.max_query_depth < 2:
            query_shapes.remove("join_aggregate")
        if self.max_query_depth < 2:
            query_shapes.remove("window_sort")
        query_shape = shape_rng.choice(query_shapes)
        query_salt = shape_rng.randrange(1, 1_000_003)
        table_rng = random.Random(schema_tree.derive("performance_fuzz", "table_count"))
        plans = tuple(
            _table_plan(
                schema_tree,
                materialization_seed,
                ordinal,
                min_columns=self.min_columns,
                max_columns=self.max_columns,
                max_indexes_per_table=self.max_indexes_per_table,
            )
            for ordinal in range(table_rng.randint(self.min_tables, self.max_tables))
        )
        manifest = SchemaManifest(
            profile=SchemaProfile.REGULAR_INNODB,
            target_feature_id="performance_fuzz",
            seed=materialization_seed,
            tables=tuple(plan.table for plan in plans),
            limits_identity="performance_fuzz_v1",
        )
        limits = SchemaLimits(
            min_tables=self.min_tables,
            max_tables=self.max_tables,
            min_columns=self.min_columns,
            max_columns=self.max_columns,
            max_indexes_per_table=self.max_indexes_per_table,
        )
        SchemaRules.mysql_8041().validate(manifest, limits=limits)

        row_rng = random.Random(schema_tree.derive("performance_fuzz", "initial_rows"))
        effective_initial_max = min(
            self.max_initial_rows,
            self.max_total_rows // len(plans),
        )
        if effective_initial_max < self.min_initial_rows:
            raise ValueError("max_total_rows cannot fit the random schema at minimum scale")
        logarithmic = math.exp(
            row_rng.uniform(math.log(self.min_initial_rows), math.log(effective_initial_max))
        )
        initial_rows = min(
            effective_initial_max,
            max(self.min_initial_rows, round(logarithmic)),
        )
        initial = ScaleKnobs().scaled(
            initial_rows / ScaleKnobs().table_rows,
            row_cap=self.max_table_rows,
        )
        boundary, driver = self._shape_contract(query_shape)
        object.__setattr__(self, "schema", manifest)
        object.__setattr__(self, "initial_scale", initial)
        object.__setattr__(self, "template_id", f"performance_fuzz_{query_shape}_v1")
        object.__setattr__(self, "boundary", boundary)
        object.__setattr__(self, "driver_family", driver)
        object.__setattr__(self, "_query_shape", query_shape)
        object.__setattr__(self, "_query_salt", query_salt)
        object.__setattr__(self, "_plans", plans)

    @staticmethod
    def _shape_contract(shape: str) -> tuple[ShapeBoundary, Family]:
        contracts = {
            "aggregate_scan": (
                ShapeBoundary(required=frozenset({Family.SCAN, Family.AGGREGATE})),
                Family.SCAN,
            ),
            "range_sort": (
                ShapeBoundary(required=frozenset({Family.SCAN, Family.SORT})),
                Family.SCAN,
            ),
            "join_aggregate": (
                ShapeBoundary(required=frozenset({Family.SCAN, Family.JOIN, Family.AGGREGATE})),
                Family.JOIN,
            ),
            "group_sort": (
                ShapeBoundary(required=frozenset({Family.SCAN, Family.AGGREGATE, Family.SORT})),
                Family.AGGREGATE,
            ),
            "window_sort": (
                ShapeBoundary(required=frozenset({Family.SCAN, Family.WINDOW, Family.SORT})),
                Family.WINDOW,
            ),
            "filtered_scan": (
                ShapeBoundary(required=frozenset({Family.SCAN})),
                Family.SCAN,
            ),
        }
        return contracts[shape]

    def for_case(self, round_number: int, query_number: int) -> PerformanceFuzzTemplate:
        round_seed = SeedTree(self.seed).derive("performance_fuzz", "round", round_number)
        query_seed = SeedTree(round_seed).derive("performance_fuzz", "query", query_number)
        return PerformanceFuzzTemplate(
            seed=query_seed,
            case_id=_case_id(self.case_id, round_number, query_number),
            schema_seed=round_seed,
            min_initial_rows=self.min_initial_rows,
            max_initial_rows=self.max_initial_rows,
            max_table_rows=self.max_table_rows,
            max_total_rows=self.max_total_rows,
            batch_rows=self.batch_rows,
            min_tables=self.min_tables,
            max_tables=self.max_tables,
            min_columns=self.min_columns,
            max_columns=self.max_columns,
            max_indexes_per_table=self.max_indexes_per_table,
            max_query_tables=self.max_query_tables,
            max_query_depth=self.max_query_depth,
        )

    def target_rows(self, scale: ScaleKnobs) -> int:
        if self._query_shape == "range_sort":
            return max(1, math.ceil(scale.table_rows * scale.range_selectivity))
        if self._query_shape == "join_aggregate":
            return scale.join_probe_rows
        if self._query_shape == "group_sort":
            return scale.aggregate_input_rows
        if self._query_shape == "window_sort":
            return scale.sort_rows
        return scale.scan_rows

    def render(self, scale: ScaleKnobs) -> str:
        first = self.schema.tables[0].name
        salt = self._query_salt
        if self._query_shape == "aggregate_scan":
            return (
                f"SELECT SUM(MOD((`q`.`id` * {salt}) + 17, 1000003)) AS `checksum` "
                f"FROM `{first}` AS `q` WHERE `q`.`id` <= {scale.scan_rows} ORDER BY 1"
            )
        if self._query_shape == "range_sort":
            selected = self.target_rows(scale)
            limit = min(selected, scale.sort_rows)
            return (
                f"SELECT `q`.`id`, SHA2(CONCAT(`q`.`id`, '{salt}', "
                f"REPEAT('x', {scale.sort_key_bytes})), 256) AS `sort_key` "
                f"FROM `{first}` AS `q` WHERE `q`.`id` <= {selected} "
                f"ORDER BY `sort_key`, `q`.`id` LIMIT {limit}"
            )
        if self._query_shape == "join_aggregate":
            second = self.schema.tables[1].name if len(self.schema.tables) > 1 else first
            return (
                f"SELECT SUM(MOD((`a`.`id` * {salt}) + `b`.`id`, 1000003)) AS `checksum` "
                f"FROM `{first}` AS `a` INNER JOIN `{second}` AS `b` "
                f"ON `b`.`id` = `a`.`id` WHERE `a`.`id` <= {scale.join_probe_rows} "
                "ORDER BY 1"
            )
        if self._query_shape == "group_sort":
            return (
                f"SELECT MOD(`q`.`id` + {salt}, {scale.aggregate_groups}) AS `group_key`, "
                f"SUM(MOD(`q`.`id` * {salt}, 1000003)) AS `checksum` FROM `{first}` AS `q` "
                f"WHERE `q`.`id` <= {scale.aggregate_input_rows} GROUP BY 1 ORDER BY 2, 1"
            )
        if self._query_shape == "window_sort":
            partitions = max(1, math.ceil(scale.sort_rows / scale.window_partition_rows))
            return (
                f"SELECT `q`.`id`, SUM(MOD(`q`.`id` * {salt}, 1000003)) OVER ("
                f"PARTITION BY MOD(`q`.`id`, {partitions}) ORDER BY `q`.`id` "
                f"ROWS BETWEEN {scale.window_frame_rows} PRECEDING AND CURRENT ROW) "
                f"AS `window_sum` FROM `{first}` AS `q` WHERE `q`.`id` <= {scale.sort_rows} "
                "ORDER BY 2, 1"
            )
        return (
            f"SELECT `q`.`id` FROM `{first}` AS `q` "
            f"WHERE MOD((`q`.`id` * {salt}) + 17, 97) BETWEEN 3 AND 19 "
            f"AND `q`.`id` <= {scale.scan_rows} ORDER BY `q`.`id`"
        )

    def data_manifest(self, scale: ScaleKnobs) -> ScalableFuzzSetupManifest:
        if scale.table_rows > self.max_table_rows:
            raise ValueError("scale.table_rows exceeds max_table_rows")
        if scale.table_rows * len(self.schema.tables) > self.max_total_rows:
            raise ValueError("generated tables exceed max_total_rows")
        materialization_seed = self.schema.seed
        helper_name = f"sf_numbers_{materialization_seed & 0xFFFF_FFFF:x}"
        statements: list[str] = [
            *(f"DROP TABLE IF EXISTS `{plan.table.name}`" for plan in reversed(self._plans)),
            f"DROP TABLE IF EXISTS `{helper_name}`",
            f"CREATE TABLE `{helper_name}` (`n` INT UNSIGNED NOT NULL PRIMARY KEY) ENGINE=InnoDB",
            _helper_insert(helper_name),
            *(plan.table.render() for plan in self._plans),
        ]
        normalized_seed = materialization_seed & ((1 << 63) - 1)
        for plan in self._plans:
            statements.extend(
                (
                    f"DROP PROCEDURE IF EXISTS `{plan.procedure_name}`",
                    _fill_procedure(plan, helper_name),
                    f"CALL `{plan.procedure_name}`({scale.table_rows}, {normalized_seed}, "
                    f"{self.batch_rows})",
                    f"DROP PROCEDURE IF EXISTS `{plan.procedure_name}`",
                )
            )
        statements.append(f"DROP TABLE IF EXISTS `{helper_name}`")
        return ScalableFuzzSetupManifest(
            template_id="performance_fuzz_round_v1",
            seed=materialization_seed,
            schema=self.schema,
            expected_rows={table.name: scale.table_rows for table in self.schema.tables},
            setup_statements=tuple(statements),
            batch_rows=self.batch_rows,
        )


__all__ = ["PerformanceFuzzTemplate", "ScalableFuzzSetupManifest"]
