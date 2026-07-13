"""Deterministic three-node materialization verification."""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from select_fuzz.config import NodeRole


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
    pass


class MaterializationFailure(RuntimeError):
    def __init__(self, role: NodeRole, error_type: str) -> None:
        self.role = role
        self.error_type = error_type
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
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="sf-perf-setup") as pool:
            futures = {
                role: pool.submit(self._port.materialize, role, database, manifest)
                for role in NodeRole
            }
            evidence = {role: futures[role].result() for role in NodeRole}
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
                "three-node schema, row-count, or content evidence differs"
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
