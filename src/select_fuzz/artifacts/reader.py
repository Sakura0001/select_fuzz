"""Readers for authoritative JSONL and complete finding bundles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import gzip
from hashlib import sha256
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any

from select_fuzz.artifacts.bundle import MAX_RESULT_BYTES
from select_fuzz.artifacts.jsonl import MAX_JSONL_RECORD_BYTES, read_jsonl
from select_fuzz.config import COMPARISON_ROLES, NodeRole


_CASE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
MAX_SQL_PAYLOAD_BYTES = 2 * 1024 * 1024 * 1024


class ArtifactValidationError(ValueError):
    """A stored bundle violates the closed artifact schema or path envelope."""


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite constant is forbidden: {value}")


def _load_json_object(payload: bytes, label: str) -> dict[str, object]:
    try:
        decoded: Any = json.loads(
            payload.decode("utf-8"), parse_constant=_reject_constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ArtifactValidationError(f"{label} is not strict UTF-8 JSON") from error
    if not isinstance(decoded, dict):
        raise ArtifactValidationError(f"{label} must contain a JSON object")
    return decoded


@dataclass(frozen=True, slots=True)
class StoredFinding:
    path: Path
    manifest: Mapping[str, object]
    results: Mapping[NodeRole, Mapping[str, object]]
    setup_sql: tuple[str, ...]
    query_sql: str
    execution_sql: tuple[str, ...] = ()

    @property
    def case_id(self) -> str:
        value = self.manifest["case_id"]
        assert isinstance(value, str)  # validated by ArtifactReader
        return value

    @property
    def replay_manifest(self) -> Mapping[str, object]:
        value = self.manifest["replay"]
        assert isinstance(value, Mapping)  # validated by ArtifactReader
        return value


class ArtifactReader:
    def __init__(
        self,
        root: str | Path,
        *,
        max_result_bytes: int = MAX_RESULT_BYTES,
        max_sql_payload_bytes: int = MAX_SQL_PAYLOAD_BYTES,
    ) -> None:
        if (
            not isinstance(max_result_bytes, int)
            or isinstance(max_result_bytes, bool)
            or max_result_bytes <= 0
        ):
            raise ValueError("max_result_bytes must be a positive integer")
        self.root = Path(root)
        self.max_result_bytes = max_result_bytes
        if (
            not isinstance(max_sql_payload_bytes, int)
            or isinstance(max_sql_payload_bytes, bool)
            or max_sql_payload_bytes <= 0
        ):
            raise ValueError("max_sql_payload_bytes must be a positive integer")
        self.max_sql_payload_bytes = max_sql_payload_bytes

    def _read_sql_payload(
        self,
        directory: Path,
        reference: object,
        expected_path: str,
    ) -> tuple[str, ...]:
        if not isinstance(reference, dict):
            raise ArtifactValidationError(f"{expected_path} reference must be an object")
        if reference.get("path") != expected_path:
            raise ArtifactValidationError(f"{expected_path} reference path is invalid")
        statement_count = reference.get("statement_count")
        uncompressed_bytes = reference.get("uncompressed_bytes")
        compressed_bytes = reference.get("compressed_bytes")
        expected_digest = reference.get("sha256")
        if (
            not isinstance(statement_count, int)
            or isinstance(statement_count, bool)
            or statement_count < 0
            or not isinstance(uncompressed_bytes, int)
            or isinstance(uncompressed_bytes, bool)
            or uncompressed_bytes < 0
            or not isinstance(compressed_bytes, int)
            or isinstance(compressed_bytes, bool)
            or compressed_bytes < 0
            or not isinstance(expected_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
        ):
            raise ArtifactValidationError(f"{expected_path} reference metadata is invalid")
        path = directory / expected_path
        try:
            if path.stat().st_size != compressed_bytes:
                raise ArtifactValidationError(
                    f"{expected_path} compressed byte count does not match"
                )
            statements: list[str] = []
            digest = sha256()
            consumed = 0
            with gzip.open(path, "rb") as stream:
                while True:
                    line = stream.readline(self.max_sql_payload_bytes - consumed + 1)
                    if not line:
                        break
                    consumed += len(line)
                    if consumed > self.max_sql_payload_bytes:
                        raise ArtifactValidationError(
                            f"{expected_path} decompressed SQL exceeds safety limit"
                        )
                    digest.update(line)
                    try:
                        statement = json.loads(
                            line.decode("utf-8"),
                            parse_constant=_reject_constant,
                        )
                    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                        raise ArtifactValidationError(
                            f"{expected_path} contains invalid JSONL"
                        ) from error
                    if not isinstance(statement, str) or not statement.strip():
                        raise ArtifactValidationError(
                            f"{expected_path} contains an invalid SQL statement"
                        )
                    statements.append(statement)
        except ArtifactValidationError:
            raise
        except (OSError, EOFError) as error:
            raise ArtifactValidationError(
                f"{expected_path} is not a valid gzip artifact"
            ) from error
        if (
            len(statements) != statement_count
            or consumed != uncompressed_bytes
            or digest.hexdigest() != expected_digest
        ):
            raise ArtifactValidationError(f"{expected_path} integrity check failed")
        return tuple(statements)

    def events(self) -> list[dict[str, object]]:
        return read_jsonl(self.root / "events.jsonl")

    def _manifest_path(self, reference: str | Path) -> Path:
        findings_root = (self.root / "findings").resolve()
        if isinstance(reference, str) and _CASE_ID.fullmatch(reference):
            candidate = findings_root / reference / "manifest.json"
        else:
            candidate = Path(reference)
            if not candidate.is_absolute():
                candidate = (
                    candidate.resolve()
                    if candidate.exists()
                    else (self.root / candidate).resolve()
                )
            candidate = candidate.resolve()
        if (
            candidate.name != "manifest.json"
            or candidate.parent.parent != findings_root
            or not candidate.is_relative_to(findings_root)
        ):
            raise ArtifactValidationError(
                "manifest path must be findings/<case_id>/manifest.json under artifact root"
            )
        return candidate

    def get_finding(self, reference: str | Path) -> StoredFinding:
        manifest_path = self._manifest_path(reference)
        try:
            manifest_payload = manifest_path.read_bytes()
        except OSError as error:
            raise ArtifactValidationError("finding manifest is unavailable") from error
        if len(manifest_payload) > MAX_JSONL_RECORD_BYTES:
            raise ArtifactValidationError("finding manifest exceeds the 8 MiB limit")
        manifest = _load_json_object(manifest_payload, "finding manifest")
        case_id = manifest.get("case_id")
        if (
            not isinstance(case_id, str)
            or _CASE_ID.fullmatch(case_id) is None
            or case_id != manifest_path.parent.name
        ):
            raise ArtifactValidationError("manifest case_id does not match its directory")
        schema_version = manifest.get("schema_version")
        if schema_version not in {1, 2}:
            raise ArtifactValidationError("unsupported finding schema_version")
        replay = manifest.get("replay")
        if not isinstance(replay, dict):
            raise ArtifactValidationError("manifest replay must be an object")
        if schema_version == 1:
            raw_setup_sql = replay.get("setup_sql")
            raw_query_sql = replay.get("query_sql")
            raw_execution_sql = replay.get("execution_sql", ())
            if not isinstance(raw_setup_sql, list) or not isinstance(raw_query_sql, str):
                raise ArtifactValidationError("legacy replay SQL is invalid")
            if not isinstance(raw_execution_sql, (list, tuple)):
                raise ArtifactValidationError("legacy execution SQL is invalid")
            setup_sql = tuple(raw_setup_sql)
            query_sql = raw_query_sql
            execution_sql = tuple(raw_execution_sql)
        else:
            setup_sql = self._read_sql_payload(
                manifest_path.parent,
                replay.get("setup_sql_ref"),
                "setup.sql.jsonl.gz",
            )
            query_statements = self._read_sql_payload(
                manifest_path.parent,
                replay.get("query_sql_ref"),
                "query.sql.jsonl.gz",
            )
            if len(query_statements) != 1:
                raise ArtifactValidationError("query SQL payload must contain one statement")
            query_sql = query_statements[0]
            raw_execution_ref = replay.get("execution_sql_ref")
            execution_sql = (
                ()
                if raw_execution_ref is None
                else self._read_sql_payload(
                    manifest_path.parent,
                    raw_execution_ref,
                    "execution.sql.jsonl.gz",
                )
            )
        result_files = manifest.get("result_files")
        pair_expected = {
            role.value: f"{role.value}.result.json.gz" for role in COMPARISON_ROLES
        }
        legacy_expected = {
            role.value: f"{role.value}.result.json.gz" for role in NodeRole
        }
        stored_roles: tuple[NodeRole, ...]
        if result_files == pair_expected:
            stored_roles = COMPARISON_ROLES
            expected = pair_expected
        elif result_files == legacy_expected:
            stored_roles = tuple(NodeRole)
            expected = legacy_expected
        else:
            raise ArtifactValidationError(
                "manifest result_files must contain a supported role set"
            )
        results: dict[NodeRole, Mapping[str, object]] = {}
        for role in stored_roles:
            result_path = manifest_path.parent / expected[role.value]
            try:
                with gzip.open(result_path, "rb") as stream:
                    payload = stream.read(self.max_result_bytes + 1)
            except (OSError, EOFError) as error:
                raise ArtifactValidationError(
                    f"{role.value} result is not a valid gzip artifact"
                ) from error
            if len(payload) > self.max_result_bytes:
                raise ArtifactValidationError(
                    f"{role.value} decompressed result exceeds safety limit"
                )
            results[role] = MappingProxyType(
                _load_json_object(payload, f"{role.value} result")
            )
        return StoredFinding(
            path=manifest_path.parent,
            manifest=MappingProxyType(manifest),
            results=MappingProxyType(results),
            setup_sql=setup_sql,
            query_sql=query_sql,
            execution_sql=execution_sql,
        )


__all__ = [
    "ArtifactReader",
    "ArtifactValidationError",
    "MAX_SQL_PAYLOAD_BYTES",
    "StoredFinding",
]
