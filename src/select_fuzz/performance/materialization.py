"""Deterministic two-instance materialization verification."""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from select_fuzz.config import COMPARISON_ROLES, NodeRole


@dataclass(frozen=True, slots=True)
class MaterializationEvidence:
    schema_digest: str
    row_counts: Mapping[str, int]
    content_digest: str

    def __post_init__(self) -> None:
        if not self.schema_digest or not self.content_digest:
            raise ValueError("materialization digests must not be empty")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in self.row_counts.values()
        ):
            raise ValueError("materialization row counts must be nonnegative integers")
        object.__setattr__(self, "row_counts", MappingProxyType(dict(self.row_counts)))


class MaterializationMismatch(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        database: str | None = None,
        sql: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self.database = database
        self.sql = sql
        self.details = {} if details is None else dict(details)
        super().__init__(message)


class MaterializationFailure(RuntimeError):
    def __init__(
        self,
        role: NodeRole,
        error_type: str,
        *,
        database: str | None = None,
        sql: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self.role = role
        self.error_type = error_type
        self.database = database
        self.sql = sql
        self.details = {} if details is None else dict(details)
        super().__init__(f"materialization failed on {role.value}: {error_type}")


class MaterializationTimeout(MaterializationFailure):
    pass


class MaterializationInfrastructureFailure(MaterializationFailure):
    pass


class MaterializationExecutionFailure(MaterializationFailure):
    pass


class MaterializationPort(Protocol):
    def materialize(
        self, role: NodeRole, database: str, manifest: object
    ) -> MaterializationEvidence: ...


class ScaleMaterializer:
    def __init__(self, port: MaterializationPort) -> None:
        self._port = port

    def rebuild_all(
        self, database: str, manifest: object
    ) -> Mapping[NodeRole, MaterializationEvidence]:
        if not database:
            raise ValueError("database must not be empty")
        prepare = getattr(self._port, "prepare", None)
        prepare_all = getattr(self._port, "prepare_all", None)
        collect_evidence = getattr(self._port, "evidence", None)
        if (
            callable(collect_evidence) and (callable(prepare_all) or callable(prepare))
        ):
            if callable(prepare_all):
                prepare_all(database, manifest)
            else:
                if not callable(prepare):  # pragma: no cover - guarded above
                    raise AssertionError("phased materializer is missing prepare")
                with ThreadPoolExecutor(
                    max_workers=2, thread_name_prefix="sf-perf-prepare"
                ) as pool:
                    futures = {
                        role: pool.submit(prepare, role, database, manifest)
                        for role in COMPARISON_ROLES
                    }
                    for role in COMPARISON_ROLES:
                        futures[role].result()
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="sf-perf-evidence") as pool:
                futures = {
                    role: pool.submit(collect_evidence, role, database, manifest)
                    for role in COMPARISON_ROLES
                }
                evidence = {role: futures[role].result() for role in COMPARISON_ROLES}
        else:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="sf-perf-setup") as pool:
                futures = {
                    role: pool.submit(self._port.materialize, role, database, manifest)
                    for role in COMPARISON_ROLES
                }
                evidence = {role: futures[role].result() for role in COMPARISON_ROLES}
        identities = {
            (
                item.schema_digest,
                tuple(sorted(item.row_counts.items())),
                item.content_digest,
            )
            for item in evidence.values()
        }
        if len(identities) != 1:
            raise MaterializationMismatch(
                "two-instance schema, row-count, or content evidence differs",
                database=database,
                details={
                    "evidence_by_role": {
                        role.value: {
                            "content_digest": evidence[role].content_digest,
                            "row_counts": dict(evidence[role].row_counts),
                            "schema_digest": evidence[role].schema_digest,
                        }
                        for role in COMPARISON_ROLES
                    }
                },
            )
        return MappingProxyType(evidence)


__all__ = [
    "MaterializationEvidence",
    "MaterializationExecutionFailure",
    "MaterializationFailure",
    "MaterializationInfrastructureFailure",
    "MaterializationMismatch",
    "MaterializationTimeout",
    "MaterializationPort",
    "ScaleMaterializer",
]
