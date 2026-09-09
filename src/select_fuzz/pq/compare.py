"""Two/three-way comparison of identical, PQ-qualified SQL and fixtures."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Sequence, cast

from select_fuzz.pq.config import Endpoint, PQConfig
from select_fuzz.pq.connection import QueryResult
from select_fuzz.pq.generator import PQGeneratedQuery
from select_fuzz.pq.mode_support import (
    COMPARABLE, SUBSTANTIVE, Candidate, ModeStats, execution_skip_reason, open_run,
    result_skip_reason,
)
from select_fuzz.pq.oracle import CompareOutcome, compare_result_sets
from select_fuzz.pq.report import (
    differential_evidence, evidence, render_bug_report, result_evidence,
)

Category = Literal["row_count", "null", "numeric", "string", "group", "join", "order",
                   "aggregate", "error_parity", "unknown"]


@dataclass(frozen=True, slots=True)
class CompareFinding:
    sql: str
    shape: str
    category: Category
    outcome: CompareOutcome
    pq_rows: int
    seq_rows: int
    explain_text: str
    seed: int = 0
    seq_explain_text: str = ""
    pq_triggered: bool = False
    relations: dict[str, CompareOutcome] = field(default_factory=dict)
    attribution: str = "PQ_SERIAL_DIVERGENCE"
    reference_rows: int | None = None
    decimal_columns: tuple[int, ...] = ()


@dataclass(slots=True)
class CompareStats(ModeStats):
    findings: list[CompareFinding] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    reference_executed: int = 0

    @property
    def mismatches(self) -> int:
        return len(self.findings)

    def by_category(self) -> dict[str, int]:
        return dict(Counter(f.category for f in self.findings))


def categorize(pq_result: QueryResult, seq_result: QueryResult,
               outcome: CompareOutcome, *, shape: str) -> Category:
    fixed: dict[str, Category] = {
        "ERROR_PARITY": "error_parity", "ROW_COUNT": "row_count", "ORDER": "order",
    }
    if outcome.verdict in fixed:
        return fixed[outcome.verdict]
    if outcome.verdict != "MISMATCH":
        return "unknown"
    # Compare column distributions, not unrelated positions of unordered rows.
    left, right = pq_result.rows, seq_result.rows
    width = min(len(pq_result.columns), len(seq_result.columns))
    for col in range(width):
        if sum(r[col] is None for r in left) != sum(r[col] is None for r in right):
            return "null"
    for col in range(width):
        a, b = [r[col] for r in left], [r[col] for r in right]
        if Counter(map(repr, a)) == Counter(map(repr, b)):
            continue
        if any(isinstance(v, (int, float, Decimal)) for v in a + b):
            return "aggregate" if shape in {"aggregate", "derived"} else "numeric"
        if any(isinstance(v, (str, bytes)) for v in a + b):
            return "string"
    if shape in {"aggregate", "derived", "subquery_in"}:
        return "group"
    return "join" if shape == "join" else "unknown"


def attribution(relations: dict[str, CompareOutcome]) -> str:
    verdicts = {key: val.verdict for key, val in relations.items()}
    if any(v not in COMPARABLE for v in verdicts.values()):
        return "REFERENCE_INCONCLUSIVE"
    if not any(v in SUBSTANTIVE for v in verdicts.values()):
        return "PRECISION_VARIANCE" if "PRECISION_VARIANCE" in verdicts.values() else "AGREEMENT"
    if "pq_reference" not in verdicts:
        return "PQ_SERIAL_DIVERGENCE"
    pq_ref = verdicts["pq_reference"] in SUBSTANTIVE
    seq_ref = verdicts["serial_reference"] in SUBSTANTIVE
    pair = verdicts["pq_serial"] in SUBSTANTIVE
    if not pair and pq_ref and seq_ref:
        return "SHARED_ENGINE_REFERENCE_DIVERGENCE"
    if pair and pq_ref and not seq_ref:
        return "PQ_PATH_CANDIDATE"
    if pair and seq_ref and not pq_ref:
        return "SERIAL_PATH_CANDIDATE"
    return "THREE_WAY_DIVERGENCE" if pq_ref and seq_ref else "UNRESOLVED_DIVERGENCE"


def run_compare(
    config: PQConfig, *, curated: Sequence[str | PQGeneratedQuery] | None = None,
    reference_endpoint: Endpoint | None = None, artifacts_dir: Path | None = None,
) -> CompareStats:
    stats = CompareStats()
    with open_run(config, stats, mode="compare", artifacts_dir=artifacts_dir,
                  reference_endpoint=reference_endpoint) as run:
        def process(candidate: Candidate) -> None:
            result = run.execute(candidate)
            if result is None:
                return
            reason = execution_skip_reason(result)
            if reason:
                candidate.reject(reason)
                return
            relations = {"pq_serial": result.outcome}
            ref = None
            if run.reference is not None:
                if run.expired():
                    candidate.reject("DURATION_BUDGET")
                    return
                ref = run.reference.execute(result.sql, row_limit=config.row_limit)
                stats.reference_executed += 1
                if ref.status == "ERROR":
                    candidate.mark("errors")
                candidate.record["reference_result"] = result_evidence(ref)
                ref_reason = result_skip_reason(ref, label="REFERENCE")
                for label, data in (("pq_reference", result.pq_result),
                                    ("serial_reference", result.seq_result)):
                    relations[label] = (CompareOutcome("INCONCLUSIVE", ref_reason) if ref_reason
                        else compare_result_sets(data, ref, compare_mode=candidate.query.compare_mode,
                            tolerance=config.tolerance,
                            decimal_columns=getattr(candidate.query, "decimal_columns", ())))
                if run.expired():
                    candidate.record["relations"] = evidence(relations)
                    candidate.reject("DURATION_BUDGET")
                    return
            label = attribution(relations)
            candidate.record.update(relations=evidence(relations), attribution=label)
            substantive = [(key, val) for key, val in relations.items() if val.verdict in SUBSTANTIVE]
            if not substantive and any(o.verdict not in COMPARABLE for o in relations.values()):
                candidate.reject("REFERENCE_INCONCLUSIVE: " + "; ".join(
                    o.detail for o in relations.values() if o.verdict not in COMPARABLE))
                return
            candidate.accept(list(relations.values()))
            if any(o.verdict == "PRECISION_VARIANCE" for o in relations.values()):
                stats.observations.append({"seed": result.seed, "sql": result.sql,
                    "relations": evidence(relations), "attribution": label})
            if not substantive:
                return
            decisive, outcome = substantive[0]
            left = result.seq_result if decisive == "serial_reference" else result.pq_result
            # Reference relations are added only after the reference result is available.
            right = result.seq_result if decisive == "pq_serial" else cast(QueryResult, ref)
            finding = CompareFinding(
                sql=result.sql, shape=result.shape,
                category=categorize(left, right, outcome, shape=result.shape), outcome=outcome,
                pq_rows=result.pq_result.row_count, seq_rows=result.seq_result.row_count,
                explain_text=result.explain_text, seed=result.seed,
                seq_explain_text=result.seq_explain_text, pq_triggered=result.pq_triggered,
                relations=relations, attribution=label,
                reference_rows=ref.row_count if ref is not None else None,
                decimal_columns=getattr(candidate.query, "decimal_columns", ()),
            )
            stats.findings.append(finding)
            bundle = differential_evidence(result, include_rows=True)
            bundle.update(finding=evidence(finding), relations=evidence(relations), attribution=label)
            if ref is not None:
                bundle["reference_result"] = result_evidence(ref, include_rows=True)
            run.artifacts.append("findings.jsonl", bundle)
            text = render_bug_report(result, setup_sql="", parameters=run.parameters,
                index=len(stats.findings), setup_path=run.setup_path, relations=relations,
                attribution=label, reference_result=ref)
            (run.artifacts.path / f"pq_candidate_{len(stats.findings):03d}.md").write_text(
                text, encoding="utf-8")
        try:
            run.run_queries(process, curated=curated)
        finally:
            columns = tuple(CompareFinding.__dataclass_fields__)
            run.artifacts.write_csv("pq_compare_findings.csv", columns,
                                    (evidence(finding) for finding in stats.findings))
    return stats


__all__ = ["Category", "CompareFinding", "CompareStats", "categorize", "run_compare"]
