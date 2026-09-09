"""Shared bounded generation, plan gates, accounting and resource ownership."""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence, TypedDict

from select_fuzz.pq.config import Endpoint, PQConfig, require_serial_endpoint
from select_fuzz.pq.connection import PQConnection
from select_fuzz.pq.generator import PQGeneratedQuery, PQGenerator
from select_fuzz.pq.materializer import materialize, write_setup
from select_fuzz.pq.report import RunArtifacts, differential_evidence, evidence, parameters_text
from select_fuzz.pq.runner import DifferentialResult, build_runner

SUBSTANTIVE = frozenset({"MISMATCH", "ROW_COUNT", "ERROR_PARITY", "ORDER"})
COMPARABLE = SUBSTANTIVE | {"MATCH", "PRECISION_VARIANCE"}


class _FixtureSetup(TypedDict):
    database: str
    table_count: int
    rows_per_table: int
    seed: int


@dataclass(slots=True)
class ModeStats:
    # Counts are per generation attempt, not per EXPLAIN or timing sample.
    total: int = 0  # query slots started (curated entries when supplied)
    total_attempts: int = 0
    regenerated: int = 0
    triggered: int = 0  # attempts with at least one freshly admitted PQ execution
    executed: int = 0  # attempts that actually executed either SQL arm
    accepted: int = 0  # candidates kept by the mode, including precision observations
    compared: int = 0  # kept candidates with complete, conclusive comparisons
    matches: int = 0
    precision: int = 0
    errors: int = 0
    skipped: int = 0
    skipped_not_pq: int = 0
    by_shape: dict[str, int] = field(default_factory=dict)
    shape_stats: dict[str, dict[str, int]] = field(default_factory=dict)
    skip_reasons: dict[str, int] = field(default_factory=dict)
    artifacts_dir: str = ""
    stopped_reason: str = ""
    elapsed_seconds: float = 0.0

    def bump(self, shape: str, metric: str) -> None:
        setattr(self, metric, getattr(self, metric) + 1)
        counts = self.shape_stats.setdefault(shape, {})
        counts[metric] = counts.get(metric, 0) + 1


@dataclass(slots=True)
class Candidate:
    query: PQGeneratedQuery
    record: dict[str, Any]
    stats: ModeStats
    counted: set[str] = field(default_factory=set)
    accepted: bool = False

    def mark(self, metric: str) -> None:
        if metric not in self.counted:
            self.stats.bump(self.query.shape, metric)
            self.counted.add(metric)

    def reject(self, reason: str, *, error: bool = False) -> None:
        self.record["skip_reason"] = reason
        self.mark("skipped")
        if error:
            self.mark("errors")
        if reason.lower().split(":", 1)[0] in {"not_pq", "no_pq"}:
            self.mark("skipped_not_pq")

    def accept(self, outcomes: Sequence[Any]) -> None:
        self.accepted = True
        self.mark("accepted")
        self.mark("compared")
        verdicts = [o.verdict for o in outcomes]
        if all(v == "MATCH" for v in verdicts):
            self.mark("matches")
        if "PRECISION_VARIANCE" in verdicts:
            self.mark("precision")
        if any(v in SUBSTANTIVE for v in verdicts):
            counts = self.stats.shape_stats[self.query.shape]
            counts["mismatches"] = counts.get("mismatches", 0) + 1


@dataclass(slots=True)
class ModeRun:
    config: PQConfig
    stats: ModeStats
    artifacts: RunArtifacts
    started: float
    runner: Any = None
    reference: Any = None
    generator: Any = None
    setup_path: Path | None = None
    snapshot: dict[str, Any] = field(default_factory=dict)
    failure: str = ""

    def expired(self) -> bool:
        if time.monotonic() - self.started >= self.config.max_duration_seconds:
            self.stats.stopped_reason = "DURATION_BUDGET"
            return True
        return False

    @property
    def parameters(self) -> str:
        return parameters_text(self.config, self.snapshot)

    def capture_snapshot(self, key: str = "initial") -> None:
        if self.runner is None or self.expired():
            return
        try:
            self.snapshot[key] = evidence(self.runner.snapshot(), redact=True)
        except Exception as exc:
            self.snapshot[key] = {"snapshot_error": f"{type(exc).__name__}: {exc}"}

    def gate(self, candidate: Candidate) -> bool:
        """Preliminary gate only. The runner must also gate every execution freshly."""
        for serial, key in ((False, "pq_plan"), (True, "serial_plan")):
            if self.expired():
                candidate.reject("DURATION_BUDGET")
                return False
            try:
                info = self.runner.explain(candidate.query.sql, serial=serial)
                candidate.record[key] = evidence(info)
            except Exception as exc:
                candidate.record[key] = {"text": "", "triggered": False,
                                         "error": f"{type(exc).__name__}: {exc}"}
        pq, seq = candidate.record["pq_plan"], candidate.record["serial_plan"]
        if self.expired():
            candidate.reject("DURATION_BUDGET")
        elif pq.get("error") or seq.get("error"):
            candidate.reject("EXPLAIN_ERROR: " + str(pq.get("error") or seq.get("error")),
                             error=True)
        elif not pq["triggered"]:
            candidate.reject("NOT_PQ")
        elif seq["triggered"]:
            candidate.reject("SERIAL_PLAN_IS_PQ")
        else:
            return True
        return False

    def execute(self, candidate: Candidate, *, reverse: bool = False) -> DifferentialResult | None:
        if self.expired():
            candidate.reject("DURATION_BUDGET")
            return None
        q = candidate.query
        result: DifferentialResult = self.runner.run_differential(
            q.sql, compare_mode=q.compare_mode,
            decimal_columns=getattr(q, "decimal_columns", ()), reverse=reverse,
        )
        result.shape, result.seed = q.shape, q.seed
        result.decimal_columns = getattr(q, "decimal_columns", ())
        candidate.record["executions"].append(differential_evidence(result))
        if any(r.status in {"OK", "ERROR"} for r in (result.pq_result, result.seq_result)):
            candidate.mark("executed")
        if result.pq_triggered and not execution_skip_reason(result):
            candidate.mark("triggered")
        if any(r.status == "ERROR" for r in (result.pq_result, result.seq_result)):
            candidate.mark("errors")
        if self.expired():
            candidate.reject("DURATION_BUDGET")
            return None
        return result

    def run_queries(
        self, process: Callable[[Candidate], None],
        *, curated: Sequence[str | PQGeneratedQuery] | None = None,
        prepare: Callable[[], None] | None = None,
    ) -> None:
        slots = len(curated) if curated is not None else (
            self.config.queries_per_round * self.config.rounds)
        for slot in range(slots):
            if self.expired() or self.generator is None:
                break
            self.stats.total += 1
            shape = None
            # Curated SQL is an explicit probe; do not rewrite it on a failed gate.
            attempts = 1 if curated is not None else self.config.max_regenerations
            for attempt in range(attempts):
                if self.expired():
                    break
                seed = self.config.seed + slot * self.config.max_regenerations + attempt
                generation_error = ""
                try:
                    q = (curated_query(curated[slot], seed=seed) if curated is not None
                         else self.generator.generate(seed=seed, shape=shape,
                                                      pq_friendly=True))
                    if shape is None:
                        shape = q.shape
                except Exception as exc:
                    generation_error = f"GENERATION_ERROR: {type(exc).__name__}: {exc}"
                    q = PQGeneratedQuery("", False, "multiset", shape or "unknown", seed)
                record: dict[str, Any] = {
                    "slot": slot, "attempt": attempt, "seed": q.seed, "sql": q.sql,
                    "shape": q.shape, "compare_mode": q.compare_mode,
                    "decimal_columns": getattr(q, "decimal_columns", ()),
                    "pq_friendly": curated is None, "curated": curated is not None,
                    "pq_plan": {}, "serial_plan": {}, "executions": [], "skip_reason": "",
                }
                candidate = Candidate(q, record, self.stats)
                candidate.mark("total_attempts")
                self.stats.by_shape[q.shape] = self.stats.by_shape.get(q.shape, 0) + 1
                if attempt:
                    candidate.mark("regenerated")
                try:
                    if generation_error:
                        candidate.reject(generation_error, error=True)
                    elif q.compare_mode not in {"exact", "multiset"}:
                        candidate.reject("AMBIGUOUS_LIMIT_OR_COMPARISON: full result contract required")
                    else:
                        if prepare is not None:
                            prepare()
                        if self.gate(candidate):
                            process(candidate)
                            if not candidate.accepted and not record["skip_reason"]:
                                candidate.reject("INCONCLUSIVE")
                except KeyboardInterrupt:
                    candidate.reject("INTERRUPTED")
                    raise
                except OSError:
                    raise  # A broken evidence destination must stop the campaign.
                except Exception as exc:
                    candidate.reject(f"EXECUTION_ERROR: {type(exc).__name__}: {exc}", error=True)
                finally:
                    record["accepted"] = candidate.accepted
                    record["counts"] = sorted(candidate.counted)
                    record["elapsed_seconds"] = time.monotonic() - self.started
                    reason = record["skip_reason"].split(":", 1)[0]
                    if reason:
                        self.stats.skip_reasons[reason] = self.stats.skip_reasons.get(reason, 0) + 1
                    self.artifacts.append("attempts.jsonl", record)
                if candidate.accepted or self.expired():
                    break


def result_skip_reason(result: Any, *, label: str) -> str:
    if getattr(result, "fallback", False):
        return f"{label}_FALLBACK"
    if result.status not in {"OK", "ERROR"}:
        return f"{label}_NOT_EXECUTED"
    if result.status == "OK":
        if not getattr(result, "complete", True) or result.row_count != len(result.rows):
            return f"{label}_INCOMPLETE_RESULT"
        if not getattr(result, "warnings_complete", True):
            return f"{label}_WARNINGS_UNAVAILABLE"
    return ""


def execution_skip_reason(result: DifferentialResult, *, performance: bool = False) -> str:
    reason = getattr(result, "skip_reason", "")
    if reason:
        return reason
    for label, data in (("PQ", result.pq_result), ("SERIAL", result.seq_result)):
        reason = result_skip_reason(data, label=label)
        if reason:
            return reason
        if performance and data.status != "OK":
            return f"{label}_EXECUTION_ERROR: {data.error}"
    if not result.pq_triggered:
        return "FRESH_PQ_GATE_REJECTED"
    if result.outcome.verdict not in COMPARABLE:
        return f"{result.outcome.verdict}: {result.outcome.detail}"
    if performance and result.outcome.verdict in SUBSTANTIVE:
        return f"RESULT_{result.outcome.verdict}: {result.outcome.detail}"
    if (result.pq_result.status == result.seq_result.status == "ERROR"
            and result.outcome.verdict == "MATCH"):
        return "BOTH_EXECUTION_ERROR"
    return ""


# A conservative lexical check, not an SQL/order proof. Even LIMIT inside a
# derived table can change membership. Strings with LIMIT need an explicit contract.
_LIMIT = re.compile(r"\bLIMIT\b", re.IGNORECASE)


def curated_query(query: str | PQGeneratedQuery, *, seed: int) -> PQGeneratedQuery:
    if isinstance(query, PQGeneratedQuery):
        return query
    if not isinstance(query, str):
        raise TypeError("curated entries must be SQL strings or PQGeneratedQuery")
    return PQGeneratedQuery(query, False, "inconclusive" if _LIMIT.search(query) else "multiset",
                            "curated", seed)


@contextmanager
def open_run(
    config: PQConfig, stats: ModeStats, *, mode: str, artifacts_dir: Path | None,
    reference_endpoint: Endpoint | None = None,
) -> Iterator[ModeRun]:
    serial_endpoint = require_serial_endpoint(config)
    if reference_endpoint is not None and (
        reference_endpoint.host, reference_endpoint.port
    ) in {(config.endpoint.host, config.endpoint.port),
          (serial_endpoint.host, serial_endpoint.port)}:
        raise ValueError("reference MySQL must use a separate endpoint from PQ and serial")
    started = time.monotonic()
    artifacts = RunArtifacts(artifacts_dir, mode=mode)
    stats.artifacts_dir = str(artifacts.path)
    run = ModeRun(config, stats, artifacts, started)
    try:
        # Include deterministic data generation and all setup work in the deadline.
        run.setup_path = write_setup(config, artifacts.path)
        if not run.expired():
            run.runner = build_runner(config)
        if run.runner is not None and not run.expired():
            setup: _FixtureSetup = dict(
                database=config.database, table_count=config.materialize_tables,
                rows_per_table=config.materialize_rows, seed=config.seed,
            )
            schema = materialize(run.runner._pq, **setup)
            if ((serial_endpoint.host, serial_endpoint.port) !=
                    (config.endpoint.host, config.endpoint.port) and not run.expired()):
                materialize(run.runner._seq, **setup)
            if not run.expired():
                run.runner.use_database(config.database)
            if reference_endpoint is not None and not run.expired():
                run.reference = PQConnection(reference_endpoint, dop=0, reference=True,
                    query_timeout_ms=max(1, int(config.query_timeout_seconds * 1000)))
                if not run.expired():
                    materialize(run.reference, **setup)
                if not run.expired():
                    run.reference.use_database(config.database)
            if not run.expired():
                run.generator = PQGenerator(schema, seed=config.seed)
            run.capture_snapshot()
        yield run
    except BaseException as exc:
        run.failure = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        cleanup_errors = []
        for resource in (run.reference, run.runner):
            if resource is not None:
                try:
                    resource.close()
                except Exception as exc:
                    cleanup_errors.append(f"{type(exc).__name__}: {exc}")
        stats.elapsed_seconds = time.monotonic() - started
        counts = {name: getattr(stats, name) for name in stats.__dataclass_fields__
                  if name not in {"findings", "records", "observations"}}
        if hasattr(stats, "mismatches"):
            counts["mismatches"] = stats.mismatches
        artifacts.write_json("summary.json", {
            "mode": mode, "seed": config.seed, "stats": counts,
            "config": evidence(config, redact=True), "sessions": run.snapshot,
            "reference_endpoint": evidence(reference_endpoint, redact=True),
            "setup_path": run.setup_path, "cleanup_path": artifacts.path / "cleanup.sql",
            "attempts_path": artifacts.path / "attempts.jsonl",
            "findings_path": artifacts.path / "findings.jsonl",
            "samples_path": artifacts.path / "samples.jsonl",
            "failure": run.failure, "cleanup_errors": cleanup_errors,
            "counter_semantics": "Per generated/curated attempt; total counts slots. "
                "Accepted/compared exclude rejected timing candidates; sample counters are separate.",
            "duration_semantics": "Deadline checked between setup stages, attempts, plans and "
                "samples; in-flight synchronous operations retain their configured timeout.",
        })
