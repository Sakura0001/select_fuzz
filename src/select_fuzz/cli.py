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

from select_fuzz.artifacts import ArtifactReader, HtmlReportBuilder
from select_fuzz.config import (
    AppConfig,
    ConfigLoadError,
    PreflightReport,
    RunMode,
    load_config,
)
from select_fuzz.correctness import build_correctness_runner
from select_fuzz.domain import RunRequest, deterministic_id
from select_fuzz.doctor import build_doctor
from select_fuzz.replay import (
    ReplayResult,
    ReplayService,
    ReplayStatus,
    build_replay_service,
)
from select_fuzz.service import RunSummary


app = typer.Typer(
    name="select-fuzz",
    no_args_is_help=True,
    help="Differential correctness and performance testing for MySQL SELECT queries.",
)


class ModeRunner(Protocol):
    def run(self, request: RunRequest, stop_event: Event) -> RunSummary: ...


ModeFactory = Callable[[AppConfig, Path], ModeRunner]
MODE_RUNNERS: dict[str, ModeFactory] = {"correctness": build_correctness_runner}


class DoctorRunner(Protocol):
    def run(self) -> PreflightReport: ...


DoctorFactory = Callable[[AppConfig], DoctorRunner]
DOCTOR_FACTORY: DoctorFactory = build_doctor
ReplayFactory = Callable[[AppConfig, Path], ReplayService]
REPLAY_FACTORY: ReplayFactory = build_replay_service


@app.command("run")
def run_command(
    mode: str = typer.Option("correctness", "--mode"),
    config: Path = typer.Option(..., "--config", exists=True, dir_okay=False),
    rounds: int | None = typer.Option(None, "--rounds", min=1),
    seed: int = typer.Option(0, "--seed"),
    workers: int | None = typer.Option(None, "--workers", min=1),
    queries_per_round: int | None = typer.Option(
        None, "--queries-per-round", min=1
    ),
    duration_seconds: float | None = typer.Option(
        None, "--duration-seconds", min=0.001
    ),
    artifacts: Path = typer.Option(Path("artifacts"), "--artifacts"),
) -> None:
    """Run one registered correctness or performance mode."""

    try:
        selected_mode = RunMode(mode)
        loaded = load_config(
            config,
            cli={
                "mode": selected_mode.value,
                "workers": workers,
                "queries_per_round": queries_per_round,
            },
        )
    except (ValueError, ConfigLoadError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from None
    factory = MODE_RUNNERS.get(selected_mode.value)
    if factory is None:
        typer.echo(f"mode is not registered: {selected_mode.value}", err=True)
        raise typer.Exit(code=2)
    section = (
        loaded.correctness
        if selected_mode is RunMode.CORRECTNESS
        else loaded.performance
    )
    run_id = deterministic_id(
        "run", selected_mode.value, seed, time.time_ns()
    )
    request = RunRequest(
        run_id=run_id,
        mode=selected_mode.value,
        seed=seed,
        workers=section.workers,
        rounds=rounds,
        queries_per_round=section.queries_per_round,
    )
    stop_event = Event()
    timer = (
        None
        if duration_seconds is None
        else Timer(duration_seconds, stop_event.set)
    )
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
    finally:
        if timer is not None:
            timer.cancel()
        for stored_signum, handler in previous_handlers.items():
            signal.signal(stored_signum, handler)
    typer.echo(json.dumps(asdict(summary), sort_keys=True, separators=(",", ":")))


def _pending_command(name: str) -> Callable[[], None]:
    def command() -> None:
        typer.echo(f"{name} is not implemented yet")
        raise typer.Exit(code=2)

    command.__name__ = name
    return command


@app.command("doctor")
def doctor_command(
    mode: str = typer.Option("correctness", "--mode"),
    config: Path = typer.Option(..., "--config", exists=True, dir_okay=False),
) -> None:
    """Validate all three nodes without exposing credentials."""

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
    document = {
        "case_id": result.case_id,
        "database": result.database,
        "original_verdict": result.original_verdict,
        "replay_verdict": (
            None if result.replay_verdict is None else result.replay_verdict.value
        ),
        "status": result.status.value,
    }
    typer.echo(json.dumps(document, sort_keys=True, separators=(",", ":")))
    if result.status is not ReplayStatus.REPRODUCED:
        raise typer.Exit(code=1)


for _command_name in ("serve", "cleanup"):
    app.command(name=_command_name)(_pending_command(_command_name))


__all__ = [
    "DOCTOR_FACTORY",
    "MODE_RUNNERS",
    "REPLAY_FACTORY",
    "DoctorFactory",
    "ModeFactory",
    "ModeRunner",
    "app",
    "doctor_command",
    "run_command",
    "replay_command",
    "report_command",
]
