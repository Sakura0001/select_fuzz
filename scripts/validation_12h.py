#!/usr/bin/env python3
"""Run the resumable MySQL 8.0.41 official-source coverage validator."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
import re
import shlex
import signal
from threading import Event
from collections.abc import Sequence

from select_fuzz.validation.report import build_coverage_report, write_validation_report
from select_fuzz.validation.runtime import (
    ProductionValidationConfig,
    run_production_validation,
)


_DURATION = re.compile(r"^(?P<value>[0-9]+(?:\.[0-9]+)?)(?P<unit>[smh])$")


def _parse_duration(value: str, *, allow_zero: bool) -> float:
    match = _DURATION.fullmatch(value.strip().lower())
    if match is None:
        raise argparse.ArgumentTypeError("duration must use s, m, or h (for example 30m)")
    amount = float(match.group("value"))
    if amount < 0 or (amount == 0 and not allow_zero):
        raise argparse.ArgumentTypeError("duration must be positive")
    return amount * {"s": 1, "m": 60, "h": 3600}[match.group("unit")]


def parse_duration(value: str) -> float:
    return _parse_duration(value, allow_zero=False)


def parse_freeze_duration(value: str) -> float:
    return _parse_duration(value, allow_zero=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=parse_duration, default=parse_duration("12h"))
    parser.add_argument("--checkpoint", type=parse_duration, default=parse_duration("30m"))
    parser.add_argument(
        "--freeze", type=parse_freeze_duration, default=parse_freeze_duration("30m")
    )
    parser.add_argument("--run-id", default="mysql-8041-validation")
    parser.add_argument("--output", type=Path, default=Path("artifacts/validation"))
    parser.add_argument("--catalog-path", type=Path)
    parser.add_argument("--seed-url", action="append", default=[])
    parser.add_argument("--regression-command", action="append", default=[])
    parser.add_argument("--regression-timeout", type=parse_duration, default=300.0)
    parser.add_argument("--max-consecutive-errors", type=int, default=5)
    parser.add_argument("--fault-seed", type=int, default=0)
    parser.add_argument(
        "--fault-command",
        action="append",
        default=[],
        metavar="KIND=ARGV",
        help="operator-controlled no-shell fault command for a scheduled FaultKind",
    )
    parser.add_argument(
        "--fault-probe",
        action="append",
        default=[],
        metavar="KIND=ARGV",
        help="no-shell recovery probe that must succeed after the matching fault",
    )
    parser.add_argument(
        "--mysql-connection-probe",
        metavar="ARGV",
        help="no-shell command that prints the current MySQL connection count",
    )
    parser.add_argument("--max-epochs", type=int, help="test/operator bounded run; unset for 12h")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.checkpoint > args.duration:
        parser.error("checkpoint interval cannot exceed duration")
    if args.freeze > args.duration:
        parser.error("freeze window cannot exceed duration")
    if args.max_epochs is not None and args.max_epochs <= 0:
        parser.error("max-epochs must be positive")
    if args.max_consecutive_errors <= 0:
        parser.error("max-consecutive-errors must be positive")

    if args.dry_run:
        report = build_coverage_report(
            run_id=args.run_id,
            sources=(),
            signatures=(),
            results=(),
            gaps=(),
            checkpoints=(),
            generated_at=datetime.now(UTC),
        )
        write_validation_report(report, args.output)
        print(
            f"dry-run complete: duration={args.duration:g}s "
            f"checkpoint={args.checkpoint:g}s output={args.output}"
        )
        return 0

    commands = tuple(tuple(shlex.split(command)) for command in args.regression_command)
    if any(not command for command in commands):
        parser.error("regression-command cannot be empty")
    allowed_faults = {
        "connection_reset",
        "worker_termination",
        "report_write_failure",
        "query_timeout",
    }
    def parse_fault_argv(
        configured_values: list[str], option: str
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        parsed: list[tuple[str, tuple[str, ...]]] = []
        for configured in configured_values:
            if "=" not in configured:
                parser.error(f"{option} must use KIND=ARGV")
            kind, raw_command = configured.split("=", 1)
            argv_command = tuple(shlex.split(raw_command))
            if kind not in allowed_faults or not argv_command:
                parser.error(f"{option} has an unknown kind or empty argv")
            parsed.append((kind, argv_command))
        return tuple(parsed)

    fault_commands = parse_fault_argv(args.fault_command, "fault-command")
    fault_probes = parse_fault_argv(args.fault_probe, "fault-probe")
    mysql_probe = (
        ()
        if args.mysql_connection_probe is None
        else tuple(shlex.split(args.mysql_connection_probe))
    )
    if args.mysql_connection_probe is not None and not mysql_probe:
        parser.error("mysql-connection-probe cannot be empty")
    config = ProductionValidationConfig(
        run_id=args.run_id,
        output_dir=args.output,
        duration_s=args.duration,
        checkpoint_s=args.checkpoint,
        freeze_s=args.freeze,
        max_epochs=args.max_epochs,
        max_consecutive_errors=args.max_consecutive_errors,
        seed_urls=tuple(args.seed_url),
        catalog_path=args.catalog_path,
        regression_commands=commands,
        regression_timeout_s=args.regression_timeout,
        fault_seed=args.fault_seed,
        fault_commands=fault_commands,
        fault_probe_commands=fault_probes,
        mysql_connection_probe_command=mysql_probe,
    )
    stop_event = Event()

    def request_stop(signum: int, frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    result = run_production_validation(config, stop_event=stop_event)
    print(
        f"validation complete: epochs={result.summary.epochs_completed} "
        f"signatures={result.summary.unique_signatures} gaps={result.summary.gaps} "
        f"elapsed={result.summary.elapsed_s:g}s"
    )
    return 2 if result.summary.aborted_reason else 0


if __name__ == "__main__":
    raise SystemExit(main())
