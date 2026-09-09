"""Opt-in MySQL checks; uses a new owned schema and always removes that schema."""

import os
from uuid import uuid4

import pytest

from select_fuzz.pq.config import Endpoint
from select_fuzz.pq.connection import PQConnection
from select_fuzz.pq.materializer import materialize

pytestmark = [pytest.mark.mysql, pytest.mark.skipif(
    not os.environ.get("PQ_TEST_MYSQL_PORT"), reason="set PQ_TEST_MYSQL_PORT for local integration",
)]


@pytest.fixture
def fixture():
    endpoint = Endpoint("127.0.0.1", int(os.environ["PQ_TEST_MYSQL_PORT"]),
                        os.environ.get("PQ_TEST_MYSQL_USER", "root"),
                        os.environ.get("PQ_TEST_MYSQL_PASSWORD", ""))
    conn = PQConnection(endpoint, dop=0, reference=True, query_timeout_ms=5000)
    name = "pq_integration_" + uuid4().hex[:12]
    created = False
    try:
        schema = materialize(conn, database=name, table_count=3, rows_per_table=80, seed=73)
        created = True
        conn.use_database(name)
        yield conn, schema
    finally:
        if created:
            conn.run_script([f"DROP DATABASE `{name}`"])
        conn.close()


def test_mysql_retention_limit_drains_cursor_and_preserves_actual_row_count(fixture):
    conn, _ = fixture
    result = conn.execute("SELECT id, opt FROM t0", row_limit=7)
    assert result.status == "OK"
    assert not result.complete
    assert len(result.rows) == 7
    assert result.row_count == 80
    assert conn.execute("SELECT COUNT(*) FROM t0").rows == ((80,),)


def test_mysql_deterministic_fixture_has_nulls_and_does_not_replace_existing_db(fixture):
    conn, schema = fixture
    count = conn.execute("SELECT COUNT(*) FROM t0 WHERE opt IS NULL")
    assert count.rows[0][0] > 0
    with pytest.raises(Exception, match="exists"):
        materialize(conn, database=schema.database, table_count=1, rows_per_table=2, seed=1)
    assert conn.execute("SELECT COUNT(*) FROM t0").rows == ((80,),)


def test_mysql_warning_capture_and_recovery_after_server_error(fixture):
    conn, _ = fixture
    assert conn.execute("SELECT * FROM missing_table").status == "ERROR"
    assert conn.execute("SELECT COUNT(*) FROM t0").rows == ((80,),)
    warned = conn.execute("SELECT CAST('abc' AS UNSIGNED) AS value")
    assert warned.status == "OK"
    assert warned.warnings_complete and warned.warnings
    assert not warned.fallback
    clean = conn.execute("SELECT 1")
    assert not clean.warnings


def test_mysql_generated_queries_are_valid_under_only_full_group_by(fixture):
    from select_fuzz.pq.generator import PQGenerator
    conn, schema = fixture
    generator = PQGenerator(schema)
    for seed in range(100):
        original = generator.generate(seed=seed)
        for q in (original, generator.generate(seed=seed, shape=original.shape, pq_friendly=True)):
            result = conn.execute(q.sql, row_limit=10000)
            assert result.status == "OK", (seed, q.shape, q.sql, result.error)
