#!/usr/bin/env python3
"""Run the production correctness pipeline against three local MySQL 8.0.41 sockets."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import re
import signal
from threading import Event, Timer
import time
from types import MappingProxyType
from typing import Any, Protocol

import mysql.connector

from select_fuzz.artifacts import CaseBundleWriter, JsonlWriter
from select_fuzz.config import NodeConfig, NodeRole
from select_fuzz.correctness import (
    CorrectnessRoundEngine,
    GeneratedRoundSource,
    JsonlEventSink,
    ProductionCoordinatorAdapter,
)
from select_fuzz.domain import RunRequest, stable_fingerprint
from select_fuzz.execution import (
    MySQLConnectorFactory,
    MySQLSetupRunner,
    NodeQueryRunner,
    QueryLimits,
    TriadCoordinator,
)
from select_fuzz.generation.coverage import CoverageLedger
from select_fuzz.generation.query_grammar import GrammarQueryGenerator, SelectGrammar
from select_fuzz.generation.query_scope import DEFAULT_QUERY_SCOPE
from select_fuzz.service import CorrectnessRunService, RunSummary


_SOCKET_PORTS = (44_061, 44_062, 44_063)
_SOCKET_USER_ENV = "SELECT_FUZZ_SOCKET_SOAK_USER"
_SOCKET_AUTH_ENV = "SELECT_FUZZ_SOCKET_SOAK_AUTH"
_SOCKET_AUTH_PLACEHOLDER = "local-socket-peer-auth"
_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")


class RunServiceLike(Protocol):
    def run(self, request: RunRequest, stop_event: Event) -> RunSummary: ...


class TimerLike(Protocol):
    def start(self) -> None: ...

    def cancel(self) -> None: ...


ConnectCallable = Callable[..., Any]
TimerFactory = Callable[[float, Callable[[], None]], TimerLike]


@dataclass(frozen=True, slots=True)
class SocketSoakConfig:
    sockets: tuple[Path, Path, Path]
    duration_seconds: float
    queries_per_round: int
    workers: int
    seed: int
    artifact_root: Path
    run_id: str
    max_rounds: int | None = None
    query_timeout_seconds: float = 15.0
    row_limit: int = 10_000
    byte_limit: int = 32 * 1024 * 1024
    min_rows_per_table: int = 10
    max_rows_per_table: int = 500
    full_thread_sql_log: bool = False

    def __post_init__(self) -> None:
        if len(set(self.sockets)) != 3:
            raise ValueError("sockets must contain three distinct paths")
        if (
            not isinstance(self.duration_seconds, (int, float))
            or isinstance(self.duration_seconds, bool)
            or not math.isfinite(self.duration_seconds)
            or self.duration_seconds < 0.001
        ):
            raise ValueError("duration_seconds must be a finite number of at least 0.001")
        for name, value in (
            ("queries_per_round", self.queries_per_round),
            ("workers", self.workers),
            ("row_limit", self.row_limit),
            ("byte_limit", self.byte_limit),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.workers > 64:
            raise ValueError("workers must not exceed 64")
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise TypeError("seed must be an integer")
        if self.max_rounds is not None and (
            not isinstance(self.max_rounds, int)
            or isinstance(self.max_rounds, bool)
            or self.max_rounds <= 0
        ):
            raise ValueError("max_rounds must be positive when supplied")
        if (
            not isinstance(self.query_timeout_seconds, (int, float))
            or isinstance(self.query_timeout_seconds, bool)
            or not 0 < self.query_timeout_seconds <= 300
        ):
            raise ValueError("query_timeout_seconds must be between zero and 300")
        for name, value in (
            ("min_rows_per_table", self.min_rows_per_table),
            ("max_rows_per_table", self.max_rows_per_table),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.min_rows_per_table > self.max_rows_per_table:
            raise ValueError("min_rows_per_table must not exceed max_rows_per_table")
        if _RUN_ID.fullmatch(self.run_id) is None:
            raise ValueError("run_id must be a safe lowercase artifact identifier")


@dataclass(frozen=True, slots=True)
class SocketSoakRuntime:
    service: RunServiceLike
    versions: Mapping[NodeRole, str]

    def __post_init__(self) -> None:
        if set(self.versions) != set(NodeRole):
            raise ValueError("versions must contain all three node roles")
        object.__setattr__(self, "versions", MappingProxyType(dict(self.versions)))


class UnixSocketConnectAdapter:
    """Translate the factory's synthetic ports into local socket connections."""

    def __init__(
        self,
        sockets_by_port: Mapping[int, Path],
        delegate: ConnectCallable = mysql.connector.connect,
    ) -> None:
        if set(sockets_by_port) != set(_SOCKET_PORTS):
            raise ValueError("socket mapping must contain exactly the three soak ports")
        if len(set(sockets_by_port.values())) != 3:
            raise ValueError("socket mapping paths must be distinct")
        self._sockets_by_port = dict(sockets_by_port)
        self._delegate = delegate

    def __call__(self, **kwargs: object) -> Any:
        port = kwargs.pop("port", None)
        if not isinstance(port, int) or isinstance(port, bool):
            raise TypeError("connector port must be an integer")
        try:
            socket_path = self._sockets_by_port[port]
        except KeyError as error:
            raise ValueError("connector port has no configured unix socket") from error
        if kwargs.get("user") != "root":
            raise ValueError("socket soak connections must use the local root account")
        kwargs.pop("host", None)
        # MySQLConnectorFactory requires late-resolved credential references. The
        # injected adapter consumes the in-memory placeholder before the connector
        # call, so no password is created, forwarded, written, or persisted.
        kwargs.pop("password", None)
        kwargs["unix_socket"] = str(socket_path)
        return self._delegate(**kwargs)


def build_nodes() -> tuple[NodeConfig, ...]:
    return tuple(
        NodeConfig(
            role=role,
            host="localhost",
            port=port,
            username_env=_SOCKET_USER_ENV,
            password_env=_SOCKET_AUTH_ENV,
        )
        for role, port in zip(NodeRole, _SOCKET_PORTS, strict=True)
    )


def build_connector(
    sockets: tuple[Path, Path, Path],
    *,
    connect: ConnectCallable = mysql.connector.connect,
) -> MySQLConnectorFactory:
    adapter = UnixSocketConnectAdapter(
        dict(zip(_SOCKET_PORTS, sockets, strict=True)),
        connect,
    )
    return MySQLConnectorFactory(
        environ={
            _SOCKET_USER_ENV: "root",
            _SOCKET_AUTH_ENV: _SOCKET_AUTH_PLACEHOLDER,
        },
        connect=adapter,
    )


def probe_mysql8041_versions(
    factory: MySQLConnectorFactory,
    nodes: Sequence[NodeConfig],
) -> dict[NodeRole, str]:
    versions: dict[NodeRole, str] = {}
    for node in nodes:
        with factory.control_session(node, "information_schema") as session:
            cursor = session.execute("SELECT VERSION()")
            try:
                rows = cursor.fetchmany(2)
            finally:
                cursor.close()
        if len(rows) != 1 or len(rows[0]) != 1 or not isinstance(rows[0][0], str):
            raise RuntimeError(f"{node.role.value} returned an invalid MySQL version row")
        version = rows[0][0]
        if version.split("-", 1)[0] != "8.0.41":
            raise RuntimeError(f"{node.role.value} must run exact MySQL 8.0.41, observed {version}")
        versions[node.role] = version
    if set(versions) != set(NodeRole):
        raise RuntimeError("version probe did not cover all three roles")
    return versions


def build_runtime(
    config: SocketSoakConfig,
    connect: ConnectCallable = mysql.connector.connect,
) -> SocketSoakRuntime:
    nodes = build_nodes()
    factory = build_connector(config.sockets, connect=connect)
    versions = probe_mysql8041_versions(factory, nodes)
    config.artifact_root.mkdir(parents=True, exist_ok=True)
    event_writer = JsonlWriter(config.artifact_root / "events.jsonl")
    artifacts = CaseBundleWriter(
        config.artifact_root,
        events=event_writer,
        full_thread_sql_log=config.full_thread_sql_log,
        query_attempt_json_log=False,
    )
    coverage_path = config.artifact_root / "coverage.json"
    coverage = (
        CoverageLedger.load(coverage_path)
        if coverage_path.is_file()
        else CoverageLedger(coverage_path)
    )
    source = GeneratedRoundSource(
        coverage,
        min_rows_per_table=config.min_rows_per_table,
        max_rows_per_table=config.max_rows_per_table,
        grammar_query_generator=GrammarQueryGenerator(SelectGrammar.default()),
        query_scope=DEFAULT_QUERY_SCOPE,
    )
    triad = TriadCoordinator(
        nodes,
        setup_runner=MySQLSetupRunner(factory),
        query_runner=NodeQueryRunner(factory),
        session_factory=factory,
    )
    fingerprints = {
        node.role: stable_fingerprint(
            {
                "role": node.role.value,
                "socket": str(socket_path),
                "transport": "unix_socket",
            }
        )
        for node, socket_path in zip(nodes, config.sockets, strict=True)
    }
    engine = CorrectnessRoundEngine(
        source,
        ProductionCoordinatorAdapter(triad),
        artifacts,
        coverage,
        QueryLimits(
            config.query_timeout_seconds,
            config.row_limit,
            config.byte_limit,
        ),
        configuration_fingerprints=fingerprints,
    )
    return SocketSoakRuntime(
        CorrectnessRunService(engine, JsonlEventSink(event_writer)),
        versions,
    )


def _finding_bundle_count(root: Path) -> int:
    findings_root = root / "findings"
    if not findings_root.is_dir():
        return 0
    return sum(
        child.is_dir() and not child.name.startswith(".") for child in findings_root.iterdir()
    )


def run_socket_soak(
    config: SocketSoakConfig,
    *,
    connect: ConnectCallable = mysql.connector.connect,
    runtime_factory: Callable[
        [SocketSoakConfig, ConnectCallable], SocketSoakRuntime
    ] = build_runtime,
    timer_factory: TimerFactory = Timer,
    monotonic: Callable[[], float] = time.monotonic,
    stop_event: Event | None = None,
) -> dict[str, object]:
    runtime = runtime_factory(config, connect)
    requested_stop = stop_event or Event()
    deadline_reached = Event()

    def expire() -> None:
        deadline_reached.set()
        requested_stop.set()

    timer = timer_factory(config.duration_seconds, expire)
    request = RunRequest(
        run_id=config.run_id,
        mode="correctness",
        seed=config.seed,
        workers=config.workers,
        rounds=config.max_rounds,
        queries_per_round=config.queries_per_round,
    )
    started = monotonic()
    timer.start()
    try:
        summary = runtime.service.run(request, requested_stop)
    finally:
        timer.cancel()
    elapsed = max(0.0, monotonic() - started)
    status = (
        "duration_elapsed"
        if deadline_reached.is_set()
        else "stopped"
        if requested_stop.is_set()
        else "completed"
    )
    return {
        "artifact_root": str(config.artifact_root),
        "coverage_path": str(config.artifact_root / "coverage.json"),
        "duration_seconds": config.duration_seconds,
        "elapsed_seconds": round(elapsed, 6),
        "events_path": str(config.artifact_root / "events.jsonl"),
        "excluded_feature_ids": sorted(DEFAULT_QUERY_SCOPE.excluded_feature_ids),
        "excluded_profiles": sorted(DEFAULT_QUERY_SCOPE.excluded_profile_reasons),
        "finding_bundles_total": _finding_bundle_count(config.artifact_root),
        "findings": summary.findings,
        "over_budget": summary.over_budget,
        "queries_completed": summary.queries_completed,
        "queries_per_round": config.queries_per_round,
        "rejected": summary.rejected,
        "rounds_completed": summary.rounds_completed,
        "run_id": summary.run_id,
        "seed": config.seed,
        "sql_log_directory": str(config.artifact_root / "sql"),
        "sql_log_paths": [
            str(path) for path in sorted((config.artifact_root / "sql").glob("worker-*.sql"))
        ],
        "status": status,
        "stopped": summary.stopped,
        "versions": {role.value: runtime.versions[role] for role in NodeRole},
        "workers": config.workers,
    }


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be nonnegative")
    return parsed


def _duration(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0.001:
        raise argparse.ArgumentTypeError("duration must be at least 0.001 seconds")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sockets",
        nargs="+",
        required=True,
        metavar="SOCKET",
        help=(
            "three paths in baseline, custom_off, custom_on order; either separate "
            "arguments or one comma-separated value"
        ),
    )
    parser.add_argument("--duration-seconds", type=_duration, default=1800.0)
    parser.add_argument("--queries-per-round", type=_positive_int, default=100)
    parser.add_argument("--workers", type=_positive_int, default=1)
    parser.add_argument("--seed", type=int, default=20260714)
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path("artifacts/mysql8041-socket-soak"),
    )
    parser.add_argument("--run-id")
    parser.add_argument("--max-rounds", type=_positive_int)
    parser.add_argument("--query-timeout-seconds", type=_duration, default=15.0)
    parser.add_argument("--row-limit", type=_positive_int, default=10_000)
    parser.add_argument("--byte-limit", type=_positive_int, default=32 * 1024 * 1024)
    parser.add_argument("--min-rows-per-table", type=_nonnegative_int, default=10)
    parser.add_argument("--max-rows-per-table", type=_nonnegative_int, default=500)
    parser.add_argument(
        "--full-thread-sql-log",
        action="store_true",
        help="append all SQL executed by each fuzz worker to worker-NNN.sql",
    )
    return parser


def _socket_paths(values: Sequence[str]) -> tuple[Path, Path, Path]:
    flattened = (
        tuple(part for part in values[0].split(",") if part) if len(values) == 1 else tuple(values)
    )
    if len(flattened) != 3:
        raise ValueError("--sockets requires exactly three paths")
    resolved = tuple(Path(value).expanduser().resolve() for value in flattened)
    return (resolved[0], resolved[1], resolved[2])


def _validate_socket_files(paths: Sequence[Path]) -> None:
    for path in paths:
        if not path.is_socket():
            raise ValueError(f"configured path is not a unix socket: {path}")


def _default_run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dt%H%M%sz")
    return f"mysql8041-socket-soak-{timestamp}"


def _strict_json(document: Mapping[str, object]) -> str:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        sockets = _socket_paths(args.sockets)
        _validate_socket_files(sockets)
        config = SocketSoakConfig(
            sockets=sockets,
            duration_seconds=args.duration_seconds,
            queries_per_round=args.queries_per_round,
            workers=args.workers,
            seed=args.seed,
            artifact_root=args.artifact_root.resolve(),
            run_id=args.run_id or _default_run_id(),
            max_rounds=args.max_rounds,
            query_timeout_seconds=args.query_timeout_seconds,
            row_limit=args.row_limit,
            byte_limit=args.byte_limit,
            min_rows_per_table=args.min_rows_per_table,
            max_rows_per_table=args.max_rows_per_table,
            full_thread_sql_log=args.full_thread_sql_log,
        )
    except (TypeError, ValueError) as error:
        parser.error(str(error))
    stop_event = Event()
    previous_handlers: dict[int, Any] = {}

    def request_stop(signum: int, frame: object) -> None:
        stop_event.set()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)
        summary = run_socket_soak(config, stop_event=stop_event)
    except Exception as error:
        print(
            _strict_json(
                {
                    "artifact_root": str(config.artifact_root),
                    "error_message": str(error),
                    "error_type": type(error).__name__,
                    "run_id": config.run_id,
                    "status": "failed",
                }
            )
        )
        return 1
    finally:
        for stored_signum, handler in previous_handlers.items():
            signal.signal(stored_signum, handler)
    print(_strict_json(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
