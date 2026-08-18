"""Production adapters and shared CLI contract for performance mode."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

from select_fuzz.artifacts import JsonlWriter, WorkerSqlLogWriter
from select_fuzz.config import COMPARISON_ROLES, AppConfig, NodeConfig, NodeRole
from select_fuzz.domain import (
    ExecutionStatus,
    NodeExecution,
    RunRequest,
    SeedTree,
    deterministic_id,
    stable_fingerprint,
)
from select_fuzz.execution import (
    DatabaseNameFactory,
    MySQLConnectorFactory,
    NodeQueryRunner,
)
from select_fuzz.execution.protocols import BarrierLike, ConnectionFactory, QuerySession
from select_fuzz.performance.artifacts import (
    PerformanceDiagnosticWriter,
    PerformanceRecorder,
)
from select_fuzz.performance.execution import FormalRunner
from select_fuzz.performance.fuzz import (
    PerformanceFuzzTemplate,
    ScalableFuzzSetupManifest,
)
from select_fuzz.performance.diagnostics import MySQLDiagnosticsCollector
from select_fuzz.performance.materialization import (
    MaterializationEvidence,
    MaterializationExecutionFailure,
    MaterializationInfrastructureFailure,
    MaterializationMismatch,
    MaterializationTimeout,
    ScaleMaterializer,
)
from select_fuzz.performance.models import PerformancePolicy, Verdict
from select_fuzz.performance.service import PerformanceService
from select_fuzz.performance.shared_round import SharedRoundCasePreparer
from select_fuzz.performance.templates import CpuDenseSetupManifest
from select_fuzz.oracle.errors import normalize_error
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


def _append_audit_sql(
    writer: WorkerSqlLogWriter | None,
    sql: str,
    *,
    role: NodeRole,
    database: str,
    phase: str,
) -> None:
    if writer is None:
        return
    metadata = {
        "database": database,
        "phase": phase,
        "role": role.value,
        "worker_id": 0,
    }
    if sql.lstrip().upper().startswith("CREATE PROCEDURE "):
        writer.append_routine(0, sql, metadata=metadata)
    else:
        writer.append(0, sql, metadata=metadata)


class _SqlLoggingQueryRunner:
    def __init__(self, core: NodeQueryRunner, writer: WorkerSqlLogWriter | None) -> None:
        self._core = core
        self._writer = writer

    def run(
        self,
        node: NodeConfig,
        database: str,
        sql: str,
        *,
        timeout_s: float,
        row_limit: int,
        byte_limit: int,
        barrier: BarrierLike | None = None,
    ) -> NodeExecution:
        _append_audit_sql(
            self._writer,
            sql,
            role=node.role,
            database=database,
            phase="performance_execute",
        )
        return self._core.run(
            node,
            database,
            sql,
            timeout_s=timeout_s,
            row_limit=row_limit,
            byte_limit=byte_limit,
            barrier=barrier,
        )


class MySQLCpuMaterializationPort:
    """Rebuild one CPU template and verify actual schema/content evidence."""

    def __init__(
        self,
        nodes: Sequence[NodeConfig],
        factory: ConnectionFactory,
        query_runner: NodeQueryRunner | _SqlLoggingQueryRunner,
        *,
        timeout_seconds: float,
        stop_event: Event,
        sql_log: WorkerSqlLogWriter | None = None,
    ) -> None:
        by_role = {node.role: node for node in nodes}
        if len(nodes) != 2 or set(by_role) != set(COMPARISON_ROLES):
            raise ValueError("materialization requires the two comparison roles")
        self._nodes = by_role
        self._factory = factory
        self._query_runner = query_runner
        self._timeout_seconds = timeout_seconds
        self._stop_event = stop_event
        self._sql_log = sql_log

    def _bounded(self, role: NodeRole, database: str, sql: str) -> NodeExecution:
        result = self._run(role, database, sql)
        if result.status is ExecutionStatus.TIMEOUT:
            raise MaterializationTimeout(
                role,
                "MaterializationTimeout",
                database=database,
                sql=sql,
                details={"node_results": self._node_result_details({role: result})},
            )
        if result.status is ExecutionStatus.INFRA_ERROR:
            raise MaterializationInfrastructureFailure(
                role,
                result.watchdog_error_type or "MySQLInfrastructureError",
                database=database,
                sql=sql,
                details={"node_results": self._node_result_details({role: result})},
            )
        if result.status is not ExecutionStatus.SUCCESS:
            raise MaterializationExecutionFailure(
                role,
                "MySQLExecutionError",
                database=database,
                sql=sql,
                details={"node_results": self._node_result_details({role: result})},
            )
        return result

    @staticmethod
    def _node_result_details(
        results: Mapping[NodeRole, NodeExecution],
    ) -> dict[str, object]:
        return {
            role.value: {
                "affected_rows": result.affected_rows,
                "error": (
                    None
                    if result.error is None
                    else {
                        "errno": result.error.errno,
                        "message": result.error.message,
                        "sqlstate": result.error.sqlstate,
                    }
                ),
                "status": result.status.value,
                "watchdog_error_type": result.watchdog_error_type,
            }
            for role, result in results.items()
        }

    def _run(self, role: NodeRole, database: str, sql: str) -> NodeExecution:
        if self._stop_event.is_set():
            raise MaterializationInfrastructureFailure(
                role, "RunStopped", database=database, sql=sql
            )
        result = self._query_runner.run(
            self._nodes[role],
            database,
            sql,
            timeout_s=self._timeout_seconds,
            row_limit=16,
            byte_limit=8 * 1024 * 1024,
        )
        if result.role is not role:
            raise MaterializationInfrastructureFailure(
                role, "RoleMismatch", database=database, sql=sql
            )
        return result

    @staticmethod
    def _validate_manifest(manifest: object) -> None:
        if not isinstance(manifest, (CpuDenseSetupManifest, ScalableFuzzSetupManifest)):
            raise TypeError("performance materialization requires a supported setup manifest")

    def prepare(
        self,
        role: NodeRole,
        database: str,
        manifest: object,
    ) -> None:
        self._validate_manifest(manifest)
        assert isinstance(manifest, (CpuDenseSetupManifest, ScalableFuzzSetupManifest))
        with self._factory.control_session(self._nodes[role], "information_schema") as session:
            create_database = f"CREATE DATABASE IF NOT EXISTS `{database}`"
            _append_audit_sql(
                self._sql_log,
                create_database,
                role=role,
                database="information_schema",
                phase="performance_setup",
            )
            _execute(session, create_database)
        for statement in manifest.setup_statements:
            self._bounded(role, database, statement)

    def prepare_all(self, database: str, manifest: object) -> None:
        """Execute setup statement-by-statement and compare both outcomes."""

        self._validate_manifest(manifest)
        assert isinstance(manifest, (CpuDenseSetupManifest, ScalableFuzzSetupManifest))
        statements = (
            ("information_schema", f"CREATE DATABASE IF NOT EXISTS `{database}`"),
            *((database, statement) for statement in manifest.setup_statements),
        )
        for statement_database, sql in statements:
            self._comparison_lockstep(statement_database, sql)

    def _comparison_lockstep(self, database: str, sql: str) -> None:
        with ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="sf-perf-setup-statement"
        ) as pool:
            futures = {
                role: pool.submit(self._run, role, database, sql)
                for role in COMPARISON_ROLES
            }
            results = {role: futures[role].result() for role in COMPARISON_ROLES}
        is_dml = sql.lstrip().split(None, 1)[0].upper() in {
            "INSERT",
            "UPDATE",
            "DELETE",
            "REPLACE",
        }
        identities: set[object] = set()
        for role in COMPARISON_ROLES:
            result = results[role]
            if result.status is ExecutionStatus.SUCCESS:
                identities.add(
                    (
                        result.status,
                        result.affected_rows if is_dml else None,
                    )
                )
            else:
                identities.add(
                    (
                        result.status,
                        None if result.error is None else normalize_error(result.error),
                    )
                )
        if len(identities) != 1:
            raise MaterializationMismatch(
                f"comparison setup outcomes differ for {sql.split(None, 1)[0]}",
                database=database,
                sql=sql,
                details={"node_results": self._node_result_details(results)},
            )
        details = {"node_results": self._node_result_details(results)}
        reference = results[NodeRole.CUSTOM_OFF]
        if reference.status is ExecutionStatus.SUCCESS:
            if is_dml and reference.affected_rows is None:
                raise MaterializationExecutionFailure(
                    NodeRole.CUSTOM_OFF,
                    "MissingAffectedRows",
                    database=database,
                    sql=sql,
                    details=details,
                )
            return
        if reference.status is ExecutionStatus.TIMEOUT:
            raise MaterializationTimeout(
                NodeRole.CUSTOM_OFF,
                "MaterializationTimeout",
                database=database,
                sql=sql,
                details=details,
            )
        if reference.status is ExecutionStatus.INFRA_ERROR:
            raise MaterializationInfrastructureFailure(
                NodeRole.CUSTOM_OFF,
                reference.watchdog_error_type or "MySQLInfrastructureError",
                database=database,
                sql=sql,
                details=details,
            )
        raise MaterializationExecutionFailure(
            NodeRole.CUSTOM_OFF,
            "MySQLExecutionError",
            database=database,
            sql=sql,
            details=details,
        )

    def evidence(
        self,
        role: NodeRole,
        database: str,
        manifest: object,
    ) -> MaterializationEvidence:
        self._validate_manifest(manifest)
        assert isinstance(manifest, (CpuDenseSetupManifest, ScalableFuzzSetupManifest))
        expected_rows = (
            {"cpu_data": manifest.expected_row_count}
            if isinstance(manifest, CpuDenseSetupManifest)
            else manifest.expected_rows
        )
        row_counts: dict[str, int] = {}
        schema_rows: list[object] = []
        sample_rows: list[object] = []
        for table_name, expected in expected_rows.items():
            count_execution = self._bounded(
                role,
                database,
                f"SELECT COUNT(*) FROM `{table_name}` ORDER BY 1",
            )
            if len(count_execution.rows) != 1 or len(count_execution.rows[0]) != 1:
                raise MaterializationExecutionFailure(
                    role,
                    "MissingEvidenceRow",
                    database=database,
                    sql=f"SELECT COUNT(*) FROM `{table_name}` ORDER BY 1",
                    details={"node_results": self._node_result_details({role: count_execution})},
                )
            row_count = count_execution.rows[0][0]
            if not isinstance(row_count, int) or isinstance(row_count, bool):
                raise MaterializationExecutionFailure(
                    role,
                    "InvalidEvidenceCount",
                    database=database,
                    sql=f"SELECT COUNT(*) FROM `{table_name}` ORDER BY 1",
                )
            if row_count != expected:
                raise MaterializationExecutionFailure(
                    role,
                    "EvidenceRowCountMismatch",
                    database=database,
                    sql=f"SELECT COUNT(*) FROM `{table_name}` ORDER BY 1",
                    details={"actual_rows": row_count, "expected_rows": expected},
                )
            row_counts[table_name] = row_count
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
                f"SELECT * FROM `{table_name}` WHERE `id` IN ("
                + ",".join(str(value) for value in sample_ids)
                + ") ORDER BY `id`",
            )
            schema = self._bounded(role, database, f"SHOW CREATE TABLE `{table_name}`")
            schema_rows.append((table_name, schema.rows))
            sample_rows.append((table_name, samples.rows))
        return MaterializationEvidence(
            schema_digest=stable_fingerprint(repr(schema_rows)),
            row_counts=row_counts,
            content_digest=stable_fingerprint(
                {
                    "expected_rows": expected_rows,
                    "samples": repr(sample_rows),
                    "setup": manifest.setup_statements,
                }
            ),
        )

    def materialize(
        self,
        role: NodeRole,
        database: str,
        manifest: object,
    ) -> MaterializationEvidence:
        """Compatibility path; ScaleMaterializer uses the phased API above."""

        self.prepare(role, database, manifest)
        return self.evidence(role, database, manifest)


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

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="sf-perf-fingerprint") as pool:
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
        nodes = self._config.comparison_nodes
        connector = MySQLConnectorFactory()
        thread_sql_log = (
            WorkerSqlLogWriter(self._artifact_root / "sql")
            if self._config.full_thread_sql_log
            else None
        )
        query_runner = _SqlLoggingQueryRunner(
            NodeQueryRunner(connector),
            thread_sql_log,
        )
        materializer = ScaleMaterializer(
            MySQLCpuMaterializationPort(
                nodes,
                connector,
                query_runner,
                timeout_seconds=self._config.performance.materialization_timeout_seconds,
                stop_event=stop_event,
                sql_log=thread_sql_log,
            )
        )
        preparation = SharedRoundCasePreparer(materializer)
        formal = FormalRunner(
            nodes,
            query_runner,
            policy,
            diagnostics=MySQLDiagnosticsCollector(connector),
        )
        records = JsonlWriter(self._artifact_root / "events.jsonl")
        fingerprints = _server_fingerprints(nodes, connector)
        records.append(
            {
                "configuration_difference": len(set(fingerprints.values())) > 1,
                "fingerprints": {
                    role.value: fingerprints[role] for role in COMPARISON_ROLES
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
            sql_root=self._artifact_root,
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
            preparation,
            formal,
            recorder,
            database_name=database_name,
            policy=policy,
        )
        template_tree = SeedTree(request.seed)
        base_template = PerformanceFuzzTemplate(
            seed=template_tree.derive("performance", "fuzz_catalog"),
            case_id=deterministic_id("perf", request.run_id, request.seed),
            min_initial_rows=self._config.performance.initial_table_rows,
            max_initial_rows=min(
                self._config.performance.initial_table_rows_max,
                self._config.performance.max_table_rows,
            ),
            max_table_rows=self._config.performance.max_table_rows,
            max_total_rows=self._config.performance.max_total_rows,
            batch_rows=self._config.performance.insert_batch_rows,
            min_tables=self._config.performance.min_tables,
            max_tables=self._config.performance.max_tables,
            min_columns=self._config.performance.min_columns,
            max_columns=self._config.performance.max_columns,
            max_indexes_per_table=self._config.performance.max_indexes_per_table,
            max_query_tables=self._config.performance.max_query_tables,
            max_query_depth=self._config.performance.max_query_depth,
        )
        result = service.run(
            [base_template],
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
                "setup_failures": result.setup_failures,
                "calibration_failures": result.setup_failures,
            }
        )
        return RunSummary(
            run_id=request.run_id,
            rounds_completed=result.rounds_completed,
            queries_completed=result.queries_completed,
            findings=(
                sum(item.verdict is not Verdict.PASS for item in result.assessments)
                + result.setup_failures
            ),
            rejected=result.rejected,
            over_budget=sum(item.verdict is Verdict.OVER_BUDGET for item in result.assessments),
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
