from __future__ import annotations

import pytest

from select_fuzz.config import NodeRole
from select_fuzz.performance.calibration import (
    CalibrationFailureKind,
    CalibrationTerminated,
)
from select_fuzz.performance.fuzz import PerformanceFuzzTemplate
from select_fuzz.performance.materialization import MaterializationMismatch
from select_fuzz.performance.shared_round import SharedRoundCasePreparer


class _Materializer:
    def __init__(self, failure: Exception | None = None) -> None:
        self.calls: list[tuple[str, object]] = []
        self.failure = failure

    def rebuild_all(self, database: str, manifest: object) -> object:
        self.calls.append((database, manifest))
        if self.failure is not None:
            raise self.failure
        return {}


def _base() -> PerformanceFuzzTemplate:
    return PerformanceFuzzTemplate(
        seed=71,
        case_id="shared_round",
        min_initial_rows=10,
        max_initial_rows=10,
        max_table_rows=100,
        max_total_rows=100,
        batch_rows=10,
        min_tables=1,
        max_tables=1,
    )


def test_one_materialization_is_reused_by_all_queries_in_the_round() -> None:
    materializer = _Materializer()
    preparer = SharedRoundCasePreparer(materializer)
    base = _base()
    first = base.for_case(1, 1)
    second = base.for_case(1, 2)

    frozen_first = preparer.prepare(first, first.initial_scale, database="sf_performance_round_1")
    frozen_second = preparer.prepare(
        second, second.initial_scale, database="sf_performance_round_1"
    )

    assert len(materializer.calls) == 1
    assert frozen_first.data_manifest == frozen_second.data_manifest
    assert frozen_first.sql != frozen_second.sql
    assert frozen_first.attempts == frozen_second.attempts == ()


def test_new_round_database_gets_one_new_materialization() -> None:
    materializer = _Materializer()
    preparer = SharedRoundCasePreparer(materializer)
    base = _base()

    for round_number in (1, 2):
        case = base.for_case(round_number, 1)
        preparer.prepare(
            case,
            case.initial_scale,
            database=f"sf_performance_round_{round_number}",
        )

    assert [database for database, _manifest in materializer.calls] == [
        "sf_performance_round_1",
        "sf_performance_round_2",
    ]


def test_materialization_mismatch_keeps_database_and_exact_failure_context() -> None:
    failure = MaterializationMismatch(
        "different affected rows",
        database="sf_performance_round_1",
        sql="INSERT INTO pf_t0 VALUES (1)",
        details={"node_results": {"baseline": {"affected_rows": 1}}},
    )
    case = _base().for_case(1, 1)

    with pytest.raises(CalibrationTerminated) as captured:
        SharedRoundCasePreparer(_Materializer(failure)).prepare(
            case, case.initial_scale, database="sf_performance_round_1"
        )

    assert captured.value.kind is CalibrationFailureKind.SETUP_MISMATCH
    assert captured.value.role is NodeRole.CUSTOM_OFF
    assert captured.value.database == "sf_performance_round_1"
    assert captured.value.failing_action_sql == "INSERT INTO pf_t0 VALUES (1)"
    assert captured.value.failure_details == failure.details
