"""Production adapters and shared CLI contract for performance mode."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

from select_fuzz.artifacts import JsonlWriter
from select_fuzz.config import AppConfig, NodeConfig, NodeRole
from select_fuzz.domain import ExecutionStatus, NodeExecution, RunRequest, SeedTree, stable_fingerprint
from select_fuzz.execution import (
    DatabaseNameFactory,
    MySQLConnectorFactory,
    NodeQueryRunner,
)
from select_fuzz.execution.protocols import ConnectionFactory, QuerySession
from select_fuzz.performance.artifacts import (
    PerformanceDiagnosticWriter,
    PerformanceRecorder,
)
from select_fuzz.performance.calibration import (
    CalibrationEngine,
    CostModel,
    ReferenceAnalyzer,
)
from select_fuzz.performance.execution import FormalRunner
from select_fuzz.performance.diagnostics import MySQLDiagnosticsCollector
from select_fuzz.performance.materialization import (
    MaterializationEvidence,
    MaterializationExecutionFailure,
    MaterializationInfrastructureFailure,
    MaterializationTimeout,
    ScaleMaterializer,
)
from select_fuzz.performance.models import PerformancePolicy, ScaleKnobs, Verdict
from select_fuzz.performance.service import PerformanceService
from select_fuzz.performance.templates import (
    CpuDenseGroupSortTemplate,
    CpuDenseJoinTemplate,
    CpuDenseRangeSortTemplate,
    CpuDenseScanTemplate,
    CpuDenseSetupManifest,
    CpuDenseWindowTemplate,
)
from select_fuzz.service import RunSummary


def _execute(session: QuerySession, sql: str) -> tuple[tuple[object, ...], ...]:
    cursor = session.execute(sql)
    rows: list[tuple[object, ...]] = []
    try:
        while True:
            batch = cursor.fetchmany(128)
            if not batch:
                break
            rows.extend(tuple(row) for row in batch)
    finally:
        cursor.close()
    return tuple(rows)


class MySQLCpuMaterializationPort:
    """Rebuild one CPU template and verify actual schema/content evidence."""

    def __init__(
        self,
        nodes: Sequence[NodeConfig],
        factory: ConnectionFactory,
        query_runner: NodeQueryRunner,
        *,
        timeout_seconds: float,
        stop_event: Event,
    ) -> None:
        by_role = {node.role: node for node in nodes}
        if len(nodes) != 3 or set(by_role) != set(NodeRole):
            raise ValueError("materialization requires all three node roles")
        self._nodes = by_role
        self._factory = factory
        self._query_runner = query_runner
        self._timeout_seconds = timeout_seconds
        self._stop_event = stop_event

    def _bounded(self, role: NodeRole, database: str, sql: str) -> NodeExecution:
        if self._stop_event.is_set():
            raise MaterializationInfrastructureFailure(role, "RunStopped")
        result = self._query_runner.run(
            self._nodes[role],
            database,
            sql,
            timeout_s=self._timeout_seconds,
            row_limit=16,
            byte_limit=8 * 1024 * 1024,
        )
        if result.status is ExecutionStatus.TIMEOUT:
            raise MaterializationTimeout(role, "MaterializationTimeout")
        if result.status is ExecutionStatus.INFRA_ERROR:
            raise MaterializationInfrastructureFailure(
                role,
                result.watchdog_error_type or "MySQLInfrastructureError",
            )
        if result.status is not ExecutionStatus.SUCCESS:
            raise MaterializationExecutionFailure(role, "MySQLExecutionError")
        return result

    def materialize(
        self,
        role: NodeRole,
        database: str,
        manifest: object,
    ) -> MaterializationEvidence:
        if not isinstance(manifest, CpuDenseSetupManifest):
            raise TypeError("CPU materialization requires CpuDenseSetupManifest")
        with self._factory.control_session(
            self._nodes[role], "information_schema"
        ) as session:
            _execute(session, f"CREATE DATABASE IF NOT EXISTS `{database}`")
        for statement in manifest.setup_statements:
            self._bounded(role, database, statement)
        count_execution = self._bounded(
            role, database, "SELECT COUNT(*) FROM `cpu_data` ORDER BY 1"
        )
        if len(count_execution.rows) != 1 or len(count_execution.rows[0]) != 1:
            raise RuntimeError("CPU materialization evidence query returned no row")
        row_count = count_execution.rows[0][0]
        if not isinstance(row_count, int) or isinstance(row_count, bool):
            raise RuntimeError("CPU materialization count is not an integer")
        if row_count != manifest.expected_row_count:
            raise RuntimeError("CPU materialization row count differs from manifest")
        sample_ids = sorted(
            {
                1,
                max(1, row_count // 4),
                max(1, row_count // 2),
                max(1, (row_count * 3) // 4),
                row_count,
            }
        )
        samples = self._bounded(
            role,
            database,
            "SELECT id, v FROM `cpu_data` WHERE id IN ("
            + ",".join(str(value) for value in sample_ids)
            + ") ORDER BY 1",
        )
        schema = self._bounded(role, database, "SHOW CREATE TABLE `cpu_data`")
        return MaterializationEvidence(
            schema_digest=stable_fingerprint(repr(schema.rows)),
            row_counts={"cpu_data": row_count},
            content_digest=stable_fingerprint(
                {
                    "expected_rows": manifest.expected_row_count,
                    "samples": repr(samples.rows),
                    "setup": manifest.setup_statements,
                }
            ),
        )


def _server_fingerprints(
    nodes: Sequence[NodeConfig],
    factory: ConnectionFactory,
) -> Mapping[NodeRole, str]:
    sql = (
        "SELECT VERSION(), @@version_comment, @@sql_mode, @@optimizer_switch, "
        "@@transaction_isolation, @@character_set_server, @@collation_server, "
        "@@innodb_page_size, @@lower_case_table_names"
    )

    def one(node: NodeConfig) -> tuple[NodeRole, str]:
        with factory.control_session(node, "information_schema") as session:
            rows = _execute(session, sql)
        if len(rows) != 1:
            raise RuntimeError("performance fingerprint probe returned no row")
        return node.role, stable_fingerprint(rows[0])

    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="sf-perf-fingerprint") as pool:
        return dict(pool.map(one, nodes))


class PerformanceModeRunner:
    def __init__(self, config: AppConfig, artifact_root: Path) -> None:
        if config.mode.value != "performance":
            raise ValueError("performance runner requires performance config mode")
        self._config = config
        self._artifact_root = artifact_root

    def run(self, request: RunRequest, stop_event: Event) -> RunSummary:
        if request.mode != "performance" or request.workers != 1:
            raise ValueError("performance request requires mode=performance and workers=1")
        policy = PerformancePolicy.from_config(self._config.performance)
        scale = ScaleKnobs.from_config(self._config.performance)
        connector = MySQLConnectorFactory()
        query_runner = NodeQueryRunner(connector)
        materializer = ScaleMaterializer(
            MySQLCpuMaterializationPort(
                self._config.nodes,
                connector,
                query_runner,
                timeout_seconds=policy.formal_timeout_seconds,
                stop_event=stop_event,
            )
        )
        calibration = CalibrationEngine(
            ReferenceAnalyzer(self._config.nodes, query_runner),
            materializer,
            policy,
            CostModel(row_cap=policy.max_table_rows),
        )
        formal = FormalRunner(
            self._config.nodes,
            query_runner,
            policy,
            diagnostics=MySQLDiagnosticsCollector(connector),
        )
        records = JsonlWriter(self._artifact_root / "events.jsonl")
        fingerprints = _server_fingerprints(self._config.nodes, connector)
        records.append(
            {
                "configuration_difference": len(set(fingerprints.values())) > 1,
                "fingerprints": {
                    role.value: fingerprints[role] for role in NodeRole
                },
                "run_id": request.run_id,
                "occurred_at": datetime.now(UTC).isoformat(),
                "type": "performance_preflight",
            }
        )
        recorder = PerformanceRecorder(
            records,
            PerformanceDiagnosticWriter(self._artifact_root),
            run_id=request.run_id,
            node_config_fingerprints=fingerprints,
        )
        names = DatabaseNameFactory()

        def database_name(round_number: int) -> str:
            return names.new(
                mode="performance",
                worker=0,
                round_number=round_number,
                seed=SeedTree(request.seed).derive("performance", round_number),
            )

        service = PerformanceService(
            calibration,
            formal,
            recorder,
            database_name=database_name,
            policy=policy,
        )
        template_tree = SeedTree(request.seed)
        result = service.run(
            [
                template(
                    seed=template_tree.derive("template", template_id),
                    case_id=f"perf_{request.run_id}_{template_id}",
                    initial_scale=scale,
                )
                for template, template_id in (
                    (CpuDenseScanTemplate, "scan"),
                    (CpuDenseRangeSortTemplate, "range_sort"),
                    (CpuDenseJoinTemplate, "join"),
                    (CpuDenseGroupSortTemplate, "group_sort"),
                    (CpuDenseWindowTemplate, "window"),
                )
            ],
            rounds=request.rounds,
            queries_per_round=request.queries_per_round,
            stop_event=stop_event,
        )
        records.append(
            {
                "type": "performance_run_summary",
                "run_id": request.run_id,
                "occurred_at": datetime.now(UTC).isoformat(),
                "rounds_started": result.rounds_started,
                "rounds_completed": result.rounds_completed,
                "queries_completed": result.queries_completed,
                "rejected": result.rejected,
                "calibration_failures": result.calibration_failures,
            }
        )
        return RunSummary(
            run_id=request.run_id,
            rounds_completed=result.rounds_completed,
            queries_completed=result.queries_completed,
            findings=sum(
                item.verdict is Verdict.PERF_ALERT for item in result.assessments
            ),
            rejected=result.rejected,
            over_budget=sum(
                item.verdict is Verdict.OVER_BUDGET for item in result.assessments
            ),
            stopped=stop_event.is_set(),
        )


def build_performance_runner(
    config: AppConfig,
    artifact_root: Path,
) -> PerformanceModeRunner:
    return PerformanceModeRunner(config, artifact_root)


__all__ = [
    "MySQLCpuMaterializationPort",
    "PerformanceModeRunner",
    "build_performance_runner",
]
