from __future__ import annotations

import re

from select_fuzz.config import FuzzConfig
from select_fuzz.generation.composite_indexes import CompositeIndexFamily
from select_fuzz.modes.fuzz.schema import (
    RANDOM_COLUMN_TYPES,
    build_table_specs,
    initial_insert_sql,
)


def test_fuzz_schema_has_at_least_fifty_random_columns_and_required_index_families() -> None:
    assert len(RANDOM_COLUMN_TYPES) >= 50
    config = FuzzConfig(
        initial_tables=2,
        min_columns_per_table=50,
        max_columns_per_table=55,
        min_indexes_per_table=4,
        max_indexes_per_table=8,
    )
    specs = build_table_specs(("fuzz_t0", "fuzz_t1"), config, seed=42)

    assert len(specs) == 2
    assert all(50 <= len(spec.columns) <= 55 for spec in specs)
    assert all(len({column.mysql_type for column in spec.columns[6:]}) >= 2 for spec in specs)
    for spec in specs:
        ddl = spec.create_sql()
        assert "PRIMARY KEY" in ddl
        assert " DESC)" in ddl
        assert "UNIQUE KEY" in ddl
        assert "((" in ddl
        assert len(spec.indexes) >= 4


def test_initial_insert_populates_random_columns() -> None:
    config = FuzzConfig(min_columns_per_table=50, max_columns_per_table=50)
    spec = build_table_specs(("fuzz_t0",), config, seed=7)[0]
    sql = initial_insert_sql(spec, 100, 7)

    assert sql.startswith("INSERT INTO `fuzz_t0`")
    assert sql.count("`") >= 100
    assert re.search(r"WHERE n <= 100 ORDER BY n$", sql)


def test_fuzz_schema_seed_window_reaches_safe_composite_index_families() -> None:
    config = FuzzConfig(
        initial_tables=1,
        min_columns_per_table=50,
        max_columns_per_table=60,
        min_indexes_per_table=4,
        max_indexes_per_table=12,
    )
    reached: dict[CompositeIndexFamily, str] = {}
    name_to_family = {
        "idx_comp_ordinary": CompositeIndexFamily.ORDINARY,
        "uq_comp_unique": CompositeIndexFamily.UNIQUE,
        "idx_comp_mixed_direction": CompositeIndexFamily.MIXED_DIRECTION,
        "idx_comp_prefix": CompositeIndexFamily.PREFIX,
        "idx_comp_wide": CompositeIndexFamily.WIDE,
    }

    for seed in range(200):
        spec = build_table_specs(("fuzz_t0",), config, seed=seed)[0]
        assert spec == build_table_specs(("fuzz_t0",), config, seed=seed)[0]
        assert len(spec.indexes) <= config.max_indexes_per_table
        ddl = spec.create_sql()
        assert "FULLTEXT" not in ddl
        assert "SPATIAL" not in ddl
        for index in spec.indexes:
            family = name_to_family.get(index.name)
            if family is not None:
                reached.setdefault(family, index.ddl)

    assert set(reached) == set(CompositeIndexFamily)
    assert reached[CompositeIndexFamily.UNIQUE].startswith("UNIQUE KEY")
    assert " DESC" in reached[CompositeIndexFamily.MIXED_DIRECTION]
    assert re.search(r"`[A-Za-z0-9_]+`\([1-9][0-9]*\)", reached[CompositeIndexFamily.PREFIX])
    assert reached[CompositeIndexFamily.WIDE].count(",") >= 2


def test_fuzz_schema_never_exceeds_a_four_index_ceiling() -> None:
    config = FuzzConfig(
        initial_tables=1,
        min_columns_per_table=50,
        max_columns_per_table=50,
        min_indexes_per_table=4,
        max_indexes_per_table=4,
    )

    for seed in range(20):
        spec = build_table_specs(("fuzz_t0",), config, seed=seed)[0]
        assert len(spec.indexes) == 4
