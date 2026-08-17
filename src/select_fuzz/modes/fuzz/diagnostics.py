"""Bounded live diagnostics and Chinese progress output for fuzz mode."""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Lock
import time

from select_fuzz.config import NodeConfig
from select_fuzz.execution.protocols import ConnectionFactory, QuerySession


_DETAIL_LIMIT = 3
_SQL_LIMIT = 300
_NO_READ_WARNING_SECONDS = 15.0
_WARNING_REPEAT_SECONDS = 30.0


def _truncate(value: object, limit: int = _SQL_LIMIT) -> str:
    return str(value)[:limit]


def _parse_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return int(value)
        except ValueError:
            return None
    return None


@dataclass(frozen=True, slots=True)
class FuzzWorkerConnection:
    worker: str
    endpoint: str
    worker_kind: str
    database: str
    connection_id: int
    connected_ns: int


class FuzzRuntimeDiagnostics:
    """Thread-safe bounded lifecycle, connection, and recent-error state."""

    def __init__(self, *, clock_ns: Callable[[], int] = time.monotonic_ns) -> None:
        self._clock_ns = clock_ns
        started_ns = clock_ns()
        self._started_ns = started_ns
        self._generation_started_ns = started_ns
        self._phase = "starting"
        self._generation: int | None = None
        self._refresh_deadline_ns: int | None = None
        self._databases_ready = 0
        self._connections: dict[str, FuzzWorkerConnection] = {}
        self._recent_issues: deque[dict[str, str]] = deque(maxlen=_DETAIL_LIMIT)
        self._lock = Lock()

    def set_phase(
        self,
        phase: str,
        *,
        generation: int | None = None,
        refresh_deadline_ns: int | None = None,
    ) -> None:
        if not phase:
            raise ValueError("phase must be nonempty")
        now_ns = self._clock_ns()
        with self._lock:
            if generation is not None and generation != self._generation:
                self._generation = generation
                self._generation_started_ns = now_ns
                self._databases_ready = 0
            self._phase = phase
            if refresh_deadline_ns is not None or generation is not None:
                self._refresh_deadline_ns = refresh_deadline_ns

    def set_databases_ready(self, count: int) -> None:
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("count must be a nonnegative integer")
        with self._lock:
            self._databases_ready = count

    def register_connection(
        self,
        *,
        worker: str,
        endpoint: str,
        worker_kind: str,
        database: str,
        connection_id: int,
    ) -> None:
        if not isinstance(connection_id, int) or isinstance(connection_id, bool):
            raise TypeError("connection_id must be an integer")
        connection = FuzzWorkerConnection(
            worker=worker,
            endpoint=endpoint,
            worker_kind=worker_kind,
            database=database,
            connection_id=connection_id,
            connected_ns=self._clock_ns(),
        )
        with self._lock:
            self._connections[worker] = connection

    def unregister_connection(self, worker: str, connection_id: int) -> None:
        with self._lock:
            current = self._connections.get(worker)
            if current is not None and current.connection_id == connection_id:
                self._connections.pop(worker, None)

    def record_issue(
        self,
        *,
        worker: str,
        endpoint: str,
        error: str,
        sql: str | None = None,
    ) -> None:
        issue = {
            "worker": _truncate(worker),
            "endpoint": _truncate(endpoint),
            "error": _truncate(error),
        }
        if sql is not None:
            issue["sql"] = _truncate(sql)
        with self._lock:
            self._recent_issues.append(issue)

    def connections(self) -> tuple[FuzzWorkerConnection, ...]:
        with self._lock:
            return tuple(sorted(self._connections.values(), key=lambda item: item.worker))

    def snapshot(self) -> dict[str, object]:
        snapshot, _ = self.snapshot_with_connections()
        return snapshot

    def snapshot_with_connections(
        self,
    ) -> tuple[dict[str, object], tuple[FuzzWorkerConnection, ...]]:
        """Return one internally consistent runtime and connection snapshot."""
        now_ns = self._clock_ns()
        with self._lock:
            phase = self._phase
            generation = self._generation
            generation_started_ns = self._generation_started_ns
            refresh_deadline_ns = self._refresh_deadline_ns
            databases_ready = self._databases_ready
            connections = tuple(
                sorted(self._connections.values(), key=lambda item: item.worker)
            )
            recent_issues = tuple(dict(issue) for issue in self._recent_issues)
        groups: Counter[str] = Counter()
        for connection in connections:
            groups[f"{connection.endpoint}_{connection.worker_kind}"] += 1
        snapshot: dict[str, object] = {
            "phase": phase,
            "generation": generation,
            "elapsed_ns": max(0, now_ns - self._started_ns),
            "generation_elapsed_ns": max(0, now_ns - generation_started_ns),
            "refresh_remaining_ns": (
                None
                if refresh_deadline_ns is None
                else max(0, refresh_deadline_ns - now_ns)
            ),
            "databases_ready": databases_ready,
            "connections": len(connections),
            "connection_groups": dict(sorted(groups.items())),
            "recent_issues": recent_issues,
        }
        return snapshot, connections


def _fetch_all(session: QuerySession, sql: str) -> tuple[tuple[object, ...], ...]:
    cursor = session.execute(sql)
    rows: list[tuple[object, ...]] = []
    try:
        while True:
            batch = cursor.fetchmany(128)
            if not batch:
                break
            rows.extend(tuple(row) for row in batch)
    finally:
        cursor.close()
    return tuple(rows)


class FuzzProcesslistCollector:
    """Best-effort PROCESSLIST evidence restricted to registered fuzz sessions."""

    def __init__(
        self,
        factory: ConnectionFactory,
        primary: NodeConfig,
        replica: NodeConfig,
        runtime: FuzzRuntimeDiagnostics,
        *,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        sample_timeout_seconds: float = 4.0,
    ) -> None:
        if sample_timeout_seconds <= 0:
            raise ValueError("sample_timeout_seconds must be positive")
        self._factory = factory
        self._nodes = {"primary": primary, "replica": replica}
        self._runtime = runtime
        self._clock_ns = clock_ns
        self._sample_timeout_seconds = float(sample_timeout_seconds)

    @staticmethod
    def _empty_endpoint(registered: int) -> dict[str, object]:
        return {
            "registered": registered,
            "visible": 0,
            "missing": registered,
            "commands": {},
            "longest_sleep_seconds": 0,
            "longest_query_seconds": 0,
            "slowest_connections": (),
            "_visible_connection_ids": (),
        }

    def collect(self) -> dict[str, object]:
        deadline_monotonic = time.monotonic() + self._sample_timeout_seconds
        by_endpoint: dict[str, list[FuzzWorkerConnection]] = {
            "primary": [],
            "replica": [],
        }
        for connection in self._runtime.connections():
            if connection.endpoint in by_endpoint:
                by_endpoint[connection.endpoint].append(connection)
        endpoints = {
            endpoint: self._collect_endpoint(
                self._nodes[endpoint],
                endpoint_connections,
                deadline_monotonic,
            )
            for endpoint, endpoint_connections in by_endpoint.items()
        }
        return {
            "sampled_at_ns": self._clock_ns(),
            "endpoints": endpoints,
        }

    def _collect_endpoint(
        self,
        node: NodeConfig,
        connections: list[FuzzWorkerConnection],
        deadline_monotonic: float,
    ) -> dict[str, object]:
        summary = self._empty_endpoint(len(connections))
        if not connections:
            return summary
        by_id = {connection.connection_id: connection for connection in connections}
        id_list = ",".join(str(connection_id) for connection_id in sorted(by_id))
        sql = (
            "SELECT ID, DB, COMMAND, TIME, STATE, INFO "
            "FROM information_schema.PROCESSLIST "
            f"WHERE ID IN ({id_list})"
        )
        try:
            deadline_session = getattr(self._factory, "control_session_until", None)
            session_context = (
                deadline_session(node, "information_schema", deadline_monotonic)
                if callable(deadline_session)
                else self._factory.control_session(node, "information_schema")
            )
            with session_context as session:
                rows = _fetch_all(session, sql)
        except Exception as error:
            summary["diagnostics_error_type"] = type(error).__name__
            summary["diagnostics_error"] = str(error)
            return summary
        commands: Counter[str] = Counter()
        visible_ids: set[int] = set()
        details: list[dict[str, object]] = []
        longest_sleep = 0
        longest_query = 0
        for row in rows:
            if len(row) < 6:
                continue
            connection_id = _parse_int(row[0])
            raw_elapsed_seconds = _parse_int(row[3])
            if connection_id is None or raw_elapsed_seconds is None:
                continue
            elapsed_seconds = max(0, raw_elapsed_seconds)
            connection = by_id.get(connection_id)
            if connection is None:
                continue
            visible_ids.add(connection_id)
            command = str(row[2] or "")
            commands[command] += 1
            if command.lower() == "sleep":
                longest_sleep = max(longest_sleep, elapsed_seconds)
            if command.lower() == "query":
                longest_query = max(longest_query, elapsed_seconds)
            details.append(
                {
                    "connection_id": connection_id,
                    "worker": connection.worker,
                    "database": str(row[1] or connection.database),
                    "command": command,
                    "time_seconds": elapsed_seconds,
                    "state": _truncate(row[4] or ""),
                    "sql": _truncate(row[5] or ""),
                }
            )
        summary.update(
            {
                "visible": len(visible_ids),
                "missing": len(set(by_id) - visible_ids),
                "commands": dict(sorted(commands.items())),
                "longest_sleep_seconds": longest_sleep,
                "longest_query_seconds": longest_query,
                "slowest_connections": tuple(
                    sorted(
                        details,
                        key=lambda item: (
                            -_int_value(item, "time_seconds"),
                            _int_value(item, "connection_id"),
                        ),
                    )[:_DETAIL_LIMIT]
                ),
                "_visible_connection_ids": tuple(sorted(visible_ids)),
            }
        )
        return summary


_STAGE_LABELS = {
    "starting": "启动",
    "connecting": "连接",
    "waiting_for_generated_sql": "等待SQL",
    "reader_executing": "读执行",
    "reader_fetching": "读拉取",
    "writer_executing": "写执行",
    "writer_fetching": "写拉取",
    "reconnecting": "重连",
    "compatibility_error_backoff": "兼容错误退避",
}

_PHASE_DIAGNOSES = {
    "starting": "正在启动诊断和SQL生成进程",
    "materializing": "正在并发创建新批次",
    "prewarming": "正在为读线程预生成首批SQL",
    "stopping": "正在停止旧批次连接",
    "failed": "运行已经失败，查看最近错误",
    "finished": "运行已经结束",
}


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _int_value(mapping: Mapping[str, object], name: str) -> int:
    value = mapping.get(name, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


class FuzzProgressReporter:
    """Stateful compact terminal renderer with bounded anomaly warnings."""

    def __init__(
        self,
        *,
        diagnostics_interval_seconds: float,
        expected_connection_groups: Mapping[str, int],
        expected_databases: int = 1,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self._interval_seconds = float(diagnostics_interval_seconds)
        self._expected_groups = dict(expected_connection_groups)
        self._expected_databases = expected_databases
        self._clock_ns = clock_ns
        self._previous_counters: dict[str, int] | None = None
        self._previous_ns: int | None = None
        self._last_read_progress_ns: int | None = None
        self._last_warning_ns: int | None = None
        self._last_warning_code: str | None = None
        self._previous_phase: str | None = None
        self._previous_generation: object = None
        self._has_previous_generation = False
        self._lock = Lock()

    def render(self, document: Mapping[str, object]) -> tuple[str, ...]:
        with self._lock:
            return self._render_locked(document)

    def _render_locked(self, document: Mapping[str, object]) -> tuple[str, ...]:
        now_ns = self._clock_ns()
        counters = _mapping(document.get("counters"))
        current_counters = {
            name: _int_value(counters, name)
            for name in (
                "databases_ready",
                "generations_ready",
                "reads",
                "writes",
                "errors",
                "timeouts",
                "connection_losses",
                "reconnects",
            )
        }
        previous = self._previous_counters or current_counters
        interval_seconds = self._interval_seconds
        if self._previous_ns is not None:
            interval_seconds = max(1e-9, (now_ns - self._previous_ns) / 1_000_000_000)
        deltas = {
            name: max(0, current_counters[name] - previous.get(name, 0))
            for name in current_counters
        }
        runtime = _mapping(document.get("runtime"))
        phase = str(runtime.get("phase", "starting"))
        generation = runtime.get("generation")
        generation_changed = (
            self._has_previous_generation and generation != self._previous_generation
        )
        reads = current_counters["reads"]
        if self._previous_phase != phase or generation_changed or phase != "running":
            self._last_read_progress_ns = now_ns
        elif self._last_read_progress_ns is None or reads > previous.get("reads", reads):
            self._last_read_progress_ns = now_ns
        if generation_changed:
            self._last_warning_ns = None
            self._last_warning_code = None
        self._previous_phase = phase
        self._previous_generation = generation
        self._has_previous_generation = True
        assert self._last_read_progress_ns is not None
        no_read_seconds = max(
            0.0,
            (now_ns - self._last_read_progress_ns) / 1_000_000_000,
        )
        diagnosis_code, diagnosis = self._diagnose(
            document,
            no_read_seconds,
            now_ns,
        )
        status = self._status_line(
            document,
            current_counters,
            deltas,
            interval_seconds,
            no_read_seconds,
            diagnosis,
            now_ns,
        )
        lines = [status]
        if self._should_warn(diagnosis_code, no_read_seconds, now_ns):
            lines.append(self._warning_line(document, diagnosis, no_read_seconds))
            self._last_warning_ns = now_ns
            self._last_warning_code = diagnosis_code
        self._previous_counters = current_counters
        self._previous_ns = now_ns
        return tuple(lines)

    def _diagnose(
        self,
        document: Mapping[str, object],
        no_read_seconds: float,
        now_ns: int,
    ) -> tuple[str, str]:
        runtime = _mapping(document.get("runtime"))
        phase = str(runtime.get("phase", "starting"))
        if phase != "running":
            return f"phase_{phase}", _PHASE_DIAGNOSES.get(phase, f"当前阶段={phase}")
        stages = _mapping(document.get("stages"))
        stage_counts = {
            str(name): value
            for name, value in stages.items()
            if isinstance(value, int) and not isinstance(value, bool)
        }
        expected_workers = sum(self._expected_groups.values())
        active_workers = sum(stage_counts.values())
        if active_workers < expected_workers:
            return (
                "workers_missing",
                f"工作线程缺失({active_workers}/{expected_workers})",
            )
        pipeline = _mapping(document.get("pipeline"))
        process_total = _int_value(pipeline, "processes_total")
        process_alive = _int_value(pipeline, "processes_alive")
        if process_total > 0 and process_alive < process_total:
            return (
                "generator_process_dead",
                f"SQL生成进程异常退出({process_alive}/{process_total})",
            )
        expected_readers = (
            self._expected_groups.get("primary_reader", 0)
            + self._expected_groups.get("replica_reader", 0)
        )
        waiting = stage_counts.get("waiting_for_generated_sql", 0)
        pipeline = _mapping(document.get("pipeline"))
        waiting_age = max(
            self._stage_age_seconds(document, "waiting_for_generated_sql"),
            _int_value(pipeline, "oldest_pending_ns") / 1_000_000_000,
        )
        if (
            no_read_seconds >= _NO_READ_WARNING_SECONDS
            and waiting >= max(1, (expected_readers + 1) // 2)
            and waiting_age >= _NO_READ_WARNING_SECONDS
        ):
            return "generation_slow", "SQL生成速度不足"
        if stage_counts.get("compatibility_error_backoff", 0) > 0:
            return (
                "compatibility_error_backoff",
                "SQL兼容错误连续发生，读线程正在受控退避",
            )
        error_storm = self._error_storm_top(document)
        if no_read_seconds >= _NO_READ_WARNING_SECONDS and error_storm:
            mysql_query, mysql_sleep = self._mysql_commands(document)
            query_not_sent = (
                self._processlist_is_fresh(document, now_ns)
                and self._processlist_has_complete_visibility(document)
                and mysql_query == 0
                and mysql_sleep > 0
                and error_storm.get("failure_stage") == "execute"
            )
            diagnosis = "客户端错误风暴"
            if query_not_sent:
                diagnosis += "；查询未发送到 MySQL，客户端快速失败"
            return "client_error_storm", diagnosis
        reader_executing = stage_counts.get("reader_executing", 0)
        reader_executing_age = self._stage_age_seconds(
            document,
            "reader_executing",
        )
        mysql_query, mysql_sleep = self._mysql_commands(document)
        mismatch_evidence = (
            no_read_seconds >= _NO_READ_WARNING_SECONDS
            and reader_executing > 0
            and reader_executing_age >= _NO_READ_WARNING_SECONDS
            and self._processlist_is_fresh(document, now_ns)
            and self._processlist_has_complete_visibility(document)
            and mysql_query == 0
            and mysql_sleep > 0
        )
        if (
            no_read_seconds >= _NO_READ_WARNING_SECONDS
            and reader_executing > 0
            and reader_executing_age >= _NO_READ_WARNING_SECONDS
            and not mismatch_evidence
        ):
            return "mysql_executing", "读查询长时间在MySQL执行"
        if (
            no_read_seconds >= _NO_READ_WARNING_SECONDS
            and stage_counts.get("reader_fetching", 0) > 0
            and self._stage_age_seconds(document, "reader_fetching")
            >= _NO_READ_WARNING_SECONDS
        ):
            return "client_fetching", "客户端长时间拉取或解析结果"
        if stage_counts.get("reconnecting", 0) > 0:
            return "reconnecting", "连接失败并正在退避重连"
        groups = _mapping(runtime.get("connection_groups"))
        for group, expected in self._expected_groups.items():
            actual = _int_value(groups, group)
            if actual != expected:
                return "connections_missing", f"连接数量异常({group}:{actual}/{expected})"
        if mismatch_evidence:
            return "app_mysql_mismatch", "程序与MySQL状态矛盾"
        if no_read_seconds >= _NO_READ_WARNING_SECONDS:
            return "no_evidence", "读取无进展但现有证据不足"
        return "healthy", "负载正常推进"

    @staticmethod
    def _stage_age_seconds(document: Mapping[str, object], stage: str) -> float:
        details = _mapping(document.get("stage_details"))
        detail = _mapping(details.get(stage))
        return _int_value(detail, "max_age_ns") / 1_000_000_000

    def _processlist_is_fresh(
        self,
        document: Mapping[str, object],
        now_ns: int,
    ) -> bool:
        processlist = _mapping(document.get("processlist"))
        sampled_at = processlist.get("sampled_at_ns")
        if not isinstance(sampled_at, int) or isinstance(sampled_at, bool):
            return False
        if processlist.get("collector_error_type") is not None:
            return False
        endpoints = _mapping(processlist.get("endpoints"))
        if any(
            _mapping(endpoint).get("diagnostics_error_type") is not None
            for endpoint in endpoints.values()
        ):
            return False
        age_seconds = max(0.0, (now_ns - sampled_at) / 1_000_000_000)
        return age_seconds <= max(
            _NO_READ_WARNING_SECONDS,
            self._interval_seconds * 2,
        )

    @staticmethod
    def _processlist_has_complete_visibility(
        document: Mapping[str, object],
    ) -> bool:
        processlist = _mapping(document.get("processlist"))
        endpoints = _mapping(processlist.get("endpoints"))
        if not endpoints:
            return False
        for value in endpoints.values():
            endpoint = _mapping(value)
            missing = endpoint.get("missing")
            if (
                not isinstance(missing, int)
                or isinstance(missing, bool)
                or missing != 0
            ):
                return False
        return True

    @staticmethod
    def _mysql_commands(document: Mapping[str, object]) -> tuple[int, int]:
        processlist = _mapping(document.get("processlist"))
        endpoints = _mapping(processlist.get("endpoints"))
        query = 0
        sleep = 0
        for value in endpoints.values():
            endpoint = _mapping(value)
            commands = _mapping(endpoint.get("commands"))
            query += _int_value(commands, "Query")
            sleep += _int_value(commands, "Sleep")
        return query, sleep

    @staticmethod
    def _error_storm_top(document: Mapping[str, object]) -> Mapping[str, object]:
        summary = _mapping(document.get("errors_summary"))
        rate = summary.get("rate_per_second")
        if not isinstance(rate, (int, float)) or isinstance(rate, bool) or rate < 10:
            return {}
        top = summary.get("top")
        if not isinstance(top, (tuple, list)) or not top:
            return {}
        return _mapping(top[0])

    def _status_line(
        self,
        document: Mapping[str, object],
        counters: Mapping[str, int],
        deltas: Mapping[str, int],
        interval_seconds: float,
        no_read_seconds: float,
        diagnosis: str,
        now_ns: int,
    ) -> str:
        runtime = _mapping(document.get("runtime"))
        generation = runtime.get("generation")
        phase = runtime.get("phase", "starting")
        elapsed = _int_value(runtime, "elapsed_ns") / 1_000_000_000
        stages = _mapping(document.get("stages"))
        stage_text = ",".join(
            f"{_STAGE_LABELS.get(str(name), str(name))}:{value}"
            for name, value in sorted(stages.items())
        ) or "无"
        details = _mapping(document.get("stage_details"))
        ages: list[tuple[str, float]] = []
        for name, value in details.items():
            detail = _mapping(value)
            ages.append((str(name), _int_value(detail, "max_age_ns") / 1_000_000_000))
        age_text = ",".join(
            f"{_STAGE_LABELS.get(name, name)}:{age:.1f}s"
            for name, age in sorted(ages, key=lambda item: (-item[1], item[0]))[:3]
        ) or "无"
        groups = _mapping(runtime.get("connection_groups"))
        group_labels = {
            "primary_writer": "主写",
            "primary_reader": "主读",
            "replica_reader": "备读",
        }
        connection_text = ",".join(
            f"{group_labels.get(group, group)}:{_int_value(groups, group)}/{expected}"
            for group, expected in self._expected_groups.items()
        )
        pipeline = _mapping(document.get("pipeline"))
        processlist_text = self._processlist_text(document, now_ns)
        refresh_remaining = runtime.get("refresh_remaining_ns")
        refresh_text = (
            "不换代"
            if refresh_remaining is None
            else f"{_int_value(runtime, 'refresh_remaining_ns') / 1_000_000_000:.1f}s"
        )
        duration_text = self._duration_text(document)
        error_storm = self._error_storm_top(document)
        error_storm_text = ""
        if error_storm:
            rate = error_storm.get("rate_per_second", 0.0)
            rendered_rate = float(rate) if isinstance(rate, (int, float)) else 0.0
            error_storm_text = (
                f" 错误风暴={error_storm.get('fingerprint', 'unknown')}:"
                f"{rendered_rate:.1f}/s"
            )
        active_threads = sum(
            _int_value(stages, str(name)) for name in stages
        )
        return (
            f"[fuzz状态] 运行={elapsed:.1f}s 批次={generation} 阶段={phase} "
            f"数据库={_int_value(runtime, 'databases_ready')}/{self._expected_databases} "
            f"累计就绪={counters['databases_ready']} 换代={refresh_text} "
            f"线程={active_threads}/"
            f"{sum(self._expected_groups.values())} 连接={{{connection_text}}} "
            f"读={counters['reads']}(+{deltas['reads']},{deltas['reads'] / interval_seconds:.1f}/s) "
            f"写={counters['writes']}(+{deltas['writes']},{deltas['writes'] / interval_seconds:.1f}/s) "
            f"错误={counters['errors']}(+{deltas['errors']}) "
            f"超时={counters['timeouts']}(+{deltas['timeouts']}) "
            f"断连={counters['connection_losses']}(+{deltas['connection_losses']}) "
            f"重连={counters['reconnects']}(+{deltas['reconnects']}) "
            f"阶段={{{stage_text}}} 阶段最长={{{age_text}}} "
            f"SQL生成器={_int_value(pipeline, 'processes_alive')}/"
            f"{_int_value(pipeline, 'processes_total')} "
            f"待生成={_int_value(pipeline, 'pending_requests')} "
            f"最久={_int_value(pipeline, 'oldest_pending_ns') / 1_000_000_000:.1f}s "
            f"耗时={{{duration_text}}}{error_storm_text} {processlist_text} "
            f"无读取={no_read_seconds:.1f}s 判断={diagnosis}"
        )

    @staticmethod
    def _duration_text(document: Mapping[str, object]) -> str:
        durations = _mapping(document.get("durations"))
        labels = (
            ("generation_compute_ns", "SQL生成"),
            ("generation_wait_ns", "SQL等待"),
            ("read_execute_ns", "读执行"),
            ("read_fetch_ns", "读拉取"),
            ("write_execute_ns", "写执行"),
            ("write_fetch_ns", "写拉取"),
            ("compatibility_error_backoff_ns", "兼容退避"),
        )
        rendered: list[str] = []
        for metric, label in labels:
            values = _mapping(durations.get(metric))
            count = _int_value(values, "count")
            if count <= 0:
                continue
            average_ms = _int_value(values, "total_ns") / count / 1_000_000
            maximum_ms = _int_value(values, "max_ns") / 1_000_000
            rendered.append(f"{label}:均{average_ms:.1f}ms/最大{maximum_ms:.1f}ms")
        return ",".join(rendered) or "无"

    def _processlist_text(
        self,
        document: Mapping[str, object],
        now_ns: int,
    ) -> str:
        processlist = _mapping(document.get("processlist"))
        sampled_at = processlist.get("sampled_at_ns")
        sample_age_seconds = (
            max(0.0, (now_ns - sampled_at) / 1_000_000_000)
            if isinstance(sampled_at, int) and not isinstance(sampled_at, bool)
            else 0.0
        )
        endpoints = _mapping(processlist.get("endpoints"))
        rendered: list[str] = []
        collector_error = processlist.get("collector_error_type")
        if collector_error is not None:
            rendered.append(
                f"采样错误={collector_error}:"
                f"{_truncate(processlist.get('collector_error', ''))}"
            )
        for endpoint_name, label in (("primary", "主节点"), ("replica", "备节点")):
            endpoint = _mapping(endpoints.get(endpoint_name))
            commands = _mapping(endpoint.get("commands"))
            error_type = endpoint.get("diagnostics_error_type")
            error_text = (
                ""
                if error_type is None
                else f",采样错误:{error_type}:{_truncate(endpoint.get('diagnostics_error', ''))}"
            )
            rendered.append(
                f"{label}={{连接:{_int_value(endpoint, 'visible')},"
                f"登记:{_int_value(endpoint, 'registered')},"
                f"缺失:{_int_value(endpoint, 'missing')},"
                f"Sleep:{_int_value(commands, 'Sleep')},"
                f"Query:{_int_value(commands, 'Query')},"
                f"最长Sleep:{_int_value(endpoint, 'longest_sleep_seconds')}s,"
                f"最长Query:{_int_value(endpoint, 'longest_query_seconds')}s{error_text}}}"
            )
        return f"采样龄={sample_age_seconds:.1f}s " + " ".join(rendered)

    def _should_warn(self, code: str, no_read_seconds: float, now_ns: int) -> bool:
        if no_read_seconds < _NO_READ_WARNING_SECONDS or code.startswith("phase_"):
            return False
        if code == "healthy":
            return False
        if code != self._last_warning_code or self._last_warning_ns is None:
            return True
        return (now_ns - self._last_warning_ns) / 1_000_000_000 >= _WARNING_REPEAT_SECONDS

    @staticmethod
    def _warning_line(
        document: Mapping[str, object],
        diagnosis: str,
        no_read_seconds: float,
    ) -> str:
        details = _mapping(document.get("stage_details"))
        workers: list[tuple[str, int]] = []
        for value in details.values():
            detail = _mapping(value)
            oldest = detail.get("oldest_workers", ())
            if not isinstance(oldest, (tuple, list)):
                continue
            for item in oldest:
                worker = _mapping(item)
                workers.append(
                    (str(worker.get("worker", "unknown")), _int_value(worker, "age_ns"))
                )
        worker_text = ",".join(
            f"{worker}:{age_ns / 1_000_000_000:.1f}s"
            for worker, age_ns in sorted(workers, key=lambda item: (-item[1], item[0]))[
                :_DETAIL_LIMIT
            ]
        ) or "无"
        runtime = _mapping(document.get("runtime"))
        recent = runtime.get("recent_issues", ())
        issue_parts: list[str] = []
        if isinstance(recent, (tuple, list)):
            for value in recent[-_DETAIL_LIMIT:]:
                issue = _mapping(value)
                sql = issue.get("sql")
                suffix = "" if sql is None else f" SQL={_truncate(sql)}"
                issue_parts.append(
                    f"{issue.get('worker', 'unknown')}@{issue.get('endpoint', 'unknown')}:"
                    f"{issue.get('error', 'unknown')}{suffix}"
                )
        issue_text = "；".join(issue_parts) or "无"
        mysql_slowest = FuzzProgressReporter._mysql_slowest_text(document)
        storm_text = FuzzProgressReporter._error_storm_warning_text(document)
        return (
            f"[fuzz警告] 连续{no_read_seconds:.1f}秒无读取进展；"
            f"初步原因={diagnosis}；最老线程={worker_text}；"
            f"MySQL最慢={mysql_slowest}；最近错误={issue_text}{storm_text}"
        )

    @staticmethod
    def _error_storm_warning_text(document: Mapping[str, object]) -> str:
        top = FuzzProgressReporter._error_storm_top(document)
        if not top:
            return ""
        watchdog = _mapping(top.get("watchdog"))

        def yes_no(value: object) -> str:
            if value is True:
                return "是"
            if value is False:
                return "否"
            return "未知"

        def success(value: object) -> str:
            if value is True:
                return "成功"
            if value is False:
                return "失败"
            return "未执行"

        raw_endpoints = top.get("endpoints")
        endpoints = (
            ",".join(str(value) for value in raw_endpoints)
            if isinstance(raw_endpoints, (tuple, list))
            else "未知"
        )
        sql = _truncate(top.get("sample_sql", "")) or "无"
        message = _truncate(top.get("message", "")) or "无"
        return (
            f"；错误取证=指纹={top.get('fingerprint', 'unknown')} "
            f"异常={top.get('error_type', 'unknown')}:{message} "
            f"阶段={top.get('failure_stage', 'unknown')} "
            f"watchdog={{超时:{yes_no(watchdog.get('timed_out'))},"
            f"KILL:{success(watchdog.get('kill_query_succeeded'))},"
            f"abort:{success(watchdog.get('abort_succeeded'))}}} "
            f"影响={_int_value(top, 'worker_count')}线程/"
            f"{_int_value(top, 'database_count')}数据库/{endpoints} SQL={sql}"
        )

    @staticmethod
    def _mysql_slowest_text(document: Mapping[str, object]) -> str:
        processlist = _mapping(document.get("processlist"))
        endpoints = _mapping(processlist.get("endpoints"))
        details: list[tuple[str, Mapping[str, object]]] = []
        for endpoint_name, value in endpoints.items():
            endpoint = _mapping(value)
            slowest = endpoint.get("slowest_connections", ())
            if not isinstance(slowest, (tuple, list)):
                continue
            for item in slowest:
                details.append((str(endpoint_name), _mapping(item)))
        rendered: list[str] = []
        for endpoint_name, item in sorted(
            details,
            key=lambda value: (
                -_int_value(value[1], "time_seconds"),
                str(value[1].get("worker", "")),
            ),
        )[:_DETAIL_LIMIT]:
            sql = _truncate(item.get("sql", "")) or "无"
            state = _truncate(item.get("state", "")) or "无"
            rendered.append(
                f"{item.get('worker', 'unknown')}@{endpoint_name}:"
                f"{item.get('command', 'unknown')} "
                f"{_int_value(item, 'time_seconds')}s state={state} SQL={sql}"
            )
        return "；".join(rendered) or "无"


__all__ = [
    "FuzzProcesslistCollector",
    "FuzzProgressReporter",
    "FuzzRuntimeDiagnostics",
    "FuzzWorkerConnection",
]
