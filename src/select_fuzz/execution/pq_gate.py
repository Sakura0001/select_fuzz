"""Same-session PQ admission, bounded plan capture and immediate fallback checks.

The caller's statement watchdog must cover these operations. EXPLAIN confirms a
plan, not worker execution; only EXPLAIN ANALYZE additionally exposes the actual
plan. Never infer PQ merely from low cost thresholds or a requested DOP.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping

from select_fuzz.execution.protocols import QuerySession
from select_fuzz.generation.query_safety import _masked_sql
from select_fuzz.pq.detector import parse_explain


PQ_REJECTED_ERRNO = 65004
_ANALYZE = re.compile(r"^\s*EXPLAIN\s+ANALYZE\s+", re.IGNORECASE)
_READ = re.compile(r"^\s*(?:SELECT|WITH|TABLE|VALUES)\b", re.IGNORECASE)
_FALLBACK = re.compile(
    r"(?:fallback|fall\s+back|retry|retried|re.?execut).*(?:non.?pq|serial|without.*parallel)"
    r"|(?:non.?pq|serial).*(?:fallback|fall\s+back|retry)",
    re.IGNORECASE,
)


class PQAdmissionRejected(RuntimeError):
    """Internal exclusion, deliberately distinguishable from an engine defect."""

    errno = PQ_REJECTED_ERRNO
    sqlstate = "HY000"

    def __init__(self, reason: str, evidence: dict[str, object] | None = None) -> None:
        self.msg = f"PQ admission rejected: {reason}"
        self.reason = reason
        self.evidence = {**(evidence or {}), "pq_rejection": reason}
        super().__init__(self.msg)


class PQAdmissionTimeout(TimeoutError):
    errno = 3024
    sqlstate = "HYT00"
    msg = "PQ admission exceeded the query deadline"

    def __init__(self, evidence: dict[str, object] | None = None) -> None:
        self.evidence = {
            **(evidence or {}),
            "pq_rejection": "admission_timeout",
            "pq_connection_reusable": False,
        }
        super().__init__(self.msg)


def is_pq_rejection(errno: int | None, evidence: Mapping[str, object] | None) -> bool:
    return errno == PQ_REJECTED_ERRNO or bool(evidence and evidence.get("pq_rejection"))


def is_pq_workload(sql: str) -> bool:
    masked = _masked_sql(sql).lstrip(" \t\r\n(")
    return bool(_READ.match(masked) or _ANALYZE.match(masked))


def admit_pq(
    session: QuerySession,
    sql: str,
    *,
    check_deadline: Callable[[], None] = lambda: None,
) -> dict[str, object]:
    """Read a fresh plan using the session's preconfigured parameters."""
    evidence: dict[str, object] = {"pq_parameter_mode": "preconfigured"}
    cursor = None
    pending_error: Exception | None = None
    try:
        # ANALYZE executes the SELECT. Its admission must use plain EXPLAIN first.
        analyze = _ANALYZE.match(_masked_sql(sql))
        explain_sql = "EXPLAIN " + (sql[analyze.end() :] if analyze else sql)
        check_deadline()
        cursor = session.execute(explain_sql)
        rows: list[tuple[object, ...]] = []
        size = 0
        while batch := cursor.fetchmany(128):
            check_deadline()
            rows.extend(batch)
            size += sum(len(str(cell)) for row in batch for cell in row)
            if len(rows) > 10000 or size > 2 * 1024 * 1024:
                # A still-unread cursor cannot be reused safely.
                evidence["pq_connection_reusable"] = False
                try:
                    session.abort()
                except Exception as abort_error:
                    evidence["pq_abort_error"] = type(abort_error).__name__
                raise PQAdmissionRejected("plan_budget_exceeded", evidence)
        names = tuple(column.name for column in cursor.columns)
        info = parse_explain(tuple(rows), names)
        evidence["pq_plan"] = {
            "text": info.text,
            "triggered": info.triggered,
            "dop": info.dop,
            "scan_rows": info.scan_rows,
        }
        if not info.triggered:
            raise PQAdmissionRejected("not_pq", evidence)
        check_deadline()
        return evidence
    except (PQAdmissionRejected, PQAdmissionTimeout) as error:
        pending_error = error
        error.evidence = {**evidence, **error.evidence}
        raise
    except Exception as error:
        errno = getattr(error, "errno", None)
        if errno in {1317, 3024} or isinstance(error, TimeoutError):
            pending_error = PQAdmissionTimeout(evidence)
            raise pending_error from error
        if not isinstance(errno, int) or 2000 <= errno < 3000:
            pending_error = error
            raise
        evidence["pq_explain_error"] = str(error)
        pending_error = PQAdmissionRejected("explain_error", evidence)
        raise pending_error from error
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception as close_error:
                cleanup = {
                    "pq_cleanup_error": type(close_error).__name__,
                    "pq_connection_reusable": False,
                }
                if isinstance(pending_error, (PQAdmissionRejected, PQAdmissionTimeout)):
                    pending_error.evidence.update(cleanup)
                elif pending_error is None:
                    raise PQAdmissionRejected(
                        "plan_cleanup_error", {**evidence, **cleanup}
                    ) from close_error


def check_pq_warnings(warnings: tuple[str, ...], evidence: dict[str, object]) -> None:
    if any(_FALLBACK.search(warning) for warning in warnings):
        raise PQAdmissionRejected("runtime_fallback", evidence)


def check_pq_analyze(
    sql: str,
    rows: tuple[tuple[object, ...], ...],
    names: tuple[str, ...],
    evidence: dict[str, object],
) -> None:
    if _ANALYZE.match(_masked_sql(sql)):
        info = parse_explain(rows, names)
        evidence["pq_runtime_plan"] = info.text
        if not info.triggered:
            raise PQAdmissionRejected("runtime_plan_not_pq", evidence)
