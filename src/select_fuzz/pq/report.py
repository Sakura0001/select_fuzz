"""Lossless evidence bundles and reports for PQ discrepancy candidates."""

from __future__ import annotations

import base64
import csv
import json
from dataclasses import fields, is_dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, cast
from uuid import uuid4

from select_fuzz.pq.runner import DifferentialResult

_SECRET_KEYS = {"password", "passwd", "pwd", "user", "username", "token", "secret",
                "credentials", "authorization", "private_key"}


def evidence(value: Any, *, redact: bool = False) -> Any:
    """Make JSON evidence without rounding decimals or conflating bytes and text."""
    if is_dataclass(value) and not isinstance(value, type):
        value = {f.name: getattr(value, f.name) for f in fields(value)}
    elif hasattr(value, "__dict__"):
        value = vars(value)
    if isinstance(value, Mapping):
        return {str(k): ("<redacted>" if redact and any(
            secret in str(k).lower() for secret in _SECRET_KEYS
        ) else evidence(v, redact=redact)) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [evidence(v, redact=redact) for v in value]
    if isinstance(value, Decimal):
        return {"type": "decimal", "value": str(value)}
    if isinstance(value, (bytes, bytearray)):
        return {"type": "bytes", "base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (datetime, date, time)):
        return {"type": type(value).__name__, "value": value.isoformat()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and (value != value or abs(value) == float("inf")):
        return {"type": "float", "value": repr(value)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {"type": type(value).__name__, "value": str(value)}


def json_text(value: Any, *, redact: bool = False) -> str:
    return json.dumps(evidence(value, redact=redact), ensure_ascii=False, allow_nan=False)


def result_evidence(result: Any, *, include_rows: bool = False) -> dict[str, Any]:
    data = {name: getattr(result, name, None) for name in (
        "status", "columns", "type_codes", "error", "errno", "elapsed_ns", "elapsed_ms",
        "row_count", "total_rows", "complete", "warnings", "warnings_complete", "fallback",
    )}
    if include_rows:
        data["rows"] = result.rows
    return cast(dict[str, Any], evidence(data))


def differential_evidence(result: Any, *, include_rows: bool = False) -> dict[str, Any]:
    return cast(dict[str, Any], evidence({
        "sql": result.sql, "seed": getattr(result, "seed", 0), "shape": result.shape,
        "pq_triggered": result.pq_triggered, "pq_plan": result.explain_text,
        "serial_plan": getattr(result, "seq_explain_text", ""),
        "skip_reason": getattr(result, "skip_reason", ""), "outcome": result.outcome,
        "plan_dop": getattr(result, "plan_dop", 0),
        "scan_rows": getattr(result, "scan_rows", None), "scan_rows_kind": "explain_estimate",
        "execution_evidence": getattr(result, "evidence", "unknown"),
        "decimal_columns": getattr(result, "decimal_columns", ()),
        "pq_result": result_evidence(result.pq_result, include_rows=include_rows),
        "serial_result": result_evidence(result.seq_result, include_rows=include_rows),
    }))


class RunArtifacts:
    """Reserve a dedicated directory; append evidence even when every plan fails."""

    def __init__(self, directory: Path | None, *, mode: str) -> None:
        root = Path(directory) if directory is not None else Path("artifacts/pq")
        root.mkdir(parents=True, exist_ok=True)
        while True:
            path = root
            if any(root.iterdir()):
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
                path = root / f"{mode}-{stamp}-{uuid4().hex[:12]}"
                path.mkdir()
            try:
                (path / "attempts.jsonl").open("x", encoding="utf-8").close()
            except FileExistsError:
                continue
            self.path = path.resolve()
            break
        for name in ("findings.jsonl", "samples.jsonl"):
            (self.path / name).open("x", encoding="utf-8").close()

    def append(self, name: str, value: Any) -> None:
        with (self.path / name).open("a", encoding="utf-8") as fh:
            fh.write(json_text(value) + "\n")

    def write_json(self, name: str, value: Any) -> None:
        with (self.path / name).open("x", encoding="utf-8") as fh:
            fh.write(json_text(value) + "\n")

    def write_csv(self, name: str, columns: Iterable[str],
                  rows: Iterable[Mapping[str, Any]]) -> None:
        with (self.path / name).open("x", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(columns))
            writer.writeheader()
            for row in rows:
                writer.writerow({k: json_text(v) if isinstance(v, (dict, list, tuple)) else v
                                 for k, v in row.items()})


def parameters_text(config: Any = None, snapshot: Any = None) -> str:
    if config is None and snapshot is None:
        return "No configuration/session evidence supplied."
    return json.dumps(evidence({"requested_config": config, "observed_sessions": snapshot},
                               redact=True), ensure_ascii=False, indent=2)


def render_bug_report(
    result: DifferentialResult,
    *,
    setup_sql: str,
    parameters: str,
    index: int,
    setup_path: Path | None = None,
    relations: Mapping[str, Any] | None = None,
    attribution: str = "PQ_SERIAL_DIVERGENCE",
    reference_result: Any = None,
) -> str:
    """Keep the historic API name; a single discrepancy is a candidate for review."""
    setup = (f"Complete deterministic schema and INSERT data: [setup.sql]({setup_path}).\n"
             f"Explicit cleanup script: [cleanup.sql]({setup_path.parent / 'cleanup.sql'}).\n"
             if setup_path else f"```sql\n{setup_sql}\n```\n")
    blocks = [
        f"# PQ discrepancy candidate #{index}\n",
        f"Shape: {result.shape}; seed: {getattr(result, 'seed', 0)}; "
        f"verdict: {result.outcome.verdict}; attribution: {attribution}.\n",
        "## Reproduction data\n" + setup,
        "## Requested configuration and observed sessions\n```json\n" + parameters + "\n```\n",
        f"## SQL\n```sql\n{result.sql}\n```\n",
        f"## PQ EXPLAIN\n```\n{result.explain_text}\n```\n",
        f"## Serial EXPLAIN\n```\n{getattr(result, 'seq_explain_text', '')}\n```\n",
        f"PQ gate accepted: {result.pq_triggered}. EXPLAIN is planning evidence; "
        "captured warnings provide fallback evidence. Actual worker activity was not measured.\n",
        f"## Exact comparison evidence\n{result.outcome.detail}\n",
    ]
    if result.outcome.first_diff:
        blocks.append("```\n" + result.outcome.first_diff.render() + "\n```\n")
    if relations:
        blocks.append("Relations:\n```json\n" + json_text(relations) + "\n```\n")
    for label, data in (("PQ", result.pq_result), ("Serial", result.seq_result),
                        ("Reference", reference_result)):
        if data is not None:
            blocks.append(f"## {label} result and warnings\n```json\n"
                          + json_text(result_evidence(data)) + "\n```\n"
                          + "Row preview (typed values; full rows in findings.jsonl):\n```\n"
                          + _format_rows(data.rows, limit=20) + "\n```\n")
    blocks.append("## Interpretation\nThis is a discrepancy candidate. Repeat and reduce the SQL "
                  "and fixture before attributing an engine defect. Precision observations and "
                  "inconclusive executions are recorded separately.\n")
    return "\n".join(blocks)


def _format_rows(rows: tuple[tuple[Any, ...], ...], *, limit: int) -> str:
    lines = [repr(row) for row in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"... ({len(rows) - limit} more rows in findings.jsonl)")
    return "\n".join(lines) if lines else "(no rows)"


__all__ = ["parameters_text", "render_bug_report"]
