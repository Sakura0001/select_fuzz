#!/usr/bin/env python3
"""Run a resumable local MySQL SELECT-grammar optimization campaign."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import json
from pathlib import Path
import signal
from threading import Event
from typing import Any

from select_fuzz.grammar_optimization import (
    GrammarOptimizationConfig,
    run_grammar_optimization_campaign,
)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _percent(value: str) -> int:
    parsed = int(value)
    if not 0 <= parsed <= 100:
        raise argparse.ArgumentTypeError("value must be from 0 to 100")
    return parsed


def _json(document: Mapping[str, object]) -> str:
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument(
        "--grammar",
        type=Path,
        default=Path("catalog/mysql-8.0.41-select.grammar.yy"),
    )
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--iterations", type=_positive_int, default=50)
    parser.add_argument("--iteration-seconds", type=_positive_float, default=180.0)
    parser.add_argument("--query-timeout-seconds", type=_positive_float, default=10.0)
    parser.add_argument("--rows-per-table", type=_positive_int, default=8)
    parser.add_argument("--row-limit", type=_positive_int, default=10_000)
    parser.add_argument("--byte-limit", type=_positive_int, default=32 * 1024 * 1024)
    parser.add_argument("--compatible-type-percent", type=_percent, default=80)
    parser.add_argument("--seed", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = GrammarOptimizationConfig(
        socket=args.socket,
        grammar_path=args.grammar,
        artifact_root=args.artifact_root,
        iterations=args.iterations,
        iteration_seconds=args.iteration_seconds,
        query_timeout_seconds=args.query_timeout_seconds,
        rows_per_table=args.rows_per_table,
        row_limit=args.row_limit,
        byte_limit=args.byte_limit,
        compatible_type_percent=args.compatible_type_percent,
        seed=args.seed,
    )
    stop_event = Event()
    previous_handlers: dict[int, Any] = {}

    def request_stop(signum: int, frame: object) -> None:
        del signum, frame
        stop_event.set()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)
        summary = run_grammar_optimization_campaign(
            config,
            stop_event=stop_event,
            on_iteration=lambda document: print(_json(document), flush=True),
        )
    finally:
        for stored_signum, handler in previous_handlers.items():
            signal.signal(stored_signum, handler)
    print(_json(summary), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
