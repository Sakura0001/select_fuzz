from __future__ import annotations

import os
import time

import pytest

from select_fuzz.config import NodeConfig, NodeRole
from select_fuzz.execution.mysql import MySQLConnectorFactory, NodeQueryRunner
from select_fuzz.execution.setup import MySQLSetupRunner
from select_fuzz.execution.triad import PrepareStatus, QueryLimits, TriadCoordinator
from select_fuzz.generation.data import DataGenerator
from select_fuzz.generation.schema import (
    ColumnDef,
    IndexDef,
    IndexPart,
    SchemaManifest,
    SchemaProfile,
    TableDef,
)
from select_fuzz.generation.setup import SetupBundleBuilder


def _integration_nodes() -> tuple[NodeConfig, ...]:
    nodes: list[NodeConfig] = []
    missing: list[str] = []
    for role in NodeRole:
        prefix = f"SELECT_FUZZ_{role.value.upper()}"
        host = os.environ.get(f"{prefix}_HOST")
        port = os.environ.get(f"{prefix}_PORT")
        if host is None or port is None:
            missing.extend((f"{prefix}_HOST", f"{prefix}_PORT"))
            continue
        nodes.append(NodeConfig(role=role, host=host, port=int(port)))
    if missing:
        pytest.skip("three-node integration endpoints are unset: " + ", ".join(missing))
    return tuple(nodes)


@pytest.mark.mysql
def test_identical_setup_and_query_run_on_three_opt_in_mysql_nodes() -> None:
    if os.environ.get("SELECT_FUZZ_MYSQL_INTEGRATION") != "1":
        pytest.skip(
            "set SELECT_FUZZ_MYSQL_INTEGRATION=1 plus three role endpoints and "
            "environment-only credentials"
        )
    if not os.environ.get("SELECT_FUZZ_MYSQL_USER") or os.environ.get(
        "SELECT_FUZZ_MYSQL_PASSWORD"
    ) is None:
        pytest.skip("environment-only MySQL credentials are not configured")
    nodes = _integration_nodes()
    table = TableDef(
        "t0",
        False,
        (
            ColumnDef("id", "BIGINT UNSIGNED", False),
            ColumnDef(
                "payload",
                "VARCHAR(80)",
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
    )
    schema = SchemaManifest(
        SchemaProfile.REGULAR_INNODB,
        "triad_setup_smoke",
        20260713,
        (table,),
    )
    bundle = SetupBundleBuilder(DataGenerator()).build(
        schema,
        seed=20260713,
        rows_per_table=20,
    )
    factory = MySQLConnectorFactory()
    coordinator = TriadCoordinator(
        nodes,
        setup_runner=MySQLSetupRunner(factory),
        query_runner=NodeQueryRunner(factory),
        session_factory=factory,
    )
    database = f"sf_it_{time.time_ns():x}"

    prepared = coordinator.prepare(bundle, database=database)
    assert prepared.status is PrepareStatus.READY
    results = coordinator.execute(
        prepared,
        "SELECT COUNT(*) FROM `t0` ORDER BY 1",
        QueryLimits(timeout_seconds=15, row_limit=100, byte_limit=1 << 20),
    )

    assert {result.role for result in results} == set(NodeRole)
    assert all(result.rows == ((20,),) for result in results)
    prepared.close()
    # Intentionally retained: the product keeps every round database for replay.
