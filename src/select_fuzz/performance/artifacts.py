"""Serialization adapter for compact and diagnostic performance records."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
from threading import Lock
from uuid import uuid4
from typing import Protocol

from select_fuzz.artifacts.jsonl import assert_no_sensitive_keys
from select_fuzz.artifacts.sql_script import (
    SourceableSqlWriter,
    write_difference_summary,
    write_minimal_failure_script,
)
from select_fuzz.config import COMPARISON_ROLES, NodeRole
from select_fuzz.performance.calibration import CalibrationTerminated
from select_fuzz.performance.models import Assessment, FormalRun, FrozenCase, Verdict


class RecordSink(Protocol):
    def append(self, record: Mapping[str, object]) -> None: ...


class DiagnosticSink(Protocol):
    def write(
        self,
        case_id: str,
        manifest: Mapping[str, object],
        files: Mapping[str, bytes],
    ) -> object: ...


def _measurement_record(run: FormalRun, role: NodeRole) -> dict[str, object]:
    item = run.measurements[role]
    return {
        "outcome": item.outcome.value,
        "root_end_ms": item.root_end_ms,
        "wall_time_ms": item.wall_time_ms,
        "started_ns": item.started_ns,
        "ended_ns": item.ended_ns,
        "connection_id": item.connection_id,
        "watchdog_fired": item.watchdog_fired,
        "error_code": item.error_code,
        "error_type": item.error_type,
        "metrics": {} if item.metrics is None else dict(item.metrics),
    }


def _artifact_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _artifact_value(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _artifact_value(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_artifact_value(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported performance artifact value: {type(value).__name__}")


def _compact_manifest(value: object) -> object:
    encoded = _artifact_value(value)
    if not (is_dataclass(value) and not isinstance(value, type)) or not isinstance(encoded, dict):
        return encoded
    statements = encoded.pop("setup_statements", None)
    if isinstance(statements, list):
        payload = "\n".join(str(statement) for statement in statements).encode("utf-8")
        encoded["setup_statement_count"] = len(statements)
        encoded["setup_sql_sha256"] = sha256(payload).hexdigest()
    return encoded


def _calibration_record(frozen: FrozenCase) -> list[dict[str, object]]:
    return [
        {
            "number": attempt.number,
            "scale": attempt.scale.as_dict(),
            "sql": attempt.sql,
            "samples_seconds": {
                role.value: list(values) for role, values in attempt.samples_seconds.items()
            },
            "failure_categories": {
                role.value: list(values) for role, values in attempt.failure_categories.items()
            },
            "medians_seconds": {
                role.value: value for role, value in attempt.medians_seconds.items()
            },
        }
        for attempt in frozen.attempts
    ]


def compact_record(
    frozen: FrozenCase,
    run: FormalRun,
    assessment: Assessment,
    node_config_fingerprints: Mapping[NodeRole, str],
    *,
    run_id: str,
    occurred_at: str,
) -> dict[str, object]:
    return {
        "type": (
            "performance_result" if assessment.verdict is Verdict.PASS else "performance_alert"
        ),
        "case_id": frozen.case_id,
        "run_id": run_id,
        "occurred_at": occurred_at,
        "template_id": frozen.template_id,
        "seed": frozen.seed,
        "database": frozen.database,
        "sql": frozen.sql,
        "scale": frozen.scale.as_dict(),
        "data_manifest": _compact_manifest(frozen.data_manifest),
        "node_config_fingerprints": {
            role.value: node_config_fingerprints[role] for role in COMPARISON_ROLES
        },
        "calibration": _calibration_record(frozen),
        "cache_state": "unverified",
        "start_skew_ms": run.start_skew_ms,
        "verdict": assessment.verdict.value,
        "reasons": list(assessment.reasons),
        "measurements": {
            role.value: _measurement_record(run, role) for role in COMPARISON_ROLES
        },
    }


class PerformanceRecorder:
    def __init__(
        self,
        records: RecordSink,
        diagnostics: DiagnosticSink | None = None,
        *,
        run_id: str,
        node_config_fingerprints: Mapping[NodeRole, str],
        sql_root: str | Path | None = None,
        now: Callable[[], str] = lambda: datetime.now(UTC).isoformat(),
    ) -> None:
        if not run_id:
            raise ValueError("run_id must not be empty")
        if set(node_config_fingerprints) != set(COMPARISON_ROLES) or any(
            not value for value in node_config_fingerprints.values()
        ):
            raise ValueError("two nonempty comparison fingerprints are required")
        self._records = records
        self._diagnostics = diagnostics
        self._node_config_fingerprints = dict(node_config_fingerprints)
        self._run_id = run_id
        self._now = now
        self._sql_root = None if sql_root is None else Path(sql_root)
        self._round_writers: dict[str, SourceableSqlWriter] = {}
        self._round_writers_lock = Lock()

    @staticmethod
    def _setup_statements(manifest: object) -> tuple[str, ...]:
        statements = getattr(manifest, "setup_statements", ())
        return tuple(statements) if isinstance(statements, tuple) else ()

    def _write_case_sql(self, frozen: FrozenCase) -> None:
        if self._sql_root is None:
            return
        with self._round_writers_lock:
            writer = self._round_writers.get(frozen.database)
            if writer is None:
                writer = SourceableSqlWriter(
                    self._sql_root / "rounds" / f"{frozen.database}.sql",
                    frozen.database,
                    metadata={
                        "run_id": self._run_id,
                        "round_seed": getattr(frozen.data_manifest, "seed", frozen.seed),
                    },
                )
                for statement in self._setup_statements(frozen.data_manifest):
                    if statement.lstrip().upper().startswith("CREATE PROCEDURE "):
                        writer.append_routine(statement)
                    else:
                        writer.append_single_line_statement(statement)
                self._round_writers[frozen.database] = writer
            writer.append_single_line_statement(
                f"EXPLAIN ANALYZE FORMAT=TREE {frozen.sql.rstrip().rstrip(';')}"
            )

    def record(
        self, frozen: FrozenCase, run: FormalRun, assessment: Assessment
    ) -> Mapping[str, object]:
        record = compact_record(
            frozen,
            run,
            assessment,
            self._node_config_fingerprints,
            run_id=self._run_id,
            occurred_at=self._now(),
        )
        self._write_case_sql(frozen)
        self._records.append(record)
        if assessment.verdict is not Verdict.PASS and self._sql_root is not None:
            failure_root = self._sql_root / "performance_failures" / frozen.case_id
            write_minimal_failure_script(
                failure_root / "case.sql",
                database=frozen.database,
                setup_statements=self._setup_statements(frozen.data_manifest),
                failing_query=(f"EXPLAIN ANALYZE FORMAT=TREE {frozen.sql.rstrip().rstrip(';')}"),
                metadata={
                    "case_id": frozen.case_id,
                    "run_id": self._run_id,
                    "verdict": assessment.verdict.value,
                },
            )
            write_difference_summary(
                failure_root / "case.diff",
                {
                    "case_id": frozen.case_id,
                    "reasons": list(assessment.reasons),
                    "verdict": assessment.verdict.value,
                },
            )
        if assessment.verdict is not Verdict.PASS and self._diagnostics is not None:
            files: dict[str, bytes] = {
                f"plans/{role.value}.tree": (run.measurements[role].tree or "").encode("utf-8")
                for role in COMPARISON_ROLES
            }
            files["calibration.json"] = json.dumps(
                record["calibration"], sort_keys=True, allow_nan=False
            ).encode("utf-8")
            files["diagnostics/metrics.json"] = json.dumps(
                record["measurements"], sort_keys=True, allow_nan=False
            ).encode("utf-8")
            files["setup/manifest.json"] = json.dumps(
                record["data_manifest"], sort_keys=True, allow_nan=False
            ).encode("utf-8")
            self._diagnostics.write(frozen.case_id, record, files)
        return record

    def record_calibration_failure(
        self,
        template: object,
        attempts: object,
        failure: CalibrationTerminated | None = None,
        *,
        attempt_number: int = 1,
    ) -> None:
        if (
            not isinstance(attempt_number, int)
            or isinstance(attempt_number, bool)
            or attempt_number <= 0
        ):
            raise ValueError("attempt_number must be positive")
        case_id = getattr(template, "case_id", "unknown")
        attempt_items = attempts if isinstance(attempts, tuple) else ()
        calibration = [
            {
                "number": attempt.number,
                "scale": attempt.scale.as_dict(),
                "sql": attempt.sql,
                "samples_seconds": {
                    role.value: list(values) for role, values in attempt.samples_seconds.items()
                },
                "failure_categories": {
                    role.value: list(values) for role, values in attempt.failure_categories.items()
                },
            }
            for attempt in attempt_items
            if hasattr(attempt, "scale")
        ]
        data_manifest = None if failure is None else failure.data_manifest
        record: dict[str, object] = {
            "type": "performance_calibration_failure",
            "case_id": str(case_id),
            "run_id": self._run_id,
            "occurred_at": self._now(),
            "diagnostic_attempt": attempt_number,
            "template_id": str(getattr(template, "template_id", "unknown")),
            "seed": getattr(template, "seed", None),
            "attempt_count": len(attempt_items),
            "failure_category": None if failure is None else failure.kind.value,
            "failure_role": None if failure is None else failure.role.value,
            "error_code": None if failure is None else failure.error_code,
            "error_type": None if failure is None else failure.error_type,
            "database": None if failure is None else failure.database,
            "failing_action_sql": (None if failure is None else failure.failing_action_sql),
            "failure_details": (
                {} if failure is None else _artifact_value(failure.failure_details)
            ),
            "scale": (
                None if failure is None or failure.scale is None else failure.scale.as_dict()
            ),
            "sql": None if failure is None else failure.sql,
            "data_manifest": (None if data_manifest is None else _compact_manifest(data_manifest)),
            "node_config_fingerprints": {
                role.value: self._node_config_fingerprints[role]
                for role in COMPARISON_ROLES
            },
            "calibration": calibration,
            "cache_state": "unverified",
        }
        self._records.append(record)
        if self._sql_root is not None and failure is not None:
            statements = self._setup_statements(data_manifest)
            if statements and failure.sql:
                failure_root = self._sql_root / "performance_failures" / str(case_id)
                write_minimal_failure_script(
                    failure_root / "case.sql",
                    database=failure.database or "sf_performance_failure",
                    setup_statements=statements,
                    failing_query=failure.sql,
                    metadata={
                        "case_id": str(case_id),
                        "run_id": self._run_id,
                        "verdict": failure.kind.value,
                    },
                )
                write_difference_summary(
                    failure_root / "case.diff",
                    {
                        "case_id": str(case_id),
                        "error_type": failure.error_type,
                        "failure_category": failure.kind.value,
                        "failure_role": failure.role.value,
                        "database": failure.database,
                        "failing_action_sql": failure.failing_action_sql,
                        "failure_details": _artifact_value(failure.failure_details),
                    },
                )
        if self._diagnostics is not None:
            files = {
                "calibration.json": json.dumps(calibration, sort_keys=True, allow_nan=False).encode(
                    "utf-8"
                )
            }
            if data_manifest is not None:
                files["setup/manifest.json"] = json.dumps(
                    record["data_manifest"], sort_keys=True, allow_nan=False
                ).encode("utf-8")
            self._diagnostics.write(f"{case_id}_attempt_{attempt_number}", record, files)


_CASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,190}$")


def _write_file(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:  # pragma: no cover - OS contract defense
                raise OSError("diagnostic write returned no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class PerformanceDiagnosticWriter:
    """Atomically publish a complete performance alert reproduction directory."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._lock = Lock()

    def write(
        self,
        case_id: str,
        manifest: Mapping[str, object],
        files: Mapping[str, bytes],
    ) -> Path:
        if _CASE_ID.fullmatch(case_id) is None:
            raise ValueError("performance case_id is unsafe")
        assert_no_sensitive_keys(manifest)
        manifest_payload = json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        validated: dict[PurePosixPath, bytes] = {}
        for name, payload in files.items():
            relative = PurePosixPath(name)
            if (
                relative.is_absolute()
                or not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)
                or relative.parts == ("manifest.json",)
            ):
                raise ValueError("diagnostic file path is unsafe")
            if not isinstance(payload, bytes):
                raise TypeError("diagnostic payloads must be bytes")
            validated[relative] = payload
        parent = self.root / "performance_findings"
        final = parent / case_id
        temporary = parent / f".{case_id}.tmp-{uuid4().hex}"
        with self._lock:
            parent.mkdir(parents=True, exist_ok=True)
            if final.exists():
                existing_files = {
                    path.relative_to(final).as_posix(): path.read_bytes()
                    for path in final.rglob("*")
                    if path.is_file() and path.name != "manifest.json"
                }
                try:
                    existing_manifest = (final / "manifest.json").read_bytes()
                except OSError as error:
                    raise FileExistsError(
                        f"performance diagnostic exists but is incomplete: {case_id}"
                    ) from error
                expected_files = {
                    relative.as_posix(): payload for relative, payload in validated.items()
                }
                if existing_manifest == manifest_payload and existing_files == expected_files:
                    return final
                raise FileExistsError(f"performance diagnostic conflict: {case_id}")
            temporary.mkdir(mode=0o700)
            try:
                _write_file(temporary / "manifest.json", manifest_payload)
                for relative, payload in validated.items():
                    _write_file(temporary.joinpath(*relative.parts), payload)
                for directory in sorted(
                    (path for path in temporary.rglob("*") if path.is_dir()),
                    key=lambda path: len(path.parts),
                    reverse=True,
                ):
                    _fsync_directory(directory)
                _fsync_directory(temporary)
                os.replace(temporary, final)
                _fsync_directory(parent)
            except Exception:
                if temporary.exists():
                    shutil.rmtree(temporary)
                raise
        return final


__all__ = [
    "DiagnosticSink",
    "PerformanceRecorder",
    "PerformanceDiagnosticWriter",
    "RecordSink",
    "compact_record",
]
