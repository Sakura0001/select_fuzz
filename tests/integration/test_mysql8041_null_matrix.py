from __future__ import annotations

from collections.abc import Sequence
import os
import time
from typing import Any

import mysql.connector
import pytest

from select_fuzz.generation.catalog import CapabilityStatus, FeatureSpec
from select_fuzz.generation.query import QueryGenerator, QueryLane
from select_fuzz.generation.schema import (
    ColumnDef,
    IndexDef,
    IndexPart,
    SchemaManifest,
    SchemaProfile,
    TableDef,
)


_SCALAR_EXPECTATIONS: dict[str, tuple[object, ...]] = {
    "comparison_null_left": (None, None, None, None, None, None, 0),
    "comparison_null_right": (None, None, None, None, None, None, 0),
    "comparison_null_both": (None, None, None, None, None, None, 1),
    "arithmetic_null_left": (None,) * 6,
    "arithmetic_null_right": (None,) * 6,
    "arithmetic_null_both": (None,) * 6,
    "bitwise_null_left": (None,) * 5,
    "bitwise_null_right": (None,) * 5,
    "bitwise_null_both": (None,) * 5,
    "logical_null_left": (0, None, None, 1, None),
    "logical_null_right": (0, None, None, 1, None),
    "logical_null_both": (None, None, None),
    "like_regexp_null_left": (None, None),
    "like_regexp_null_right": (None, None),
    "like_regexp_null_both": (None, None),
    "between_null_value": (None, None),
    "between_null_lower": (None, None),
    "between_null_upper": (None, None),
    "between_null_bounds": (None, None),
    "between_null_all": (None, None),
    "in_null_left": (None, None),
    "in_null_right": (1, None, 0, None),
    "in_null_both": (None, None),
}

_JOIN_VARIANTS = (
    "nullable_key_left",
    "nullable_key_right",
    "nullable_key_both",
)


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


def _target(feature_id: str) -> FeatureSpec:
    return FeatureSpec(
        feature_id,
        "query",
        (8, 0, 41),
        frozenset({SchemaProfile.REGULAR_INNODB.value}),
        frozenset({"query_expression"}),
        frozenset({"read_only_select"}),
        capability_status=CapabilityStatus.GENERATOR_SUPPORTED,
        evidence_lock_ready=True,
    )


def _manifest() -> SchemaManifest:
    return SchemaManifest(
        SchemaProfile.REGULAR_INNODB,
        "select_query_specification",
        8_041,
        tuple(
            TableDef(
                f"t{ordinal}",
                False,
                (
                    ColumnDef("id", "BIGINT", False),
                    ColumnDef("amount", "INT", True),
                ),
                (
                    IndexDef(
                        "PRIMARY",
                        (IndexPart(column_name="id"),),
                        unique=True,
                        primary=True,
                    ),
                ),
            )
            for ordinal in range(2)
        ),
    )


def _execute_on_triad(
    connections: Sequence[Any],
    *,
    sql: str,
    label: str,
) -> tuple[tuple[tuple[object, ...], ...], ...]:
    outcomes: list[tuple[tuple[object, ...], ...]] = []
    for node_index, connection in enumerate(connections):
        cursor = connection.cursor()
        try:
            cursor.execute(sql)
            outcomes.append(tuple(tuple(row) for row in cursor.fetchall()))
        except mysql.connector.Error as error:
            pytest.fail(
                f"{label} failed on node={node_index} errno={error.errno} "
                f"sqlstate={error.sqlstate}: {sql}"
            )
        finally:
            cursor.close()
    return tuple(outcomes)


@pytest.mark.mysql
@pytest.mark.timeout(600)
def test_null_matrix_has_exact_three_valued_results_on_8041_triad() -> None:
    manifest = _manifest()
    generator = QueryGenerator()
    database = f"sf_null_matrix_{time.time_ns():x}"[-64:]
    connections = [
        mysql.connector.connect(unix_socket=socket, user="root", autocommit=True)
        for socket in _sockets()
    ]
    try:
        for connection in connections:
            cursor = connection.cursor()
            cursor.execute("SELECT VERSION()")
            assert cursor.fetchone()[0].startswith("8.0.41")
            cursor.execute("SET SESSION time_zone = '+00:00'")
            cursor.execute(f"CREATE DATABASE `{database}`")
            cursor.execute(f"USE `{database}`")
            for table in manifest.tables:
                cursor.execute(table.render())
            for table in manifest.tables:
                cursor.execute(
                    f"INSERT INTO `{table.name}` (`id`, `amount`) "
                    "VALUES (1, 1), (2, NULL), (3, 3)"
                )
            cursor.close()

        # The scalar recipes are tableless, so each one must return exactly the
        # declared truth-table row rather than merely agree across the nodes.
        for ordinal, (variant, expected) in enumerate(_SCALAR_EXPECTATIONS.items()):
            generated = generator.generate(
                manifest,
                target=_target("function_deterministic_scalar"),
                seed=8_041_000 + ordinal,
                case_ordinal=ordinal,
                lane=QueryLane.VALID,
                directed_variant=variant,
                estimated_rows_by_table={"t0": 3, "t1": 3},
            )
            outcomes = _execute_on_triad(
                connections,
                sql=generated.sql,
                label=variant,
            )
            assert outcomes == ((expected,),) * 3, (variant, generated.sql)

        # Each ordinary '=' JOIN has two true matches.  The NULL/NULL rows do
        # not match because an ON condition keeps TRUE and rejects UNKNOWN.
        expected_join_rows = ((1, 1), (3, 3))
        for ordinal, variant in enumerate(_JOIN_VARIANTS):
            generated = generator.generate(
                manifest,
                target=_target("join_inner_cross_straight"),
                seed=8_042_000 + ordinal,
                case_ordinal=ordinal,
                lane=QueryLane.VALID,
                directed_variant=variant,
                estimated_rows_by_table={"t0": 3, "t1": 3},
            )
            outcomes = _execute_on_triad(
                connections,
                sql=generated.sql,
                label=variant,
            )
            assert outcomes == ((expected_join_rows),) * 3, (variant, generated.sql)

        # WHERE amount IS NULL guarantees an all-NULL aggregate input, while
        # COUNT(*) proves that it is not accidentally the empty-input case.
        aggregate = generator.generate(
            manifest,
            target=_target("function_aggregate"),
            seed=8_043_000,
            lane=QueryLane.VALID,
            directed_variant="aggregate_all_null",
            estimated_rows_by_table={"t0": 3, "t1": 3},
        )
        aggregate_outcomes = _execute_on_triad(
            connections,
            sql=aggregate.sql,
            label="aggregate_all_null",
        )
        expected_aggregate = (1, None, None, None, None, 0, 0)
        assert aggregate_outcomes == ((expected_aggregate,),) * 3, aggregate.sql
    finally:
        for connection in connections:
            cursor = connection.cursor()
            try:
                cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
            finally:
                cursor.close()
                connection.close()
