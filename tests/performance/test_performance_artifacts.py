from __future__ import annotations

import json
from pathlib import Path

from select_fuzz.config import COMPARISON_ROLES, NodeRole
from select_fuzz.performance.artifacts import (
    PerformanceDiagnosticWriter,
    PerformanceRecorder,
)
from select_fuzz.performance.calibration import (
    CalibrationFailureKind,
    CalibrationTerminated,
)
from select_fuzz.performance.models import (
    Assessment,
    CalibrationAttempt,
    FormalRun,
    FrozenCase,
    Measurement,
    Outcome,
    ScaleKnobs,
    Verdict,
)
from select_fuzz.performance.templates import CpuDenseSetupManifest
from select_fuzz.performance.tree import Family, ShapeBoundary


class _Records:
    def __init__(self) -> None:
        self.items: list[object] = []

    def append(self, record: object) -> None:
        self.items.append(record)


class _Diagnostics:
    def __init__(self) -> None:
        self.case_id = ""
        self.manifest: object = None
        self.files: dict[str, bytes] = {}
        self.case_ids: list[str] = []

    def write(self, case_id: str, manifest: object, files: object) -> None:
        self.case_ids.append(case_id)
        self.case_id = case_id
        self.manifest = manifest
        self.files = dict(files)  # type: ignore[arg-type]


def _completed_formal_run() -> FormalRun:
    return FormalRun(
        measurements={
            role: Measurement(
                role=role,
                outcome=Outcome.COMPLETED,
                started_ns=1,
                ended_ns=2,
                connection_id=10,
                root_end_ms=1.0,
                tree="-> Table scan on pf_t0 (actual time=0..1 rows=1 loops=1)",
                cache_state="unverified",
            )
            for role in COMPARISON_ROLES
        },
        start_skew_ms=0.0,
    )


def test_round_sql_contains_one_setup_and_multiple_explain_analyze_queries(
    tmp_path: Path,
) -> None:
    manifest = CpuDenseSetupManifest(
        "shared",
        7,
        1,
        (
            "CREATE TABLE pf_t0 (id BIGINT PRIMARY KEY)",
            "INSERT INTO pf_t0 VALUES (1)",
        ),
    )
    recorder = PerformanceRecorder(
        _Records(),
        run_id="run-shared-round",
        node_config_fingerprints={role: f"fp-{role.value}" for role in COMPARISON_ROLES},
        sql_root=tmp_path,
    )
    for number in (1, 2):
        frozen = FrozenCase(
            case_id=f"case_{number}",
            template_id="shared_query_v1",
            seed=number,
            database="sf_performance_shared_1",
            scale=ScaleKnobs(),
            data_manifest=manifest,
            sql=f"SELECT {number} FROM pf_t0",
            boundary=ShapeBoundary(frozenset({Family.SCAN})),
            medians_seconds={},
            attempts=(),
        )
        recorder.record(frozen, _completed_formal_run(), Assessment(Verdict.PASS))

    scripts = list((tmp_path / "rounds").glob("*.sql"))
    assert [script.name for script in scripts] == ["sf_performance_shared_1.sql"]
    sql = scripts[0].read_text(encoding="utf-8")
    assert sql.count("CREATE TABLE pf_t0") == 1
    assert sql.count("INSERT INTO pf_t0 VALUES (1)") == 1
    assert sql.count("EXPLAIN ANALYZE FORMAT=TREE SELECT") == 2
    assert "EXPLAIN ANALYZE FORMAT=TREE SELECT 1 FROM pf_t0;" in sql
    assert "EXPLAIN ANALYZE FORMAT=TREE SELECT 2 FROM pf_t0;" in sql


def test_alert_artifact_contains_every_input_needed_to_rebuild_and_diagnose() -> None:
    attempt = CalibrationAttempt(
        number=1,
        scale=ScaleKnobs(),
        sql="SELECT SUM(v) FROM cpu_data",
        samples_seconds={NodeRole.CUSTOM_OFF: (5.0, 5.1, 5.2)},
        medians_seconds={NodeRole.CUSTOM_OFF: 5.1},
    )
    frozen = FrozenCase(
        case_id="case_1",
        template_id="cpu_scan_v1",
        seed=1,
        database="perf_1",
        scale=ScaleKnobs(),
        data_manifest={
            "setup_statements": [
                "CREATE TABLE cpu_data(id BIGINT PRIMARY KEY, v BIGINT)",
                "INSERT INTO cpu_data VALUES (1, 2)",
            ],
            "expected_row_count": 1,
        },
        sql="SELECT SUM(v) FROM cpu_data",
        boundary=ShapeBoundary(frozenset({Family.SCAN})),
        medians_seconds={NodeRole.CUSTOM_OFF: 5.1},
        attempts=(attempt,),
    )
    run = FormalRun(
        measurements={
            role: Measurement(
                role=role,
                outcome=Outcome.COMPLETED,
                started_ns=1,
                ended_ns=2,
                connection_id=10,
                root_end_ms=10_000.0,
                tree="-> Table scan on cpu_data (actual time=0..10000 rows=1 loops=1)",
                cache_state="unverified",
            )
            for role in COMPARISON_ROLES
        },
        start_skew_ms=1.0,
    )
    records, diagnostics = _Records(), _Diagnostics()
    fingerprints = {role: f"fingerprint-{role.value}" for role in COMPARISON_ROLES}
    recorder = PerformanceRecorder(
        records,
        diagnostics,
        run_id="run-perf-1",
        node_config_fingerprints=fingerprints,
        now=lambda: "2026-07-13T00:00:00Z",
    )

    record = recorder.record(frozen, run, Assessment(Verdict.PERF_ALERT, ("VS_CUSTOM_OFF",)))

    assert record["template_id"] == "cpu_scan_v1"
    assert record["run_id"] == "run-perf-1"
    assert record["occurred_at"] == "2026-07-13T00:00:00Z"
    assert record["data_manifest"] == frozen.data_manifest
    assert record["node_config_fingerprints"] == {
        role.value: value for role, value in fingerprints.items()
    }
    assert record["calibration"][0]["samples_seconds"] == {  # type: ignore[index]
        "custom_off": [5.0, 5.1, 5.2],
    }
    assert set(diagnostics.files) == {
        "plans/custom_off.tree",
        "plans/custom_on.tree",
        "diagnostics/metrics.json",
        "calibration.json",
        "setup/manifest.json",
    }


def test_calibration_failure_record_keeps_classification_and_reproduction(
    tmp_path: Path,
) -> None:
    records, diagnostics = _Records(), _Diagnostics()
    recorder = PerformanceRecorder(
        records,
        diagnostics,
        run_id="run-perf-2",
        node_config_fingerprints={role: f"fp-{role.value}" for role in COMPARISON_ROLES},
        sql_root=tmp_path,
        now=lambda: "2026-07-13T00:00:01Z",
    )
    failure = CalibrationTerminated(
        CalibrationFailureKind.PARSE,
        NodeRole.CUSTOM_OFF,
        error_type="PlanParseError",
        scale=ScaleKnobs(table_rows=123),
        sql="SELECT SUM(v) FROM cpu_data",
        data_manifest=CpuDenseSetupManifest(
            "test",
            1,
            1,
            (
                "CREATE TABLE cpu_data(id BIGINT)",
                "INSERT INTO cpu_data VALUES(1)",
            ),
        ),
        database="sf_performance_real_failure_1",
        failing_action_sql="INSERT INTO cpu_data VALUES(1)",
        failure_details={
            "node_results": {
                "custom_off": {"status": "success", "affected_rows": 1},
                "custom_on": {"status": "error", "affected_rows": None},
            }
        },
    )

    recorder.record_calibration_failure(
        type("Template", (), {"case_id": "bad_1", "template_id": "cpu_v1", "seed": 9})(),
        (),
        failure,
        attempt_number=2,
    )

    record = records.items[0]
    assert record["failure_category"] == "parse"  # type: ignore[index]
    assert record["error_type"] == "PlanParseError"  # type: ignore[index]
    assert record["template_id"] == "cpu_v1"  # type: ignore[index]
    assert record["scale"]["table_rows"] == 123  # type: ignore[index]
    assert record["sql"] == "SELECT SUM(v) FROM cpu_data"  # type: ignore[index]
    assert record["run_id"] == "run-perf-2"  # type: ignore[index]
    assert record["occurred_at"] == "2026-07-13T00:00:01Z"  # type: ignore[index]
    assert record["diagnostic_attempt"] == 2  # type: ignore[index]
    assert record["database"] == "sf_performance_real_failure_1"  # type: ignore[index]
    assert record["failing_action_sql"] == "INSERT INTO cpu_data VALUES(1)"  # type: ignore[index]
    assert "replica_parameters_sha256" not in record  # type: ignore[operator]
    script = tmp_path / "performance_failures" / "bad_1" / "case.sql"
    assert "sf_performance_real_failure_1" in script.read_text(encoding="utf-8")
    assert diagnostics.case_ids == ["bad_1_attempt_2"]
    assert diagnostics.files.keys() >= {"setup/manifest.json", "calibration.json"}


def test_diagnostic_writer_atomically_publishes_manifest_and_files(
    tmp_path: Path,
) -> None:
    writer = PerformanceDiagnosticWriter(tmp_path)

    published = writer.write(
        "perf_case_1",
        {"case_id": "perf_case_1", "type": "performance_alert"},
        {
            "plans/custom_off.tree": b"-> Table scan",
            "setup/manifest.json": b'{"rows":100}',
        },
    )

    assert published == tmp_path / "performance_findings" / "perf_case_1"
    assert json.loads((published / "manifest.json").read_text())["case_id"] == "perf_case_1"
    assert (published / "plans" / "custom_off.tree").read_bytes() == b"-> Table scan"
    assert not list((tmp_path / "performance_findings").glob(".*.tmp-*"))


def test_diagnostic_writer_is_idempotent_for_identical_attempt(tmp_path: Path) -> None:
    writer = PerformanceDiagnosticWriter(tmp_path)
    manifest = {"case_id": "retry_attempt_1", "type": "performance_calibration_failure"}
    files = {"calibration.json": b"[]"}

    first = writer.write("retry_attempt_1", manifest, files)
    second = writer.write("retry_attempt_1", manifest, files)

    assert first == second


def test_exhausted_calibration_without_failure_object_still_gets_diagnostics() -> None:
    records, diagnostics = _Records(), _Diagnostics()
    recorder = PerformanceRecorder(
        records,
        diagnostics,
        run_id="run-exhausted",
        node_config_fingerprints={role: f"fp-{role.value}" for role in COMPARISON_ROLES},
        now=lambda: "2026-07-13T00:00:02Z",
    )
    attempt = CalibrationAttempt(
        number=1,
        scale=ScaleKnobs(),
        sql="SELECT 1 ORDER BY 1",
        samples_seconds={NodeRole.CUSTOM_OFF: (1.0, 1.0, 1.0)},
        medians_seconds={NodeRole.CUSTOM_OFF: 1.0},
    )
    template = type(
        "Template", (), {"case_id": "exhausted_1", "template_id": "cpu_v1", "seed": 8}
    )()

    recorder.record_calibration_failure(template, (attempt,), attempt_number=1)

    assert diagnostics.case_ids == ["exhausted_1_attempt_1"]
    assert "calibration.json" in diagnostics.files
