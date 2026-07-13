from __future__ import annotations

import os
import time

import mysql.connector
import pytest

from select_fuzz.generation.query import QueryGenerator, QueryLane
from select_fuzz.generation.schema import SchemaGenerator, SchemaLimits, SchemaProfile
from select_fuzz.generation.setup import SetupBundleBuilder
from select_fuzz.performance.models import ScaleKnobs
from select_fuzz.performance.templates import (
    CpuDenseGroupSortTemplate,
    CpuDenseJoinTemplate,
    CpuDenseRangeSortTemplate,
    CpuDenseScanTemplate,
    CpuDenseWindowTemplate,
)
from select_fuzz.performance.tree import parse_tree


def _sockets() -> tuple[str, ...]:
    if os.environ.get("SELECT_FUZZ_MYSQL_SOCKET_INTEGRATION") != "1":
        pytest.skip("set SELECT_FUZZ_MYSQL_SOCKET_INTEGRATION=1 and socket list")
    sockets_value = os.environ.get("SELECT_FUZZ_MYSQL_SOCKETS")
    if sockets_value is None:
        pytest.skip("SELECT_FUZZ_MYSQL_SOCKETS is unset")
    sockets = tuple(item for item in sockets_value.split(",") if item)
    if len(sockets) != 3:
        pytest.skip("SELECT_FUZZ_MYSQL_SOCKETS must contain exactly three paths")
    return sockets


@pytest.mark.mysql
@pytest.mark.timeout(600)
def test_every_schema_profile_and_generated_query_on_three_exact_8041_sockets() -> None:
    sockets = _sockets()
    query_generator = QueryGenerator()
    catalog = query_generator.feature_catalog()
    schema_generator = SchemaGenerator()
    setup_builder = SetupBundleBuilder()
    connections = [
        mysql.connector.connect(
            unix_socket=socket,
            user="root",
            autocommit=True,
        )
        for socket in sockets
    ]
    try:
        for connection in connections:
            cursor = connection.cursor()
            cursor.execute("SELECT VERSION()")
            assert cursor.fetchone()[0].startswith("8.0.41")
            cursor.close()
        for ordinal, profile in enumerate(SchemaProfile):
            candidates = sorted(
                (
                    spec
                    for spec in catalog
                    if spec.compatible_profiles == frozenset({profile.value})
                ),
                key=lambda spec: spec.feature_id,
            )
            assert candidates, profile
            target = candidates[0]
            schema = schema_generator.generate(
                target,
                seed=804100 + ordinal,
                limits=SchemaLimits(max_tables=2, max_columns=6),
            )
            assert schema.profile is profile
            bundle = setup_builder.build(
                schema,
                seed=804200 + ordinal,
                rows_per_table=8,
            )
            query_targets = sorted(
                (
                    spec
                    for spec in catalog
                    if spec.evidence_lock_ready
                    and profile.value in spec.compatible_profiles
                ),
                key=lambda spec: spec.feature_id,
            )
            query_sql = (
                query_generator.generate(
                    schema,
                    target=query_targets[0],
                    seed=804300 + ordinal,
                    lane=QueryLane.VALID,
                    estimated_rows_by_table={
                        table.name: 8 for table in schema.tables
                    },
                ).sql
                if query_targets
                else f"SELECT COUNT(*) FROM `{schema.tables[0].name}` ORDER BY 1"
            )
            database = f"sf_socket_{profile.value}_{time.time_ns():x}"[-64:]
            outcomes: list[tuple[tuple[object, ...], ...]] = []
            for connection in connections:
                cursor = connection.cursor()
                cursor.execute(f"CREATE DATABASE `{database}`")
                cursor.execute(f"USE `{database}`")
                for statement in bundle.statements:
                    cursor.execute(statement)
                cursor.execute(query_sql)
                outcomes.append(tuple(tuple(row) for row in cursor.fetchall()))
                cursor.close()
            assert outcomes[0] == outcomes[1] == outcomes[2]
    finally:
        for connection in connections:
            connection.close()


@pytest.mark.mysql
@pytest.mark.timeout(600)
def test_every_cpu_dense_template_parses_on_three_exact_8041_sockets() -> None:
    sockets = _sockets()
    scale = ScaleKnobs(
        table_rows=1000,
        scan_rows=800,
        join_build_rows=200,
        join_probe_rows=700,
        aggregate_input_rows=600,
        aggregate_groups=40,
        sort_rows=500,
        window_partition_rows=100,
        window_frame_rows=10,
    )
    templates = (
        CpuDenseScanTemplate(1, "scan", scale),
        CpuDenseRangeSortTemplate(2, "range", scale),
        CpuDenseJoinTemplate(3, "join", scale),
        CpuDenseGroupSortTemplate(4, "group", scale),
        CpuDenseWindowTemplate(5, "window", scale),
    )
    connections = [
        mysql.connector.connect(unix_socket=socket, user="root", autocommit=True)
        for socket in sockets
    ]
    try:
        for template in templates:
            database = f"sf_perf_socket_{template.case_id}_{time.time_ns():x}"
            trees: list[str] = []
            for connection in connections:
                cursor = connection.cursor()
                cursor.execute(f"CREATE DATABASE `{database}`")
                cursor.execute(f"USE `{database}`")
                for statement in template.data_manifest(scale).setup_statements:
                    cursor.execute(statement)
                cursor.execute(f"EXPLAIN ANALYZE FORMAT=TREE {template.render(scale)}")
                row = cursor.fetchone()
                assert row is not None and isinstance(row[0], str)
                template.boundary.validate(
                    parse_tree(row[0], completed=True), "socket_smoke"
                )
                trees.append(row[0])
                cursor.close()
            assert len(trees) == 3
    finally:
        for connection in connections:
            connection.close()


@pytest.mark.mysql
@pytest.mark.timeout(1800)
def test_every_evidence_ready_query_variant_executes_on_three_exact_8041_sockets() -> None:
    sockets = _sockets()
    query_generator = QueryGenerator()
    targets = tuple(
        target
        for target in query_generator.feature_catalog()
        if target.evidence_lock_ready
    )
    schema_generator = SchemaGenerator()
    setup_builder = SetupBundleBuilder()
    connections = [
        mysql.connector.connect(unix_socket=socket, user="root", autocommit=True)
        for socket in sockets
    ]
    try:
        covered: set[str] = set()
        for ordinal, target in enumerate(targets):
            seed = 8_041_000 + ordinal
            schema = schema_generator.generate(
                target,
                seed=seed,
                limits=SchemaLimits(max_tables=3, max_columns=7),
            )
            bundle = setup_builder.build(schema, seed=seed + 1, rows_per_table=8)
            generated = query_generator.generate(
                schema,
                target=target,
                seed=seed + 2,
                lane=QueryLane.VALID,
                estimated_rows_by_table={table.name: 8 for table in schema.tables},
            )
            database = f"sf_all_shapes_{ordinal}_{time.time_ns():x}"[-64:]
            outcomes: list[tuple[tuple[object, ...], ...]] = []
            for connection in connections:
                cursor = connection.cursor()
                cursor.execute(f"CREATE DATABASE `{database}`")
                cursor.execute(f"USE `{database}`")
                for statement in bundle.statements:
                    cursor.execute(statement)
                cursor.execute(generated.sql)
                outcomes.append(tuple(tuple(row) for row in cursor.fetchall()))
                cursor.close()
            assert outcomes[0] == outcomes[1] == outcomes[2], target.feature_id
            covered.add(target.feature_id)
        assert covered == {target.feature_id for target in targets}
    finally:
        for connection in connections:
            connection.close()


@pytest.mark.mysql
@pytest.mark.timeout(900)
def test_online_gap_directed_variants_execute_on_three_exact_8041_sockets() -> None:
    sockets = _sockets()
    query_generator = QueryGenerator()
    targets = {
        target.feature_id: target for target in query_generator.feature_catalog()
    }
    requested = (
        ("select_query_specification", "scalar_aggregate"),
        ("join_outer_natural", "left"),
        ("join_outer_natural", "left_subquery"),
        ("join_inner_cross_straight", "inner_cast"),
        ("join_inner_cross_straight", "inner_subquery"),
        ("subquery_result_kinds", "scalar_limit"),
        ("subquery_result_kinds", "table_limit"),
        ("grouping_with_rollup", "scalar_rollup"),
    )
    schema_generator = SchemaGenerator()
    setup_builder = SetupBundleBuilder()
    connections = [
        mysql.connector.connect(unix_socket=socket, user="root", autocommit=True)
        for socket in sockets
    ]
    try:
        executed: set[str] = set()
        for ordinal, (feature_id, directed_variant) in enumerate(requested):
            target = targets[feature_id]
            if not target.evidence_lock_ready:
                continue
            seed = 8_042_000 + ordinal
            schema = schema_generator.generate(
                target,
                seed=seed,
                limits=SchemaLimits(max_tables=3, max_columns=7),
            )
            bundle = setup_builder.build(schema, seed=seed + 1, rows_per_table=8)
            generated = query_generator.generate(
                schema,
                target=target,
                seed=seed + 2,
                lane=QueryLane.VALID,
                directed_variant=directed_variant,
                estimated_rows_by_table={table.name: 8 for table in schema.tables},
            )
            database = f"sf_gap_shapes_{ordinal}_{time.time_ns():x}"[-64:]
            outcomes: list[tuple[tuple[object, ...], ...]] = []
            for connection in connections:
                cursor = connection.cursor()
                cursor.execute(f"CREATE DATABASE `{database}`")
                cursor.execute(f"USE `{database}`")
                for statement in bundle.statements:
                    cursor.execute(statement)
                cursor.execute(generated.sql)
                outcomes.append(tuple(tuple(row) for row in cursor.fetchall()))
                cursor.close()
            assert outcomes[0] == outcomes[1] == outcomes[2], directed_variant
            executed.add(directed_variant)
        assert "scalar_aggregate" in executed
        assert "left_subquery" in executed
        assert "inner_subquery" in executed
    finally:
        for connection in connections:
            connection.close()
