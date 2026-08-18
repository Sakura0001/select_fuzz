from __future__ import annotations

from dataclasses import dataclass

import pytest

from select_fuzz.config import COMPARISON_ROLES, NodeConfig, NodeRole
from select_fuzz.domain import ErrorInfo, ExecutionStatus, NodeExecution
from select_fuzz.performance.calibration import (
    CalibrationFailureKind,
    CalibrationInfrastructurePause,
    CalibrationEngine,
    CalibrationExhausted,
    CalibrationTerminated,
    ReferenceAnalyzer,
)
from select_fuzz.performance.materialization import MaterializationMismatch
from select_fuzz.performance.models import PerformancePolicy, ScaleKnobs
from select_fuzz.performance.tree import Family, ShapeBoundary


def _tree(seconds: float) -> str:
    return (
        f"-> Table scan on t (cost=1 rows=100) (actual time=0..{seconds * 1000} rows=100 loops=1)"
    )


def _execution(role: NodeRole, seconds: float) -> NodeExecution:
    return NodeExecution.success(
        role=role,
        connection_id=1,
        started_ns=0,
        ended_ns=int(seconds * 1_000_000_000),
        performance_payload={"tree": _tree(seconds)},
    )


@dataclass(frozen=True)
class _Template:
    seed: int = 7
    case_id: str = "case_7"
    template_id: str = "cpu_scan_v1"
    boundary: ShapeBoundary = ShapeBoundary(frozenset({Family.SCAN}))
    driver_family: Family = Family.SCAN

    def render(self, scale: ScaleKnobs) -> str:
        return f"SELECT SUM(v) FROM t WHERE id <= {scale.scan_rows}"

    def data_manifest(self, scale: ScaleKnobs) -> object:
        return {"rows": scale.table_rows, "seed": self.seed}

    def target_rows(self, scale: ScaleKnobs) -> int:
        return scale.scan_rows


class _Materializer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def rebuild_all(self, database: str, manifest: object) -> object:
        self.calls.append((database, manifest))
        return manifest


class _AnalyzePort:
    def __init__(self, values: dict[NodeRole, list[float]]) -> None:
        self.values = {role: list(samples) for role, samples in values.items()}
        self.calls: list[tuple[NodeRole, str, float]] = []

    def explain_tree(self, role: NodeRole, database: str, sql: str) -> str:
        del role, database, sql
        return "-> Table scan on t (cost=1 rows=100000)"

    def analyze(
        self, role: NodeRole, database: str, sql: str, *, timeout_s: float
    ) -> NodeExecution:
        del database
        self.calls.append((role, sql, timeout_s))
        return _execution(role, self.values[role].pop(0))


def test_calibration_runs_custom_off_three_times_and_freezes_one_case() -> None:
    port = _AnalyzePort({NodeRole.CUSTOM_OFF: [4.0, 5.0, 6.0]})
    materializer = _Materializer()
    template = _Template()

    frozen = CalibrationEngine(port, materializer, PerformancePolicy()).calibrate(
        template, ScaleKnobs(), database="perf_round_7"
    )

    assert frozen.medians_seconds == {NodeRole.CUSTOM_OFF: 5.0}
    assert [role for role, _, _ in port.calls] == [NodeRole.CUSTOM_OFF] * 3
    assert frozen.sql == template.render(frozen.scale)
    assert frozen.data_manifest == template.data_manifest(frozen.scale)
    assert materializer.calls == [(frozen.database, frozen.data_manifest)]


def test_calibration_scales_up_each_round_then_reports_bounded_exhaustion() -> None:
    policy = PerformancePolicy(max_calibration_rounds=3)
    port = _AnalyzePort(
        {NodeRole.CUSTOM_OFF: [1.0] * 9}
    )

    with pytest.raises(CalibrationExhausted) as captured:
        CalibrationEngine(port, _Materializer(), policy).calibrate(
            _Template(), ScaleKnobs(), database="perf_round_8"
        )

    attempts = captured.value.attempts
    assert len(attempts) == 3
    assert attempts[1].scale.table_rows > attempts[0].scale.table_rows
    assert attempts[2].scale.table_rows > attempts[1].scale.table_rows


class _CoreRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[NodeRole, str, str, float, int, int, object]] = []

    def run(
        self,
        node: NodeConfig,
        database: str,
        sql: str,
        *,
        timeout_s: float,
        row_limit: int,
        byte_limit: int,
        barrier: object = None,
    ) -> NodeExecution:
        self.calls.append((node.role, database, sql, timeout_s, row_limit, byte_limit, barrier))
        return _execution(node.role, 5.0)


def test_reference_analyzer_is_a_thin_shared_node_query_runner_adapter() -> None:
    nodes = tuple(
        NodeConfig(role=role, host=f"{role.value}.example") for role in COMPARISON_ROLES
    )
    core = _CoreRunner()
    adapter = ReferenceAnalyzer(nodes, core)

    result = adapter.analyze(
        NodeRole.CUSTOM_OFF,
        "perf_1",
        "SELECT SUM(v) FROM t;",
        timeout_s=60.0,
    )

    assert result.role is NodeRole.CUSTOM_OFF
    assert core.calls == [
        (
            NodeRole.CUSTOM_OFF,
            "perf_1",
            "EXPLAIN ANALYZE FORMAT=TREE SELECT SUM(v) FROM t",
            60.0,
            1,
            32 * 1024 * 1024,
            None,
        )
    ]
    with pytest.raises(ValueError, match="reference"):
        adapter.analyze(NodeRole.CUSTOM_ON, "perf_1", "SELECT 1", timeout_s=60.0)


class _InfraCoreRunner(_CoreRunner):
    def run(
        self,
        node: NodeConfig,
        database: str,
        sql: str,
        *,
        timeout_s: float,
        row_limit: int,
        byte_limit: int,
        barrier: object = None,
    ) -> NodeExecution:
        del database, sql, timeout_s, row_limit, byte_limit, barrier
        return _failure(node.role, ExecutionStatus.INFRA_ERROR, 2013)


def test_reference_explain_infrastructure_failure_requests_pause() -> None:
    nodes = tuple(
        NodeConfig(role=role, host=f"{role.value}.example") for role in COMPARISON_ROLES
    )

    with pytest.raises(CalibrationInfrastructurePause):
        ReferenceAnalyzer(nodes, _InfraCoreRunner()).explain_tree(
            NodeRole.CUSTOM_OFF, "perf_1", "SELECT 1"
        )


def _failure(role: NodeRole, status: ExecutionStatus, errno: int) -> NodeExecution:
    return NodeExecution.failure(
        role=role,
        status=status,
        started_ns=0,
        ended_ns=1,
        connection_id=None,
        error=ErrorInfo(errno, "HY000", "redacted diagnostic"),
    )


class _OutcomePort:
    def __init__(self, execution: NodeExecution) -> None:
        self.execution = execution
        self.call_count = 0

    def explain_tree(self, role: NodeRole, database: str, sql: str) -> str:
        del role, database, sql
        return "-> Table scan on t (cost=1 rows=100000)"

    def analyze(
        self, role: NodeRole, database: str, sql: str, *, timeout_s: float
    ) -> NodeExecution:
        del database, sql, timeout_s
        self.call_count += 1
        if role is self.execution.role:
            return self.execution
        return _execution(role, 5.0)


def test_infrastructure_calibration_failure_pauses_instead_of_scaling() -> None:
    port = _OutcomePort(_failure(NodeRole.CUSTOM_OFF, ExecutionStatus.INFRA_ERROR, 2013))

    with pytest.raises(CalibrationInfrastructurePause) as captured:
        CalibrationEngine(port, _Materializer(), PerformancePolicy()).calibrate(
            _Template(), ScaleKnobs(), database="perf_infra"
        )

    assert captured.value.kind is CalibrationFailureKind.INFRA
    assert captured.value.role is NodeRole.CUSTOM_OFF
    assert captured.value.error_code == 2013


@pytest.mark.parametrize(
    "execution,kind",
    [
        (
            _failure(NodeRole.CUSTOM_OFF, ExecutionStatus.ERROR, 1064),
            CalibrationFailureKind.EXECUTION,
        ),
        (
            NodeExecution.success(
                role=NodeRole.CUSTOM_OFF,
                connection_id=1,
                started_ns=0,
                ended_ns=1,
                performance_payload={"tree": "not a tree"},
            ),
            CalibrationFailureKind.PARSE,
        ),
        (
            NodeExecution.success(
                role=NodeRole.CUSTOM_OFF,
                connection_id=1,
                started_ns=0,
                ended_ns=1,
                performance_payload={
                    "tree": "-> Aggregate (cost=1 rows=1) (actual time=0..5000 rows=1 loops=1)"
                },
            ),
            CalibrationFailureKind.SHAPE,
        ),
    ],
)
def test_non_timeout_calibration_failures_are_classified_and_terminate(
    execution: NodeExecution, kind: CalibrationFailureKind
) -> None:
    with pytest.raises(CalibrationTerminated) as captured:
        CalibrationEngine(_OutcomePort(execution), _Materializer(), PerformancePolicy()).calibrate(
            _Template(), ScaleKnobs(), database="perf_bad"
        )

    assert captured.value.kind is kind


def test_timeout_rejects_case_without_scaling_below_random_initial_volume() -> None:
    timeout = _failure(NodeRole.CUSTOM_OFF, ExecutionStatus.TIMEOUT, 3024)
    with pytest.raises(CalibrationExhausted) as captured:
        CalibrationEngine(
            _OutcomePort(timeout),
            _Materializer(),
            PerformancePolicy(max_calibration_rounds=2),
        ).calibrate(_Template(), ScaleKnobs(), database="perf_timeout")

    assert len(captured.value.attempts) == 1
    assert captured.value.attempts[0].scale.table_rows >= ScaleKnobs().table_rows


def test_first_timeout_short_circuits_and_rejects_the_candidate() -> None:
    timeout = _failure(NodeRole.CUSTOM_OFF, ExecutionStatus.TIMEOUT, 3024)
    port = _OutcomePort(timeout)
    with pytest.raises(CalibrationExhausted):
        CalibrationEngine(
            port, _Materializer(), PerformancePolicy(max_calibration_rounds=2)
        ).calibrate(_Template(), ScaleKnobs(), database="perf_timeout_fast")

    # One custom_off timeout rejects this candidate; remaining samples are skipped.
    assert getattr(port, "call_count", 0) == 1


def test_materialization_mismatch_is_terminal_setup_mismatch_not_infra_pause() -> None:
    class MismatchMaterializer:
        def rebuild_all(self, database: str, manifest: object) -> object:
            del manifest
            raise MaterializationMismatch(
                "different content",
                database=database,
                sql="INSERT INTO cpu_data VALUES (1)",
                details={"node_results": {"custom_off": {"affected_rows": 1}}},
            )

    with pytest.raises(CalibrationTerminated) as captured:
        CalibrationEngine(
            _AnalyzePort({NodeRole.CUSTOM_OFF: [5.0] * 3}),
            MismatchMaterializer(),
            PerformancePolicy(),
        ).calibrate(_Template(), ScaleKnobs(), database="perf_setup_mismatch")

    assert captured.value.kind is CalibrationFailureKind.SETUP_MISMATCH
    assert not isinstance(captured.value, CalibrationInfrastructurePause)
    assert captured.value.database == "perf_setup_mismatch"
    assert captured.value.failing_action_sql == "INSERT INTO cpu_data VALUES (1)"
    assert captured.value.failure_details["node_results"] == {
        "custom_off": {"affected_rows": 1}
    }


class _WrongExplainShapePort(_AnalyzePort):
    def explain_tree(self, role: NodeRole, database: str, sql: str) -> str:
        del role, database, sql
        return "-> Aggregate (cost=1 rows=1)"


def test_explain_plan_shape_failure_is_not_mislabeled_as_parse_failure() -> None:
    port = _WrongExplainShapePort(
        {NodeRole.CUSTOM_OFF: [5.0] * 3}
    )

    with pytest.raises(CalibrationTerminated) as captured:
        CalibrationEngine(port, _Materializer(), PerformancePolicy()).calibrate(
            _Template(), ScaleKnobs(), database="perf_shape"
        )

    assert captured.value.kind is CalibrationFailureKind.SHAPE


class _BrokenMaterializer:
    def rebuild_all(self, database: str, manifest: object) -> object:
        del database, manifest
        raise RuntimeError("must not escape: private endpoint")


def test_materialization_exception_becomes_safe_infrastructure_pause() -> None:
    port = _AnalyzePort(
        {NodeRole.CUSTOM_OFF: [5.0] * 3}
    )

    with pytest.raises(CalibrationInfrastructurePause) as captured:
        CalibrationEngine(port, _BrokenMaterializer(), PerformancePolicy()).calibrate(
            _Template(), ScaleKnobs(), database="perf_setup"
        )

    assert captured.value.error_type == "RuntimeError"
    assert "private endpoint" not in str(captured.value)
