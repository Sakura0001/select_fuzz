from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Sequence

from select_fuzz.artifacts.bundle import CaseBundleWriter, FindingRecord
from select_fuzz.artifacts.reader import ArtifactReader
from select_fuzz.config import NodeRole
from select_fuzz.domain import ColumnMeta, ErrorInfo, ExecutionStatus, NodeExecution
from select_fuzz.execution import DatabaseNameFactory, PrepareStatus, QueryLimits
from select_fuzz.oracle import OracleVerdict
from select_fuzz.replay import (
    ReplayCase,
    ReplayService,
    ReplayStatus,
    TriadReplayAdapter,
)



def _finding() -> FindingRecord:
    return FindingRecord(
        case_id="case_finding_1",
        run_id="run_1",
        mode="correctness",
        databases={role: "sf_c_20260713t120000_w0_r1_sabc_n123_q0" for role in NodeRole},
        seeds={"round": 7, "schema": 11, "query": 13},
        setup_sql=(
            "CREATE TABLE `t0` (`id` BIGINT PRIMARY KEY);",
            "INSERT INTO `t0` VALUES (1),(2);",
        ),
        query_sql="SELECT `id` FROM `t0` ORDER BY 1",
        query_limits={"timeout_seconds": 15, "row_limit": 10_000, "byte_limit": 32 << 20},
        payload_sha256="c" * 64,
        original_verdict="result_mismatch",
        first_difference={"category": "rows"},
        statistics={"baseline_rows": 2, "custom_on_rows": 1},
        configuration_fingerprints={role: f"fp-{role.value}" for role in NodeRole},
        results={
            role: {"role": role.value, "status": "success", "rows": [[1], [2]]}
            for role in NodeRole
        },
    )


def _success(role: NodeRole, rows: tuple[tuple[object, ...], ...]) -> NodeExecution:
    return NodeExecution.success(
        role=role,
        connection_id=100 + list(NodeRole).index(role),
        started_ns=1,
        ended_ns=2,
        columns=(ColumnMeta("id", 8, False, False, False),),
        rows=rows,
    )


def _mismatch() -> tuple[NodeExecution, ...]:
    return (
        _success(NodeRole.BASELINE, ((1,), (2,))),
        _success(NodeRole.CUSTOM_OFF, ((1,), (2,))),
        _success(NodeRole.CUSTOM_ON, ((1,),)),
    )


class _ReplayCoordinator:
    def __init__(self, executions: Sequence[NodeExecution]) -> None:
        self.executions = tuple(executions)
        self.calls: list[tuple[ReplayCase, str]] = []

    def replay(
        self, replay_case: ReplayCase, new_database: str
    ) -> tuple[NodeExecution, ...]:
        self.calls.append((replay_case, new_database))
        return self.executions


def _service(root: Path, coordinator: _ReplayCoordinator) -> ReplayService:
    return ReplayService(
        ArtifactReader(root),
        coordinator,
        DatabaseNameFactory(clock_ns=lambda: 1_784_134_800_123_456_789),
    )


def test_deterministic_finding_replays_as_reproduced_by_case_id_and_manifest_path(
    tmp_path: Path,
) -> None:
    published = CaseBundleWriter(tmp_path).write_finding(_finding())
    coordinator = _ReplayCoordinator(_mismatch())
    service = _service(tmp_path, coordinator)

    by_id = service.replay("case_finding_1")
    by_path = service.replay(published / "manifest.json")

    assert by_id.status is ReplayStatus.REPRODUCED
    assert by_id.original_verdict == OracleVerdict.RESULT_MISMATCH.value
    assert by_id.replay_verdict is OracleVerdict.RESULT_MISMATCH
    assert by_path.status is ReplayStatus.REPRODUCED
    assert len(coordinator.calls) == 2
    replay_case, first_database = coordinator.calls[0]
    assert replay_case.setup_sql == _finding().setup_sql
    assert replay_case.query_sql == _finding().query_sql
    assert replay_case.query_limits == QueryLimits(15, 10_000, 32 << 20)
    assert replay_case.payload_sha256 == _finding().payload_sha256
    assert first_database.startswith("sf_c_")
    assert first_database not in set(_finding().databases.values())
    assert coordinator.calls[1][1] != first_database


def test_replay_that_no_longer_mismatches_is_not_reproduced(tmp_path: Path) -> None:
    CaseBundleWriter(tmp_path).write_finding(_finding())
    all_match = tuple(_success(role, ((1,), (2,))) for role in NodeRole)

    result = _service(tmp_path, _ReplayCoordinator(all_match)).replay(
        "case_finding_1"
    )

    assert result.status is ReplayStatus.NOT_REPRODUCED
    assert result.replay_verdict is OracleVerdict.MATCH


def test_replay_infrastructure_result_never_enters_semantic_oracle(
    tmp_path: Path,
) -> None:
    CaseBundleWriter(tmp_path).write_finding(_finding())
    executions = list(_mismatch())
    executions[2] = NodeExecution.failure(
        role=NodeRole.CUSTOM_ON,
        status=ExecutionStatus.INFRA_ERROR,
        started_ns=1,
        ended_ns=2,
        connection_id=None,
        error=ErrorInfo(2003, "HY000", "connection unavailable"),
        connection_reusable=False,
    )

    result = _service(tmp_path, _ReplayCoordinator(executions)).replay(
        "case_finding_1"
    )

    assert result.status is ReplayStatus.INFRASTRUCTURE_ERROR
    assert result.replay_verdict is None


class _Prepared:
    def __init__(self, status: PrepareStatus = PrepareStatus.READY) -> None:
        self.status = status
        self.closed = False
        self.database = ""

    def close(self) -> None:
        self.closed = True


class _ExecutionBatch:
    def __init__(
        self, prepared: _Prepared, executions: tuple[NodeExecution, ...]
    ) -> None:
        self.prepared = prepared
        self.executions = executions


class _Triad:
    def __init__(self, status: PrepareStatus = PrepareStatus.READY) -> None:
        self.prepared = _Prepared(status)
        self.received: list[tuple[ReplayCase, str, object]] = []

    def prepare_until_recovered(
        self, bundle: ReplayCase, *, database: str, retry: object
    ) -> _Prepared:
        self.received.append((bundle, database, retry))
        self.prepared.database = database
        return self.prepared

    def execute(
        self, prepared: _Prepared, sql: str, limits: QueryLimits
    ) -> _ExecutionBatch:
        assert sql == _finding().query_sql
        assert limits == QueryLimits(15, 10_000, 32 << 20)
        return _ExecutionBatch(prepared, _mismatch())


def test_production_triad_replay_adapter_uses_stored_setup_query_limits_and_closes_round(
    tmp_path: Path,
) -> None:
    CaseBundleWriter(tmp_path).write_finding(_finding())
    triad = _Triad()
    service = _service(tmp_path, TriadReplayAdapter(triad))  # type: ignore[arg-type]

    result = service.replay("case_finding_1")

    assert result.status is ReplayStatus.REPRODUCED
    replay_case, database, _retry = triad.received[0]
    assert replay_case.statements == _finding().setup_sql
    assert database == result.database
    assert triad.prepared.closed is True


def test_production_triad_replay_adapter_classifies_semantic_setup_failure(
    tmp_path: Path,
) -> None:
    CaseBundleWriter(tmp_path).write_finding(_finding())
    triad = _Triad(PrepareStatus.SETUP_MISMATCH)

    result = _service(
        tmp_path, TriadReplayAdapter(triad)  # type: ignore[arg-type]
    ).replay("case_finding_1")

    assert result.status is ReplayStatus.PREPARATION_FAILED
    assert result.executions == ()
    assert triad.prepared.closed is True


def test_setup_mismatch_finding_replays_as_reproduced_setup_failure(
    tmp_path: Path,
) -> None:
    CaseBundleWriter(tmp_path).write_finding(
        replace(_finding(), original_verdict=PrepareStatus.SETUP_MISMATCH.value)
    )
    triad = _Triad(PrepareStatus.SETUP_MISMATCH)

    result = _service(
        tmp_path, TriadReplayAdapter(triad)  # type: ignore[arg-type]
    ).replay("case_finding_1")

    assert result.status is ReplayStatus.REPRODUCED
    assert result.replay_verdict is None
