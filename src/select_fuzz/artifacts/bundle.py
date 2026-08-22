"""Atomic compact-pass and complete-finding publication."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import fcntl
import gzip
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import shutil
from threading import Lock
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from select_fuzz.artifacts.jsonl import JsonlWriter, assert_no_sensitive_keys
from select_fuzz.artifacts.query_log import WorkerQueryLogWriter
from select_fuzz.artifacts.sql_script import (
    MAX_DIFF_BYTES,
    MAX_DIFF_ROWS,
    SourceableSqlWriter,
    WorkerSqlLogWriter,
    write_difference_summary,
    write_minimal_failure_script,
)
from select_fuzz.config import COMPARISON_ROLES, NodeRole
from select_fuzz.domain import ExecutionStatus, NodeExecution
from select_fuzz.execution.setup import validate_database_name
from select_fuzz.execution.triad import QueryLimits
from select_fuzz.generation.query_contract import ExpectedErrorKind


_ARTIFACT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SQLSTATE = re.compile(r"^[0-9A-Z]{5}$")
MAX_RESULT_BYTES = 64 * 1024 * 1024
_GENERATOR_CONTRACT_VERDICTS = frozenset({"expected_error_mismatch", "unexpected_valid_error"})


def _encode_artifact_value(value: object) -> object:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return {"$type": "int", "value": str(value)}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite floats cannot be stored in result artifacts")
        return {"$type": "float", "hex": value.hex()}
    if isinstance(value, Decimal):
        return {"$type": "decimal", "value": str(value)}
    if isinstance(value, bytes):
        return {"$type": "bytes", "hex": value.hex()}
    if isinstance(value, datetime):
        return {"$type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"$type": "date", "value": value.isoformat()}
    if isinstance(value, time):
        return {"$type": "time", "value": value.isoformat()}
    if isinstance(value, timedelta):
        microseconds = (value.days * 86_400 + value.seconds) * 1_000_000 + value.microseconds
        return {"$type": "timedelta", "microseconds": str(microseconds)}
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("result mapping cells require string keys")
        return {
            "$type": "mapping",
            "items": [[key, _encode_artifact_value(value[key])] for key in sorted(value)],
        }
    if isinstance(value, (tuple, list)):
        return {
            "$type": "sequence",
            "items": [_encode_artifact_value(child) for child in value],
        }
    if isinstance(value, (set, frozenset)):
        children = [_encode_artifact_value(child) for child in value]
        children.sort(
            key=lambda child: json.dumps(
                child, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
        )
        return {"$type": "set", "items": children}
    raise TypeError(f"unsupported result artifact value: {type(value).__qualname__}")


def artifact_cell_to_value(value: object) -> object:
    """Decode the closed lossless cell representation used in result bundles."""

    if value is None or isinstance(value, (bool, str)):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("encoded artifact cell must be primitive or tagged object")
    kind = value.get("$type")
    if kind == "int":
        return int(str(value.get("value")))
    if kind == "float":
        return float.fromhex(str(value.get("hex")))
    if kind == "decimal":
        return Decimal(str(value.get("value")))
    if kind == "bytes":
        return bytes.fromhex(str(value.get("hex")))
    if kind == "datetime":
        return datetime.fromisoformat(str(value.get("value")))
    if kind == "date":
        return date.fromisoformat(str(value.get("value")))
    if kind == "time":
        return time.fromisoformat(str(value.get("value")))
    if kind == "timedelta":
        return timedelta(microseconds=int(str(value.get("microseconds"))))
    if kind in {"sequence", "set"}:
        items = value.get("items")
        if not isinstance(items, list):
            raise ValueError("encoded artifact collection requires an items list")
        decoded = tuple(artifact_cell_to_value(child) for child in items)
        return decoded if kind == "sequence" else frozenset(decoded)
    if kind == "mapping":
        items = value.get("items")
        if not isinstance(items, list):
            raise ValueError("encoded artifact mapping requires an items list")
        decoded_mapping: dict[str, object] = {}
        for item in items:
            if not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str):
                raise ValueError("encoded artifact mapping item is invalid")
            decoded_mapping[item[0]] = artifact_cell_to_value(item[1])
        return MappingProxyType(decoded_mapping)
    raise ValueError("encoded artifact cell has an unknown type tag")


def node_execution_to_artifact(execution: NodeExecution) -> dict[str, object]:
    """Serialize one complete typed execution without lossy JSON coercion."""

    if not isinstance(execution, NodeExecution):
        raise TypeError("execution must be NodeExecution")
    columns = [
        {
            "binary": column.binary,
            "character_set_id": column.character_set_id,
            "column_length": column.column_length,
            "decimals": column.decimals,
            "flags": column.flags,
            "name": column.name,
            "nullable": column.nullable,
            "type_code": column.type_code,
            "unsigned": column.unsigned,
        }
        for column in execution.columns
    ]
    error = (
        None
        if execution.error is None
        else {
            "errno": execution.error.errno,
            "message": execution.error.message,
            "sqlstate": execution.error.sqlstate,
        }
    )
    return {
        "affected_rows": execution.affected_rows,
        "columns": columns,
        "connection_id": execution.connection_id,
        "connection_reusable": execution.connection_reusable,
        "elapsed_ns": execution.elapsed_ns,
        "ended_ns": execution.ended_ns,
        "error": error,
        "failure_evidence": (
            None if execution.failure_evidence is None else dict(execution.failure_evidence)
        ),
        "performance_payload": (
            None
            if execution.performance_payload is None
            else _encode_artifact_value(execution.performance_payload)
        ),
        "role": execution.role.value,
        "rows": [[_encode_artifact_value(cell) for cell in row] for row in execution.rows],
        "started_ns": execution.started_ns,
        "status": execution.status.value,
        "warnings": list(execution.warnings),
        "watchdog_error_type": execution.watchdog_error_type,
        "watchdog_fired": execution.watchdog_fired,
    }


def _validate_id(value: object, label: str) -> str:
    if not isinstance(value, str) or _ARTIFACT_ID.fullmatch(value) is None:
        raise ValueError(f"{label} must be a safe lowercase artifact identifier")
    return value


def _validate_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _comparison_role_mapping(
    value: Mapping[NodeRole, Any], label: str
) -> Mapping[NodeRole, Any]:
    if not isinstance(value, Mapping) or set(value) != set(COMPARISON_ROLES):
        raise ValueError(f"{label} must contain custom_off and custom_on")
    return MappingProxyType({role: value[role] for role in COMPARISON_ROLES})


def _canonical_json(value: object, *, max_bytes: int = MAX_RESULT_BYTES) -> bytes:
    assert_no_sensitive_keys(value)
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise type(error)(f"artifact is not strict JSON: {error}") from error
    if len(payload) > max_bytes:
        raise ValueError(f"artifact JSON exceeds the {max_bytes}-byte safety limit")
    return payload


def _error_identity(value: object, label: str) -> tuple[int, str]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    errno = value.get("errno")
    sqlstate = value.get("sqlstate")
    if not isinstance(errno, int) or isinstance(errno, bool) or not 0 <= errno <= 0xFFFF:
        raise ValueError(f"{label} errno must be an unsigned 16-bit integer")
    if not isinstance(sqlstate, str) or _SQLSTATE.fullmatch(sqlstate) is None:
        raise ValueError(f"{label} sqlstate must be five uppercase alphanumerics")
    return errno, sqlstate


def _generator_contract_details(
    first_difference: Mapping[str, object],
) -> (
    tuple[
        str,
        tuple[tuple[int, str] | None, ...],
        tuple[int, str] | None,
    ]
    | None
):
    if first_difference.get("category") != "generator_contract":
        return None
    reason = first_difference.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("generator_contract reason must be a nonempty string")
    if "observed_identities" not in first_difference:
        raise ValueError("generator_contract requires observed_identities")
    raw_observed = first_difference["observed_identities"]
    if not isinstance(raw_observed, (tuple, list)) or len(raw_observed) != len(
        COMPARISON_ROLES
    ):
        raise ValueError("generator_contract observed_identities require two roles")
    observed = tuple(
        None if identity is None else _error_identity(identity, f"observed_identities[{index}]")
        for index, identity in enumerate(raw_observed)
    )
    if "expected_error" not in first_difference:
        raise ValueError("generator_contract requires expected_error")
    raw_expected = first_difference["expected_error"]
    if raw_expected is None:
        expected = None
    else:
        if not isinstance(raw_expected, Mapping):
            raise TypeError("expected_error must be a mapping")
        expected = _error_identity(raw_expected, "expected_error")
        raw_kind = raw_expected.get("kind")
        if not isinstance(raw_kind, str):
            raise ValueError("expected_error kind must be an ExpectedErrorKind")
        try:
            ExpectedErrorKind(raw_kind)
        except ValueError as error:
            raise ValueError("expected_error kind must be an ExpectedErrorKind") from error
    return reason, observed, expected


def _validate_generator_contract_finding(
    original_verdict: str,
    first_difference: Mapping[str, object],
) -> None:
    is_generator_verdict = original_verdict in _GENERATOR_CONTRACT_VERDICTS
    is_generator_contract = first_difference.get("category") == "generator_contract"
    if is_generator_verdict != is_generator_contract:
        raise ValueError("generator-contract verdicts and details must be paired")
    if not is_generator_contract:
        return
    _generator_contract_details(first_difference)
    expected_error = first_difference["expected_error"]
    if original_verdict == "expected_error_mismatch":
        if not isinstance(expected_error, Mapping):
            raise ValueError("expected_error_mismatch requires an expected error contract")
    elif expected_error is not None:
        raise ValueError("unexpected_valid_error cannot contain an expected error contract")


@dataclass(frozen=True, slots=True)
class PassRecord:
    case_id: str
    run_id: str
    database: str
    seed: int
    query_sql: str
    row_count: int
    result_digest: str
    column_metadata_digest: str
    elapsed_ns_by_role: Mapping[NodeRole, int]
    coverage_tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_id(self.case_id, "case_id")
        _validate_id(self.run_id, "run_id")
        validate_database_name(self.database)
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise ValueError("seed must be an integer")
        if not isinstance(self.query_sql, str) or not self.query_sql.strip():
            raise ValueError("query_sql must not be empty")
        if (
            not isinstance(self.row_count, int)
            or isinstance(self.row_count, bool)
            or self.row_count < 0
        ):
            raise ValueError("row_count must be nonnegative")
        _validate_digest(self.result_digest, "result_digest")
        _validate_digest(self.column_metadata_digest, "column_metadata_digest")
        elapsed = _comparison_role_mapping(self.elapsed_ns_by_role, "elapsed_ns_by_role")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in elapsed.values()
        ):
            raise ValueError("elapsed_ns_by_role values must be nonnegative integers")
        object.__setattr__(self, "elapsed_ns_by_role", elapsed)
        tags = tuple(self.coverage_tags)
        if any(not isinstance(tag, str) or not tag for tag in tags):
            raise ValueError("coverage_tags must contain nonempty strings")
        object.__setattr__(self, "coverage_tags", tags)

    def to_event(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "column_metadata_digest": self.column_metadata_digest,
            "coverage_tags": self.coverage_tags,
            "database": self.database,
            "elapsed_ns_by_role": {
                role.value: self.elapsed_ns_by_role[role] for role in COMPARISON_ROLES
            },
            "query_sql": self.query_sql,
            "result_digest": self.result_digest,
            "row_count": self.row_count,
            "run_id": self.run_id,
            "seed": self.seed,
            "type": "pass",
        }


@dataclass(frozen=True, slots=True)
class FindingRecord:
    case_id: str
    run_id: str
    mode: str
    databases: Mapping[NodeRole, str]
    seeds: Mapping[str, int]
    setup_sql: tuple[str, ...]
    query_sql: str
    query_limits: Mapping[str, int | float]
    payload_sha256: str
    original_verdict: str
    first_difference: Mapping[str, object]
    statistics: Mapping[str, object]
    configuration_fingerprints: Mapping[NodeRole, str]
    results: Mapping[NodeRole, Mapping[str, object]]
    requires_same_session: bool = False
    replica_parameters_sha256: str | None = None
    execution_sql: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_id(self.case_id, "case_id")
        _validate_id(self.run_id, "run_id")
        if self.mode not in {"correctness", "performance"}:
            raise ValueError("mode must be correctness or performance")
        databases = _comparison_role_mapping(self.databases, "databases")
        for database in databases.values():
            validate_database_name(database)
        object.__setattr__(self, "databases", databases)
        if not isinstance(self.seeds, Mapping) or not self.seeds:
            raise ValueError("seeds must be a nonempty mapping")
        if any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, int)
            or isinstance(value, bool)
            for key, value in self.seeds.items()
        ):
            raise ValueError("seeds must map nonempty names to integers")
        object.__setattr__(self, "seeds", MappingProxyType(dict(self.seeds)))
        setup_sql = tuple(self.setup_sql)
        if not setup_sql or any(
            not isinstance(statement, str) or not statement.strip() for statement in setup_sql
        ):
            raise ValueError("setup_sql must contain nonempty statements")
        object.__setattr__(self, "setup_sql", setup_sql)
        if not isinstance(self.query_sql, str) or not self.query_sql.strip():
            raise ValueError("query_sql must not be empty")
        if not isinstance(self.query_limits, Mapping):
            raise TypeError("query_limits must be a mapping")
        try:
            timeout_seconds = self.query_limits["timeout_seconds"]
            row_limit = self.query_limits["row_limit"]
            byte_limit = self.query_limits["byte_limit"]
            if (
                not isinstance(row_limit, int)
                or isinstance(row_limit, bool)
                or not isinstance(byte_limit, int)
                or isinstance(byte_limit, bool)
            ):
                raise ValueError("row_limit and byte_limit must be integers")
            limits = QueryLimits(
                timeout_seconds=timeout_seconds,
                row_limit=row_limit,
                byte_limit=byte_limit,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("query_limits are invalid") from error
        if set(self.query_limits) != {"timeout_seconds", "row_limit", "byte_limit"}:
            raise ValueError("query_limits contain unsupported keys")
        object.__setattr__(
            self,
            "query_limits",
            MappingProxyType(
                {
                    "byte_limit": limits.byte_limit,
                    "row_limit": limits.row_limit,
                    "timeout_seconds": limits.timeout_seconds,
                }
            ),
        )
        _validate_digest(self.payload_sha256, "payload_sha256")
        if not isinstance(self.original_verdict, str) or not self.original_verdict:
            raise ValueError("original_verdict must not be empty")
        if not isinstance(self.first_difference, Mapping) or not isinstance(
            self.statistics, Mapping
        ):
            raise TypeError("finding detail fields must be mappings")
        first_difference = MappingProxyType(dict(self.first_difference))
        _validate_generator_contract_finding(self.original_verdict, first_difference)
        object.__setattr__(self, "first_difference", first_difference)
        object.__setattr__(self, "statistics", MappingProxyType(dict(self.statistics)))
        fingerprints = _comparison_role_mapping(
            self.configuration_fingerprints, "configuration_fingerprints"
        )
        if any(not isinstance(value, str) or not value for value in fingerprints.values()):
            raise ValueError("configuration fingerprints must be nonempty strings")
        object.__setattr__(self, "configuration_fingerprints", fingerprints)
        results = _comparison_role_mapping(self.results, "results")
        if any(not isinstance(value, Mapping) for value in results.values()):
            raise TypeError("results must contain JSON mappings")
        for role in COMPARISON_ROLES:
            payload = results[role]
            if payload.get("role") != role.value:
                raise ValueError(f"result role does not match {role.value} artifact")
            if payload.get("status") not in {status.value for status in ExecutionStatus}:
                raise ValueError(f"{role.value} result status is invalid")
        object.__setattr__(self, "results", results)
        if not isinstance(self.requires_same_session, bool):
            raise TypeError("requires_same_session must be a bool")
        if self.replica_parameters_sha256 is not None:
            _validate_digest(self.replica_parameters_sha256, "replica_parameters_sha256")
        execution_sql = tuple(self.execution_sql)
        if any(
            not isinstance(statement, str) or not statement.strip() for statement in execution_sql
        ):
            raise ValueError("execution_sql must contain only nonempty statements")
        object.__setattr__(self, "execution_sql", execution_sql)

    def manifest(self) -> dict[str, object]:
        result_files = {
            role.value: f"{role.value}.result.json.gz" for role in COMPARISON_ROLES
        }
        databases = {role.value: self.databases[role] for role in COMPARISON_ROLES}
        replay = {
            "databases": databases,
            "payload_sha256": self.payload_sha256,
            "query_sql": self.query_sql,
            "query_limits": dict(self.query_limits),
            "requires_same_session": self.requires_same_session,
            "seeds": dict(self.seeds),
            "setup_sql": self.setup_sql,
        }
        manifest = {
            "case_id": self.case_id,
            "configuration_fingerprints": {
                role.value: self.configuration_fingerprints[role]
                for role in COMPARISON_ROLES
            },
            "databases": databases,
            "first_difference": dict(self.first_difference),
            "mode": self.mode,
            "original_verdict": self.original_verdict,
            "payload_sha256": self.payload_sha256,
            "query_sql": self.query_sql,
            "query_limits": dict(self.query_limits),
            "replay": replay,
            "result_files": result_files,
            "run_id": self.run_id,
            "schema_version": 1,
            "seeds": dict(self.seeds),
            "setup_sql": self.setup_sql,
            "statistics": dict(self.statistics),
        }
        if self.replica_parameters_sha256 is not None:
            manifest["replica_parameters_sha256"] = self.replica_parameters_sha256
            replay["replica_parameters_sha256"] = self.replica_parameters_sha256
        if self.execution_sql:
            manifest["execution_sql"] = self.execution_sql
            replay["execution_sql"] = self.execution_sql
        return manifest


def _finding_event(record: FindingRecord) -> dict[str, object]:
    event: dict[str, object] = {
        "bundle_path": f"findings/{record.case_id}/manifest.json",
        "case_id": record.case_id,
        "classification": record.original_verdict,
        "mode": record.mode,
        "original_verdict": record.original_verdict,
        "run_id": record.run_id,
        "type": "finding",
    }
    contract = _generator_contract_details(record.first_difference)
    if contract is None:
        return event
    reason, observed, expected = contract
    event["reason"] = reason
    identity = next(
        (candidate for candidate in observed if candidate is not None),
        expected,
    )
    if identity is not None:
        event["errno"], event["sqlstate"] = identity
    return event


def _write_fsynced(path: Path, payload: bytes, fsync: Callable[[int], None]) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:  # pragma: no cover - OS contract defense
                raise OSError("artifact write returned no progress")
            offset += written
        fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path, fsync: Callable[[int], None]) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        fsync(descriptor)
    finally:
        os.close(descriptor)


def _is_routine_statement(sql: str) -> bool:
    normalized = sql.lstrip().upper()
    return normalized.startswith(
        ("CREATE PROCEDURE ", "CREATE FUNCTION ", "CREATE TRIGGER ", "CREATE EVENT ")
    )


def _append_source_statement(writer: SourceableSqlWriter, sql: str) -> None:
    if _is_routine_statement(sql):
        writer.append_routine(sql)
    else:
        writer.append_statement(sql)


def _compact_stored_result(result: Mapping[str, object]) -> dict[str, object]:
    """Keep tiny result bodies; replace large bodies with count and digest."""

    compact = dict(result)
    rows = compact.get("rows")
    if not isinstance(rows, list):
        return compact
    encoded = _canonical_json(rows)
    compact["row_count"] = len(rows)
    compact["result_digest"] = sha256(encoded).hexdigest()
    if len(rows) > MAX_DIFF_ROWS or len(encoded) > MAX_DIFF_BYTES:
        compact["rows"] = []
        compact["rows_truncated"] = True
    else:
        compact["rows_truncated"] = False
    return compact


class CaseBundleWriter:
    """Publish compact passes and complete finding directories durably."""

    def __init__(
        self,
        root: str | Path,
        *,
        events: JsonlWriter | None = None,
        fsync: Callable[[int], None] = os.fsync,
        full_thread_sql_log: bool = False,
        query_attempt_json_log: bool = True,
        record_pass_events: bool = True,
    ) -> None:
        self.root = Path(root)
        self._events = events or JsonlWriter(self.root / "events.jsonl")
        self._query_log = (
            WorkerQueryLogWriter(self.root / "sql", fsync=fsync) if query_attempt_json_log else None
        )
        self._thread_sql_log = (
            WorkerSqlLogWriter(self.root / "sql", fsync=fsync) if full_thread_sql_log else None
        )
        self._record_pass_events = record_pass_events
        self._fsync = fsync
        self._lock = Lock()
        self._round_writers: dict[tuple[int, str], SourceableSqlWriter] = {}

    def write_query_record(
        self,
        worker_id: int,
        record: Mapping[str, object],
    ) -> None:
        """Durably append one query-attempt state transition for a worker."""

        if self._query_log is not None:
            self._query_log.append(worker_id, record)

    def begin_round_sql(
        self,
        worker_id: int,
        *,
        database: str,
        setup_sql: tuple[str, ...],
        queries: tuple[str, ...],
        metadata: Mapping[str, object],
    ) -> Path:
        """Publish the canonical round script and optionally append worker setup SQL."""

        writer = SourceableSqlWriter(
            self.root / "rounds" / f"{database}.sql",
            database,
            metadata=metadata,
            fsync=self._fsync,
        )
        for statement in setup_sql:
            writer.append_single_line_statement(statement)
        for query in queries:
            writer.append_single_line_statement(query)
        with self._lock:
            self._round_writers[(worker_id, database)] = writer
        if self._thread_sql_log is not None:
            log = self._thread_sql_log
            header = {**metadata, "database": database, "phase": "round_setup"}
            log.append(worker_id, "SET NAMES utf8mb4", metadata=header)
            log.append(worker_id, "SET SESSION time_zone = '+00:00'")
            log.append(worker_id, f"CREATE DATABASE IF NOT EXISTS `{database}`")
            log.append(worker_id, f"USE `{database}`")
            for statement in setup_sql:
                if _is_routine_statement(statement):
                    log.append_routine(worker_id, statement)
                else:
                    log.append(worker_id, statement)
        return writer.path

    def append_round_sql(self, worker_id: int, database: str, sql: str) -> None:
        """Append one actually attempted query to its canonical round script."""

        with self._lock:
            writer = self._round_writers.get((worker_id, database))
        if writer is None:
            raise RuntimeError("round SQL writer has not been initialized")
        writer.append_single_line_statement(sql)

    def append_round_dml_batch(
        self,
        worker_id: int,
        database: str,
        statements: tuple[str, ...],
    ) -> None:
        """Append one attempted transaction surrounded by exactly one blank line."""

        if not statements:
            raise ValueError("DML batch artifact requires executed statements")
        self.begin_round_dml_batch(worker_id, database)
        for statement in statements:
            self.append_round_dml_sql(worker_id, database, statement)
        self.end_round_dml_batch(worker_id, database)

    def begin_round_dml_batch(self, worker_id: int, database: str) -> None:
        with self._lock:
            writer = self._round_writers.get((worker_id, database))
        if writer is None:
            raise RuntimeError("round SQL writer has not been initialized")
        writer.append_blank_line()

    def append_round_dml_sql(self, worker_id: int, database: str, sql: str) -> None:
        self.append_round_sql(worker_id, database, sql)

    def end_round_dml_batch(self, worker_id: int, database: str) -> None:
        with self._lock:
            writer = self._round_writers.get((worker_id, database))
        if writer is None:
            raise RuntimeError("round SQL writer has not been initialized")
        writer.append_blank_line()

    def append_thread_query_sql(
        self,
        worker_id: int,
        sql: str,
        *,
        metadata: Mapping[str, object],
    ) -> None:
        """Append SQL immediately before an execution attempt when enabled."""

        if self._thread_sql_log is not None:
            self._thread_sql_log.append(worker_id, sql, metadata=metadata)

    def write_pass(self, record: PassRecord) -> None:
        if not isinstance(record, PassRecord):
            raise TypeError("write_pass requires PassRecord")
        if not self._record_pass_events:
            return
        event = record.to_event()
        _canonical_json(event)
        self._events.append(event)

    def write_finding(self, record: FindingRecord) -> Path:
        if not isinstance(record, FindingRecord):
            raise TypeError("write_finding requires FindingRecord")
        event = _finding_event(record)
        _canonical_json(event)
        manifest_payload = _canonical_json(record.manifest())
        compact_results = {
            role: _compact_stored_result(record.results[role])
            for role in COMPARISON_ROLES
        }
        result_payloads = {
            role: _canonical_json(compact_results[role]) for role in COMPARISON_ROLES
        }
        findings_root = self.root / "findings"
        final = findings_root / record.case_id
        temporary = findings_root / f".{record.case_id}.tmp-{uuid4().hex}"
        with self._lock:
            findings_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock_path = findings_root / ".publish.lock"
            lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
                if final.exists():
                    raise FileExistsError(f"finding already exists: {record.case_id}")
                temporary.mkdir(mode=0o700)
                try:
                    _write_fsynced(
                        temporary / "manifest.json",
                        manifest_payload,
                        self._fsync,
                    )
                    write_minimal_failure_script(
                        temporary / "case.sql",
                        database=record.databases[NodeRole.CUSTOM_OFF],
                        setup_statements=record.setup_sql,
                        failing_query=record.query_sql,
                        metadata={
                            "case_id": record.case_id,
                            "run_id": record.run_id,
                            "verdict": record.original_verdict,
                        },
                    )
                    write_difference_summary(
                        temporary / "case.diff",
                        {
                            "case_id": record.case_id,
                            "first_difference": dict(record.first_difference),
                            "row_counts": {
                                role.value: compact_results[role].get("row_count", 0)
                                for role in COMPARISON_ROLES
                            },
                            "result_digests": {
                                role.value: compact_results[role].get("result_digest")
                                for role in COMPARISON_ROLES
                            },
                            "verdict": record.original_verdict,
                        },
                    )
                    for role in COMPARISON_ROLES:
                        compressed = gzip.compress(result_payloads[role], compresslevel=9, mtime=0)
                        _write_fsynced(
                            temporary / f"{role.value}.result.json.gz",
                            compressed,
                            self._fsync,
                        )
                    _fsync_directory(temporary, self._fsync)
                    os.replace(temporary, final)
                    _fsync_directory(findings_root, self._fsync)
                except Exception:
                    if temporary.exists():
                        shutil.rmtree(temporary)
                    raise
            finally:
                try:
                    fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(lock_descriptor)
        self._events.append(event)
        return final


__all__ = [
    "CaseBundleWriter",
    "FindingRecord",
    "MAX_RESULT_BYTES",
    "PassRecord",
    "artifact_cell_to_value",
    "node_execution_to_artifact",
]
