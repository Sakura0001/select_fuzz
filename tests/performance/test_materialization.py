from __future__ import annotations

import pytest

from select_fuzz.config import NodeRole
from select_fuzz.performance.materialization import (
    MaterializationEvidence,
    MaterializationMismatch,
    ScaleMaterializer,
)


class _Port:
    def __init__(self, *, wrong_rows: bool = False) -> None:
        self.wrong_rows = wrong_rows
        self.manifest_ids: list[int] = []

    def materialize(
        self, role: NodeRole, database: str, manifest: object
    ) -> MaterializationEvidence:
        del database
        self.manifest_ids.append(id(manifest))
        rows = 99 if self.wrong_rows and role is NodeRole.CUSTOM_ON else 100
        return MaterializationEvidence("schema", {"t": rows}, "content")


def test_materializer_sends_the_exact_manifest_to_all_three_roles() -> None:
    port = _Port()
    manifest = {"rows": 100}

    evidence = ScaleMaterializer(port).rebuild_all("perf_1", manifest)

    assert set(evidence) == set(NodeRole)
    assert port.manifest_ids == [id(manifest)] * 3


def test_materializer_rejects_role_row_count_mismatch() -> None:
    with pytest.raises(MaterializationMismatch):
        ScaleMaterializer(_Port(wrong_rows=True)).rebuild_all("perf_1", {"rows": 100})


def test_materialization_evidence_and_database_validation_are_strict() -> None:
    with pytest.raises(ValueError, match="digests"):
        MaterializationEvidence("", {"t": 1}, "content")
    with pytest.raises(ValueError, match="row counts"):
        MaterializationEvidence("schema", {"t": -1}, "content")
    with pytest.raises(ValueError, match="database"):
        ScaleMaterializer(_Port()).rebuild_all("", {"rows": 100})


def test_materializer_supports_phased_ports_without_prepare_all() -> None:
    class PhasedPort:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def prepare(self, role: NodeRole, database: str, manifest: object) -> None:
            self.calls.append(f"prepare:{role.value}:{database}:{manifest}")

        def synchronize(self, database: str, manifest: object) -> None:
            assert len(self.calls) == 3
            self.calls.append(f"sync:{database}:{manifest}")

        def evidence(
            self, role: NodeRole, database: str, manifest: object
        ) -> MaterializationEvidence:
            self.calls.append(f"evidence:{role.value}:{database}:{manifest}")
            return MaterializationEvidence("schema", {"t": 1}, "content")

        def materialize(
            self, role: NodeRole, database: str, manifest: object
        ) -> MaterializationEvidence:
            raise AssertionError((role, database, manifest))

    port = PhasedPort()

    evidence = ScaleMaterializer(port).rebuild_all("perf_phased_1", {"rows": 1})

    assert set(evidence) == set(NodeRole)
    assert port.calls[3] == "sync:perf_phased_1:{'rows': 1}"
