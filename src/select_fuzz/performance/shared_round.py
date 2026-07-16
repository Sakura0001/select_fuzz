"""Prepare one shared dataset per performance round and freeze many queries."""

from __future__ import annotations

from typing import Protocol

from select_fuzz.config import NodeRole
from select_fuzz.performance.calibration import (
    CalibrationFailureKind,
    CalibrationInfrastructurePause,
    CalibrationTerminated,
    PerformanceTemplate,
)
from select_fuzz.performance.materialization import (
    MaterializationExecutionFailure,
    MaterializationInfrastructureFailure,
    MaterializationMismatch,
    MaterializationTimeout,
)
from select_fuzz.performance.models import FrozenCase, ScaleKnobs


class RoundMaterializer(Protocol):
    def rebuild_all(self, database: str, manifest: object) -> object: ...


class SharedRoundCasePreparer:
    """Materialize the first query in a database and reuse it for later queries."""

    def __init__(self, materializer: RoundMaterializer) -> None:
        self._materializer = materializer
        self._database: str | None = None
        self._manifest: object | None = None

    def prepare(
        self,
        template: PerformanceTemplate,
        initial: ScaleKnobs,
        *,
        database: str,
    ) -> FrozenCase:
        if not database:
            raise ValueError("database must not be empty")
        manifest = template.data_manifest(initial)
        sql = template.render(initial)
        if database != self._database:
            self._materialize(initial, database, manifest, sql)
            self._database = database
            self._manifest = manifest
        elif manifest != self._manifest:
            raise CalibrationTerminated(
                CalibrationFailureKind.SETUP_MISMATCH,
                NodeRole.BASELINE,
                error_type="RoundManifestChanged",
                scale=initial,
                sql=sql,
                data_manifest=manifest,
                database=database,
                failure_details={
                    "reason": "queries in one performance round must share one dataset"
                },
            )
        return FrozenCase(
            case_id=template.case_id,
            template_id=template.template_id,
            seed=template.seed,
            database=database,
            scale=initial,
            data_manifest=manifest,
            sql=sql,
            boundary=template.boundary,
            medians_seconds={},
            attempts=(),
        )

    def _materialize(
        self,
        initial: ScaleKnobs,
        database: str,
        manifest: object,
        sql: str,
    ) -> None:
        try:
            self._materializer.rebuild_all(database, manifest)
        except MaterializationTimeout as error:
            raise CalibrationTerminated(
                CalibrationFailureKind.TIMEOUT,
                error.role,
                error_type=error.error_type,
                scale=initial,
                sql=sql,
                data_manifest=manifest,
                database=error.database or database,
                failing_action_sql=error.sql,
                failure_details=error.details,
            ) from error
        except MaterializationExecutionFailure as error:
            raise CalibrationTerminated(
                CalibrationFailureKind.EXECUTION,
                error.role,
                error_type=error.error_type,
                scale=initial,
                sql=sql,
                data_manifest=manifest,
                database=error.database or database,
                failing_action_sql=error.sql,
                failure_details=error.details,
            ) from error
        except MaterializationInfrastructureFailure as error:
            raise CalibrationInfrastructurePause(
                CalibrationFailureKind.INFRA,
                error.role,
                error_type=error.error_type,
                scale=initial,
                sql=sql,
                data_manifest=manifest,
                database=error.database or database,
                failing_action_sql=error.sql,
                failure_details=error.details,
            ) from error
        except MaterializationMismatch as error:
            raise CalibrationTerminated(
                CalibrationFailureKind.SETUP_MISMATCH,
                NodeRole.BASELINE,
                error_type=type(error).__name__,
                scale=initial,
                sql=sql,
                data_manifest=manifest,
                database=error.database or database,
                failing_action_sql=error.sql,
                failure_details=error.details,
            ) from error
        except Exception as error:
            raise CalibrationInfrastructurePause(
                CalibrationFailureKind.INFRA,
                NodeRole.BASELINE,
                error_type=type(error).__name__,
                scale=initial,
                sql=sql,
                data_manifest=manifest,
                database=database,
            ) from error


__all__ = ["RoundMaterializer", "SharedRoundCasePreparer"]
