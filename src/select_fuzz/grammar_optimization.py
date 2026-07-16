"""Local, opt-in diagnostics for optimizing the SELECT grammar.

This module is intentionally separate from the production correctness artifact
contract.  It records every generated SQL statement before admission so grammar
authors can inspect both successful and rejected candidates during a campaign.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
import json
import math
from pathlib import Path
import secrets
import shutil
from threading import Event
import time
from typing import Any

import mysql.connector

from select_fuzz.correctness import GeneratedRoundSource
from select_fuzz.config import NodeConfig, NodeRole
from select_fuzz.domain import ExecutionStatus, NodeExecution, RunRequest, SeedTree
from select_fuzz.execution import MySQLConnectorFactory, NodeQueryRunner
from select_fuzz.generation.coverage import CoverageLedger
from select_fuzz.generation.query_grammar import (
    CandidateQuery,
    CandidateRejected,
    GrammarQueryConfig,
    GrammarQueryGenerator,
    SelectGrammar,
)
from select_fuzz.generation.schema import SchemaLimits
from select_fuzz.service import RoundContext


class FailureOwner(StrEnum):
    """The component most likely responsible for one failed candidate."""

    GRAMMAR = "grammar"
    GENERATOR = "generator"
    METADATA = "metadata_not_available"
    RANDOM_DATA = "random_data_or_soft_type"
    INFRASTRUCTURE = "infrastructure"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FailureClassification:
    category: str
    owner: FailureOwner


_GENERATOR_ERRNOS = frozenset({1052, 1054, 1066, 1109, 1248})
_GRAMMAR_ERRNOS = frozenset(
    {
        1064,  # parser rejection
        1111,  # invalid aggregate placement
        1140,  # aggregate/nonaggregate without GROUP BY
        1221,  # invalid clause/operator combination
        1222,  # set operands have different column counts
        1235,  # parser-supported shape rejected by this server version
        1241,  # operand column count
        1242,  # scalar subquery returns multiple rows
        3593,  # window function placement
        3065,  # DISTINCT ORDER BY expression is not projected
    }
)
_METADATA_ERRNOS = frozenset({1176, 1191, 1214, 1283})
_RANDOM_DATA_ERRNOS = frozenset(
    {
        1210,  # incorrect arguments, commonly cross-type soft lane
        1264,
        1292,
        1366,
        1411,
        1582,
        1690,
        3037,
        3055,
        3140,
        3141,
        3144,
        3143,
        3146,
        3548,
        3513,
        3514,
        3685,
        3995,
        3854,
        1525,
    }
)


def classify_mysql_failure(
    *,
    phase: str,
    errno: int,
    sqlstate: str,
    message: str,
) -> FailureClassification:
    """Classify one MySQL failure without hiding the original error identity."""

    del sqlstate
    normalized = message.casefold()
    if errno == 1247 and "forward reference in item list" in normalized:
        return FailureClassification(
            "lateral_transitive_outer_reference",
            FailureOwner.GENERATOR,
        )
    if errno in _GENERATOR_ERRNOS or any(
        marker in normalized
        for marker in (
            "unknown column",
            "not unique table/alias",
            "every derived table must have its own alias",
        )
    ):
        return FailureClassification("invalid_identifier_scope", FailureOwner.GENERATOR)
    if errno in _METADATA_ERRNOS or any(
        marker in normalized
        for marker in ("fulltext index", "match columns", "index hint")
    ):
        return FailureClassification("requires_unavailable_metadata", FailureOwner.METADATA)
    if errno in _GRAMMAR_ERRNOS or any(
        marker in normalized
        for marker in (
            "syntax",
            "not supported yet",
            "invalid use of group function",
            "window function",
        )
    ):
        return FailureClassification("invalid_sql_shape", FailureOwner.GRAMMAR)
    if errno in _RANDOM_DATA_ERRNOS or any(
        marker in normalized
        for marker in (
            "invalid json",
            "invalid gis",
            "incorrect arguments",
            "truncated incorrect",
            "out of range",
        )
    ):
        return FailureClassification("soft_type_or_value_domain", FailureOwner.RANDOM_DATA)
    if phase == "infrastructure" or 2000 <= errno < 3000:
        return FailureClassification("connection_or_timeout", FailureOwner.INFRASTRUCTURE)
    if errno == 1038:
        return FailureClassification("server_resource_limit", FailureOwner.INFRASTRUCTURE)
    return FailureClassification("unclassified_mysql_error", FailureOwner.UNKNOWN)


@dataclass(frozen=True, slots=True)
class GrammarOptimizationConfig:
    socket: Path
    grammar_path: Path
    artifact_root: Path
    iterations: int = 50
    iteration_seconds: float = 180.0
    query_timeout_seconds: float = 10.0
    rows_per_table: int = 8
    row_limit: int = 10_000
    byte_limit: int = 32 * 1024 * 1024
    compatible_type_percent: int = 80
    seed: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "socket", Path(self.socket).expanduser().resolve())
        object.__setattr__(
            self, "grammar_path", Path(self.grammar_path).expanduser().resolve()
        )
        object.__setattr__(
            self, "artifact_root", Path(self.artifact_root).expanduser().resolve()
        )
        for name in ("iterations", "rows_per_table", "row_limit", "byte_limit"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("iteration_seconds", "query_timeout_seconds"):
            value = getattr(self, name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a finite positive number")
        if self.query_timeout_seconds > 300:
            raise ValueError("query_timeout_seconds must not exceed 300")
        if not 0 <= self.compatible_type_percent <= 100:
            raise ValueError("compatible_type_percent must be from 0 to 100")
        if self.seed is not None and (
            not isinstance(self.seed, int) or isinstance(self.seed, bool)
        ):
            raise TypeError("seed must be an integer when supplied")


def _strict_json(document: object, *, pretty: bool = False) -> str:
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
    )


def _atomic_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(_strict_json(document, pretty=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _append_sql(stream: Any, candidate_id: int, candidate: CandidateQuery) -> None:
    stream.write(
        f"-- candidate={candidate_id} seed={candidate.seed} "
        f"grammar_sha256={candidate.grammar_hash}\n{candidate.sql};\n\n"
    )
    stream.flush()


def _append_failure_sql(
    stream: Any,
    candidate_id: int,
    candidate: CandidateQuery,
    *,
    phase: str,
    errno: int,
    sqlstate: str,
    message: str,
    classification: FailureClassification,
) -> None:
    clean_message = " ".join(message.split())
    stream.write(
        f"-- candidate={candidate_id} phase={phase} errno={errno} "
        f"sqlstate={sqlstate} owner={classification.owner.value} "
        f"category={classification.category}\n"
        f"-- message={clean_message}\n{candidate.sql};\n\n"
    )
    stream.flush()


def _execute_and_consume(connection: Any, sql: str) -> None:
    cursor = connection.cursor(buffered=True)
    try:
        cursor.execute(sql)
        if cursor.with_rows:
            cursor.fetchall()
    finally:
        cursor.close()


def _connect(socket: Path, timeout_seconds: float) -> Any:
    return mysql.connector.connect(
        user="root",
        unix_socket=str(socket),
        autocommit=True,
        connection_timeout=max(1, math.ceil(timeout_seconds)),
        read_timeout=max(1, math.ceil(timeout_seconds) + 1),
        write_timeout=max(1, math.ceil(timeout_seconds) + 1),
    )


def _build_bounded_runner(socket: Path) -> tuple[NodeQueryRunner, NodeConfig]:
    port = 44_061
    username_env = "SELECT_FUZZ_GRAMMAR_OPT_USER"
    password_env = "SELECT_FUZZ_GRAMMAR_OPT_AUTH"

    def connect(**kwargs: object) -> Any:
        received_port = kwargs.pop("port", None)
        if received_port != port:
            raise ValueError("grammar optimization connector received an unknown port")
        kwargs.pop("host", None)
        kwargs.pop("password", None)
        kwargs["unix_socket"] = str(socket)
        return mysql.connector.connect(**kwargs)

    node = NodeConfig(
        role=NodeRole.BASELINE,
        host="localhost",
        port=port,
        username_env=username_env,
        password_env=password_env,
    )
    factory = MySQLConnectorFactory(
        environ={username_env: "root", password_env: "local-socket-auth"},
        connect=connect,
    )
    return NodeQueryRunner(factory), node


def _materialize_round(
    config: GrammarOptimizationConfig,
    generator: GrammarQueryGenerator,
    *,
    campaign_seed: int,
    iteration: int,
) -> tuple[Any, RoundContext]:
    request = RunRequest(
        run_id=f"grammar-opt-{campaign_seed:x}",
        mode="correctness",
        seed=campaign_seed,
        workers=1,
        rounds=config.iterations,
        queries_per_round=1,
    )
    for attempt in range(64):
        round_seed = SeedTree(campaign_seed).derive(
            "grammar_optimization", iteration, attempt
        )
        context = RoundContext(request, 0, iteration, round_seed)
        ledger = CoverageLedger(config.artifact_root / "coverage.json")
        source = GeneratedRoundSource(
            ledger,
            rows_per_table=config.rows_per_table,
            schema_limits=SchemaLimits(
                min_tables=2,
                max_tables=4,
                min_columns=4,
                max_columns=8,
                max_indexes_per_table=4,
            ),
            grammar_query_generator=generator,
        )
        materialized = source.materialize(context)
        if not materialized.bundle.requires_same_session:
            return materialized, context
    raise RuntimeError("unable to materialize a non-temporary optimization schema")


def _prepare_database(connection: Any, materialized: Any) -> None:
    database = materialized.database
    _execute_and_consume(connection, f"CREATE DATABASE IF NOT EXISTS `{database}`")
    _execute_and_consume(connection, f"USE `{database}`")
    for statement in materialized.bundle.statements:
        _execute_and_consume(connection, statement)


def _bounded_failure(
    execution: NodeExecution,
    *,
    phase: str,
) -> tuple[int, str, str, FailureClassification]:
    error = execution.error
    if error is None:
        errno, sqlstate, message = 0, "HY000", f"{execution.status.value} without error"
    else:
        errno, sqlstate, message = error.errno, error.sqlstate, error.message
    if errno == 65_001:
        classification = FailureClassification(
            "bounded_runner_limit", FailureOwner.INFRASTRUCTURE
        )
    elif errno == 65_003 or execution.status is ExecutionStatus.TIMEOUT:
        classification = FailureClassification(
            "query_timeout", FailureOwner.INFRASTRUCTURE
        )
    elif errno == 65_002 or execution.status is ExecutionStatus.INFRA_ERROR:
        classification = FailureClassification(
            "connection_or_server_crash", FailureOwner.INFRASTRUCTURE
        )
    else:
        classification = classify_mysql_failure(
            phase=phase,
            errno=errno,
            sqlstate=sqlstate,
            message=message,
        )
    return errno, sqlstate, message, classification


def _write_iteration_analysis(
    iteration_root: Path,
    *,
    summary: Mapping[str, object],
    failure_groups: Mapping[tuple[str, str, str, int], list[Mapping[str, object]]],
    trace_counts: Mapping[str, Counter[str]],
) -> None:
    grouped = []
    for (phase, owner, category, errno), samples in sorted(failure_groups.items()):
        grouped.append(
            {
                "category": category,
                "count": len(samples),
                "errno": errno,
                "owner": owner,
                "phase": phase,
                "samples": list(samples[:5]),
            }
        )
    _atomic_json(iteration_root / "failure-groups.json", grouped)
    trace_risk = [
        {
            "execution_failure": counts["execution_failure"],
            "explain_failure": counts["explain_failure"],
            "safety_failure": counts["safety_failure"],
            "success": counts["success"],
            "trace": trace,
        }
        for trace, counts in sorted(trace_counts.items())
    ]
    _atomic_json(iteration_root / "trace-risk.json", trace_risk)
    _atomic_json(iteration_root / "summary.json", dict(summary))


def _run_iteration(
    config: GrammarOptimizationConfig,
    *,
    campaign_seed: int,
    iteration: int,
    stop_event: Event,
    monotonic: Callable[[], float],
) -> dict[str, object]:
    iteration_root = config.artifact_root / f"iteration-{iteration:03d}"
    iteration_root.mkdir(parents=True, exist_ok=True)
    snapshot_path = iteration_root / "grammar.yy"
    shutil.copyfile(config.grammar_path, snapshot_path)
    grammar = SelectGrammar.from_path(snapshot_path)
    generator = GrammarQueryGenerator(
        grammar,
        config=GrammarQueryConfig(
            compatible_type_percent=config.compatible_type_percent,
            max_tables_per_query_block=4,
        ),
    )
    materialized, context = _materialize_round(
        config,
        generator,
        campaign_seed=campaign_seed,
        iteration=iteration,
    )
    lifecycle_path = iteration_root / "lifecycle.jsonl"
    counts: Counter[str] = Counter()
    owner_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    trace_counts: dict[str, Counter[str]] = defaultdict(Counter)
    failure_groups: dict[
        tuple[str, str, str, int], list[Mapping[str, object]]
    ] = defaultdict(list)
    started = monotonic()
    candidate_ordinal = 0
    status = "completed"
    runner, node = _build_bounded_runner(config.socket)
    with _connect(config.socket, config.query_timeout_seconds) as setup_connection:
        _prepare_database(setup_connection, materialized)
    with (
        lifecycle_path.open("a", encoding="utf-8") as lifecycle,
        (iteration_root / "candidates.sql").open("a", encoding="utf-8") as candidates,
        (iteration_root / "failures.sql").open("a", encoding="utf-8") as failures,
        (iteration_root / "passed.sql").open("a", encoding="utf-8") as passed,
    ):
        deadline = monotonic() + config.iteration_seconds
        while monotonic() < deadline and not stop_event.is_set():
            candidate_seed = SeedTree(context.round_seed).derive(
                "grammar_candidate", candidate_ordinal
            )
            try:
                candidate = generator.generate(
                    materialized.schema,
                    seed=candidate_seed,
                )
            except CandidateRejected as error:
                rejected_candidate = error.candidate
                if rejected_candidate is None:
                    counts["generation_rejected"] += 1
                    event = {
                        "candidate": candidate_ordinal,
                        "error_message": str(error),
                        "phase": "generation",
                        "seed": candidate_seed,
                        "status": "rejected",
                    }
                else:
                    counts["candidate_sql"] += 1
                    counts["safety_failure"] += 1
                    classification = FailureClassification(
                        "read_only_safety_gate", FailureOwner.GENERATOR
                    )
                    owner_counts[classification.owner.value] += 1
                    category_counts[classification.category] += 1
                    message = str(error.__cause__ or error)
                    _append_sql(candidates, candidate_ordinal, rejected_candidate)
                    _append_failure_sql(
                        failures,
                        candidate_ordinal,
                        rejected_candidate,
                        phase="safety",
                        errno=0,
                        sqlstate="HY000",
                        message=message,
                        classification=classification,
                    )
                    for trace in rejected_candidate.production_trace:
                        trace_counts[trace]["safety_failure"] += 1
                    failure_groups[
                        (
                            "safety",
                            classification.owner.value,
                            classification.category,
                            0,
                        )
                    ].append(
                        {
                            "candidate": candidate_ordinal,
                            "message": " ".join(message.split()),
                            "sql": rejected_candidate.sql,
                        }
                    )
                    event = {
                        "candidate": candidate_ordinal,
                        "category": classification.category,
                        "error_message": message,
                        "grammar_sha256": rejected_candidate.grammar_hash,
                        "owner": classification.owner.value,
                        "phase": "safety",
                        "production_trace": rejected_candidate.production_trace,
                        "seed": rejected_candidate.seed,
                        "status": "failure",
                    }
                lifecycle.write(_strict_json(event) + "\n")
                lifecycle.flush()
                candidate_ordinal += 1
                continue
            counts["candidate_sql"] += 1
            _append_sql(candidates, candidate_ordinal, candidate)
            common_event: dict[str, object] = {
                "candidate": candidate_ordinal,
                "grammar_sha256": candidate.grammar_hash,
                "production_trace": candidate.production_trace,
                "seed": candidate.seed,
            }
            explain = runner.run(
                node,
                materialized.database,
                f"EXPLAIN {candidate.sql}",
                timeout_s=config.query_timeout_seconds,
                row_limit=config.row_limit,
                byte_limit=config.byte_limit,
            )
            if explain.status is not ExecutionStatus.SUCCESS:
                errno, sqlstate, message, classification = _bounded_failure(
                    explain,
                    phase="explain",
                )
                counts["explain_failure"] += 1
                owner_counts[classification.owner.value] += 1
                category_counts[classification.category] += 1
                for trace in candidate.production_trace:
                    trace_counts[trace]["explain_failure"] += 1
                sample = {
                    "candidate": candidate_ordinal,
                    "message": " ".join(message.split()),
                    "sql": candidate.sql,
                }
                failure_groups[
                    (
                        "explain",
                        classification.owner.value,
                        classification.category,
                        errno,
                    )
                ].append(sample)
                _append_failure_sql(
                    failures,
                    candidate_ordinal,
                    candidate,
                    phase="explain",
                    errno=errno,
                    sqlstate=sqlstate,
                    message=message,
                    classification=classification,
                )
                event = {
                    **common_event,
                    "category": classification.category,
                    "errno": errno,
                    "error_message": message,
                    "owner": classification.owner.value,
                    "phase": "explain",
                    "sqlstate": sqlstate,
                    "status": "failure",
                }
            else:
                counts["explain_success"] += 1
                execution = runner.run(
                    node,
                    materialized.database,
                    candidate.sql,
                    timeout_s=config.query_timeout_seconds,
                    row_limit=config.row_limit,
                    byte_limit=config.byte_limit,
                )
                if execution.status is not ExecutionStatus.SUCCESS:
                    errno, sqlstate, message, classification = _bounded_failure(
                        execution,
                        phase="execute",
                    )
                    counts["execution_failure"] += 1
                    owner_counts[classification.owner.value] += 1
                    category_counts[classification.category] += 1
                    for trace in candidate.production_trace:
                        trace_counts[trace]["execution_failure"] += 1
                    sample = {
                        "candidate": candidate_ordinal,
                        "message": " ".join(message.split()),
                        "sql": candidate.sql,
                    }
                    failure_groups[
                        (
                            "execute",
                            classification.owner.value,
                            classification.category,
                            errno,
                        )
                    ].append(sample)
                    _append_failure_sql(
                        failures,
                        candidate_ordinal,
                        candidate,
                        phase="execute",
                        errno=errno,
                        sqlstate=sqlstate,
                        message=message,
                        classification=classification,
                    )
                    event = {
                        **common_event,
                        "category": classification.category,
                        "errno": errno,
                        "error_message": message,
                        "owner": classification.owner.value,
                        "phase": "execute",
                        "sqlstate": sqlstate,
                        "status": "failure",
                    }
                else:
                    counts["execution_success"] += 1
                    for trace in candidate.production_trace:
                        trace_counts[trace]["success"] += 1
                    _append_sql(passed, candidate_ordinal, candidate)
                    event = {
                        **common_event,
                        "phase": "execute",
                        "status": "success",
                    }
            lifecycle.write(_strict_json(event) + "\n")
            lifecycle.flush()
            candidate_ordinal += 1
        if stop_event.is_set():
            status = "interrupted"
    elapsed = max(0.0, monotonic() - started)
    candidate_sql = counts["candidate_sql"]
    summary: dict[str, object] = {
        "candidate_sql": candidate_sql,
        "category_counts": dict(sorted(category_counts.items())),
        "database": materialized.database,
        "elapsed_seconds": round(elapsed, 6),
        "execution_failure": counts["execution_failure"],
        "execution_success": counts["execution_success"],
        "explain_failure": counts["explain_failure"],
        "explain_success": counts["explain_success"],
        "generation_rejected": counts["generation_rejected"],
        "grammar_sha256": grammar.sha256,
        "iteration": iteration,
        "owner_counts": dict(sorted(owner_counts.items())),
        "safety_failure": counts["safety_failure"],
        "schema_profile": materialized.schema.profile.value,
        "status": status,
        "successful_execution_rate": (
            0.0 if not candidate_sql else round(counts["execution_success"] / candidate_sql, 6)
        ),
    }
    _write_iteration_analysis(
        iteration_root,
        summary=summary,
        failure_groups=failure_groups,
        trace_counts=trace_counts,
    )
    return summary


def _campaign_summary(root: Path, campaign_seed: int) -> dict[str, object]:
    iterations = []
    totals: Counter[str] = Counter()
    for path in sorted(root.glob("iteration-*/summary.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        iterations.append(document)
        for name in (
            "candidate_sql",
            "execution_failure",
            "execution_success",
            "explain_failure",
            "explain_success",
            "generation_rejected",
            "safety_failure",
        ):
            totals[name] += int(document.get(name, 0))
    candidate_sql = totals["candidate_sql"]
    return {
        "candidate_sql": candidate_sql,
        "execution_failure": totals["execution_failure"],
        "execution_success": totals["execution_success"],
        "explain_failure": totals["explain_failure"],
        "explain_success": totals["explain_success"],
        "generation_rejected": totals["generation_rejected"],
        "safety_failure": totals["safety_failure"],
        "iteration_summaries": iterations,
        "iterations_completed": len(iterations),
        "seed": campaign_seed,
        "successful_execution_rate": (
            0.0 if not candidate_sql else round(totals["execution_success"] / candidate_sql, 6)
        ),
        "updated_at": datetime.now(UTC).isoformat(),
    }


def run_grammar_optimization_campaign(
    config: GrammarOptimizationConfig,
    *,
    stop_event: Event | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    on_iteration: Callable[[Mapping[str, object]], None] | None = None,
) -> dict[str, object]:
    """Run or resume a local campaign, reloading the grammar every iteration."""

    if not config.socket.is_socket():
        raise ValueError(f"configured path is not a Unix socket: {config.socket}")
    if not config.grammar_path.is_file():
        raise ValueError(f"grammar file does not exist: {config.grammar_path}")
    config.artifact_root.mkdir(parents=True, exist_ok=True)
    campaign_path = config.artifact_root / "campaign.json"
    if campaign_path.is_file():
        campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
        campaign_seed = int(campaign["seed"])
    else:
        campaign_seed = config.seed if config.seed is not None else secrets.randbits(63)
        campaign = {
            **asdict(config),
            "artifact_root": str(config.artifact_root),
            "created_at": datetime.now(UTC).isoformat(),
            "grammar_path": str(config.grammar_path),
            "seed": campaign_seed,
            "socket": str(config.socket),
        }
        _atomic_json(campaign_path, campaign)
    requested_stop = stop_event or Event()
    completed = len(list(config.artifact_root.glob("iteration-*/summary.json")))
    for iteration in range(completed, config.iterations):
        if requested_stop.is_set():
            break
        summary = _run_iteration(
            config,
            campaign_seed=campaign_seed,
            iteration=iteration,
            stop_event=requested_stop,
            monotonic=monotonic,
        )
        aggregate = _campaign_summary(config.artifact_root, campaign_seed)
        _atomic_json(config.artifact_root / "campaign-summary.json", aggregate)
        if on_iteration is not None:
            on_iteration(summary)
    aggregate = _campaign_summary(config.artifact_root, campaign_seed)
    _atomic_json(config.artifact_root / "campaign-summary.json", aggregate)
    return aggregate


__all__ = [
    "FailureClassification",
    "FailureOwner",
    "GrammarOptimizationConfig",
    "classify_mysql_failure",
    "run_grammar_optimization_campaign",
]
