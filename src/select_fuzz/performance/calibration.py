"""Plan-seeded, reference-only scale calibration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from statistics import median
from typing import Protocol

from select_fuzz.config import COMPARISON_ROLES, NodeConfig, NodeRole
from select_fuzz.domain import NodeExecution
from select_fuzz.execution.protocols import BarrierLike
from select_fuzz.performance.execution import classify_execution
from select_fuzz.performance.materialization import (
    MaterializationExecutionFailure,
    MaterializationInfrastructureFailure,
    MaterializationMismatch,
    MaterializationTimeout,
)
from select_fuzz.performance.models import (
    CalibrationAttempt,
    FrozenCase,
    Outcome,
    PerformancePolicy,
    ScaleKnobs,
)
from select_fuzz.performance.tree import (
    Family,
    PlanParseError,
    ShapeBoundary,
    TreePlan,
    parse_tree,
)


REFERENCE_ROLES = (NodeRole.CUSTOM_OFF,)


class CalibrationFailureKind(StrEnum):
    TIMEOUT = "timeout"
    INFRA = "infra"
    EXECUTION = "execution"
    PARSE = "parse"
    SHAPE = "shape"
    SETUP_MISMATCH = "setup_mismatch"


class CalibrationExhausted(RuntimeError):
    def __init__(self, attempts: tuple[CalibrationAttempt, ...]) -> None:
        super().__init__(f"performance calibration exhausted after {len(attempts)} rounds")
        self.attempts = attempts


class CalibrationDivergence(CalibrationExhausted):
    pass


class CalibrationTerminated(RuntimeError):
    """Semantic failure that must be persisted and must terminate this case."""

    def __init__(
        self,
        kind: CalibrationFailureKind,
        role: NodeRole,
        *,
        error_code: int | None = None,
        error_type: str | None = None,
        attempts: tuple[CalibrationAttempt, ...] = (),
        scale: ScaleKnobs | None = None,
        sql: str | None = None,
        data_manifest: object | None = None,
        database: str | None = None,
        failing_action_sql: str | None = None,
        failure_details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(f"calibration {kind.value} on {role.value}")
        self.kind = kind
        self.role = role
        self.error_code = error_code
        self.error_type = error_type
        self.attempts = attempts
        self.scale = scale
        self.sql = sql
        self.data_manifest = data_manifest
        self.database = database
        self.failing_action_sql = failing_action_sql
        self.failure_details = {} if failure_details is None else dict(failure_details)


class CalibrationInfrastructurePause(CalibrationTerminated):
    pass


class PerformanceTemplate(Protocol):
    @property
    def seed(self) -> int: ...

    @property
    def case_id(self) -> str: ...

    @property
    def template_id(self) -> str: ...

    @property
    def boundary(self) -> ShapeBoundary: ...

    @property
    def driver_family(self) -> Family: ...

    def render(self, scale: ScaleKnobs) -> str: ...

    def data_manifest(self, scale: ScaleKnobs) -> object: ...

    def target_rows(self, scale: ScaleKnobs) -> int: ...


class CalibrationPort(Protocol):
    def explain_tree(self, role: NodeRole, database: str, sql: str) -> str: ...

    def analyze(
        self,
        role: NodeRole,
        database: str,
        sql: str,
        *,
        timeout_s: float,
    ) -> NodeExecution: ...


class SharedQueryRunner(Protocol):
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
    ) -> NodeExecution: ...


def _tree_payload(execution: NodeExecution) -> str | None:
    payload = execution.performance_payload
    if payload is not None:
        tree = payload.get("tree")
        if isinstance(tree, str):
            return tree
    if execution.rows and execution.rows[0] and isinstance(execution.rows[0][0], str):
        return execution.rows[0][0]
    return None


class ReferenceAnalyzer:
    """Thin adapter from calibration operations to the shared bounded runner."""

    def __init__(self, nodes: Sequence[NodeConfig], runner: SharedQueryRunner) -> None:
        by_role = {node.role: node for node in nodes}
        if len(nodes) != 2 or set(by_role) != set(COMPARISON_ROLES):
            raise ValueError("reference analyzer requires the two comparison roles")
        self._nodes = by_role
        self._runner = runner

    def explain_tree(self, role: NodeRole, database: str, sql: str) -> str:
        raw = self._run(role, database, f"EXPLAIN FORMAT=TREE {sql.rstrip().rstrip(';')}")
        tree = _tree_payload(raw)
        outcome = classify_execution(raw)
        if raw.role is not role or outcome is not Outcome.COMPLETED or tree is None:
            failure_kind = (
                CalibrationFailureKind.INFRA
                if outcome is Outcome.INFRA_ERROR
                else CalibrationFailureKind.EXECUTION
            )
            exception_type = (
                CalibrationInfrastructurePause
                if failure_kind is CalibrationFailureKind.INFRA
                else CalibrationTerminated
            )
            raise exception_type(
                failure_kind,
                role,
                error_code=None if raw.error is None else raw.error.errno,
                error_type=raw.watchdog_error_type
                or (
                    "MySQLInfrastructureError"
                    if failure_kind is CalibrationFailureKind.INFRA
                    else "MySQLError"
                ),
            )
        return tree

    def analyze(
        self,
        role: NodeRole,
        database: str,
        sql: str,
        *,
        timeout_s: float,
    ) -> NodeExecution:
        return self._run(
            role,
            database,
            f"EXPLAIN ANALYZE FORMAT=TREE {sql.rstrip().rstrip(';')}",
            timeout_s=timeout_s,
        )

    def _run(
        self,
        role: NodeRole,
        database: str,
        sql: str,
        *,
        timeout_s: float = 15.0,
    ) -> NodeExecution:
        if role not in REFERENCE_ROLES:
            raise ValueError("calibration operation is restricted to reference roles")
        return self._runner.run(
            self._nodes[role],
            database,
            sql,
            timeout_s=timeout_s,
            row_limit=1,
            byte_limit=32 * 1024 * 1024,
        )


class Materializer(Protocol):
    def rebuild_all(self, database: str, manifest: object) -> object: ...


class CostModel:
    """Seed scale from the reference plans' actual estimated driver work."""

    def __init__(self, *, row_cap: int) -> None:
        if row_cap <= 0:
            raise ValueError("row_cap must be positive")
        self._row_cap = row_cap

    def seed_scale(
        self,
        template: PerformanceTemplate,
        initial: ScaleKnobs,
        plans: Mapping[NodeRole, TreePlan],
    ) -> ScaleKnobs:
        if set(plans) != set(REFERENCE_ROLES):
            raise ValueError("cost model requires the custom_off reference plan")
        observed = max(plan.estimated_work(template.driver_family) for plan in plans.values())
        if observed <= 0:
            raise PlanParseError("estimated driver work must be positive")
        factor = min(8.0, max(0.25, template.target_rows(initial) / observed))
        return initial.scaled(factor, row_cap=self._row_cap)


@dataclass(frozen=True, slots=True)
class _Observation:
    seconds: float | None
    failure: CalibrationFailureKind | None


class CalibrationEngine:
    def __init__(
        self,
        port: CalibrationPort,
        materializer: Materializer,
        policy: PerformancePolicy,
        cost_model: CostModel | None = None,
    ) -> None:
        self._port = port
        self._materializer = materializer
        self._policy = policy
        self._cost_model = cost_model or CostModel(row_cap=policy.max_table_rows)

    def calibrate(
        self,
        template: PerformanceTemplate,
        initial: ScaleKnobs,
        *,
        database: str,
    ) -> FrozenCase:
        if not database:
            raise ValueError("database must not be empty")
        initial_manifest = template.data_manifest(initial)
        initial_sql = template.render(initial)
        try:
            self._materializer.rebuild_all(database, initial_manifest)
        except MaterializationTimeout as error:
            raise CalibrationTerminated(
                CalibrationFailureKind.TIMEOUT,
                error.role,
                error_type=error.error_type,
                scale=initial,
                sql=initial_sql,
                data_manifest=initial_manifest,
                database=database,
                failing_action_sql=error.sql,
                failure_details=error.details,
            ) from error
        except MaterializationExecutionFailure as error:
            raise CalibrationTerminated(
                CalibrationFailureKind.EXECUTION,
                error.role,
                error_type=error.error_type,
                scale=initial,
                sql=initial_sql,
                data_manifest=initial_manifest,
                database=database,
                failing_action_sql=error.sql,
                failure_details=error.details,
            ) from error
        except MaterializationInfrastructureFailure as error:
            raise CalibrationInfrastructurePause(
                CalibrationFailureKind.INFRA,
                error.role,
                error_type=error.error_type,
                scale=initial,
                sql=initial_sql,
                data_manifest=initial_manifest,
                database=database,
                failing_action_sql=error.sql,
                failure_details=error.details,
            ) from error
        except MaterializationMismatch as error:
            raise CalibrationTerminated(
                CalibrationFailureKind.SETUP_MISMATCH,
                NodeRole.CUSTOM_OFF,
                error_type=type(error).__name__,
                scale=initial,
                sql=initial_sql,
                data_manifest=initial_manifest,
                database=error.database or database,
                failing_action_sql=error.sql,
                failure_details=error.details,
            ) from error
        except Exception as error:
            raise CalibrationInfrastructurePause(
                CalibrationFailureKind.INFRA,
                NodeRole.CUSTOM_OFF,
                error_type=type(error).__name__,
                scale=initial,
                sql=initial_sql,
                data_manifest=initial_manifest,
                database=database,
            ) from error
        try:
            plans = {
                role: parse_tree(
                    self._port.explain_tree(role, database, initial_sql),
                    completed=False,
                )
                for role in REFERENCE_ROLES
            }
        except CalibrationTerminated as error:
            exception_type = (
                CalibrationInfrastructurePause
                if isinstance(error, CalibrationInfrastructurePause)
                else CalibrationTerminated
            )
            raise exception_type(
                error.kind,
                error.role,
                error_code=error.error_code,
                error_type=error.error_type,
                scale=initial,
                sql=initial_sql,
                data_manifest=initial_manifest,
                database=database,
                failing_action_sql=error.failing_action_sql,
                failure_details=error.failure_details,
            ) from error
        except PlanParseError as error:
            raise CalibrationTerminated(
                CalibrationFailureKind.PARSE,
                NodeRole.CUSTOM_OFF,
                error_type=type(error).__name__,
                scale=initial,
                sql=initial_sql,
                data_manifest=initial_manifest,
                database=database,
            ) from error
        for role, plan in plans.items():
            try:
                template.boundary.validate(plan, role.value)
            except PlanParseError as error:
                raise CalibrationTerminated(
                    CalibrationFailureKind.SHAPE,
                    role,
                    error_type=type(error).__name__,
                    scale=initial,
                    sql=initial_sql,
                    data_manifest=initial_manifest,
                    database=database,
                ) from error
        try:
            scale = self._cost_model.seed_scale(template, initial, plans)
        except PlanParseError as error:
            raise CalibrationTerminated(
                CalibrationFailureKind.PARSE,
                NodeRole.CUSTOM_OFF,
                error_type=type(error).__name__,
                scale=initial,
                sql=initial_sql,
                data_manifest=initial_manifest,
                database=database,
            ) from error
        if scale.table_rows < initial.table_rows:
            scale = initial

        attempts: list[CalibrationAttempt] = []
        lower, upper = self._policy.calibration_band_seconds
        for number in range(1, self._policy.max_calibration_rounds + 1):
            manifest = template.data_manifest(scale)
            if number > 1 or scale != initial:
                try:
                    self._materializer.rebuild_all(database, manifest)
                except MaterializationTimeout as error:
                    raise CalibrationTerminated(
                        CalibrationFailureKind.TIMEOUT,
                        error.role,
                        error_type=error.error_type,
                        attempts=tuple(attempts),
                        scale=scale,
                        sql=template.render(scale),
                        data_manifest=manifest,
                        database=database,
                        failing_action_sql=error.sql,
                        failure_details=error.details,
                    ) from error
                except MaterializationExecutionFailure as error:
                    raise CalibrationTerminated(
                        CalibrationFailureKind.EXECUTION,
                        error.role,
                        error_type=error.error_type,
                        attempts=tuple(attempts),
                        scale=scale,
                        sql=template.render(scale),
                        data_manifest=manifest,
                        database=database,
                        failing_action_sql=error.sql,
                        failure_details=error.details,
                    ) from error
                except MaterializationInfrastructureFailure as error:
                    raise CalibrationInfrastructurePause(
                        CalibrationFailureKind.INFRA,
                        error.role,
                        error_type=error.error_type,
                        attempts=tuple(attempts),
                        scale=scale,
                        sql=template.render(scale),
                        data_manifest=manifest,
                        database=database,
                        failing_action_sql=error.sql,
                        failure_details=error.details,
                    ) from error
                except MaterializationMismatch as error:
                    raise CalibrationTerminated(
                        CalibrationFailureKind.SETUP_MISMATCH,
                        NodeRole.CUSTOM_OFF,
                        error_type=type(error).__name__,
                        attempts=tuple(attempts),
                        scale=scale,
                        sql=template.render(scale),
                        data_manifest=manifest,
                        database=error.database or database,
                        failing_action_sql=error.sql,
                        failure_details=error.details,
                    ) from error
                except Exception as error:
                    raise CalibrationInfrastructurePause(
                        CalibrationFailureKind.INFRA,
                        NodeRole.CUSTOM_OFF,
                        error_type=type(error).__name__,
                        attempts=tuple(attempts),
                        scale=scale,
                        sql=template.render(scale),
                        data_manifest=manifest,
                        database=database,
                    ) from error
            sql = template.render(scale)
            samples: dict[NodeRole, list[float | None]] = {role: [] for role in REFERENCE_ROLES}
            failures: dict[NodeRole, list[str | None]] = {role: [] for role in REFERENCE_ROLES}
            candidate_timed_out = False
            for _ in range(self._policy.calibration_runs_per_reference):
                for role in REFERENCE_ROLES:
                    execution = self._port.analyze(
                        role,
                        database,
                        sql,
                        timeout_s=self._policy.formal_timeout_seconds,
                    )
                    try:
                        observation = self._observe(execution, template.boundary)
                    except CalibrationTerminated as error:
                        exception_type = (
                            CalibrationInfrastructurePause
                            if isinstance(error, CalibrationInfrastructurePause)
                            else CalibrationTerminated
                        )
                        raise exception_type(
                            error.kind,
                            error.role,
                            error_code=error.error_code,
                            error_type=error.error_type,
                            attempts=tuple(attempts),
                            scale=scale,
                            sql=sql,
                            data_manifest=manifest,
                            database=database,
                            failing_action_sql=error.failing_action_sql,
                            failure_details=error.failure_details,
                        ) from error
                    samples[role].append(observation.seconds)
                    failures[role].append(
                        None if observation.failure is None else observation.failure.value
                    )
                    if observation.failure is CalibrationFailureKind.TIMEOUT:
                        candidate_timed_out = True
                        break
                if candidate_timed_out:
                    break
            medians = self._medians(samples)
            attempt = CalibrationAttempt(
                number=number,
                scale=scale,
                sql=sql,
                samples_seconds={role: tuple(values) for role, values in samples.items()},
                medians_seconds=medians,
                failure_categories={role: tuple(values) for role, values in failures.items()},
            )
            attempts.append(attempt)
            if len(medians) == 1 and all(lower <= value <= upper for value in medians.values()):
                return FrozenCase(
                    case_id=template.case_id,
                    template_id=template.template_id,
                    seed=template.seed,
                    database=database,
                    scale=scale,
                    data_manifest=manifest,
                    sql=sql,
                    boundary=template.boundary,
                    medians_seconds=medians,
                    attempts=tuple(attempts),
                )
            timed_out = candidate_timed_out or any(
                category == CalibrationFailureKind.TIMEOUT.value
                for categories in failures.values()
                for category in categories
            )
            if timed_out or (medians and max(medians.values()) > upper):
                # The random initial volume is a floor. Slow/timeout cases are
                # unsuitable for this lane and are resampled by the service.
                raise CalibrationExhausted(tuple(attempts))
            scale = scale.scaled(
                self._policy.scale_multiplier,
                row_cap=self._policy.max_table_rows,
            )
        raise CalibrationExhausted(tuple(attempts))

    @staticmethod
    def _observe(execution: NodeExecution, boundary: ShapeBoundary) -> _Observation:
        outcome = classify_execution(execution)
        if outcome is Outcome.TIMEOUT:
            return _Observation(None, CalibrationFailureKind.TIMEOUT)
        if outcome is Outcome.INFRA_ERROR:
            raise CalibrationInfrastructurePause(
                CalibrationFailureKind.INFRA,
                execution.role,
                error_code=None if execution.error is None else execution.error.errno,
                error_type=execution.watchdog_error_type or "MySQLInfrastructureError",
            )
        if outcome is not Outcome.COMPLETED:
            raise CalibrationTerminated(
                CalibrationFailureKind.EXECUTION,
                execution.role,
                error_code=None if execution.error is None else execution.error.errno,
                error_type=execution.watchdog_error_type or "MySQLError",
            )
        tree = _tree_payload(execution)
        if tree is None:
            raise CalibrationTerminated(
                CalibrationFailureKind.PARSE,
                execution.role,
                error_type="MissingTreePayload",
            )
        try:
            plan = parse_tree(tree, completed=True)
        except PlanParseError as error:
            raise CalibrationTerminated(
                CalibrationFailureKind.PARSE,
                execution.role,
                error_type=type(error).__name__,
            ) from error
        try:
            boundary.validate(plan, execution.role.value)
        except PlanParseError as error:
            raise CalibrationTerminated(
                CalibrationFailureKind.SHAPE,
                execution.role,
                error_type=type(error).__name__,
            ) from error
        if plan.root.end_ms is None:
            raise CalibrationTerminated(
                CalibrationFailureKind.PARSE,
                execution.role,
                error_type="MissingRootTime",
            )
        return _Observation(plan.root.end_ms / 1000, None)

    @staticmethod
    def _medians(
        samples: Mapping[NodeRole, list[float | None]],
    ) -> dict[NodeRole, float]:
        medians: dict[NodeRole, float] = {}
        for role, values in samples.items():
            completed = [value for value in values if value is not None]
            if values and len(completed) == len(values):
                medians[role] = float(median(completed))
        return medians


__all__ = [
    "CalibrationDivergence",
    "CalibrationEngine",
    "CalibrationExhausted",
    "CalibrationFailureKind",
    "CalibrationInfrastructurePause",
    "CalibrationPort",
    "CalibrationTerminated",
    "CostModel",
    "PerformanceTemplate",
    "REFERENCE_ROLES",
    "ReferenceAnalyzer",
]
