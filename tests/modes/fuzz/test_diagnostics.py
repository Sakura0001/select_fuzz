from __future__ import annotations

from contextlib import contextmanager
from typing import cast

from select_fuzz.config import NodeConfig, NodeRole
from select_fuzz.modes.fuzz.diagnostics import (
    FuzzProcesslistCollector,
    FuzzProgressReporter,
    FuzzRuntimeDiagnostics,
)


class _Cursor:
    columns = ()
    affected_rows = None

    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def fetchmany(self, size: int) -> tuple[tuple[object, ...], ...]:
        del size
        rows, self._rows = self._rows, []
        return tuple(rows)

    def warnings(self) -> tuple[str, ...]:
        return ()

    def close(self) -> None:
        return None


class _Session:
    def __init__(self, factory: _Factory, endpoint: str) -> None:
        self._factory = factory
        self._endpoint = endpoint

    def connection_id(self) -> int:
        return 999

    def is_alive(self) -> bool:
        return True

    def execute(self, sql: str) -> _Cursor:
        self._factory.sql.append((self._endpoint, sql))
        error = self._factory.errors.get(self._endpoint)
        if error is not None:
            raise error
        return _Cursor(list(self._factory.rows.get(self._endpoint, ())))

    def abort(self) -> None:
        return None

    def close(self) -> None:
        return None


class _Factory:
    def __init__(self) -> None:
        self.rows: dict[str, tuple[tuple[object, ...], ...]] = {}
        self.errors: dict[str, Exception] = {}
        self.sql: list[tuple[str, str]] = []

    @contextmanager
    def control_session(self, node, database):  # type: ignore[no-untyped-def]
        del database
        yield _Session(self, node.host)


class _DeadlineFactory(_Factory):
    def __init__(self) -> None:
        super().__init__()
        self.deadlines: list[float] = []

    @contextmanager
    def control_session_until(  # type: ignore[no-untyped-def]
        self,
        node,
        database,
        deadline_monotonic,
    ):
        self.deadlines.append(deadline_monotonic)
        with self.control_session(node, database) as session:
            yield session


def test_runtime_diagnostics_bounds_issues_and_protects_new_connection() -> None:
    current = [100]
    tracker = FuzzRuntimeDiagnostics(clock_ns=lambda: current[0])
    tracker.set_phase("materializing", generation=0, refresh_deadline_ns=1_000)
    tracker.register_connection(
        worker="db0:reader-replica:0",
        endpoint="replica",
        worker_kind="reader",
        database="sf_f_case",
        connection_id=42,
    )
    tracker.register_connection(
        worker="db0:reader-replica:0",
        endpoint="replica",
        worker_kind="reader",
        database="sf_f_case",
        connection_id=43,
    )
    tracker.unregister_connection("db0:reader-replica:0", 42)
    for ordinal in range(5):
        tracker.record_issue(
            worker=f"reader-{ordinal}",
            endpoint="replica",
            error=f"error-{ordinal}",
            sql="S" * 400,
        )
    current[0] = 250

    snapshot = tracker.snapshot()

    assert snapshot["phase"] == "materializing"
    assert snapshot["generation"] == 0
    assert snapshot["generation_elapsed_ns"] == 150
    assert snapshot["refresh_remaining_ns"] == 750
    assert snapshot["connections"] == 1
    assert snapshot["connection_groups"] == {"replica_reader": 1}
    assert [issue["error"] for issue in snapshot["recent_issues"]] == [
        "error-2",
        "error-3",
        "error-4",
    ]
    assert all(len(issue["sql"]) == 300 for issue in snapshot["recent_issues"])
    assert [connection.connection_id for connection in tracker.connections()] == [43]


def test_processlist_collector_filters_registered_ids_and_summarizes_nodes() -> None:
    current = [100]
    tracker = FuzzRuntimeDiagnostics(clock_ns=lambda: current[0])
    tracker.register_connection(
        worker="db0:writer-primary:0",
        endpoint="primary",
        worker_kind="writer",
        database="sf_f_case",
        connection_id=11,
    )
    tracker.register_connection(
        worker="db0:reader-primary:0",
        endpoint="primary",
        worker_kind="reader",
        database="sf_f_case",
        connection_id=12,
    )
    tracker.register_connection(
        worker="db0:reader-replica:1",
        endpoint="replica",
        worker_kind="reader",
        database="sf_f_case",
        connection_id=21,
    )
    factory = _Factory()
    factory.rows = {
        "primary": (
            (11, "sf_f_case", "Sleep", 18, "", None),
            (12, "sf_f_case", "Query", 7, "executing", "SELECT " + "x" * 400),
        ),
        "replica": (),
    }
    collector = FuzzProcesslistCollector(
        factory,  # type: ignore[arg-type]
        NodeConfig(role=NodeRole.CUSTOM_ON, host="primary"),
        NodeConfig(role=NodeRole.CUSTOM_ON, host="replica", port=3307),
        tracker,
        clock_ns=lambda: current[0],
    )

    snapshot = collector.collect()

    primary = snapshot["endpoints"]["primary"]
    replica = snapshot["endpoints"]["replica"]
    assert primary["registered"] == 2
    assert primary["visible"] == 2
    assert primary["missing"] == 0
    assert primary["commands"] == {"Query": 1, "Sleep": 1}
    assert primary["longest_sleep_seconds"] == 18
    assert primary["longest_query_seconds"] == 7
    assert primary["slowest_connections"][0]["worker"] == "db0:writer-primary:0"
    assert len(primary["slowest_connections"][1]["sql"]) == 300
    assert replica["registered"] == 1
    assert replica["visible"] == 0
    assert replica["missing"] == 1
    assert any("ID IN (11,12)" in sql for endpoint, sql in factory.sql if endpoint == "primary")
    assert any("ID IN (21)" in sql for endpoint, sql in factory.sql if endpoint == "replica")


def test_processlist_collector_degrades_to_diagnostic_error() -> None:
    tracker = FuzzRuntimeDiagnostics()
    tracker.register_connection(
        worker="db0:reader-primary:0",
        endpoint="primary",
        worker_kind="reader",
        database="sf_f_case",
        connection_id=12,
    )
    factory = _Factory()
    factory.errors["primary"] = PermissionError("PROCESS denied")
    collector = FuzzProcesslistCollector(
        factory,  # type: ignore[arg-type]
        NodeConfig(role=NodeRole.CUSTOM_ON, host="primary"),
        NodeConfig(role=NodeRole.CUSTOM_ON, host="replica", port=3307),
        tracker,
    )

    snapshot = collector.collect()

    primary = snapshot["endpoints"]["primary"]
    assert primary["registered"] == 1
    assert primary["visible"] == 0
    assert primary["diagnostics_error_type"] == "PermissionError"
    assert primary["diagnostics_error"] == "PROCESS denied"


def test_processlist_collector_reuses_one_absolute_deadline_for_both_nodes() -> None:
    tracker = FuzzRuntimeDiagnostics()
    for endpoint, connection_id in (("primary", 11), ("replica", 21)):
        tracker.register_connection(
            worker=f"db0:reader-{endpoint}:0",
            endpoint=endpoint,
            worker_kind="reader",
            database="sf_f_case",
            connection_id=connection_id,
        )
    factory = _DeadlineFactory()
    collector = FuzzProcesslistCollector(
        factory,  # type: ignore[arg-type]
        NodeConfig(role=NodeRole.CUSTOM_ON, host="primary"),
        NodeConfig(role=NodeRole.CUSTOM_ON, host="replica", port=3307),
        tracker,
    )

    collector.collect()

    assert len(factory.deadlines) == 2
    assert factory.deadlines[0] == factory.deadlines[1]


def _document(
    *,
    phase: str = "running",
    generation: int = 0,
    reads: int = 0,
    stages: dict[str, int] | None = None,
    processes_total: int = 4,
    processes_alive: int = 4,
    processlist: dict[str, object] | None = None,
    connection_groups: dict[str, int] | None = None,
    errors: int = 2,
    errors_summary: dict[str, object] | None = None,
) -> dict[str, object]:
    stage_counts = stages or {
        "waiting_for_generated_sql": 8,
        "reader_executing": 1,
        "writer_executing": 3,
    }
    stage_details = {
        stage: {
            "count": count,
            "max_age_ns": 20_000_000_000,
            "oldest_workers": (
                {"worker": f"db0:{stage}:0", "age_ns": 20_000_000_000},
            ),
        }
        for stage, count in stage_counts.items()
    }
    return {
        "counters": {
            "databases_ready": 1,
            "generations_ready": 1,
            "reads": reads,
            "writes": 10,
            "errors": errors,
            "timeouts": 1,
            "connection_losses": 0,
            "reconnects": 0,
        },
        "stages": stage_counts,
        "stage_details": stage_details,
        "durations": {},
        "pipeline": {
            "processes_total": processes_total,
            "processes_alive": processes_alive,
            "registered_databases": 1,
            "pending_requests": 9,
            "pending_readers": 8,
            "oldest_pending_ns": 20_000_000_000,
            "max_pending_per_reader": 3,
        },
        "runtime": {
            "phase": phase,
            "generation": generation,
            "elapsed_ns": 15_000_000_000,
            "generation_elapsed_ns": 15_000_000_000,
            "refresh_remaining_ns": 100_000_000_000,
            "connections": 12,
            "connection_groups": connection_groups
            or {
                "primary_writer": 3,
                "primary_reader": 3,
                "replica_reader": 6,
            },
            "recent_issues": (
                {
                    "worker": "reader-1",
                    "endpoint": "replica",
                    "error": "query_timeout",
                    "sql": "SELECT 1",
                },
            ),
        },
        "processlist": processlist
        or {
            "sampled_at_ns": 15_000_000_000,
            "endpoints": {
                "primary": {
                    "registered": 6,
                    "visible": 6,
                    "missing": 0,
                    "commands": {"Sleep": 5, "Query": 1},
                    "longest_sleep_seconds": 4,
                    "longest_query_seconds": 1,
                    "slowest_connections": (),
                },
                "replica": {
                    "registered": 6,
                    "visible": 6,
                    "missing": 0,
                    "commands": {"Sleep": 6},
                    "longest_sleep_seconds": 8,
                    "longest_query_seconds": 0,
                    "slowest_connections": (),
                },
            },
        },
        "errors_summary": errors_summary
        or {
            "total_count": errors,
            "interval_count": 0,
            "rate_per_second": 0.0,
            "fingerprint_count": 0,
            "other_count": 0,
            "other_interval_count": 0,
            "top": (),
        },
    }


def test_reporter_renders_compatibility_backoff_with_english_json_metric_keys() -> None:
    reporter = FuzzProgressReporter(
        diagnostics_interval_seconds=5,
        expected_connection_groups={
            "primary_writer": 3,
            "primary_reader": 3,
            "replica_reader": 6,
        },
        clock_ns=lambda: 20_000_000_000,
    )
    document = _document(stages={"compatibility_error_backoff": 12})
    document["durations"] = {
        "compatibility_error_backoff_ns": {
            "count": 3,
            "total_ns": 60_000_000,
            "max_ns": 40_000_000,
        }
    }

    line = reporter.render(document)[0]

    assert "兼容错误退避:12" in line
    assert "兼容退避:均20.0ms/最大40.0ms" in line
    assert "判断=SQL兼容错误连续发生，读线程正在受控退避" in line
    assert "compatibility_error_backoff" in document["stages"]


def test_reporter_identifies_sql_generation_stall_and_throttles_warning() -> None:
    current = [0]
    reporter = FuzzProgressReporter(
        diagnostics_interval_seconds=5,
        expected_connection_groups={
            "primary_writer": 3,
            "primary_reader": 3,
            "replica_reader": 6,
        },
        clock_ns=lambda: current[0],
    )
    document = _document()
    processlist = cast(dict[str, object], document["processlist"])
    endpoints = cast(dict[str, object], processlist["endpoints"])
    replica = cast(dict[str, object], endpoints["replica"])
    replica["slowest_connections"] = (
        {
            "worker": "db0:reader-replica:1",
            "command": "Sleep",
            "time_seconds": 23,
            "state": "waiting for client",
            "sql": "SELECT * FROM fuzz_t0",
        },
    )
    reporter.render(document)
    current[0] = 15_000_000_000

    lines = reporter.render(document)

    assert "判断=SQL生成速度不足" in lines[0]
    assert "SQL生成器=4/4" in lines[0]
    assert "主节点={连接:6,登记:6,缺失:0,Sleep:5,Query:1" in lines[0]
    assert len(lines) == 2
    assert lines[1].startswith("[fuzz警告]")
    assert "初步原因=SQL生成速度不足" in lines[1]
    assert "query_timeout" in lines[1]
    assert "SELECT 1" in lines[1]
    assert "MySQL最慢=db0:reader-replica:1@replica:Sleep 23s" in lines[1]
    assert "SQL=SELECT * FROM fuzz_t0" in lines[1]

    current[0] = 20_000_000_000
    assert len(reporter.render(_document())) == 1
    current[0] = 46_000_000_000
    assert len(reporter.render(_document())) == 2


def test_reporter_prioritizes_phase_dead_process_and_mysql_mismatch() -> None:
    current = [0]
    reporter = FuzzProgressReporter(
        diagnostics_interval_seconds=5,
        expected_connection_groups={
            "primary_writer": 1,
            "primary_reader": 1,
            "replica_reader": 1,
        },
        clock_ns=lambda: current[0],
    )
    materializing = reporter.render(_document(phase="materializing"))[0]
    assert "判断=正在并发创建新批次" in materializing

    dead = reporter.render(
        _document(
            stages={
                "waiting_for_generated_sql": 1,
                "reader_executing": 1,
                "writer_executing": 1,
            },
            processes_total=4,
            processes_alive=3,
        )
    )[0]
    assert "判断=SQL生成进程异常退出" in dead

    current[0] = 15_000_000_000
    reporter.render(
        _document(
            stages={"reader_executing": 2, "writer_executing": 1},
            connection_groups={
                "primary_writer": 1,
                "primary_reader": 1,
                "replica_reader": 1,
            },
            processlist={
                "sampled_at_ns": current[0],
                "endpoints": {
                    "primary": {
                        "registered": 2,
                        "visible": 2,
                        "missing": 0,
                        "commands": {"Sleep": 2},
                        "longest_sleep_seconds": 20,
                        "longest_query_seconds": 0,
                        "slowest_connections": (),
                    },
                    "replica": {
                        "registered": 1,
                        "visible": 1,
                        "missing": 0,
                        "commands": {"Sleep": 1},
                        "longest_sleep_seconds": 20,
                        "longest_query_seconds": 0,
                        "slowest_connections": (),
                    },
                },
            },
        )
    )
    current[0] = 30_000_000_000
    mismatch = reporter.render(
        _document(
            stages={"reader_executing": 2, "writer_executing": 1},
            connection_groups={
                "primary_writer": 1,
                "primary_reader": 1,
                "replica_reader": 1,
            },
            processlist={
                "sampled_at_ns": current[0],
                "endpoints": {
                    "primary": {
                        "registered": 2,
                        "visible": 2,
                        "missing": 0,
                        "commands": {"Sleep": 2},
                        "longest_sleep_seconds": 20,
                        "longest_query_seconds": 0,
                        "slowest_connections": (),
                    },
                    "replica": {
                        "registered": 1,
                        "visible": 1,
                        "missing": 0,
                        "commands": {"Sleep": 1},
                        "longest_sleep_seconds": 20,
                        "longest_query_seconds": 0,
                        "slowest_connections": (),
                    },
                },
            },
        )
    )[0]
    assert "判断=程序与MySQL状态矛盾" in mismatch


def test_reporter_resets_no_read_timer_when_running_phase_begins() -> None:
    current = [0]
    reporter = FuzzProgressReporter(
        diagnostics_interval_seconds=5,
        expected_connection_groups={
            "primary_writer": 3,
            "primary_reader": 3,
            "replica_reader": 6,
        },
        clock_ns=lambda: current[0],
    )
    reporter.render(_document(phase="materializing"))
    current[0] = 60_000_000_000

    lines = reporter.render(_document(phase="running"))

    assert len(lines) == 1
    assert "无读取=0.0s" in lines[0]
    assert "判断=负载正常推进" in lines[0]


def test_reporter_resets_no_read_timer_when_generation_changes_between_ticks() -> None:
    current = [0]
    reporter = FuzzProgressReporter(
        diagnostics_interval_seconds=60,
        expected_connection_groups={
            "primary_writer": 3,
            "primary_reader": 3,
            "replica_reader": 6,
        },
        clock_ns=lambda: current[0],
    )
    reporter.render(_document(generation=0))
    current[0] = 60_000_000_000

    lines = reporter.render(_document(generation=1))

    assert len(lines) == 1
    assert "无读取=0.0s" in lines[0]
    assert "判断=负载正常推进" in lines[0]


def test_reporter_does_not_claim_mysql_mismatch_when_connections_are_missing() -> None:
    current = [0]
    reporter = FuzzProgressReporter(
        diagnostics_interval_seconds=5,
        expected_connection_groups={
            "primary_writer": 1,
            "primary_reader": 1,
            "replica_reader": 1,
        },
        clock_ns=lambda: current[0],
    )
    processlist = {
        "sampled_at_ns": 0,
        "endpoints": {
            "primary": {
                "registered": 2,
                "visible": 1,
                "missing": 1,
                "commands": {"Sleep": 1},
                "longest_sleep_seconds": 20,
                "longest_query_seconds": 0,
                "slowest_connections": (),
            },
            "replica": {
                "registered": 1,
                "visible": 1,
                "missing": 0,
                "commands": {"Sleep": 1},
                "longest_sleep_seconds": 20,
                "longest_query_seconds": 0,
                "slowest_connections": (),
            },
        },
    }
    reporter.render(
        _document(
            stages={"reader_executing": 2, "writer_executing": 1},
            processlist=processlist,
        )
    )
    current[0] = 15_000_000_000
    processlist["sampled_at_ns"] = current[0]

    line = reporter.render(
        _document(
            stages={"reader_executing": 2, "writer_executing": 1},
            processlist=processlist,
        )
    )[0]

    assert "判断=读查询长时间在MySQL执行" in line
    assert "程序与MySQL状态矛盾" not in line


def test_reporter_prioritizes_generator_wait_over_mysql_mismatch() -> None:
    current = [0]
    reporter = FuzzProgressReporter(
        diagnostics_interval_seconds=5,
        expected_connection_groups={
            "primary_writer": 1,
            "primary_reader": 1,
            "replica_reader": 1,
        },
        clock_ns=lambda: current[0],
    )
    document = _document(
        stages={"waiting_for_generated_sql": 1, "reader_executing": 1, "writer_executing": 1},
        processlist={
            "sampled_at_ns": 0,
            "endpoints": {
                "primary": {
                    "registered": 2,
                    "visible": 2,
                    "missing": 0,
                    "commands": {"Sleep": 2},
                    "longest_sleep_seconds": 20,
                    "longest_query_seconds": 0,
                    "slowest_connections": (),
                },
                "replica": {
                    "registered": 1,
                    "visible": 1,
                    "missing": 0,
                    "commands": {"Sleep": 1},
                    "longest_sleep_seconds": 20,
                    "longest_query_seconds": 0,
                    "slowest_connections": (),
                },
            },
        },
    )
    reporter.render(document)
    current[0] = 15_000_000_000
    cast(dict[str, object], document["processlist"])["sampled_at_ns"] = current[0]

    line = reporter.render(document)[0]

    assert "判断=SQL生成速度不足" in line


def test_reporter_identifies_client_error_storm_before_mysql_mismatch() -> None:
    current = [0]
    reporter = FuzzProgressReporter(
        diagnostics_interval_seconds=5,
        expected_connection_groups={
            "primary_writer": 1,
            "primary_reader": 1,
            "replica_reader": 1,
        },
        clock_ns=lambda: current[0],
    )
    processlist = {
        "sampled_at_ns": 0,
        "endpoints": {
            "primary": {
                "registered": 2,
                "visible": 2,
                "missing": 0,
                "commands": {"Sleep": 2},
                "longest_sleep_seconds": 120,
                "longest_query_seconds": 0,
                "slowest_connections": (),
            },
            "replica": {
                "registered": 1,
                "visible": 1,
                "missing": 0,
                "commands": {"Sleep": 1},
                "longest_sleep_seconds": 120,
                "longest_query_seconds": 0,
                "slowest_connections": (),
            },
        },
    }
    base = _document(
        errors=0,
        stages={"reader_executing": 2, "writer_executing": 1},
        connection_groups={
            "primary_writer": 1,
            "primary_reader": 1,
            "replica_reader": 1,
        },
        processlist=processlist,
    )
    reporter.render(base)
    current[0] = 15_000_000_000
    processlist["sampled_at_ns"] = current[0]
    storm = _document(
        errors=7500,
        stages={"reader_executing": 2, "writer_executing": 1},
        connection_groups={
            "primary_writer": 1,
            "primary_reader": 1,
            "replica_reader": 1,
        },
        processlist=processlist,
        errors_summary={
            "total_count": 7500,
            "interval_count": 2500,
            "rate_per_second": 500.0,
            "fingerprint_count": 1,
            "other_count": 0,
            "other_interval_count": 0,
            "top": (
                {
                    "fingerprint": "8f32a6d417cb",
                    "total_count": 7500,
                    "interval_count": 2500,
                    "rate_per_second": 500.0,
                    "worker_count": 72,
                    "database_count": 6,
                    "endpoints": ("primary", "replica"),
                    "failure_stage": "execute",
                    "error_type": "InternalError",
                    "message": "Unread result found",
                    "connection_id": 71,
                    "mysql_visibility": {
                        "visible": True,
                        "reason": "periodic_processlist_sample",
                    },
                    "watchdog": {
                        "timed_out": True,
                        "kill_query_succeeded": True,
                        "abort_attempted": True,
                        "abort_succeeded": True,
                    },
                    "sample_sql": "SELECT 1",
                },
            ),
        },
    )

    lines = reporter.render(storm)

    assert "判断=客户端错误风暴；查询未发送到 MySQL，客户端快速失败" in lines[0]
    assert "错误风暴=8f32a6d417cb:500.0/s" in lines[0]
    assert len(lines) == 2
    assert "指纹=8f32a6d417cb" in lines[1]
    assert "异常=InternalError:Unread result found" in lines[1]
    assert "阶段=execute" in lines[1]
    assert "watchdog={超时:是,KILL:成功,abort:成功}" in lines[1]
    assert "影响=72线程/6数据库/primary,replica" in lines[1]
    assert "SQL=SELECT 1" in lines[1]

    current[0] = 30_000_000_000
    processlist["sampled_at_ns"] = current[0]
    processlist["endpoints"]["replica"]["missing"] = 1
    incomplete_lines = reporter.render(storm)
    assert "判断=客户端错误风暴" in incomplete_lines[0]
    assert "查询未发送到 MySQL" not in incomplete_lines[0]


def test_reporter_rejects_stale_mysql_sample_and_new_stage_as_long_running() -> None:
    current = [0]
    reporter = FuzzProgressReporter(
        diagnostics_interval_seconds=5,
        expected_connection_groups={
            "primary_writer": 1,
            "primary_reader": 1,
            "replica_reader": 1,
        },
        clock_ns=lambda: current[0],
    )
    reporter.render(
        _document(
            stages={"reader_executing": 2, "writer_executing": 1},
        )
    )
    current[0] = 20_000_000_000
    document = _document(
        stages={"reader_executing": 2, "writer_executing": 1},
        processlist={
            "sampled_at_ns": 0,
            "endpoints": {
                "primary": {
                    "registered": 2,
                    "visible": 2,
                    "missing": 0,
                    "commands": {"Sleep": 2},
                    "longest_sleep_seconds": 20,
                    "longest_query_seconds": 0,
                    "slowest_connections": (),
                },
                "replica": {
                    "registered": 1,
                    "visible": 1,
                    "missing": 0,
                    "commands": {"Sleep": 1},
                    "longest_sleep_seconds": 20,
                    "longest_query_seconds": 0,
                    "slowest_connections": (),
                },
            },
        },
    )
    document["stage_details"] = {
        "reader_executing": {
            "count": 2,
            "max_age_ns": 1_000_000_000,
            "oldest_workers": (),
        },
        "writer_executing": {
            "count": 1,
            "max_age_ns": 20_000_000_000,
            "oldest_workers": (),
        },
    }
    document["runtime"]["connection_groups"] = {  # type: ignore[index]
        "primary_writer": 1,
        "primary_reader": 1,
        "replica_reader": 1,
    }

    status = reporter.render(document)[0]

    assert "采样龄=20.0s" in status
    assert "判断=程序与MySQL状态矛盾" not in status
    assert "判断=读查询长时间在MySQL执行" not in status
    assert "判断=读取无进展但现有证据不足" in status
