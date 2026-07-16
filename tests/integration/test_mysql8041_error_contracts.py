from __future__ import annotations

import os
import time

import mysql.connector
import pytest

from select_fuzz.generation.catalog import CapabilityStatus, FeatureSpec
from select_fuzz.generation.query import QueryGenerator, QueryLane
from select_fuzz.generation.query_ast import ExpectedErrorKind
from select_fuzz.generation.schema import (
    ColumnDef,
    IndexDef,
    IndexPart,
    SchemaManifest,
    SchemaProfile,
    TableDef,
)
from select_fuzz.generation.setup import SetupBundleBuilder


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


def _manifest() -> SchemaManifest:
    return SchemaManifest(
        SchemaProfile.REGULAR_INNODB,
        "select_query_specification",
        1,
        (
            TableDef(
                "items",
                False,
                (
                    ColumnDef("id", "BIGINT", False),
                    ColumnDef(
                        "payload",
                        "VARCHAR(16)",
                        True,
                        "utf8mb4",
                        "utf8mb4_0900_ai_ci",
                    ),
                ),
                (
                    IndexDef(
                        "PRIMARY",
                        (IndexPart(column_name="id"),),
                        unique=True,
                        primary=True,
                    ),
                ),
            ),
        ),
    )


def _target() -> FeatureSpec:
    return FeatureSpec(
        "select_query_specification",
        "query_expression",
        (8, 0, 0),
        frozenset({SchemaProfile.REGULAR_INNODB.value}),
        frozenset({"query_expression"}),
        frozenset({"read_only_select"}),
        capability_status=CapabilityStatus.GENERATOR_SUPPORTED,
        evidence_lock_ready=True,
    )


@pytest.mark.mysql
@pytest.mark.timeout(120)
def test_every_negative_contract_matches_exact_8041_errno_and_sqlstate() -> None:
    generator = QueryGenerator()
    manifest = _manifest()
    generated_by_kind = {}
    for seed in range(100):
        generated = generator.generate(
            manifest,
            target=_target(),
            seed=seed,
            case_ordinal=seed,
            lane=QueryLane.NEGATIVE,
            estimated_rows_by_table={"items": 1},
        )
        assert generated.expected_error is not None
        generated_by_kind.setdefault(generated.expected_error.kind, generated)
        if set(generated_by_kind) == set(ExpectedErrorKind):
            break
    assert set(generated_by_kind) == set(ExpectedErrorKind)

    setup = SetupBundleBuilder().build(manifest, seed=1, rows_per_table=1)
    database = f"sf_error_contract_{time.time_ns():x}"[-64:]
    connections = [
        mysql.connector.connect(unix_socket=socket, user="root", autocommit=True)
        for socket in _sockets()
    ]
    try:
        for connection in connections:
            cursor = connection.cursor()
            cursor.execute("SELECT VERSION()")
            assert cursor.fetchone()[0].startswith("8.0.41")
            cursor.execute(f"CREATE DATABASE `{database}`")
            cursor.execute(f"USE `{database}`")
            for statement in setup.statements:
                cursor.execute(statement)
            cursor.close()

        for kind, generated in generated_by_kind.items():
            expected = generated.expected_error
            assert expected is not None
            observed = []
            for connection in connections:
                cursor = connection.cursor()
                try:
                    cursor.execute(generated.sql)
                    cursor.fetchall()
                except mysql.connector.Error as error:
                    observed.append((error.errno, error.sqlstate))
                else:
                    pytest.fail(f"{kind.value} unexpectedly succeeded: {generated.sql}")
                finally:
                    cursor.close()
            assert observed == [
                (expected.expected_errno, expected.expected_sqlstate)
            ] * 3, kind.value
    finally:
        for connection in connections:
            cursor = connection.cursor()
            cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
            cursor.close()
            connection.close()
