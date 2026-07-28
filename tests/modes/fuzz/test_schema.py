from __future__ import annotations

import re

from select_fuzz.config import FuzzConfig
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
