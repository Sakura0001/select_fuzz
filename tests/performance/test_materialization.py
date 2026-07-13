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
