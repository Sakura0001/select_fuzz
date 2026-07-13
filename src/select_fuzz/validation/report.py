"""Deterministic coverage artifacts and operator runbook data."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
import json
import os
from pathlib import Path
from typing import Any

from select_fuzz.validation.models import (
    EpochCheckpoint,
    FeatureSignature,
    GapRecord,
    ReachabilityResult,
    SourceCandidate,
    TelemetrySample,
)


@dataclass(frozen=True, slots=True)
class Saturation:
    signatures_per_checkpoint: tuple[int, ...]
    new_signatures_per_checkpoint: tuple[int, ...]

    @property
    def new_signatures_last_checkpoint(self) -> int:
        return self.new_signatures_per_checkpoint[-1] if self.new_signatures_per_checkpoint else 0


@dataclass(frozen=True, slots=True)
class CoverageReport:
    run_id: str
    generated_at: datetime
    sources: tuple[SourceCandidate, ...]
    signatures: tuple[FeatureSignature, ...]
    results: tuple[ReachabilityResult, ...]
    gaps: tuple[GapRecord, ...]
    checkpoints: tuple[EpochCheckpoint, ...]
    telemetry: tuple[TelemetrySample, ...]
    saturation: Saturation
    priority_counts: tuple[tuple[str, int], ...]

    @property
    def unresolved_by_priority(self) -> dict[str, int]:
        return dict(self.priority_counts)

    @property
    def status_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(result.status.value for result in self.results).items()))


def build_coverage_report(
    *,
    run_id: str,
    sources: tuple[SourceCandidate, ...],
    signatures: tuple[FeatureSignature, ...],
    results: tuple[ReachabilityResult, ...],
    gaps: tuple[GapRecord, ...],
    checkpoints: tuple[EpochCheckpoint, ...],
    telemetry: tuple[TelemetrySample, ...] = (),
    generated_at: datetime | None = None,
) -> CoverageReport:
    if not run_id:
        raise ValueError("run_id must not be empty")
    result_keys = {result.signature_key for result in results}
    signature_keys = {signature.key for signature in signatures}
    if not result_keys <= signature_keys:
        raise ValueError("results must refer to supplied signatures")
    ordered_checkpoints = tuple(sorted(checkpoints, key=lambda item: item.epoch))
    counts = tuple(checkpoint.unique_signatures for checkpoint in ordered_checkpoints)
    deltas = tuple(
        count - (counts[index - 1] if index else 0)
        for index, count in enumerate(counts)
    )
    if any(delta < 0 for delta in deltas):
        raise ValueError("checkpoint signature totals cannot decrease")
    priority_counts = tuple(sorted(Counter(gap.priority for gap in gaps).items()))
    return CoverageReport(
        run_id=run_id,
        generated_at=generated_at or datetime.now(UTC),
        sources=tuple(sorted(sources, key=lambda item: (item.url, item.content_sha256))),
        signatures=tuple(sorted(signatures, key=lambda item: item.key)),
        results=tuple(sorted(results, key=lambda item: item.signature_key)),
        gaps=tuple(sorted(gaps, key=lambda item: item.signature_key)),
        checkpoints=ordered_checkpoints,
        telemetry=tuple(sorted(telemetry, key=lambda item: (item.epoch, item.monotonic_s))),
        saturation=Saturation(counts, deltas),
        priority_counts=priority_counts,
    )


def _source_manifest(report: CoverageReport) -> list[dict[str, Any]]:
    return [
        {
            "url": source.url,
            "content_sha256": source.content_sha256,
            "fetched_at": source.fetched_at.isoformat(),
            "media_type": source.media_type,
            "offline_only": True,
        }
        for source in report.sources
    ]


def _gap_rows(report: CoverageReport) -> list[dict[str, Any]]:
    return [
        {
            "signature_key": gap.signature_key,
            "priority": gap.priority,
            "status": gap.status.value,
            "reasons": list(gap.reasons),
            "discovered_at": gap.discovered_at.isoformat(),
        }
        for gap in report.gaps
    ]


def _coverage_payload(report: CoverageReport) -> dict[str, Any]:
    by_key = {result.signature_key: result for result in report.results}
    matrix = []
    for signature in report.signatures:
        result = by_key.get(signature.key)
        matrix.append(
            {
                "signature_key": signature.key,
                "version": signature.version,
                "nodes": list(signature.nodes),
                "requirements": list(signature.requirements),
                "status": None if result is None else result.status.value,
                "witness_seed": None if result is None else result.witness_seed,
                "witness_feature_id": None if result is None else result.witness_feature_id,
            }
        )
    latest = report.telemetry[-1] if report.telemetry else None
    return {
        "run_id": report.run_id,
        "generated_at": report.generated_at.isoformat(),
        "status_counts": report.status_counts,
        "unresolved_by_priority": report.unresolved_by_priority,
        "coverage_matrix": matrix,
        "saturation": {
            "signatures_per_checkpoint": list(
                report.saturation.signatures_per_checkpoint
            ),
            "new_signatures_per_checkpoint": list(
                report.saturation.new_signatures_per_checkpoint
            ),
            "new_signatures_last_checkpoint": (
                report.saturation.new_signatures_last_checkpoint
            ),
        },
        "telemetry": {
            "samples": len(report.telemetry),
            "latest": None
            if latest is None
            else {
                "epoch": latest.epoch,
                "monotonic_s": latest.monotonic_s,
                "rss_bytes": latest.rss_bytes,
                "threads": latest.threads,
                "open_fds": latest.open_fds,
                "mysql_connections": latest.mysql_connections,
            },
        },
    }


def operator_runbook_data() -> dict[str, Any]:
    return {
        "raw_web_sql_policy": "never_execute",
        "duration": "12h",
        "checkpoint": "30m",
        "freeze": "30m",
        "commands": [
            "git status --short",
            "uv run python -m select_fuzz doctor --mode correctness --config config/local-8041.yaml",
            "uv run python scripts/validation_12h.py --duration 12h --checkpoint 30m --freeze 30m",
            "uv run pytest -q",
            "ruff check src tests scripts",
            "mypy src/select_fuzz",
            "git diff --check",
        ],
        "gap_workflow": [
            "create an isolated codex/gap-<id> worktree",
            "add and prove a failing reachability regression test",
            "implement the smallest generator change outside the running validator",
            "run focused and full gates, then commit only related files",
            "push when a remote exists and restart a new epoch at the new commit",
        ],
        "recovery": {
            "resume_from": "latest transactional checkpoint",
            "event_truth": "SQLite state plus de-duplicated append-only event_id",
            "corrupt_jsonl_tail": "ignore incomplete final line; never rewrite the ledger",
        },
    }


def write_validation_report(report: CoverageReport, output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    payloads: dict[str, bytes] = {
        "coverage.json": _json_bytes(_coverage_payload(report)),
        "source-manifest.json": _json_bytes(_source_manifest(report)),
        "gaps.json": _json_bytes(_gap_rows(report)),
        "operator-runbook.json": _json_bytes(operator_runbook_data()),
        "index.html": _render_html(report).encode(),
    }
    written: dict[str, Path] = {}
    for name, payload in payloads.items():
        path = output_dir / name
        _atomic_write(path, payload)
        written[name] = path
    return written


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        + b"\n"
    )


def _render_html(report: CoverageReport) -> str:
    rows = "".join(
        "<tr><td><code>"
        + escape(result.signature_key)
        + "</code></td><td>"
        + escape(result.status.value)
        + "</td></tr>"
        for result in report.results
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>select-fuzz validation {escape(report.run_id)}</title>
<style>body{{font:14px system-ui;margin:2rem}}table{{border-collapse:collapse}}td,th{{border:1px solid #bbb;padding:.4rem}}</style>
</head><body><h1>Validation coverage</h1>
<p>Run: <code>{escape(report.run_id)}</code></p>
<p>Unique signatures: {len(report.signatures)}; unresolved gaps: {len(report.gaps)}</p>
<table><thead><tr><th>Signature</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table>
</body></html>"""


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name("." + path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


__all__ = [
    "CoverageReport",
    "Saturation",
    "build_coverage_report",
    "operator_runbook_data",
    "write_validation_report",
]
