"""Opt-in regressions against scripts/local_mysql_lab.py's isolated instances."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
from threading import Timer
import time

import mysql.connector
import pytest

from select_fuzz.config import NodeConfig, NodeRole
from select_fuzz.execution.mysql import MySQLConnectorFactory
from select_fuzz.modes.fuzz.dml import FuzzDmlGenerator, FuzzTable
from select_fuzz.modes.fuzz.execution import StreamingQueryExecutor


pytestmark = [
    pytest.mark.mysql,
    pytest.mark.skipif(os.environ.get("SELECT_FUZZ_LOCAL_MYSQL_TESTS") != "1",
                       reason="explicit opt-in for the isolated local MySQL lab"),
]


class _PrivateEnvironment(dict[str, str]):
    def __repr__(self) -> str:
        return "<local lab credentials>"


@pytest.fixture
def lab_database():  # type: ignore[no-untyped-def]
    state_path = Path(__file__).resolve().parents[2] / ".local/mysql-validation/private-state.json"
    state = json.loads(state_path.read_text())
    node = NodeConfig(role=NodeRole.BASELINE, host="127.0.0.1",
                      port=state["instances"][0]["port"])
    environment = _PrivateEnvironment(SELECT_FUZZ_MYSQL_USER="sf_lab",
                                      SELECT_FUZZ_MYSQL_PASSWORD=state["password"])
    connection = mysql.connector.connect(host=node.host, port=node.port, user="sf_lab",
                                         password=state["password"], autocommit=True,
                                         use_pure=True, connection_timeout=3)
    database = "sf_local_regression_" + secrets.token_hex(6)
    cursor = connection.cursor()
    cursor.execute(f"CREATE DATABASE `{database}`")
    cursor.execute(f"USE `{database}`")
    try:
        yield node, environment, database, cursor
    finally:
        cursor.execute(f"DROP DATABASE `{database}`")
        cursor.close()
        connection.close()


@pytest.mark.parametrize("use_pure", [False, True], ids=["c", "python"])
def test_incomplete_results_cannot_outlive_deadline_during_close(lab_database, use_pure):  # type: ignore[no-untyped-def]
    node, environment, database, cursor = lab_database
    cursor.execute("CREATE TABLE numbers (n INT PRIMARY KEY)")
    cursor.executemany("INSERT INTO numbers VALUES (%s)", [(i,) for i in range(512)])
    factory = MySQLConnectorFactory(environ=environment, use_pure=use_pure,
                                    control_use_pure=True)
    executor = StreamingQueryExecutor(factory)
    emergency_fired = []
    with factory.query_session(node, database) as session:
        connection_id = session.connection_id()

        def emergency_stop():  # type: ignore[no-untyped-def]
            emergency_fired.append(True)
            with factory.control_session(node, database) as control:
                kill = control.execute(f"KILL CONNECTION {connection_id}")
                kill.close()

        # This outer fuse also bounds the test when run against the broken implementation.
        emergency = Timer(3.0, emergency_stop)
        emergency.start()
        started = time.monotonic()
        try:
            result = executor.execute_session(
                session, "SELECT a.n,b.n,c.n FROM numbers a CROSS JOIN numbers b "
                         "CROSS JOIN numbers c",
                node=node, database=database, timeout_seconds=0.15, started_ns=0,
            )
        finally:
            elapsed = time.monotonic() - started
            emergency.cancel()
            emergency.join()
    assert not emergency_fired
    assert elapsed < 2.0
    assert result.timed_out and not result.success
    assert executor.active_queries == 0


def test_generated_insert_accepts_maximum_width_payload(lab_database):  # type: ignore[no-untyped-def]
    _node, _environment, _database, cursor = lab_database
    cursor.execute(
        "CREATE TABLE payload_probe (id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY, "
        "tenant_id BIGINT UNSIGNED, amount BIGINT, status INT, updated_at DATETIME(6), "
        "payload VARCHAR(255)) ENGINE=InnoDB"
    )
    cursor.execute("INSERT INTO payload_probe (tenant_id,amount,status,updated_at,payload) "
                   "VALUES (1,1,1,'2020-01-01',REPEAT('x',255))")
    generator = FuzzDmlGenerator(
        (FuzzTable("payload_probe", "id", ("amount", "status", "payload")),),
        batch_rows_min=1, batch_rows_max=1, delete_batch_rows_min=1, delete_batch_rows_max=1,
    )
    statement = generator.generate_insert(seed=1234, known_high_watermark=1)
    cursor.execute(statement.sql)
    assert cursor.rowcount == 1
    cursor.execute("SHOW WARNINGS")
    assert cursor.fetchall() == []
    cursor.execute("SELECT CHAR_LENGTH(payload), RIGHT(payload,6) FROM payload_probe WHERE id=2")
    assert cursor.fetchone() == (255, "-i1234")
