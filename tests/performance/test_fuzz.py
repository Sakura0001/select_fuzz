from __future__ import annotations

import re

import pytest

from select_fuzz.generation.composite_indexes import CompositeIndexFamily
from select_fuzz.generation.schema import IndexKind, SortDirection
from select_fuzz.performance.calibration import PerformanceTemplate
from select_fuzz.performance.fuzz import (
    PerformanceFuzzTemplate,
    ScalableFuzzSetupManifest,
)
from select_fuzz.performance.models import ScaleKnobs


def _template(seed: int = 17) -> PerformanceFuzzTemplate:
    return PerformanceFuzzTemplate(
        seed=seed,
        case_id="fuzz_case",
        min_initial_rows=100_000,
        max_initial_rows=1_000_000,
        max_table_rows=50_000_000,
        batch_rows=10_000,
    )


def test_performance_fuzz_case_is_seed_reproducible_and_implements_protocol() -> None:
    left = _template(991)
    right = _template(991)

    protocol_value: PerformanceTemplate = left
    assert protocol_value.seed == 991
    assert left.schema.canonical_bytes() == right.schema.canonical_bytes()
    assert left.initial_scale == right.initial_scale
    assert left.template_id == right.template_id
    assert left.render(left.initial_scale) == right.render(right.initial_scale)
    assert left.data_manifest(left.initial_scale) == right.data_manifest(right.initial_scale)


def test_performance_fuzz_randomizes_schema_indexes_types_ranges_and_query_shapes() -> None:
    cases = tuple(_template(seed) for seed in range(40, 80))
    schema_fingerprints = {case.schema.canonical_bytes() for case in cases}
    table_counts = {len(case.schema.tables) for case in cases}
    declarations = {
        column.mysql_type
        for case in cases
        for table in case.schema.tables
        for column in table.columns
        if column.name != "id"
    }
    secondary_index_counts = {
        sum(not index.primary for table in case.schema.tables for index in table.indexes)
        for case in cases
    }
    template_ids = {case.template_id for case in cases}

    assert len(schema_fingerprints) > 30
    assert len(table_counts) > 1
    assert len(declarations) > 20
    assert any("(" in declaration for declaration in declarations)
    assert len(secondary_index_counts) > 1
    assert len(template_ids) >= 4


def test_performance_fuzz_excludes_special_types_indexes_and_always_reads_generated_table() -> None:
    for seed in range(100):
        case = _template(seed)
        generated_tables = {table.name for table in case.schema.tables}
        sql = case.render(case.initial_scale)

        assert re.search(r"\bFROM\s+`(?:" + "|".join(generated_tables) + r")`", sql)
        assert not {"JSON", "GEOMETRY", "POINT"} & {
            column.base_type for table in case.schema.tables for column in table.columns
        }
        assert all(
            index.kind not in {IndexKind.FULLTEXT, IndexKind.SPATIAL, IndexKind.MULTIVALUE}
            for table in case.schema.tables
            for index in table.indexes
        )
        assert sql.lstrip().upper().startswith("SELECT")
        assert ";" not in sql


def test_performance_seed_window_reaches_safe_composite_index_families() -> None:
    reached = {}
    name_to_family = {
        "idx_comp_ordinary": CompositeIndexFamily.ORDINARY,
        "uq_comp_unique": CompositeIndexFamily.UNIQUE,
        "idx_comp_mixed_direction": CompositeIndexFamily.MIXED_DIRECTION,
        "idx_comp_prefix": CompositeIndexFamily.PREFIX,
        "idx_comp_wide": CompositeIndexFamily.WIDE,
    }

    for seed in range(300):
        case = PerformanceFuzzTemplate(
            seed=seed,
            case_id=f"composite_{seed}",
            min_columns=6,
            max_columns=10,
            max_indexes_per_table=6,
        )
        for table in case.schema.tables:
            assert len(table.indexes) <= 6
            signatures = [
                (index.unique, index.kind, index.parts) for index in table.indexes
            ]
            assert len(signatures) == len(set(signatures))
            for index in table.indexes:
                assert index.kind not in {
                    IndexKind.FULLTEXT,
                    IndexKind.SPATIAL,
                    IndexKind.MULTIVALUE,
                }
                family = name_to_family.get(index.name)
                if family is not None:
                    reached.setdefault(family, index)

    assert set(reached) == set(CompositeIndexFamily)
    assert reached[CompositeIndexFamily.UNIQUE].parts[0].column_name == "id"
    assert {
        part.direction
        for part in reached[CompositeIndexFamily.MIXED_DIRECTION].parts
    } == {SortDirection.ASC, SortDirection.DESC}
    assert any(
        part.prefix_length is not None
        for part in reached[CompositeIndexFamily.PREFIX].parts
    )
    assert 3 <= len(reached[CompositeIndexFamily.WIDE].parts) <= 4


def test_scalable_manifest_uses_deterministic_batched_procedures_with_bounded_sql() -> None:
    case = _template(20260715)
    small = case.data_manifest(case.initial_scale)
    large_scale = case.initial_scale.scaled(50.0, row_cap=case.max_table_rows)
    large = case.data_manifest(large_scale)

    assert isinstance(small, ScalableFuzzSetupManifest)
    assert small.table_names == tuple(table.name for table in case.schema.tables)
    assert all(count == case.initial_scale.table_rows for count in small.expected_rows.values())
    assert all(count == large_scale.table_rows for count in large.expected_rows.values())
    assert any("CREATE PROCEDURE" in statement for statement in small.setup_statements)
    assert any("WHILE v_offset < p_target" in statement for statement in small.setup_statements)
    assert all(
        "\n" not in statement
        for statement in small.setup_statements
        if statement.startswith("CREATE PROCEDURE")
    )
    assert any("CROSS JOIN" in statement for statement in small.setup_statements)
    assert any("CALL `sf_fill_" in statement for statement in small.setup_statements)
    assert not any("RAND(" in statement.upper() for statement in small.setup_statements)
    assert not any("%" in statement and "+ -(" in statement for statement in small.setup_statements)
    assert abs(sum(map(len, large.setup_statements)) - sum(map(len, small.setup_statements))) < 100


def test_initial_rows_are_seeded_in_range_and_scale_is_capped() -> None:
    initial_rows = {_template(seed).initial_scale.table_rows for seed in range(100)}

    assert min(initial_rows) >= 100_000
    assert max(initial_rows) <= 1_000_000
    assert len(initial_rows) > 90

    case = _template(9)
    over_cap = ScaleKnobs().scaled(1_000.0, row_cap=100_000_000)
    with pytest.raises(ValueError, match="max_table_rows"):
        case.data_manifest(over_cap)


def test_for_case_reuses_round_schema_and_derives_distinct_queries() -> None:
    base = _template(44)

    first = base.for_case(3, 7)
    repeated = base.for_case(3, 7)
    other = base.for_case(3, 8)
    next_round = base.for_case(4, 7)

    assert first.case_id == "fuzz_case_r3_q7"
    assert first.schema.canonical_bytes() == repeated.schema.canonical_bytes()
    assert first.render(first.initial_scale) == repeated.render(repeated.initial_scale)
    assert first.schema.canonical_bytes() == other.schema.canonical_bytes()
    assert first.initial_scale == other.initial_scale
    assert first.data_manifest(first.initial_scale) == other.data_manifest(other.initial_scale)
    assert first.render(first.initial_scale) != other.render(other.initial_scale)
    assert first.schema.canonical_bytes() != next_round.schema.canonical_bytes()


def test_query_budget_limits_remove_multi_table_and_deep_shapes() -> None:
    cases = tuple(
        PerformanceFuzzTemplate(
            seed=seed,
            case_id=f"limited_{seed}",
            max_query_tables=1,
            max_query_depth=1,
        )
        for seed in range(30)
    )

    assert all("JOIN" not in case.render(case.initial_scale).upper() for case in cases)
    assert all(" OVER (" not in case.render(case.initial_scale).upper() for case in cases)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"min_initial_rows": 0}, "min_initial_rows"),
        ({"min_initial_rows": 10, "max_initial_rows": 9}, "max_initial_rows"),
        ({"max_initial_rows": 11, "max_table_rows": 10}, "max_table_rows"),
        ({"batch_rows": 0}, "batch_rows"),
    ],
)
def test_performance_fuzz_rejects_invalid_volume_limits(
    kwargs: dict[str, int], message: str
) -> None:
    values = {
        "seed": 1,
        "case_id": "case",
        "min_initial_rows": 10,
        "max_initial_rows": 20,
        "max_table_rows": 30,
        "batch_rows": 10,
        **kwargs,
    }

    with pytest.raises(ValueError, match=message):
        PerformanceFuzzTemplate(**values)


@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        ({"seed": True}, TypeError, "seed"),
        ({"schema_seed": True}, TypeError, "schema_seed"),
        ({"case_id": "not-safe"}, ValueError, "case_id"),
        ({"max_total_rows": 29}, ValueError, "max_total_rows"),
        ({"min_tables": 2, "max_tables": 1}, ValueError, "min_tables"),
        ({"min_columns": 4, "max_columns": 3}, ValueError, "min_columns"),
        ({"batch_rows": 10_001}, ValueError, "batch_rows"),
    ],
)
def test_performance_fuzz_rejects_invalid_identity_and_shape_limits(
    kwargs: dict[str, object], error: type[Exception], message: str
) -> None:
    values: dict[str, object] = {
        "seed": 1,
        "case_id": "case",
        "min_initial_rows": 10,
        "max_initial_rows": 20,
        "max_table_rows": 30,
        "max_total_rows": 90,
        "batch_rows": 10,
        **kwargs,
    }

    with pytest.raises(error, match=message):
        PerformanceFuzzTemplate(**values)  # type: ignore[arg-type]
