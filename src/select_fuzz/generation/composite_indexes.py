"""Representation-independent planning for seeded composite BTREE indexes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import random
import re
from collections.abc import Iterable, Sequence


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BASE_TYPE = re.compile(r"^([A-Z]+)")
_DECLARED_LENGTH = re.compile(r"^[A-Z]+\(([0-9]+)")
_UNSUPPORTED_TYPES = frozenset(
    {
        "JSON",
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
_CHARACTER_TYPES = frozenset(
    {"CHAR", "VARCHAR", "NCHAR", "NVARCHAR", "TINYTEXT", "TEXT", "MEDIUMTEXT", "LONGTEXT"}
)
_BINARY_TYPES = frozenset(
    {"BINARY", "VARBINARY", "TINYBLOB", "BLOB", "MEDIUMBLOB", "LONGBLOB"}
)
_LOB_TYPES = frozenset(
    {"TINYTEXT", "TEXT", "MEDIUMTEXT", "LONGTEXT", "TINYBLOB", "BLOB", "MEDIUMBLOB", "LONGBLOB"}
)
_FIXED_BYTES = {
    "TINYINT": 1,
    "SMALLINT": 2,
    "MEDIUMINT": 3,
    "INT": 4,
    "INTEGER": 4,
    "BIGINT": 8,
    "FLOAT": 4,
    "DOUBLE": 8,
    "DATE": 3,
    "TIME": 7,
    "DATETIME": 8,
    "TIMESTAMP": 7,
    "YEAR": 1,
    "BOOL": 1,
    "BOOLEAN": 1,
    "ENUM": 2,
    "SET": 8,
}


class CompositeIndexFamily(StrEnum):
    """Composite shapes that production modes must make seed-reachable."""

    ORDINARY = "ordinary"
    UNIQUE = "unique"
    MIXED_DIRECTION = "mixed_direction"
    PREFIX = "prefix"
    WIDE = "wide"


@dataclass(frozen=True, slots=True)
class CompositeColumn:
    """The physical facts needed to decide whether a column can be indexed."""

    name: str
    mysql_type: str
    charset_bytes: int = 1

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.name):
            raise ValueError("column name must be a MySQL-safe identifier")
        if not isinstance(self.mysql_type, str) or not self.mysql_type.strip():
            raise ValueError("mysql_type must be nonempty")
        if (
            not isinstance(self.charset_bytes, int)
            or isinstance(self.charset_bytes, bool)
            or self.charset_bytes < 1
            or self.charset_bytes > 4
        ):
            raise ValueError("charset_bytes must be between 1 and 4")

    @property
    def base_type(self) -> str:
        match = _BASE_TYPE.match(self.mysql_type.strip().upper())
        return "" if match is None else match.group(1)


@dataclass(frozen=True, slots=True)
class CompositeIndexPartPlan:
    """One rendered index part plus its conservative physical byte cost."""

    column_name: str
    estimated_bytes: int
    prefix_length: int | None = None
    descending: bool = False

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.column_name):
            raise ValueError("index column name must be a MySQL-safe identifier")
        if (
            not isinstance(self.estimated_bytes, int)
            or isinstance(self.estimated_bytes, bool)
            or self.estimated_bytes < 1
        ):
            raise ValueError("estimated_bytes must be positive")
        if self.prefix_length is not None and (
            not isinstance(self.prefix_length, int)
            or isinstance(self.prefix_length, bool)
            or self.prefix_length < 1
        ):
            raise ValueError("prefix_length must be positive")
        if not isinstance(self.descending, bool):
            raise TypeError("descending must be a boolean")


@dataclass(frozen=True, slots=True)
class CompositeIndexPlan:
    """A safe composite shape that a mode can adapt to its schema model."""

    family: CompositeIndexFamily
    parts: tuple[CompositeIndexPartPlan, ...]
    unique: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.family, CompositeIndexFamily):
            raise TypeError("family must be a CompositeIndexFamily")
        object.__setattr__(self, "parts", tuple(self.parts))
        names = tuple(part.column_name for part in self.parts)
        if len(names) < 2 or len(names) > 4:
            raise ValueError("a composite index plan requires between two and four parts")
        if len(set(names)) != len(names):
            raise ValueError("composite index column names must be unique")
        if not isinstance(self.unique, bool):
            raise TypeError("unique must be a boolean")

    @property
    def estimated_bytes(self) -> int:
        return sum(part.estimated_bytes for part in self.parts)


def _declared_length(column: CompositeColumn) -> int | None:
    match = _DECLARED_LENGTH.match(column.mysql_type.strip().upper())
    return None if match is None else int(match.group(1))


def _full_index_bytes(column: CompositeColumn) -> int | None:
    base = column.base_type
    if not base or base in _UNSUPPORTED_TYPES or base in _LOB_TYPES:
        return None
    if base in _FIXED_BYTES:
        return _FIXED_BYTES[base]
    length = _declared_length(column)
    if base == "BIT" and length is not None:
        return max(1, (length + 7) // 8)
    if base in {"DECIMAL", "NUMERIC"} and length is not None:
        return (length + 1) // 2 + 1
    if base in {"CHAR", "VARCHAR", "NCHAR", "NVARCHAR"} and length is not None:
        return length * column.charset_bytes
    if base in {"BINARY", "VARBINARY"} and length is not None:
        return length
    return None


def _prefix_unit_bytes(column: CompositeColumn) -> int | None:
    if column.base_type in _CHARACTER_TYPES:
        return column.charset_bytes
    if column.base_type in _BINARY_TYPES:
        return 1
    return None


def _prefix_cap(column: CompositeColumn) -> int:
    declared = _declared_length(column)
    return 32 if declared is None else min(32, declared)


def _full_part(column: CompositeColumn, *, descending: bool = False) -> CompositeIndexPartPlan | None:
    estimated_bytes = _full_index_bytes(column)
    if estimated_bytes is None:
        return None
    return CompositeIndexPartPlan(
        column_name=column.name,
        estimated_bytes=estimated_bytes,
        descending=descending,
    )


def _first_fitting_full_parts(
    columns: Sequence[CompositeColumn],
    *,
    count: int,
    budget: int,
    rng: random.Random,
    excluded: Iterable[str] = (),
) -> tuple[CompositeIndexPartPlan, ...]:
    excluded_names = frozenset(excluded)
    shuffled = [column for column in columns if column.name not in excluded_names]
    rng.shuffle(shuffled)
    selected: list[CompositeIndexPartPlan] = []
    used_bytes = 0
    for column in shuffled:
        part = _full_part(column)
        if part is None or used_bytes + part.estimated_bytes > budget:
            continue
        selected.append(part)
        used_bytes += part.estimated_bytes
        if len(selected) == count:
            return tuple(selected)
    return ()


def _ordinary_plan(
    columns: Sequence[CompositeColumn], rng: random.Random, budget: int
) -> CompositeIndexPlan | None:
    parts = _first_fitting_full_parts(columns, count=2, budget=budget, rng=rng)
    if not parts:
        return None
    return CompositeIndexPlan(CompositeIndexFamily.ORDINARY, parts)


def _unique_plan(
    columns: Sequence[CompositeColumn],
    *,
    rng: random.Random,
    budget: int,
    identity_column: str,
    required_columns: Sequence[str],
) -> CompositeIndexPlan | None:
    by_name = {column.name: column for column in columns}
    required_names = tuple(dict.fromkeys((identity_column, *required_columns)))
    required_parts: list[CompositeIndexPartPlan] = []
    for name in required_names:
        column = by_name.get(name)
        part = None if column is None else _full_part(column)
        if part is None:
            return None
        required_parts.append(part)
    required_bytes = sum(part.estimated_bytes for part in required_parts)
    if required_bytes >= budget:
        return None
    candidate_parts = _first_fitting_full_parts(
        columns,
        count=1,
        budget=budget - required_bytes,
        rng=rng,
        excluded=required_names,
    )
    if not candidate_parts:
        return None
    parts = (required_parts[0], candidate_parts[0], *required_parts[1:])
    if len(parts) > 4:
        return None
    return CompositeIndexPlan(CompositeIndexFamily.UNIQUE, parts, unique=True)


def _mixed_direction_plan(
    columns: Sequence[CompositeColumn], rng: random.Random, budget: int
) -> CompositeIndexPlan | None:
    parts = _first_fitting_full_parts(columns, count=2, budget=budget, rng=rng)
    if not parts:
        return None
    return CompositeIndexPlan(
        CompositeIndexFamily.MIXED_DIRECTION,
        (parts[0], CompositeIndexPartPlan(parts[1].column_name, parts[1].estimated_bytes, descending=True)),
    )


def _prefix_plan(
    columns: Sequence[CompositeColumn], rng: random.Random, budget: int
) -> CompositeIndexPlan | None:
    prefix_columns = [column for column in columns if _prefix_unit_bytes(column) is not None]
    rng.shuffle(prefix_columns)
    full_columns = list(columns)
    rng.shuffle(full_columns)
    for prefix_column in prefix_columns:
        unit_bytes = _prefix_unit_bytes(prefix_column)
        assert unit_bytes is not None
        for full_column in full_columns:
            if full_column.name == prefix_column.name:
                continue
            full_part = _full_part(full_column)
            if full_part is None:
                continue
            available = budget - full_part.estimated_bytes
            prefix_length = min(_prefix_cap(prefix_column), available // unit_bytes)
            if prefix_length < 1:
                continue
            return CompositeIndexPlan(
                CompositeIndexFamily.PREFIX,
                (
                    CompositeIndexPartPlan(
                        prefix_column.name,
                        prefix_length * unit_bytes,
                        prefix_length=prefix_length,
                    ),
                    full_part,
                ),
            )
    return None


def _wide_plan(
    columns: Sequence[CompositeColumn], rng: random.Random, budget: int
) -> CompositeIndexPlan | None:
    preferred_count = rng.choice((3, 4))
    for count in dict.fromkeys((preferred_count, 4, 3)):
        parts = _first_fitting_full_parts(columns, count=count, budget=budget, rng=rng)
        if parts:
            return CompositeIndexPlan(CompositeIndexFamily.WIDE, parts)
    return None


def build_composite_index_candidates(
    columns: Sequence[CompositeColumn],
    *,
    rng: random.Random,
    index_byte_budget: int,
    identity_column: str = "id",
    unique_required_columns: Sequence[str] = (),
) -> tuple[CompositeIndexPlan, ...]:
    """Return deterministic safe candidates; impossible families are omitted."""

    if (
        not isinstance(index_byte_budget, int)
        or isinstance(index_byte_budget, bool)
        or index_byte_budget <= 0
    ):
        raise ValueError("index_byte_budget must be positive")
    planned_columns = tuple(columns)
    names = tuple(column.name for column in planned_columns)
    if len(set(names)) != len(names):
        raise ValueError("column names must be unique")
    if not _IDENTIFIER.fullmatch(identity_column):
        raise ValueError("identity_column must be a MySQL-safe identifier")
    required = tuple(unique_required_columns)
    if any(not _IDENTIFIER.fullmatch(name) for name in required):
        raise ValueError("unique_required_columns must contain MySQL-safe identifiers")

    plans = (
        _ordinary_plan(planned_columns, rng, index_byte_budget),
        _unique_plan(
            planned_columns,
            rng=rng,
            budget=index_byte_budget,
            identity_column=identity_column,
            required_columns=required,
        ),
        _mixed_direction_plan(planned_columns, rng, index_byte_budget),
        _prefix_plan(planned_columns, rng, index_byte_budget),
        _wide_plan(planned_columns, rng, index_byte_budget),
    )
    return tuple(plan for plan in plans if plan is not None)


__all__ = [
    "CompositeColumn",
    "CompositeIndexFamily",
    "CompositeIndexPartPlan",
    "CompositeIndexPlan",
    "build_composite_index_candidates",
]
