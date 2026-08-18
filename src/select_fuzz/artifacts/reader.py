"""Readers for authoritative JSONL and complete finding bundles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import gzip
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any

from select_fuzz.artifacts.bundle import MAX_RESULT_BYTES
from select_fuzz.artifacts.jsonl import MAX_JSONL_RECORD_BYTES, read_jsonl
from select_fuzz.config import COMPARISON_ROLES, NodeRole


_CASE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")


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
    ) -> None:
        if (
            not isinstance(max_result_bytes, int)
            or isinstance(max_result_bytes, bool)
            or max_result_bytes <= 0
        ):
            raise ValueError("max_result_bytes must be a positive integer")
        self.root = Path(root)
        self.max_result_bytes = max_result_bytes

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
        if manifest.get("schema_version") != 1:
            raise ArtifactValidationError("unsupported finding schema_version")
        replay = manifest.get("replay")
        if not isinstance(replay, dict):
            raise ArtifactValidationError("manifest replay must be an object")
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
        )


__all__ = ["ArtifactReader", "ArtifactValidationError", "StoredFinding"]
