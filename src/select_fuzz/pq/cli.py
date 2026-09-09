"""CLI for bounded PQ hardening runs on explicitly configured test endpoints."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast, Literal
from uuid import uuid4

import mysql.connector
import typer

from select_fuzz.pq.config import Endpoint, PQConfig, Tolerance, require_serial_endpoint

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False,
                  help="PQ hardening: Fast / Compare / Performance modes.")


def _build_config(
    *, mode: str, host: str, port: int, user: str, password: str, database: str,
    queries: int, rows: int, tables: int, seed: int, dop: int, repeats: int,
    serial_endpoint: Endpoint | None = None,
) -> PQConfig:
    return PQConfig(
        endpoint=Endpoint(host=host, port=port, user=user, password=password, database=database),
        database=database, dop_on=dop, queries_per_round=queries, seed=seed,
        materialize_rows=rows, materialize_tables=tables,
        mode=cast(Literal["fast", "compare", "performance"], mode), perf_repeats=repeats,
        serial_endpoint=serial_endpoint,
    )


@app.command()
def run(
    mode: str = typer.Option("fast", "--mode", help="fast | compare | performance"),
    host: str = typer.Option("127.0.0.1", "--host", envvar="SELECT_FUZZ_PQ_HOST"),
    port: int = typer.Option(3306, "--port"),
    user: str = typer.Option("root", "--user", envvar="SELECT_FUZZ_MYSQL_USER"),
    password: str = typer.Option("", "--password", envvar="SELECT_FUZZ_MYSQL_PASSWORD"),
    serial_host: str | None = typer.Option(None, "--serial-host", envvar="SELECT_FUZZ_SERIAL_HOST",
        help="Required preconfigured serial endpoint; runtime parameters remain unchanged"),
    serial_port: int = typer.Option(3306, "--serial-port"),
    serial_user: str = typer.Option("root", "--serial-user", envvar="SELECT_FUZZ_SERIAL_MYSQL_USER"),
    serial_password: str = typer.Option("", "--serial-password",
        envvar="SELECT_FUZZ_SERIAL_MYSQL_PASSWORD"),
    database: str | None = typer.Option(None, "--database", help="New test schema; auto-named by default"),
    queries: int = typer.Option(100, "--queries", help="Logical SQL slots (each has a bounded retry budget)"),
    rows: int = typer.Option(20000, "--rows"),
    tables: int = typer.Option(4, "--tables"),
    seed: int = typer.Option(42, "--seed"),
    dop: int = typer.Option(4, "--dop", help="Expected preconfigured DOP; observed plans determine labels"),
    repeats: int = typer.Option(5, "--repeats"),
    warmups: int = typer.Option(1, "--warmups"),
    dop_sweep: str = typer.Option("", "--dop-sweep",
        help="Compatibility option: only the single expected --dop value; sweeps are disabled"),
    max_attempts: int = typer.Option(12, "--max-attempts", help="Maximum generated candidates per slot"),
    timeout_seconds: float = typer.Option(30.0, "--timeout-seconds",
        help="Client I/O and result-transfer budget; server timeout remains preconfigured"),
    duration_seconds: float = typer.Option(600.0, "--duration-seconds", help="Run deadline; checked between statements"),
    row_limit: int = typer.Option(50000, "--row-limit", help="Retained rows; over-budget results are inconclusive"),
    decimal_absolute: str = typer.Option("1e-9", "--decimal-absolute", help="Expression columns only; 0 for exact"),
    decimal_relative: str = typer.Option("0", "--decimal-relative"),
    float_absolute: float = typer.Option(1e-6, "--float-absolute"),
    float_relative: float = typer.Option(1e-5, "--float-relative"),
    double_absolute: float = typer.Option(1e-12, "--double-absolute"),
    double_relative: float = typer.Option(1e-9, "--double-relative"),
    multiset_budget: int = typer.Option(1000000, "--multiset-budget"),
    reference_host: str | None = typer.Option(None, "--reference-host", help="Optional MySQL endpoint (compare only)"),
    reference_port: int = typer.Option(3306, "--reference-port"),
    reference_user: str = typer.Option("root", "--reference-user"),
    reference_password: str = typer.Option("", "--reference-password", envvar="SELECT_FUZZ_REFERENCE_MYSQL_PASSWORD"),
    artifacts: Path = typer.Option(Path("artifacts/pq"), "--artifacts", help="Parent directory; each run gets a new subdirectory"),
) -> None:
    """Create a dedicated fixture, test PQ-qualified SQL, and save evidence."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:6]
    database = database or "pq_" + stamp
    try:
        config = _build_config(
            mode=mode, host=host, port=port, user=user, password=password,
            database=database, queries=queries, rows=rows, tables=tables,
            seed=seed, dop=dop, repeats=repeats,
            serial_endpoint=(Endpoint(serial_host, serial_port, serial_user, serial_password,
                                      database) if serial_host else None),
        )
        tolerance = Tolerance(
            float_absolute=float_absolute, float_relative=float_relative,
            double_absolute=double_absolute, double_relative=double_relative,
            decimal_absolute=Decimal(decimal_absolute), decimal_relative=Decimal(decimal_relative),
            multiset_budget=multiset_budget,
        )
        config = replace(
            config, max_regenerations=max_attempts, query_timeout_seconds=timeout_seconds,
            max_duration_seconds=duration_seconds, row_limit=row_limit, tolerance=tolerance,
            perf_dops=tuple(int(v.strip()) for v in dop_sweep.split(",") if v.strip()),
            perf_warmups=warmups,
        )
        serial = require_serial_endpoint(config)
        if reference_host and mode != "compare":
            raise ValueError("--reference-host requires --mode compare")
        reference = (Endpoint(reference_host, reference_port, reference_user,
                              reference_password, database) if reference_host else None)
        if reference and (reference.host, reference.port) in {(host, port),
                                                             (serial.host, serial.port)}:
            raise ValueError("reference MySQL must use a separate endpoint from PQ and serial")
    except (ValueError, InvalidOperation) as exc:
        typer.echo(f"Invalid configuration: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    art_dir = artifacts / f"{mode}_{stamp}"
    art_dir.mkdir(parents=True, exist_ok=False)
    typer.echo(f"[{mode}] database={database} artifacts={art_dir.resolve()}")
    try:
        if mode == "fast":
            from select_fuzz.pq.fast import run_fast
            res = run_fast(config, artifacts_dir=art_dir)
            stats = res.stats
            typer.echo(
                f"[fast] attempts={stats.total_attempts} triggered={stats.triggered} "
                f"executed={stats.executed} matches={stats.matches} "
                f"mismatches={stats.mismatches} errors={stats.errors}"
            )
            accepted = stats.triggered
        elif mode == "compare":
            from select_fuzz.pq.compare import run_compare
            compare_stats = run_compare(config, reference_endpoint=reference, artifacts_dir=art_dir)
            typer.echo(
                f"[compare] total={compare_stats.total} triggered={compare_stats.triggered} "
                f"skipped_not_pq={compare_stats.skipped_not_pq} matches={compare_stats.matches} "
                f"mismatches={compare_stats.mismatches} by_category={compare_stats.by_category()}"
            )
            accepted = compare_stats.triggered
        else:
            from select_fuzz.pq.performance import run_performance
            perf_stats = run_performance(config, artifacts_dir=art_dir)
            typer.echo(
                f"[performance] total={perf_stats.total} triggered={perf_stats.triggered} "
                f"skipped_not_pq={perf_stats.skipped_not_pq} measured={len(perf_stats.records)} "
                f"avg_speedup={perf_stats.avg_speedup:.3f} regressions={len(perf_stats.regressions)}"
            )
            accepted = len(perf_stats.records)
        if accepted == 0:
            typer.echo("No qualifying PQ samples; inspect skip reasons and session settings.", err=True)
            raise typer.Exit(code=3)
    except KeyboardInterrupt:
        typer.echo("Interrupted; sessions closed. Evidence is in the run directory.", err=True)
        raise typer.Exit(code=130) from None
    except (mysql.connector.Error, RuntimeError, OSError, ValueError) as exc:
        typer.echo(f"Run failed: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    app()
