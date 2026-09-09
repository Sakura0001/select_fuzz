"""Shared, freshly gated PQ/serial execution with admissibility evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from select_fuzz.pq.config import PQConfig
from select_fuzz.pq.connection import PQConnection, QueryResult, open_pair
from select_fuzz.pq.detector import ExplainInfo, parse_explain
from select_fuzz.pq.oracle import CompareOutcome, compare_result_sets


@dataclass(slots=True)
class DifferentialResult:
    sql: str
    explain_text: str
    pq_triggered: bool
    pq_result: QueryResult
    seq_result: QueryResult
    outcome: CompareOutcome
    shape: str = ""
    seq_explain_text: str = ""
    skip_reason: str = ""
    seed: int = 0
    decimal_columns: tuple[int, ...] = ()
    plan_dop: int = 0
    scan_rows: int | None = None
    evidence: str = "plan_confirmed_no_reported_fallback"

    @property
    def is_mismatch(self) -> bool:
        return self.outcome.verdict in {"MISMATCH", "ERROR_PARITY", "ROW_COUNT", "ORDER"}


class DifferentialRunner:
    def __init__(self, pq_conn: PQConnection, seq_conn: PQConnection, *, config: PQConfig) -> None:
        self._pq = pq_conn
        self._seq = seq_conn
        self._config = config

    def explain(self, sql: str, *, serial: bool = False) -> ExplainInfo:
        conn = self._seq if serial else self._pq
        result = conn.execute(f"EXPLAIN {sql}", row_limit=10_000)
        if result.status != "OK" or not result.complete:
            return ExplainInfo(text=result.error, triggered=False,
                               error=result.error or "incomplete EXPLAIN")
        return parse_explain(result.rows, result.columns)

    def use_database(self, database: str) -> None:
        self._pq.use_database(database)
        self._seq.use_database(database)

    def set_dop(self, dop: int) -> None:
        raise ValueError("DOP is preconfigured; this harness cannot change runtime parameters")

    def run_differential(
        self, sql: str, *, compare_mode: str, decimal_columns: tuple[int, ...] = (),
        reverse: bool = False,
    ) -> DifferentialResult:
        # Even if a caller preflighted this SQL, gate the actual execution here.
        # Configuration and endpoint labels do not prove a serial execution plan.
        info = self.explain(sql)
        seq_info = self.explain(sql, serial=True)
        reason = ""
        if info.error or seq_info.error:
            reason = "explain_error"
        elif not info.triggered:
            reason = "not_pq"
        elif seq_info.triggered:
            reason = "serial_plan_is_pq"
        pq = seq = QueryResult("SKIPPED", (), (), (), complete=False)
        if not reason:
            first, second = (self._seq, self._pq) if reverse else (self._pq, self._seq)
            a = first.execute(sql, row_limit=self._config.row_limit)
            b = second.execute(sql, row_limit=self._config.row_limit)
            pq, seq = (b, a) if reverse else (a, b)
            if pq.fallback or seq.fallback:
                reason = "runtime_fallback"
            elif any(r.status == "OK" and not r.complete for r in (pq, seq)):
                reason = "incomplete_result"
            elif any(r.status == "OK" and not r.warnings_complete for r in (pq, seq)):
                reason = "warnings_unavailable"
        if reason:
            outcome = CompareOutcome("INCONCLUSIVE", reason)
        else:
            outcome = compare_result_sets(
                pq, seq, compare_mode=compare_mode, tolerance=self._config.tolerance,
                decimal_columns=decimal_columns,
            )
            if outcome.verdict in {"BUDGET", "INCONCLUSIVE"}:
                reason = "comparison_inconclusive"
        return DifferentialResult(
            sql=sql, explain_text=info.text, pq_triggered=info.triggered and not reason,
            pq_result=pq, seq_result=seq, outcome=outcome,
            seq_explain_text=seq_info.text, skip_reason=reason,
            decimal_columns=decimal_columns, plan_dop=info.dop, scan_rows=info.scan_rows,
            evidence="excluded" if reason else "plan_confirmed_no_reported_fallback",
        )

    def snapshot(self) -> dict[str, Any]:
        return {"pq": self._pq.snapshot(), "serial": self._seq.snapshot(),
                "execution_evidence": "EXPLAIN plus immediate warnings; not worker runtime telemetry"}

    def close(self) -> None:
        self._pq.close()
        self._seq.close()


def build_runner(config: PQConfig) -> DifferentialRunner:
    pq_conn, seq_conn = open_pair(config)
    return DifferentialRunner(pq_conn, seq_conn, config=config)


__all__ = ["DifferentialResult", "DifferentialRunner", "ExplainInfo", "build_runner"]
