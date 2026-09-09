"""Balanced, freshly gated performance samples with complete correctness checks."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from select_fuzz.pq.config import PQConfig, validate_performance_dops
from select_fuzz.pq.mode_support import Candidate, ModeStats, execution_skip_reason, open_run
from select_fuzz.pq.report import RunArtifacts, differential_evidence, evidence

# These labels identify candidates for repetition, not statistically proven regressions.
_SLOWER_RATIO = 0.9
_SMALL_SCAN_ESTIMATE = 1000


@dataclass(frozen=True, slots=True)
class PerfRecord:
    # Retain the original positional fields and CLI accessors.
    sql: str
    explain_text: str
    triggered: bool
    dop: int
    pq_ms: float
    non_pq_ms: float
    speedup: float
    scan_rows: int | None  # EXPLAIN estimate, never an actual scan count
    returned_rows: int
    shape: str
    seed: int = 0
    requested_dop: int = 0  # Legacy field: expected configuration, never a SET request.
    planned_dop: int = 0  # 0 means unknown or varied between samples
    seq_explain_text: str = ""
    actual_scan_rows: int | None = None
    pq_times_ms: tuple[float, ...] = ()
    serial_times_ms: tuple[float, ...] = ()
    planned_dops: tuple[int, ...] = ()
    scan_rows_estimates: tuple[int | None, ...] = ()
    returned_rows_samples: tuple[int, ...] = ()
    warmups: int = 0
    samples: tuple[dict[str, Any], ...] = ()
    evidence: str = "plan_confirmed_no_reported_fallback"
    scan_rows_kind: str = "explain_estimate"
    flags: tuple[str, ...] = ()
    data_profile: str | None = None  # EXPLAIN alone does not establish data skew.

    def __post_init__(self) -> None:
        flags = list(self.flags)
        if self.speedup < _SLOWER_RATIO and "PQ_SLOWER" not in flags:
            flags.append("PQ_SLOWER")
        if (self.scan_rows is not None and 0 <= self.scan_rows < _SMALL_SCAN_ESTIMATE
                and self.speedup < 1 and "SMALL_DATA_OVERHEAD" not in flags):
            flags.append("SMALL_DATA_OVERHEAD")
        object.__setattr__(self, "flags", tuple(flags))

    @property
    def regression(self) -> str | None:
        return self.flags[0] if self.flags else None


@dataclass(slots=True)
class PerfStats(ModeStats):
    records: list[PerfRecord] = field(default_factory=list)
    sample_attempts: int = 0
    executed_samples: int = 0
    compared_samples: int = 0
    precision_samples: int = 0
    measured_samples: int = 0

    @property
    def avg_speedup(self) -> float:
        return statistics.mean(r.speedup for r in self.records) if self.records else 0.0

    @property
    def regressions(self) -> list[PerfRecord]:
        return [r for r in self.records if r.regression]


def run_performance(config: PQConfig, *, artifacts_dir: Path | None = None) -> PerfStats:
    validate_performance_dops(config.perf_dops, expected_dop=config.dop_on)
    stats = PerfStats()
    dop = config.dop_on
    with open_run(config, stats, mode="performance", artifacts_dir=artifacts_dir) as run:
        sample_number = 0

        def process(candidate: Candidate) -> None:
            nonlocal sample_number
            outcomes = []
            if run.expired():
                candidate.reject("DURATION_BUDGET")
                return
            candidate.record.setdefault("dop_plans", []).append({
                "requested_dop": dop, "pq_plan": candidate.record["pq_plan"],
                "serial_plan": candidate.record["serial_plan"],
            })
            if f"expected-dop-{dop}" not in run.snapshot:
                run.capture_snapshot(f"expected-dop-{dop}")
            measured = []
            samples = []
            for sample in range(config.perf_warmups + config.perf_repeats):
                if run.expired():
                    candidate.reject("DURATION_BUDGET")
                    return
                reverse = bool(sample_number % 2)
                sample_number += 1
                stats.bump(candidate.query.shape, "sample_attempts")
                stamp = {"slot": candidate.record["slot"], "attempt": candidate.record["attempt"],
                         "seed": candidate.query.seed, "sql": candidate.query.sql,
                         "shape": candidate.query.shape, "requested_dop": dop,
                         "sample": sample, "warmup": sample < config.perf_warmups,
                         "order": "serial_first" if reverse else "pq_first"}
                # A start record survives interruption inside a synchronous driver call.
                run.artifacts.append("samples.jsonl", {**stamp, "event": "started"})
                result = run.execute(candidate, reverse=reverse)
                if result is None:
                    run.artifacts.append("samples.jsonl", {**stamp, "event": "excluded",
                        "skip_reason": candidate.record["skip_reason"]})
                    return
                if any(r.status in {"OK", "ERROR"} for r in (result.pq_result, result.seq_result)):
                    stats.bump(candidate.query.shape, "executed_samples")
                reason = execution_skip_reason(result, performance=True)
                times = (result.pq_result.elapsed_ms, result.seq_result.elapsed_ms)
                if not reason and any(not math.isfinite(t) or t <= 0 for t in times):
                    reason = "INVALID_TIMING: positive finite durations required"
                detail = {**stamp, "event": "excluded" if reason else "completed",
                          "skip_reason": reason, **differential_evidence(result)}
                # Explicit timing rejection reason takes precedence over runner skip_reason.
                detail["skip_reason"] = reason
                samples.append(detail)
                run.artifacts.append("samples.jsonl", detail)
                if reason:
                    candidate.reject(reason)
                    run.artifacts.append("findings.jsonl", {"kind": "performance_exclusion",
                        **stamp, "reason": reason,
                        "result": differential_evidence(result, include_rows=True)})
                    return  # Discard all prior valid samples of this candidate.
                stats.bump(candidate.query.shape, "compared_samples")
                if result.outcome.verdict == "PRECISION_VARIANCE":
                    stats.bump(candidate.query.shape, "precision_samples")
                outcomes.append(result.outcome)
                if sample >= config.perf_warmups:
                    measured.append(result)
                    stats.bump(candidate.query.shape, "measured_samples")
            pq_times = tuple(r.pq_result.elapsed_ms for r in measured)
            serial_times = tuple(r.seq_result.elapsed_ms for r in measured)
            pq_ms, seq_ms = statistics.median(pq_times), statistics.median(serial_times)
            last = measured[-1]
            planned_dops = tuple(r.plan_dop for r in measured)
            planned = planned_dops[0] if len(set(planned_dops)) == 1 else 0
            estimates = tuple(r.scan_rows for r in measured)
            # Unknown estimates remain unknown; a one-row aggregate is not small input.
            estimate = (max(v for v in estimates if v is not None)
                        if all(v is not None for v in estimates) else None)
            record = PerfRecord(
                sql=candidate.query.sql, explain_text=last.explain_text, triggered=True,
                dop=planned, pq_ms=pq_ms, non_pq_ms=seq_ms, speedup=seq_ms / pq_ms,
                scan_rows=estimate, returned_rows=last.pq_result.row_count,
                shape=candidate.query.shape, seed=candidate.query.seed,
                requested_dop=dop, planned_dop=planned, seq_explain_text=last.seq_explain_text,
                pq_times_ms=pq_times, serial_times_ms=serial_times, planned_dops=planned_dops,
                scan_rows_estimates=estimates,
                returned_rows_samples=tuple(r.pq_result.row_count for r in measured),
                warmups=config.perf_warmups, samples=tuple(samples), evidence=last.evidence,
            )
            candidate.accept(outcomes)
            stats.records.append(record)
            candidate.record["performance_records"] = evidence([record])
            if record.flags:
                run.artifacts.append("findings.jsonl", {"kind": "performance_candidate",
                                                      "record": evidence(record)})
        try:
            run.run_queries(process)
        finally:
            _write_report(stats, run.artifacts)
    return stats


def _write_report(stats: PerfStats, artifacts: RunArtifacts) -> None:
    columns = tuple(PerfRecord.__dataclass_fields__) + ("regression",)
    artifacts.write_csv("pq_performance.csv", columns,
        ({**evidence(record), "regression": record.regression or ""} for record in stats.records))


__all__ = ["PerfRecord", "PerfStats", "run_performance"]
