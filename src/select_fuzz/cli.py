"""Command-line entry point for Select Fuzz."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
import json
from pathlib import Path
import signal
from threading import Event, Timer
import time
from typing import Any, Protocol

import typer
import uvicorn

from select_fuzz.artifacts import ArtifactReader, HtmlReportBuilder
from select_fuzz.api import create_app
from select_fuzz.api.replays import ProductionReplayExecutor
from select_fuzz.api.run_state import RunStore
from select_fuzz.api.supervisor import SelectFuzzCommandBuilder, SubprocessSupervisor
from select_fuzz.config import (
    AppConfig,
    ConfigLoadError,
    PreflightReport,
    RunMode,
    load_config,
)
from select_fuzz.cleanup import (
    CleanupReport,
    CleanupService,
    ManagedDatabaseError,
    build_cleanup_service,
)
from select_fuzz.domain import RunRequest, deterministic_id
from select_fuzz.doctor import build_doctor
from select_fuzz.replay import (
    ReplayResult,
    ReplayService,
    ReplayStatus,
    build_replay_service,
)
from select_fuzz.regression import write_seed_corpus
from select_fuzz.modes import MODE_REGISTRY, ModeFactory, ModeRunner


app = typer.Typer(
    name="select-fuzz",
    no_args_is_help=True,
    help="MySQL correctness, performance, and concurrent read/write fuzz testing.",
)


MODE_RUNNERS: dict[str, ModeFactory] = {
    name: definition.factory for name, definition in MODE_REGISTRY.items()
}


class DoctorRunner(Protocol):
    def run(self) -> PreflightReport: ...


DoctorFactory = Callable[[AppConfig], DoctorRunner]
DOCTOR_FACTORY: DoctorFactory = build_doctor
ReplayFactory = Callable[[AppConfig, Path], ReplayService]
REPLAY_FACTORY: ReplayFactory = build_replay_service
CleanupFactory = Callable[[AppConfig], CleanupService]
CLEANUP_FACTORY: CleanupFactory = build_cleanup_service


@app.command("run")
def run_command(
    mode: str = typer.Option("correctness", "--mode"),
    config: Path = typer.Option(..., "--config", exists=True, dir_okay=False),
    rounds: int | None = typer.Option(None, "--rounds", min=1),
    seed: int = typer.Option(0, "--seed"),
    workers: int | None = typer.Option(None, "--workers", min=1),
    queries_per_round: int | None = typer.Option(None, "--queries-per-round", min=1),
    duration_seconds: float | None = typer.Option(None, "--duration-seconds", min=0.001),
    timeout_seconds: float | None = typer.Option(None, "--timeout-seconds", min=0.001, max=300),
    degradation_ratio: float | None = typer.Option(None, "--degradation-ratio", min=0),
    data_rows_min: int | None = typer.Option(None, "--data-rows-min", min=1),
    data_rows_max: int | None = typer.Option(None, "--data-rows-max", min=1),
    databases: int | None = typer.Option(None, "--databases", min=1, max=32),
    writer_threads_per_database: int | None = typer.Option(
        None,
        "--writer-threads-per-database",
        min=1,
        max=64,
    ),
    reader_threads_per_database: int | None = typer.Option(
        None,
        "--reader-threads-per-database",
        min=3,
        max=192,
    ),
    full_thread_sql_log: bool | None = typer.Option(
        None,
        "--full-thread-sql-log/--no-full-thread-sql-log",
        help="Append every executed SQL statement to one sourceable file per worker.",
    ),
    artifacts: Path = typer.Option(Path("artifacts"), "--artifacts"),
) -> None:
    """Run one registered correctness, performance, or concurrent fuzz mode."""

    try:
        selected_mode = RunMode(mode)
        overrides: dict[str, object] = {
            "mode": selected_mode.value,
            "workers": workers,
            "queries_per_round": queries_per_round,
            "full_thread_sql_log": full_thread_sql_log,
        }
        if selected_mode is RunMode.CORRECTNESS:
            overrides.update(
                {
                    "correctness.timeout_seconds": timeout_seconds,
                    "correctness.min_rows_per_table": data_rows_min,
                    "correctness.max_rows_per_table": data_rows_max,
                }
            )
        elif selected_mode is RunMode.PERFORMANCE:
            overrides.update(
                {
                    "performance.formal_timeout_seconds": timeout_seconds,
                    "performance.regression_threshold": degradation_ratio,
                    "performance.initial_table_rows": data_rows_min,
                    "performance.max_table_rows": data_rows_max,
                }
            )
        else:
            overrides.update(
                {
                    "fuzz.databases": databases,
                    "fuzz.writer_threads_per_database": writer_threads_per_database,
                    "fuzz.reader_threads_per_database": reader_threads_per_database,
                    "fuzz.query_timeout_seconds": timeout_seconds,
                    "fuzz.initial_rows_per_table": data_rows_min,
                    "fuzz.max_rows_per_database": data_rows_max,
                }
            )
        loaded = load_config(config, cli=overrides)
    except (ValueError, ConfigLoadError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from None
    factory = MODE_RUNNERS.get(selected_mode.value)
    if factory is None:
        typer.echo(f"mode is not registered: {selected_mode.value}", err=True)
        raise typer.Exit(code=2)
    if selected_mode is RunMode.CORRECTNESS:
        request_workers = loaded.correctness.workers
        request_queries = loaded.correctness.queries_per_round
    elif selected_mode is RunMode.PERFORMANCE:
        request_workers = loaded.performance.workers
        request_queries = loaded.performance.queries_per_round
    else:
        request_workers = 1
        request_queries = 1
    run_id = deterministic_id("run", selected_mode.value, seed, time.time_ns())
    request = RunRequest(
        run_id=run_id,
        mode=selected_mode.value,
        seed=seed,
        workers=request_workers,
        rounds=rounds,
        queries_per_round=request_queries,
    )
    stop_event = Event()
    timer = None if duration_seconds is None else Timer(duration_seconds, stop_event.set)
    previous_handlers: dict[int, Any] = {}

    def request_stop(signum: int, frame: object) -> None:
        stop_event.set()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)
        if timer is not None:
            timer.start()
        summary = factory(loaded, artifacts).run(request, stop_event)
    except Exception as error:
        if selected_mode is RunMode.FUZZ:
            message = f"运行失败：{type(error).__name__}：{error}"
        else:
            message = f"run failed: {type(error).__name__}: {error}"
        typer.echo(message, err=True)
        raise typer.Exit(code=1) from None
    finally:
        if timer is not None:
            timer.cancel()
        for stored_signum, handler in previous_handlers.items():
            signal.signal(stored_signum, handler)
    typer.echo(json.dumps(asdict(summary), sort_keys=True, separators=(",", ":")))
    if summary.findings > 0:
        raise typer.Exit(code=1)


@app.command("doctor")
def doctor_command(
    mode: str = typer.Option("correctness", "--mode"),
    config: Path = typer.Option(..., "--config", exists=True, dir_okay=False),
) -> None:
    """Validate all configured primary/replica endpoints without exposing credentials."""

    try:
        selected_mode = RunMode(mode)
        loaded = load_config(config, cli={"mode": selected_mode.value})
    except (ValueError, ConfigLoadError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from None
    report = DOCTOR_FACTORY(loaded).run()
    document = {
        "can_start": report.can_start,
        "fatals": [issue.model_dump(mode="json") for issue in report.fatals],
        "warnings": [issue.model_dump(mode="json") for issue in report.warnings],
    }
    typer.echo(json.dumps(document, sort_keys=True, separators=(",", ":")))
    if not report.can_start:
        raise typer.Exit(code=1)


@app.command("report")
def report_command(
    artifacts: Path = typer.Option(Path("artifacts"), "--artifacts"),
    output: Path = typer.Option(Path("artifacts/report.html"), "--output"),
) -> None:
    """Rebuild a static HTML report from the authoritative artifact log."""

    try:
        written = HtmlReportBuilder(ArtifactReader(artifacts)).write(output)
    except Exception as error:
        typer.echo(f"report failed: {type(error).__name__}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(str(written))


@app.command("replay")
def replay_command(
    config: Path = typer.Option(..., "--config", exists=True, dir_okay=False),
    artifacts: Path = typer.Option(Path("artifacts"), "--artifacts"),
    finding: str = typer.Option(..., "--finding"),
) -> None:
    """Replay one stored correctness finding on a fresh three-node database."""

    try:
        loaded = load_config(config, cli={"mode": RunMode.CORRECTNESS.value})
        result: ReplayResult = REPLAY_FACTORY(loaded, artifacts).replay(finding)
    except Exception as error:
        typer.echo(f"replay failed: {type(error).__name__}", err=True)
        raise typer.Exit(code=1) from None
    oracle_verdict = None if result.replay_verdict is None else result.replay_verdict.value
    effective_verdict = (
        result.replay_classification if result.replay_classification is not None else oracle_verdict
    )
    document = {
        "case_id": result.case_id,
        "database": result.database,
        "original_verdict": result.original_verdict,
        "replay_verdict": effective_verdict,
        "oracle_verdict": oracle_verdict,
        "status": result.status.value,
    }
    typer.echo(json.dumps(document, sort_keys=True, separators=(",", ":")))
    if result.status is not ReplayStatus.REPRODUCED:
        raise typer.Exit(code=1)


@app.command("regression-seeds")
def regression_seeds_command(
    output: Path = typer.Option(Path("tests/regression/seeds.json"), "--output"),
    seed: int = typer.Option(20260712, "--seed"),
) -> None:
    """Freeze versioned generator seeds and expected coverage tags."""

    try:
        written = write_seed_corpus(output, seed=seed)
    except Exception as error:
        typer.echo(f"regression seed write failed: {type(error).__name__}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(str(written))


@app.command("serve")
def serve_command(
    config: Path = typer.Option(..., "--config", exists=True, dir_okay=False),
    artifacts: Path = typer.Option(Path("artifacts"), "--artifacts"),
    state: Path | None = typer.Option(None, "--state", dir_okay=False),
    spa_dist: Path = typer.Option(Path("frontend/dist"), "--spa-dist"),
    port: int = typer.Option(8765, "--port", min=1, max=65535),
) -> None:
    """Run the loopback FastAPI and React control plane."""

    try:
        loaded = load_config(config)
        if not (spa_dist / "index.html").is_file():
            raise ValueError("SPA build is unavailable; run npm --prefix frontend run build")
        state_path = state or artifacts / "control-plane.sqlite3"
        store = RunStore(state_path)
        supervisor = SubprocessSupervisor(
            store,
            SelectFuzzCommandBuilder(config, artifacts),
        )
        correctness_config = loaded.model_copy(update={"mode": RunMode.CORRECTNESS})
        replay_executor = ProductionReplayExecutor(
            build_replay_service(correctness_config, artifacts)
        )
        api = create_app(
            state_path=state_path,
            artifact_root=artifacts,
            supervisor=supervisor,
            replay_executor=replay_executor,
            spa_dist=spa_dist,
        )
    except Exception as error:
        typer.echo(f"serve failed: {type(error).__name__}", err=True)
        raise typer.Exit(code=1) from None
    uvicorn.run(api, host="127.0.0.1", port=port, log_level="warning")


@app.command("cleanup")
def cleanup_command(
    config: Path = typer.Option(..., "--config", exists=True, dir_okay=False),
    databases: list[str] = typer.Option(..., "--database"),
    execute: bool = typer.Option(False, "--execute"),
) -> None:
    """Plan or explicitly drop retained managed databases on all three nodes."""

    try:
        loaded = load_config(config)
        report: CleanupReport = CLEANUP_FACTORY(loaded).run(tuple(databases), execute=execute)
    except (ConfigLoadError, ManagedDatabaseError, ValueError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from None
    except Exception as error:
        typer.echo(f"cleanup failed: {type(error).__name__}", err=True)
        raise typer.Exit(code=1) from None
    document = {
        "databases": list(report.databases),
        "execute": report.execute,
        "nodes": [
            {
                "database": item.database,
                "dropped": item.dropped,
                "error_type": item.error_type,
                "role": item.role.value,
            }
            for item in report.nodes
        ],
        "success": report.success,
    }
    typer.echo(json.dumps(document, sort_keys=True, separators=(",", ":")))
    if not report.success:
        raise typer.Exit(code=1)


__all__ = [
    "DOCTOR_FACTORY",
    "CLEANUP_FACTORY",
    "MODE_RUNNERS",
    "REPLAY_FACTORY",
    "DoctorFactory",
    "CleanupFactory",
    "ModeFactory",
    "ModeRunner",
    "app",
    "cleanup_command",
    "doctor_command",
    "run_command",
    "replay_command",
    "regression_seeds_command",
    "report_command",
    "serve_command",
]
