"""Fast mode: bounded, freshly gated PQ/serial discrepancy discovery."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from select_fuzz.pq.config import PQConfig
from select_fuzz.pq.mode_support import (
    SUBSTANTIVE, Candidate, ModeStats, execution_skip_reason, open_run,
)
from select_fuzz.pq.report import differential_evidence, render_bug_report
from select_fuzz.pq.runner import DifferentialResult


@dataclass(slots=True)
class FastStats(ModeStats):
    mismatches: int = 0


@dataclass(slots=True)
class FastRunResult:
    stats: FastStats
    bugs: list[DifferentialResult]  # Historic API name; entries are candidates, not proven bugs.
    setup_sql: str
    artifacts_dir: Path | None = None


def run_fast(config: PQConfig, *, artifacts_dir: Path | None = None) -> FastRunResult:
    stats = FastStats()
    bugs: list[DifferentialResult] = []
    with open_run(config, stats, mode="fast", artifacts_dir=artifacts_dir) as run:
        def process(candidate: Candidate) -> None:
            result = run.execute(candidate)
            if result is None:
                return
            reason = execution_skip_reason(result)
            if reason:
                candidate.reject(reason)
                return
            candidate.accept([result.outcome])
            if result.outcome.verdict in SUBSTANTIVE:
                stats.mismatches += 1
                bugs.append(result)
                run.artifacts.append("findings.jsonl", differential_evidence(result, include_rows=True))
                text = render_bug_report(result, setup_sql="", parameters=run.parameters,
                                         index=len(bugs), setup_path=run.setup_path)
                (run.artifacts.path / f"pq_candidate_{len(bugs):03d}.md").write_text(
                    text, encoding="utf-8")
        try:
            run.run_queries(process)
        finally:
            run.artifacts.write_csv("pq_fast_findings.csv",
                ("seed", "shape", "verdict", "detail", "sql", "explain_text",
                 "seq_explain_text", "pq_triggered"),
                ({"seed": r.seed, "shape": r.shape, "verdict": r.outcome.verdict,
                  "detail": r.outcome.detail, "sql": r.sql, "explain_text": r.explain_text,
                  "seq_explain_text": r.seq_explain_text, "pq_triggered": r.pq_triggered}
                 for r in bugs))
        setup_sql = run.setup_path.read_text(encoding="utf-8") if run.setup_path else ""
        return FastRunResult(stats, bugs, setup_sql, run.artifacts.path)


__all__ = ["FastRunResult", "FastStats", "run_fast"]
