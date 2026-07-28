"""Deterministic random schema plans used by the concurrent fuzz mode."""

from __future__ import annotations

from dataclasses import dataclass
import random

from select_fuzz.config import FuzzConfig
from select_fuzz.generation.query_grammar import GrammarColumn, GrammarTable


CORE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "BIGINT UNSIGNED"),
    ("tenant_id", "BIGINT UNSIGNED"),
    ("amount", "BIGINT"),
    ("status", "INT"),
    ("updated_at", "DATETIME(6)"),
    ("payload", "VARCHAR(255)"),
)

RANDOM_COLUMN_TYPES: tuple[str, ...] = (
    "TINYINT",
    "TINYINT UNSIGNED",
    "SMALLINT",
    "SMALLINT UNSIGNED",
    "MEDIUMINT",
    "MEDIUMINT UNSIGNED",
    "INT",
    "INT UNSIGNED",
    "BIGINT",
    "BIGINT UNSIGNED",
    "DECIMAL(20,6)",
    "DECIMAL(30,10)",
    "NUMERIC(18,4)",
    "FLOAT",
    "FLOAT(10,4)",
    "DOUBLE",
    "DOUBLE(16,6)",
    "BOOLEAN",
    "BOOL",
    "BIT(1)",
    "BIT(8)",
    "BIT(32)",
    "BIT(64)",
    "DATE",
    "DATETIME",
    "DATETIME(3)",
    "DATETIME(6)",
    "TIMESTAMP",
    "TIMESTAMP(3)",
    "TIMESTAMP(6)",
    "TIME",
    "TIME(3)",
    "TIME(6)",
    "YEAR",
    "CHAR(16)",
    "CHAR(32)",
    "NCHAR(16)",
    "VARCHAR(64)",
    "VARCHAR(128)",
    "VARCHAR(255)",
    "NVARCHAR(64)",
    "BINARY(8)",
    "BINARY(32)",
    "VARBINARY(32)",
    "VARBINARY(64)",
    "VARBINARY(255)",
    "TINYTEXT",
    "TEXT",
    "MEDIUMTEXT",
    "LONGTEXT",
    "TINYBLOB",
    "BLOB",
    "MEDIUMBLOB",
    "LONGBLOB",
    "ENUM('alpha','beta','gamma')",
    "SET('red','green','blue')",
)

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


def _quote_identifier(value: str) -> str:
    if not value or not value.replace("_", "").isalnum() or value[0].isdigit():
        raise ValueError("identifier must be alphanumeric snake_case")
    return f"`{value}`"


@dataclass(frozen=True, slots=True)
class FuzzColumnSpec:
    name: str
    mysql_type: str
    nullable: bool = True

    def ddl(self) -> str:
        nullability = " NULL" if self.nullable else " NOT NULL"
        auto_increment = " AUTO_INCREMENT" if self.name == "id" else ""
        return f"{_quote_identifier(self.name)} {self.mysql_type}{nullability}{auto_increment}"


@dataclass(frozen=True, slots=True)
class FuzzIndexSpec:
    name: str
    ddl: str


@dataclass(frozen=True, slots=True)
class FuzzTableSpec:
    name: str
    columns: tuple[FuzzColumnSpec, ...]
    indexes: tuple[FuzzIndexSpec, ...]

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    @property
    def index_names(self) -> tuple[str, ...]:
        return tuple(index.name for index in self.indexes)

    def grammar_table(self) -> GrammarTable:
        return GrammarTable(
            self.name,
            tuple(GrammarColumn(column.name, column.mysql_type) for column in self.columns),
            self.index_names,
        )

    def create_sql(self) -> str:
        definitions = [column.ddl() for column in self.columns]
        definitions.extend(index.ddl for index in self.indexes)
        return (
            f"CREATE TABLE IF NOT EXISTS {_quote_identifier(self.name)} ("
            + ",".join(definitions)
            + ") ENGINE=InnoDB"
        )


def _digit_source(alias: str) -> str:
    values = " UNION ALL ".join(
        f"SELECT {value}" if value else "SELECT 0 AS n" for value in range(10)
    )
    return f"({values}) AS {alias}"


def _sequence_sql(rows: int) -> str:
    digits = max(1, len(str(rows - 1)))
    sources = " CROSS JOIN ".join(_digit_source(f"d{index}") for index in range(digits))
    terms = " + ".join(f"{10**index} * d{index}.n" for index in range(digits))
    return f"(SELECT 1 + {terms} AS n FROM {sources}) AS seq"


def _value_expression(column: FuzzColumnSpec, seed: int) -> str:
    name = column.name
    normalized = column.mysql_type.upper()
    salt = abs(seed) % 10007
    if normalized == "TINYINT":
        return f"MOD(n + {salt}, 100) - 50"
    if normalized == "TINYINT UNSIGNED":
        return f"MOD(n + {salt}, 100)"
    if normalized == "SMALLINT":
        return f"MOD(n + {salt}, 30000) - 15000"
    if normalized == "SMALLINT UNSIGNED":
        return f"MOD(n + {salt}, 30000)"
    if normalized == "MEDIUMINT":
        return f"MOD(n + {salt}, 1000000) - 500000"
    if normalized == "MEDIUMINT UNSIGNED":
        return f"MOD(n + {salt}, 1000000)"
    if normalized in {"INT", "BIGINT"}:
        return f"MOD(n + {salt}, 1000000)"
    if normalized in {"INT UNSIGNED", "BIGINT UNSIGNED"}:
        return f"MOD(n + {salt}, 1000000) + 1"
    if normalized in {"BOOLEAN", "BOOL"}:
        return f"MOD(n + {salt}, 2)"
    if normalized.startswith(("DECIMAL", "NUMERIC")):
        return f"CAST(MOD(n + {salt}, 1000000) AS DECIMAL(30,10)) / 10"
    if normalized.startswith(("FLOAT", "DOUBLE")):
        return f"MOD(n + {salt}, 1000000) / 10.0"
    if normalized.startswith("BIT"):
        return f"MOD(n + {salt}, 2)"
    if normalized == "YEAR":
        return f"2020 + MOD(n + {salt}, 20)"
    if normalized.startswith(("CHAR", "NCHAR")):
        return f"LPAD(MOD(n + {salt}, 100000000), 16, '0')"
    if normalized.startswith(("VARCHAR", "NVARCHAR")):
        return f"CONCAT('v{salt}-', n)"
    if normalized.startswith(("ENUM", "SET")):
        return "'alpha'" if normalized.startswith("ENUM") else "'red,green'"
    if normalized == "DATE":
        return f"DATE_ADD('2020-01-01', INTERVAL MOD(n + {salt}, 3650) DAY)"
    if normalized.startswith(("DATETIME", "TIMESTAMP")):
        return (
            "TIMESTAMPADD(SECOND, "
            f"MOD(n + {salt}, 31536000), '2020-01-01 00:00:00')"
        )
    if normalized.startswith("TIME"):
        return f"SEC_TO_TIME(MOD(n + {salt}, 86400))"
    if normalized.startswith("BINARY("):
        width = int(normalized.split("(", 1)[1].rstrip(")"))
        return f"UNHEX(LPAD(HEX(MOD(n + {salt}, 100000000)), {width * 2}, '0'))"
    if normalized.startswith("VARBINARY"):
        return f"CONVERT(CONCAT('b{salt}-', n) USING binary)"
    if normalized in _LOB_TYPES:
        if "TEXT" in normalized:
            return f"CONCAT('text-{salt}-', n)"
        return f"CONVERT(CONCAT('blob-{salt}-', n) USING binary)"
    raise ValueError(f"unsupported fuzz column type for {name}: {column.mysql_type}")


def initial_insert_sql(spec: FuzzTableSpec, rows: int, seed: int) -> str:
    if rows < 1:
        raise ValueError("rows must be positive")
    expressions: list[str] = []
    for column in spec.columns:
        if column.name == "id":
            expressions.append("n")
        elif column.name == "tenant_id":
            expressions.append(f"MOD(n + {abs(seed) % 10007}, 1024) + 1")
        elif column.name == "amount":
            expressions.append(f"MOD((n * 1103515245) + {seed}, 1000000)")
        elif column.name == "status":
            expressions.append(f"MOD(n + {seed}, 16)")
        elif column.name == "updated_at":
            expressions.append(
                "TIMESTAMPADD(SECOND, MOD(n, 31536000), '2020-01-01 00:00:00')"
            )
        elif column.name == "payload":
            expressions.append(f"CONCAT('payload-{abs(seed) % 10007}-', n)")
        else:
            expressions.append(_value_expression(column, seed))
    names = ",".join(_quote_identifier(column.name) for column in spec.columns)
    return (
        f"INSERT INTO {_quote_identifier(spec.name)} ({names}) SELECT "
        + ", ".join(expressions)
        + f" FROM {_sequence_sql(rows)} WHERE n <= {rows} ORDER BY n"
    )


def _expression_index(rng: random.Random, index: int) -> FuzzIndexSpec:
    choice = rng.choice(("payload", "amount", "status", "tenant_id"))
    if choice == "payload":
        expression = "LOWER(`payload`)"
    else:
        expression = f"ABS(`{choice}`)"
    name = f"idx_expr_{index:02d}_{choice}"
    return FuzzIndexSpec(name, f"KEY {_quote_identifier(name)} (({expression}))")


def _is_indexable(column: FuzzColumnSpec) -> bool:
    return column.mysql_type.upper() not in _LOB_TYPES


def build_table_specs(
    table_names: tuple[str, ...],
    config: FuzzConfig,
    *,
    seed: int,
) -> tuple[FuzzTableSpec, ...]:
    """Build a deterministic schema with at least fifty columns per table."""

    specs: list[FuzzTableSpec] = []
    for table_index, table_name in enumerate(table_names):
        rng = random.Random(seed + table_index * 1_000_003)
        column_count = rng.randint(
            config.min_columns_per_table,
            config.max_columns_per_table,
        )
        columns = [
            FuzzColumnSpec(name, mysql_type, nullable=False)
            for name, mysql_type in CORE_COLUMNS
        ]
        shuffled_types = list(RANDOM_COLUMN_TYPES)
        rng.shuffle(shuffled_types)
        columns.extend(
            FuzzColumnSpec(
                f"c{column_index:03d}",
                shuffled_types[column_index % len(shuffled_types)],
                nullable=True,
            )
            for column_index in range(column_count - len(CORE_COLUMNS))
        )
        extras = tuple(column.name for column in columns[len(CORE_COLUMNS) :])
        indexable_extras = tuple(
            column.name
            for column in columns[len(CORE_COLUMNS) :]
            if _is_indexable(column)
        )
        if not extras or not indexable_extras:
            raise ValueError("fuzz tables require random extension columns")

        indexes: list[FuzzIndexSpec] = []
        primary_direction = " DESC" if rng.choice((False, True)) else ""
        indexes.append(
            FuzzIndexSpec(
                "PRIMARY",
                f"PRIMARY KEY (`id`{primary_direction})",
            )
        )
        descending_column = rng.choice(indexable_extras)
        descending_name = f"idx_desc_{descending_column}"
        indexes.append(
            FuzzIndexSpec(
                descending_name,
                f"KEY {_quote_identifier(descending_name)} ({_quote_identifier(descending_column)} DESC)",
            )
        )
        # Initial data deliberately exercises every random type, so many
        # candidates (ENUM/SET, small integers, dates) cannot safely carry a
        # unique key.  The business payload is generated from the row number
        # and remains unique across the initial batch and DML inserts.
        unique_column = "payload"
        unique_name = "uq_payload"
        indexes.append(
            FuzzIndexSpec(
                unique_name,
                f"UNIQUE KEY {_quote_identifier(unique_name)} ({_quote_identifier(unique_column)})",
            )
        )
        indexes.append(_expression_index(rng, 0))

        target_indexes = rng.randint(
            config.min_indexes_per_table,
            config.max_indexes_per_table,
        )
        index_number = 1
        while len(indexes) < target_indexes:
            column = rng.choice(indexable_extras)
            index_name = f"idx_rand_{index_number:02d}_{column}"
            indexes.append(
                FuzzIndexSpec(
                    index_name,
                    f"KEY {_quote_identifier(index_name)} ({_quote_identifier(column)})",
                )
            )
            index_number += 1
        specs.append(FuzzTableSpec(table_name, tuple(columns), tuple(indexes)))
    return tuple(specs)


__all__ = [
    "CORE_COLUMNS",
    "FuzzColumnSpec",
    "FuzzIndexSpec",
    "FuzzTableSpec",
    "RANDOM_COLUMN_TYPES",
    "build_table_specs",
    "initial_insert_sql",
]
