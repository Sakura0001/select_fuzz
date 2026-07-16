from __future__ import annotations

import os

import mysql.connector
import pytest

from select_fuzz.generation.catalog import CapabilityStatus, FeatureSpec
from select_fuzz.generation.function_registry import DETERMINISTIC_FUNCTION_SIGNATURES
from select_fuzz.generation.query import QueryGenerator, QueryLane
from select_fuzz.generation.schema import (
    ColumnDef,
    IndexDef,
    IndexPart,
    SchemaManifest,
    SchemaProfile,
    TableDef,
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


def _manifest() -> SchemaManifest:
    return SchemaManifest(
        SchemaProfile.REGULAR_INNODB,
        "function_deterministic_scalar",
        1,
        (
            TableDef(
                "items",
                False,
                (ColumnDef("id", "BIGINT", False),),
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
        "function_deterministic_scalar",
        "functions_operators",
        (8, 0, 0),
        frozenset({SchemaProfile.REGULAR_INNODB.value}),
        frozenset({"function_expression"}),
        frozenset({"deterministic_expression", "read_only_select"}),
        capability_status=CapabilityStatus.GENERATOR_SUPPORTED,
        evidence_lock_ready=True,
    )


@pytest.mark.mysql
@pytest.mark.timeout(600)
def test_every_deterministic_function_and_null_witness_on_exact_8041_triad() -> None:
    generator = QueryGenerator()
    manifest = _manifest()
    target = _target()
    variants = tuple(
        variant
        for signature in DETERMINISTIC_FUNCTION_SIGNATURES
        for variant in (
            signature.signature_id,
            *(
                f"{signature.signature_id}_null_{position}"
                for position in sorted(signature.null_argument_positions)
            ),
        )
    )
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
            cursor.close()

        for ordinal, variant in enumerate(variants):
            generated = generator.generate(
                manifest,
                target=target,
                seed=8_041_000 + ordinal,
                case_ordinal=ordinal,
                lane=QueryLane.VALID,
                directed_variant=variant,
                estimated_rows_by_table={"items": 0},
            )
            outcomes: list[tuple[tuple[object, ...], ...]] = []
            for connection in connections:
                cursor = connection.cursor()
                try:
                    cursor.execute(generated.sql)
                    outcomes.append(tuple(tuple(row) for row in cursor.fetchall()))
                except mysql.connector.Error as error:
                    pytest.fail(
                        f"{variant} failed with errno={error.errno} "
                        f"sqlstate={error.sqlstate}: {generated.sql}"
                    )
                finally:
                    cursor.close()
            assert outcomes[0] == outcomes[1] == outcomes[2], variant
    finally:
        for connection in connections:
            connection.close()
