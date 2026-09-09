import pytest

from select_fuzz.pq.config import Endpoint, PQConfig, Tolerance
from select_fuzz.pq.connection import PQConnection, QueryResult
from select_fuzz.pq.detector import detect_pq
from select_fuzz.pq.materializer import _table_ddl, materialize
from select_fuzz.pq.runner import DifferentialRunner


def result(rows=((1,),), columns=("x",), types=(3,), **kwargs):
    return QueryResult("OK", tuple(rows), columns, types, **kwargs)


@pytest.mark.parametrize("plan", [
    "-> Parallel table scan on t0",
    "-> Parallel index scan on a using k",
    "-> Parallel index range scan on a using k",
    "-> Parallel index lookup on a using k",
    "Gather: 4 workers, parallel scan on a",
    "1 SIMPLE <gather1> ALL Parallel execute (4 workers, db.t0)",
])
def test_documented_positive_markers(plan):
    assert detect_pq(plan)


@pytest.mark.parametrize("plan", [
    "EXPLAIN-ERR: near 'Parallel execute (4 workers)'",
    "force parallel is ON; optimizer chose serial",
    "Gather: 0 workers",
    "Parallel execute (0 workers, t0)",
    "Using where; Using filesort",
])
def test_options_errors_and_zero_workers_are_not_pq_evidence(plan):
    assert not detect_pq(plan)


@pytest.mark.parametrize("updates", [
    {"dop_on": 0}, {"dop_off": 4}, {"queries_per_round": 0},
    {"row_limit": 0}, {"max_regenerations": 0}, {"materialize_tables": 0},
    {"query_timeout_seconds": float("nan")}, {"perf_repeats": 0},
    {"database": "mysql"}, {"database": "pq; DROP DATABASE user_db"},
])
def test_invalid_run_settings_fail_before_connect(updates):
    with pytest.raises(ValueError):
        PQConfig(endpoint=Endpoint("127.0.0.1"), **updates)


def test_tolerance_rejects_negative_and_nonfinite_bounds():
    with pytest.raises(ValueError):
        Tolerance(double_absolute=-1)
    with pytest.raises(ValueError):
        Tolerance(float_relative=float("inf"))


class Wire:
    """Unbuffered cursor boundary: closing with unread rows is an error."""

    def __init__(self, rows, warning_rows=(), fetch_error=None, session_rows=()):
        self.rows = list(rows)
        self.warning_rows = list(warning_rows)
        self.fetch_error = fetch_error
        self.session_rows = list(session_rows)
        self.commands = []
        self.unread = False
        self.closed = False
        self.autocommit = True

    def cursor(self):
        if self.unread:
            raise RuntimeError("Unread result found")
        return Cursor(self)

    def close(self):
        self.closed = True
        self.unread = False

    def rollback(self):
        self.unread = False


class Cursor:
    def __init__(self, wire):
        self.wire = wire
        self.description = None
        self.pending = []

    def execute(self, sql):
        self.wire.commands.append(sql)
        if sql == "SELECT payload FROM t":
            self.pending = list(self.wire.rows)
            self.description = (("payload", 3),)
        elif sql == "SHOW WARNINGS":
            self.pending = list(self.wire.warning_rows)
            self.description = (("Level", 253), ("Code", 3), ("Message", 253))
        elif sql.startswith("SHOW SESSION VARIABLES"):
            self.pending = list(self.wire.session_rows)
            self.description = (("Variable_name", 253), ("Value", 253))
        self.wire.unread = bool(self.pending)

    def fetchmany(self, count):
        if self.wire.fetch_error and self.description == (("payload", 3),):
            raise self.wire.fetch_error
        out, self.pending = self.pending[:count], self.pending[count:]
        self.wire.unread = bool(self.pending)
        return out

    def fetchall(self):
        return self.fetchmany(len(self.pending))

    def close(self):
        if self.wire.unread:
            raise RuntimeError("Unread result found")


def connection(monkeypatch, wire, **kwargs):
    monkeypatch.setattr("mysql.connector.connect", lambda **kw: wire)
    return PQConnection(Endpoint("127.0.0.1"), dop=4, **kwargs)


def test_row_limit_never_returns_complete_prefix_or_breaks_session(monkeypatch):
    wire = Wire([(i,) for i in range(7)])
    conn = connection(monkeypatch, wire)
    r = conn.execute("SELECT payload FROM t", row_limit=3)
    assert not r.complete
    assert r.total_rows == 7
    assert len(r.rows) == 3
    assert conn.execute("SELECT payload FROM t", row_limit=10).complete


def test_exact_limit_is_complete_and_warns_immediately(monkeypatch):
    wire = Wire([(1,), (2,)], [("Warning", 9999, "Fallback to non-PQ execution")])
    conn = connection(monkeypatch, wire)
    r = conn.execute("SELECT payload FROM t", row_limit=2)
    assert r.complete
    assert r.fallback
    assert r.warnings == tuple(wire.warning_rows)
    i = wire.commands.index("SELECT payload FROM t")
    assert wire.commands[i + 1] == "SHOW WARNINGS"


def test_fetch_error_does_not_get_masked_by_cursor_close(monkeypatch):
    wire = Wire([(1,)], fetch_error=TimeoutError("fetch deadline"))
    conn = connection(monkeypatch, wire)
    r = conn.execute("SELECT payload FROM t")
    assert r.status == "ERROR"
    assert "fetch deadline" in r.error


def test_reference_connection_never_sets_vendor_variables(monkeypatch):
    wire = Wire([])
    connection(monkeypatch, wire, reference=True)
    assert all("parallel" not in s.lower() and "pq_" not in s.lower() for s in wire.commands)


class Session:
    def __init__(self, parallel):
        self.parallel = parallel
        self.executed = []
        self.dop = 4 if parallel else 0
        self.closed = False
        self.query_result = result()

    def execute_scalar_text(self, sql):
        return "Gather: 4 workers" if self.parallel else "Table scan on t"

    def execute(self, sql, **kwargs):
        self.executed.append(sql)
        if sql.startswith("EXPLAIN"):
            return result(
                ((1, "SIMPLE", "<gather1>" if self.parallel else "t", 12000,
                  "Parallel execute (4 workers, db.t)" if self.parallel else "Using where"),),
                ("id", "select_type", "table", "rows", "Extra"), (3, 253, 253, 8, 253),
            )
        return self.query_result

    def close(self):
        self.closed = True


@pytest.mark.parametrize("pq_on,seq_on", [(False, False), (True, True)])
def test_differential_gates_both_plans_without_executing_sql(pq_on, seq_on):
    pq, seq = Session(pq_on), Session(seq_on)
    runner = DifferentialRunner(pq, seq, config=PQConfig(Endpoint("127.0.0.1")))
    r = runner.run_differential("SELECT x FROM t", compare_mode="multiset")
    assert r.outcome.verdict == "INCONCLUSIVE"
    assert not r.is_mismatch
    assert "SELECT x FROM t" not in pq.executed + seq.executed


def test_runtime_fallback_excluded_despite_parallel_plan():
    pq, seq = Session(True), Session(False)
    pq.query_result = result(fallback=True)
    runner = DifferentialRunner(pq, seq, config=PQConfig(Endpoint("127.0.0.1")))
    r = runner.run_differential("SELECT x FROM t", compare_mode="multiset")
    assert not r.pq_triggered
    assert r.skip_reason == "runtime_fallback"
    assert not r.is_mismatch


def test_structured_plan_reads_estimates_by_column_not_numeric_regex():
    pq, seq = Session(True), Session(False)
    runner = DifferentialRunner(pq, seq, config=PQConfig(Endpoint("127.0.0.1")))
    info = runner.explain("SELECT x FROM t")
    assert info.dop == 4
    assert info.scan_rows == 12000


def test_nullable_opt_and_materialize_never_drops_existing_database():
    assert "opt INT NULL" in _table_ddl("t0")
    statements = []

    class Setup:
        def run_script(self, stmts):
            statements.extend(stmts)

    materialize(Setup(), database="pq_test", table_count=1, rows_per_table=2, seed=1)
    assert not any("DROP DATABASE" in s.upper() for s in statements)
    assert any("ANALYZE TABLE" in s for s in statements)


def test_open_failure_closes_partial_pair(monkeypatch):
    from select_fuzz.pq.connection import open_pair
    wire = Wire([])
    attempts = []

    def connect(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 2:
            raise RuntimeError("second session unavailable")
        return wire

    monkeypatch.setattr("mysql.connector.connect", connect)
    with pytest.raises(RuntimeError, match="second session"):
        open_pair(PQConfig(Endpoint(), serial_endpoint=Endpoint("127.0.0.2")))
    assert wire.closed


def test_wire_timeouts_remain_client_side_without_parameter_writes(monkeypatch):
    options = {}
    wire = Wire([])

    def connect(**kwargs):
        options.update(kwargs)
        return wire

    monkeypatch.setattr("mysql.connector.connect", connect)
    PQConnection(Endpoint(), dop=4, query_timeout_ms=2500)
    assert options["connection_timeout"] > 0
    assert options["read_timeout"] > 0
    assert options["write_timeout"] > 0
    assert not any(s.lstrip().upper().startswith("SET ") for s in wire.commands)
    assert any("sql_mode" in s and "max_execution_time" in s for s in wire.commands)


def test_full_setup_artifact_replays_identical_seeded_rows(tmp_path):
    from select_fuzz.pq.materializer import write_setup, setup_statements
    config = PQConfig(Endpoint(), database="pq_fixture", materialize_rows=17, materialize_tables=2)
    path = write_setup(config, tmp_path)
    statements = list(setup_statements(config.database, table_count=2, rows_per_table=17, seed=1))
    assert path.read_text().splitlines()[1:] == (";\n".join(statements) + ";").splitlines()
    assert "INSERT INTO" in path.read_text()
    assert "DROP DATABASE" not in path.read_text()
    assert "DROP DATABASE `pq_fixture`" in (tmp_path / "cleanup.sql").read_text()


@pytest.mark.parametrize("reference", [False, True])
def test_open_only_reads_inherited_runtime_parameters(monkeypatch, reference):
    wire = Wire([], session_rows=[("parallel_default_dop", "8"),
                                  ("sql_mode", "STRICT_TRANS_TABLES"),
                                  ("max_execution_time", "0")])
    conn = connection(monkeypatch, wire, reference=reference)
    assert not any(sql.lstrip().upper().startswith("SET ") for sql in wire.commands)
    assert conn.session_values["sql_mode"] == "STRICT_TRANS_TABLES"
    assert conn.session_values["max_execution_time"] == "0"
    if not reference:
        assert conn.snapshot()["dop"] == 8
        assert conn.snapshot()["expected_dop"] == 4


def test_reconnect_reads_changed_settings_without_restoring_parameters(monkeypatch):
    first = Wire([(1,)], fetch_error=TimeoutError("fetch deadline"))
    second = Wire([(2,)], session_rows=[("parallel_default_dop", "8")])
    wires = iter((first, second))
    monkeypatch.setattr("mysql.connector.connect", lambda **kw: next(wires))
    conn = PQConnection(Endpoint(), dop=4)
    conn.use_database("pq_fixture")
    assert conn.execute("SELECT payload FROM t").status == "ERROR"
    assert conn.execute("SELECT payload FROM t").rows == ((2,),)
    assert "USE `pq_fixture`" in second.commands
    assert not any(sql.lstrip().upper().startswith("SET ")
                   for sql in first.commands + second.commands)
    assert conn.session_values["parallel_default_dop"] == "8"


def test_set_dop_rejected_without_sending_sql(monkeypatch):
    wire = Wire([])
    conn = connection(monkeypatch, wire)
    before = list(wire.commands)
    with pytest.raises(ValueError, match="preconfigured"):
        conn.set_dop(8)
    assert wire.commands == before


def test_pair_requires_explicit_serial_endpoint_before_connect(monkeypatch):
    from select_fuzz.pq.connection import open_pair
    calls = []
    monkeypatch.setattr("mysql.connector.connect", lambda **kw: calls.append(kw) or Wire([]))
    with pytest.raises(ValueError, match="serial_endpoint"):
        open_pair(PQConfig(Endpoint()))
    assert not calls


def test_pair_opens_the_explicit_preconfigured_serial_endpoint(monkeypatch):
    from select_fuzz.pq.connection import open_pair
    serial = Endpoint("127.0.0.2", 3307, "serial_reader", "serial-secret")
    config = PQConfig(Endpoint(), serial_endpoint=serial)
    calls = []
    monkeypatch.setattr("mysql.connector.connect", lambda **kw: calls.append(kw) or Wire([]))
    pq, seq = open_pair(config)
    assert [(call["host"], call["port"], call["user"]) for call in calls] == [
        ("127.0.0.1", 3306, "root"), ("127.0.0.2", 3307, "serial_reader")]
    pq.close()
    seq.close()


@pytest.mark.parametrize("dops", [(2, 8), (8,)])
def test_automatic_dop_changes_are_invalid_configuration(dops):
    with pytest.raises(ValueError, match="preconfigured"):
        PQConfig(Endpoint(), dop_on=4, perf_dops=dops)


def test_continuous_small_batches_obey_total_client_deadline(monkeypatch):
    wire = Wire([(i,) for i in range(20)])
    conn = connection(monkeypatch, wire, query_timeout_ms=1000)
    ticks = iter(range(0, 20_000_000_000, 600_000_000))
    monkeypatch.setattr("select_fuzz.pq.connection.time.perf_counter_ns", lambda: next(ticks))
    result = conn.execute("SELECT payload FROM t", row_limit=1)
    assert result.status == "ERROR"
    assert "result transfer exceeded deadline" in result.error
    assert not result.complete
    assert wire.closed
    assert not any(s.lstrip().upper().startswith("SET ") for s in wire.commands)


@pytest.mark.parametrize("late_phase", ["execute_empty", "execute_no_description", "fetch_eof"])
def test_total_deadline_rejects_late_empty_results(monkeypatch, late_phase):
    elapsed = [0]

    class LateCursor(Cursor):
        def execute(self, sql):
            super().execute(sql)
            if sql == "SELECT payload FROM t" and late_phase.startswith("execute"):
                elapsed[0] = 2_100_000_000
                if late_phase == "execute_no_description":
                    self.description = None

        def fetchmany(self, count):
            rows = super().fetchmany(count)
            if self.description == (("payload", 3),) and late_phase == "fetch_eof":
                elapsed[0] = 2_100_000_000
            return rows

    wire = Wire([])
    monkeypatch.setattr(wire, "cursor", lambda: LateCursor(wire))
    conn = connection(monkeypatch, wire, query_timeout_ms=1000)
    monkeypatch.setattr("select_fuzz.pq.connection.time.perf_counter_ns", lambda: elapsed[0])
    result = conn.execute("SELECT payload FROM t")
    assert result.status == "ERROR"
    assert "exceeded deadline" in result.error
    assert not result.complete
    assert wire.closed
