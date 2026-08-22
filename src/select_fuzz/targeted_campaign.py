"""Controlled TaurusDB feature campaign used after the generic comparison soak.

The campaign deliberately keeps its scope small and auditable: every case gets
an independent retained database, every statement and node outcome is written
before the next case starts, and connection failures are classified separately
from query results.  It is not part of the generic SQL grammar or correctness
oracle because several Taurus-only features are intentionally unsupported by
the open-source MySQL node.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import random
import signal
from threading import Event, Lock
import time
from typing import Any, cast

import mysql.connector


LOST_CONNECTION_ERRNOS = frozenset({2006, 2013, 2055})
FEATURE_ONLY = frozenset({"flashback", "pq_parallel", "second_level_partition"})
OPTIMIZER_SWITCHES = (
    "derived_merge_no_subquery_check",
    "partial_result_cache",
    "offset_pushdown",
    "backward_index_scan",
    "simplify_count_not_null",
    "left_join_elimination",
    "icp_cost_based",
)
FLASHBACK_STABILIZATION_SECONDS = 1.5


def classify_connection_event(
    *,
    status: str,
    errno: int | None,
    watchdog_fired: bool,
    stage: str = "execute",
) -> str:
    """Classify a connection error without mistaking a killed query for a crash."""

    if errno in LOST_CONNECTION_ERRNOS:
        if stage != "execute":
            return "connection_lost_infra"
        return "timeout_connection" if watchdog_fired else "crash_candidate"
    if status == "timeout" or watchdog_fired:
        return "timeout"
    return "database_error"


def make_database_name(worker: str, ordinal: int, seed: int) -> str:
    """Return a safe, unique, retained database name."""

    worker_id = "".join(ch.lower() if ch.isalnum() else "_" for ch in worker).strip("_")
    worker_id = worker_id or "worker"
    digest = hashlib.sha256(f"{worker_id}:{ordinal}:{seed}:{time.time_ns()}".encode()).hexdigest()
    name = f"sf_t_{worker_id[:12]}_{ordinal}_{digest[:12]}"
    return name[:64]


def _canonical_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, bytes):
        return {"$type": "bytes", "hex": value.hex()}
    if isinstance(value, (bytearray, memoryview)):
        return {"$type": "bytes", "hex": bytes(value).hex()}
    if isinstance(value, (datetime,)):
        return {"$type": "datetime", "value": value.isoformat()}
    if isinstance(value, Decimal):
        return {"$type": "decimal", "value": str(value)}
    if isinstance(value, (tuple, list)):
        return [_canonical_value(child) for child in value]
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(child) for key, child in value.items()}
    return {"$type": type(value).__name__, "value": str(value)}


def _column_metadata(description: Sequence[object] | None) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for item in description or ():
        fields = tuple(item) if isinstance(item, (tuple, list)) else ()
        result.append(
            {
                "name": fields[0] if len(fields) > 0 else None,
                "type_code": fields[1] if len(fields) > 1 else None,
                "display_size": fields[2] if len(fields) > 2 else None,
                "internal_size": fields[3] if len(fields) > 3 else None,
                "precision": fields[4] if len(fields) > 4 else None,
                "scale": fields[5] if len(fields) > 5 else None,
                "null_ok": fields[6] if len(fields) > 6 else None,
            }
        )
    return result


def compare_result_payloads(
    left: Mapping[str, object], right: Mapping[str, object]
) -> tuple[bool, str]:
    """Compare two captured outcomes and keep rows/metadata/status distinct."""

    left_status = left.get("status")
    right_status = right.get("status")
    if left_status != right_status:
        return False, "status"
    if left_status != "success":
        left_error = left.get("error")
        right_error = right.get("error")
        return (left_error == right_error), ("match" if left_error == right_error else "error")
    if left.get("columns") != right.get("columns"):
        return False, "metadata"
    if left.get("rows") != right.get("rows"):
        return False, "rows"
    return True, "match"


@dataclass(frozen=True, slots=True)
class TargetCase:
    feature: str
    setup: tuple[str, ...]
    queries: tuple[str, ...]
    feature_only: bool = False


@dataclass(frozen=True, slots=True)
class CampaignNode:
    role: str
    host: str
    port: int
    user_env: str
    password_env: str


def _left_join_case() -> TargetCase:
    return TargetCase(
        "pq_left_join",
        (
            "CREATE TABLE parent (id INT PRIMARY KEY, v INT NOT NULL)",
            "CREATE TABLE child (p_id INT PRIMARY KEY, v INT NOT NULL)",
            "INSERT INTO parent VALUES (1,10),(2,20),(3,30)",
            "INSERT INTO child VALUES (1,100),(2,200),(3,300)",
        ),
        (
            "SELECT p.id, p.v FROM parent AS p LEFT JOIN child AS c ON c.p_id=p.id ORDER BY p.id",
            "EXPLAIN FORMAT=JSON SELECT p.id, p.v FROM parent AS p LEFT JOIN child AS c ON c.p_id=p.id ORDER BY p.id",
        ),
    )


def _pq_case() -> TargetCase:
    return TargetCase(
        "pq_parallel",
        (
            "CREATE TABLE parent (id INT PRIMARY KEY, v INT NOT NULL)",
            "CREATE TABLE child (p_id INT PRIMARY KEY, v INT NOT NULL)",
            "INSERT INTO parent VALUES (1,10),(2,20),(3,30)",
            "INSERT INTO child VALUES (1,100),(2,200),(3,300)",
            "SET SESSION force_parallel_execute=ON",
        ),
        (
            "SELECT p.id, SUM(c.v) AS total FROM parent AS p LEFT JOIN child AS c ON c.p_id=p.id GROUP BY p.id WITH ROLLUP ORDER BY p.id",
            "SELECT p.id, RANK() OVER (ORDER BY p.v) AS rank_value FROM parent AS p LEFT JOIN child AS c ON c.p_id=p.id ORDER BY p.id",
            "EXPLAIN FORMAT=JSON SELECT p.id, SUM(c.v) AS total FROM parent AS p LEFT JOIN child AS c ON c.p_id=p.id GROUP BY p.id WITH ROLLUP ORDER BY p.id",
        ),
        feature_only=True,
    )


def _flashback_case() -> TargetCase:
    return TargetCase(
        "flashback",
        (
            "CREATE TABLE t_back (id INT PRIMARY KEY, v VARCHAR(32)) BACKQUERY=1",
            "INSERT INTO t_back VALUES (1,'before')",
        ),
        (
            "SELECT v FROM t_back",
            "UPDATE t_back SET v='after' WHERE id=1",
            "SELECT v FROM t_back",
            "SELECT v FROM t_back AS OF TIMESTAMP '{FLASHBACK_TS}'",
        ),
        feature_only=True,
    )


def _partition_case(parent: str, child: str) -> TargetCase:
    # The first-level/second-level pair is intentionally varied.  TaurusDB
    # accepts combinations that stock MySQL 8.0.22 rejects at CREATE TABLE.
    if parent == "RANGE" and child == "LIST":
        partition = "PARTITION BY RANGE (id) SUBPARTITION BY LIST (k) (PARTITION p0 VALUES LESS THAN (10) (SUBPARTITION p0s0 VALUES IN (0), SUBPARTITION p0s1 VALUES IN (1)), PARTITION p1 VALUES LESS THAN MAXVALUE (SUBPARTITION p1s0 VALUES IN (0), SUBPARTITION p1s1 VALUES IN (1)))"
    elif parent == "RANGE" and child == "HASH":
        partition = "PARTITION BY RANGE (id) SUBPARTITION BY HASH (k) SUBPARTITIONS 2 (PARTITION p0 VALUES LESS THAN (10), PARTITION p1 VALUES LESS THAN MAXVALUE)"
    elif parent == "LIST" and child == "HASH":
        partition = "PARTITION BY LIST (id) SUBPARTITION BY HASH (k) SUBPARTITIONS 2 (PARTITION p0 VALUES IN (1,2), PARTITION p1 VALUES IN (3,4))"
    elif parent == "HASH" and child == "LIST":
        partition = "PARTITION BY HASH (id) PARTITIONS 2 SUBPARTITION BY LIST (k) (SUBPARTITION s0 VALUES IN (0), SUBPARTITION s1 VALUES IN (1))"
    elif parent == "KEY" and child == "RANGE":
        partition = "PARTITION BY KEY (id) PARTITIONS 2 SUBPARTITION BY RANGE (k) (SUBPARTITION s0 VALUES LESS THAN (1), SUBPARTITION s1 VALUES LESS THAN MAXVALUE)"
    else:
        partition = "PARTITION BY RANGE (id) SUBPARTITION BY KEY (k) SUBPARTITIONS 2 (PARTITION p0 VALUES LESS THAN (10), PARTITION p1 VALUES LESS THAN MAXVALUE)"
    return TargetCase(
        "second_level_partition",
        (
            f"CREATE TABLE t_part (id INT NOT NULL, k INT NOT NULL, v INT) {partition}",
            "INSERT INTO t_part VALUES (1,0,10),(2,1,20),(11,0,30),(12,1,40)",
        ),
        ("SELECT COUNT(*), SUM(v) FROM t_part",),
        feature_only=True,
    )


def _optimizer_case(switch: str, value: str) -> TargetCase:
    return TargetCase(
        "optimizer_switch",
        (
            "CREATE TABLE parent (id INT PRIMARY KEY, v INT NOT NULL)",
            "CREATE TABLE child (p_id INT PRIMARY KEY, v INT NOT NULL)",
            "INSERT INTO parent VALUES (1,10),(2,20),(3,30)",
            "INSERT INTO child VALUES (1,100),(2,200),(3,300)",
            f"SET SESSION optimizer_switch='{switch}={value}'",
        ),
        (
            "SELECT p.id, p.v FROM parent AS p LEFT JOIN child AS c ON c.p_id=p.id ORDER BY p.id",
            "EXPLAIN FORMAT=JSON SELECT p.id, p.v FROM parent AS p LEFT JOIN child AS c ON c.p_id=p.id ORDER BY p.id",
        ),
    )


def build_target_cases() -> tuple[TargetCase, ...]:
    cases: list[TargetCase] = [_left_join_case(), _pq_case(), _flashback_case()]
    for parent, child in (
        ("RANGE", "LIST"),
        ("RANGE", "HASH"),
        ("LIST", "HASH"),
        ("HASH", "LIST"),
        ("KEY", "RANGE"),
        ("RANGE", "KEY"),
    ):
        cases.append(_partition_case(parent, child))
    for switch in OPTIMIZER_SWITCHES:
        for value in ("on", "off"):
            cases.append(_optimizer_case(switch, value))
    return tuple(cases)


def _error_payload(error: BaseException) -> dict[str, object]:
    raw_errno = getattr(error, "errno", None)
    errno = raw_errno if isinstance(raw_errno, int) else None
    raw_sqlstate = getattr(error, "sqlstate", None)
    sqlstate = raw_sqlstate if isinstance(raw_sqlstate, str) else None
    message = str(error)[:4096]
    return {
        "errno": errno,
        "sqlstate": sqlstate,
        "message": message,
        "type": type(error).__name__,
    }


def _is_timeout_error(error: BaseException, elapsed: float, timeout: float) -> bool:
    text = str(error).lower()
    return elapsed >= timeout or "timeout" in text or "timed out" in text


def _connect(node: CampaignNode, database: str) -> Any:
    username = os.environ.get(node.user_env)
    password = os.environ.get(node.password_env)
    if username is None or password is None:
        raise RuntimeError(f"missing credential environment for {node.role}")
    return mysql.connector.connect(
        host=node.host,
        port=node.port,
        user=username,
        password=password,
        database=database,
        autocommit=True,
        connection_timeout=5,
        read_timeout=15,
        write_timeout=15,
    )


def _run_node(
    node: CampaignNode,
    database: str,
    case: TargetCase,
    *,
    timeout_seconds: float = 10.0,
) -> dict[str, object]:
    started = time.monotonic()
    connection: Any | None = None
    try:
        connection = _connect(node, "information_schema")
        cursor = connection.cursor()
        try:
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{database}`")
        finally:
            cursor.close()
        connection.database = database
        cursor = connection.cursor()
        try:
            flashback_timestamp: str | None = None
            for statement in case.setup:
                cursor.execute(statement)
                if cursor.with_rows:
                    cursor.fetchall()
                if statement.startswith("INSERT INTO t_back"):
                    # TaurusDB keeps a short BACKQUERY read-view window.  Let
                    # the insert commit before capturing the historical point;
                    # otherwise AS OF can correctly reject the timestamp as
                    # overlapping the preceding DDL.
                    time.sleep(FLASHBACK_STABILIZATION_SECONDS)
                    cursor.execute("SELECT NOW(6)")
                    row = cursor.fetchone()
                    flashback_timestamp = None if row is None else str(row[0])
            query_results: list[dict[str, object]] = []
            for query in case.queries:
                if "{FLASHBACK_TS}" in query:
                    if flashback_timestamp is None:
                        query_results.append(
                            {
                                "status": "error",
                                "error": {
                                    "errno": None,
                                    "sqlstate": None,
                                    "message": "flashback timestamp was not captured",
                                    "type": "CampaignError",
                                    "classification": "database_error",
                                },
                                "columns": [],
                                "rows": [],
                                "elapsed_ms": 0.0,
                                "sql": query,
                            }
                        )
                        continue
                    query = query.replace("{FLASHBACK_TS}", flashback_timestamp)
                query_started = time.monotonic()
                try:
                    cursor.execute(query)
                    rows = cursor.fetchall() if cursor.with_rows else []
                    query_results.append(
                        {
                            "status": "success",
                            "columns": _column_metadata(cursor.description),
                            "rows": _canonical_value(rows),
                            "elapsed_ms": round((time.monotonic() - query_started) * 1000, 3),
                            "sql": query,
                        }
                    )
                except Exception as error:
                    elapsed = time.monotonic() - query_started
                    error_payload = _error_payload(error)
                    watchdog = _is_timeout_error(error, elapsed, timeout_seconds)
                    error_payload["classification"] = classify_connection_event(
                        status="timeout" if watchdog else "error",
                        errno=error_payload["errno"] if isinstance(error_payload["errno"], int) else None,
                        watchdog_fired=watchdog,
                        stage="execute",
                    )
                    query_results.append(
                        {
                            "status": "timeout" if watchdog else "error",
                            "error": error_payload,
                            "columns": [],
                            "rows": [],
                            "elapsed_ms": round(elapsed * 1000, 3),
                            "sql": query,
                        }
                    )
                    try:
                        connection.rollback()
                    except Exception:
                        pass
            return {
                "role": node.role,
                "status": "success",
                "setup_error": None,
                "queries": query_results,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            }
        except Exception as error:
            payload = _error_payload(error)
            payload["classification"] = classify_connection_event(
                status="error",
                errno=payload["errno"] if isinstance(payload["errno"], int) else None,
                watchdog_fired=False,
                stage="setup",
            )
            return {
                "role": node.role,
                "status": "setup_error",
                "setup_error": payload,
                "queries": [],
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            }
    except Exception as error:
        payload = _error_payload(error)
        payload["classification"] = classify_connection_event(
            status="error",
            errno=payload["errno"] if isinstance(payload["errno"], int) else None,
            watchdog_fired=False,
            stage="connection_open",
        )
        return {
            "role": node.role,
            "status": "setup_error",
            "setup_error": payload,
            "queries": [],
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
        }
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def _comparison(
    feature: str,
    off: Mapping[str, object],
    on: Mapping[str, object],
) -> dict[str, object]:
    def query_list(value: Mapping[str, object]) -> list[Mapping[str, object]]:
        raw = value.get("queries")
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, Mapping)]

    def error_mapping(value: object) -> Mapping[str, object] | None:
        return value if isinstance(value, Mapping) else None

    def has_crash(value: Mapping[str, object]) -> bool:
        setup_error = error_mapping(value.get("setup_error"))
        if setup_error is not None and setup_error.get("classification") == "crash_candidate":
            return True
        for query in query_list(value):
            query_error = error_mapping(query.get("error"))
            if query_error is not None and query_error.get("classification") == "crash_candidate":
                return True
        return False

    def has_infrastructure_pause(value: Mapping[str, object]) -> bool:
        infrastructure_classes = {"connection_lost_infra", "timeout_connection", "timeout"}
        setup_error = error_mapping(value.get("setup_error"))
        if setup_error is not None and setup_error.get("classification") in infrastructure_classes:
            return True
        for query in query_list(value):
            query_error = error_mapping(query.get("error"))
            if query_error is not None and query_error.get("classification") in infrastructure_classes:
                return True
        return False

    setup_errors = {
        role: error
        for role, value in (("custom_off", off), ("custom_on", on))
        if (error := error_mapping(value.get("setup_error"))) is not None
    }
    crash_roles = {
        role
        for role, value in (("custom_off", off), ("custom_on", on))
        if has_crash(value)
    }
    if crash_roles:
        return {
            "matched": False,
            "category": "crash_candidate",
            "crash_roles": sorted(crash_roles),
        }
    infrastructure_roles = {
        role
        for role, value in (("custom_off", off), ("custom_on", on))
        if has_infrastructure_pause(value)
    }
    if infrastructure_roles:
        return {
            "matched": True,
            "category": "infrastructure_pause",
            "infrastructure_roles": sorted(infrastructure_roles),
        }
    if feature in FEATURE_ONLY:
        return {"matched": True, "category": "capability_probe"}
    if feature == "optimizer_switch" and setup_errors:
        # Stock MySQL has no Taurus-only optimizer_switch names.  Preserve the
        # capability result but do not call errno=1193/1231 a correctness bug.
        expected_capability_errors = {1193, 1231}
        if all(
            isinstance(error.get("errno"), int)
            and cast(int, error["errno"]) in expected_capability_errors
            for error in setup_errors.values()
        ):
            return {"matched": True, "category": "capability_probe"}
        return {"matched": False, "category": "status"}
    off_queries: list[object] = list(query_list(off))
    on_queries: list[object] = list(query_list(on))
    if len(off_queries) != len(on_queries):
        return {"matched": False, "category": "status"}
    for left, right in zip(off_queries, on_queries, strict=True):
        if not isinstance(left, Mapping) or not isinstance(right, Mapping):
            return {"matched": False, "category": "status"}
        matched, category = compare_result_payloads(left, right)
        if not matched:
            return {"matched": False, "category": category}
    return {"matched": True, "category": "match"}


class TargetedCampaign:
    def __init__(
        self,
        *,
        nodes: Sequence[CampaignNode],
        artifact_root: Path,
        duration_seconds: float,
        workers: int,
        seed: int,
    ) -> None:
        if len(nodes) != 2 or {node.role for node in nodes} != {"custom_off", "custom_on"}:
            raise ValueError("targeted campaign requires custom_off and custom_on nodes")
        if duration_seconds <= 0 or workers <= 0 or workers > 64:
            raise ValueError("duration_seconds must be positive and workers must be 1..64")
        self.nodes = tuple(nodes)
        self.root = artifact_root
        self.duration_seconds = duration_seconds
        self.workers = workers
        self.seed = seed
        self.stop_event = Event()
        self._write_lock = Lock()
        self._sequence = 0
        self._events_path = self.root / "events.jsonl"
        self._cases_path = self.root / "cases"

    def _append(self, event: Mapping[str, object]) -> None:
        with self._write_lock:
            row = {"sequence": self._sequence, **dict(event)}
            self._sequence += 1
            self._events_path.parent.mkdir(parents=True, exist_ok=True)
            with self._events_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())

    def _run_worker(self, worker_id: int) -> None:
        rng = random.Random(self.seed + worker_id)
        cases = build_target_cases()
        ordinal = worker_id
        deadline = time.monotonic() + self.duration_seconds
        while not self.stop_event.is_set() and time.monotonic() < deadline:
            case = cases[rng.randrange(len(cases))]
            database = make_database_name(f"w{worker_id}", ordinal, rng.getrandbits(128))
            ordinal += self.workers
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="sf-target-node") as pool:
                futures = {
                    node.role: pool.submit(_run_node, node, database, case) for node in self.nodes
                }
                outcomes = {role: future.result() for role, future in futures.items()}
            comparison = _comparison(
                case.feature,
                outcomes["custom_off"],
                outcomes["custom_on"],
            )
            case_id = "case_" + hashlib.sha256(
                f"{case.feature}:{database}:{ordinal}".encode()
            ).hexdigest()[:24]
            record = {
                "type": "target_case",
                "case_id": case_id,
                "feature": case.feature,
                "database": database,
                "worker_id": worker_id,
                "seed": self.seed,
                "setup_sql": list(case.setup),
                "queries": list(case.queries),
                "nodes": outcomes,
                "comparison": comparison,
            }
            self._append(record)
            self._cases_path.mkdir(parents=True, exist_ok=True)
            case_path = self._cases_path / f"{case_id}.json"
            with case_path.open("w", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if comparison.get("category") in {"rows", "metadata", "status", "error", "crash_candidate"}:
                self._append(
                    {
                        "type": "finding",
                        "case_id": case_id,
                        "feature": case.feature,
                        "classification": comparison.get("category"),
                        "database": database,
                    }
                )

    def run(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._append(
            {
                "type": "run_started",
                "seed": self.seed,
                "workers": self.workers,
                "duration_seconds": self.duration_seconds,
                "started_at": datetime.now(UTC).isoformat(),
            }
        )
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="sf-target-worker") as pool:
            futures = [pool.submit(self._run_worker, worker_id) for worker_id in range(self.workers)]
            try:
                for future in futures:
                    future.result()
            except KeyboardInterrupt:
                self.stop_event.set()
                raise
        self._append({"type": "run_finished", "finished_at": datetime.now(UTC).isoformat()})


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=float, default=14_400)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/taurus-feature-campaign"))
    parser.add_argument("--local-host", default="127.0.0.1")
    parser.add_argument("--local-port", type=int, default=13_307)
    parser.add_argument("--taurus-host", default="116.63.205.246")
    parser.add_argument("--taurus-port", type=int, default=3306)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    seed = args.seed if args.seed is not None else int(time.time())
    nodes = (
        CampaignNode("custom_off", args.local_host, args.local_port, "SELECT_FUZZ_LOCAL_MYSQL_USER", "SELECT_FUZZ_LOCAL_MYSQL_PASSWORD"),
        CampaignNode("custom_on", args.taurus_host, args.taurus_port, "SELECT_FUZZ_TAURUS_MYSQL_USER", "SELECT_FUZZ_TAURUS_MYSQL_PASSWORD"),
    )
    campaign = TargetedCampaign(
        nodes=nodes,
        artifact_root=args.artifacts,
        duration_seconds=args.duration_seconds,
        workers=args.workers,
        seed=seed,
    )
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda _signum, _frame: campaign.stop_event.set())
    campaign.run()


if __name__ == "__main__":
    main()
