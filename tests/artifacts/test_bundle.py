from __future__ import annotations

import gzip
import json
import os
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from select_fuzz.artifacts import CaseBundleWriter as ExportedCaseBundleWriter
from select_fuzz.artifacts.bundle import (
    CaseBundleWriter,
    FindingRecord,
    PassRecord,
    artifact_cell_to_value,
    node_execution_to_artifact,
)
from select_fuzz.artifacts.jsonl import JsonlWriter, read_jsonl
from select_fuzz.artifacts.reader import ArtifactReader, ArtifactValidationError
from select_fuzz.artifacts.report import HtmlReportBuilder
from select_fuzz.config import COMPARISON_ROLES, NodeRole
from select_fuzz.domain import ColumnMeta, NodeExecution


def _pass(case_id: str = "case_pass_1") -> PassRecord:
    return PassRecord(
        case_id=case_id,
        run_id="run_1",
        database="sf_c_20260713t120000_w0_r1_sabc_n123_q0",
        seed=7,
        query_sql="SELECT COUNT(*) FROM `t0` ORDER BY 1",
        row_count=1,
        result_digest="a" * 64,
        column_metadata_digest="b" * 64,
        elapsed_ns_by_role={role: 1_000_000 for role in COMPARISON_ROLES},
        coverage_tags=("join.inner", "aggregate.count"),
    )


def _finding(case_id: str = "case_finding_1") -> FindingRecord:
    return FindingRecord(
        case_id=case_id,
        run_id="run_1",
        mode="correctness",
        databases={
            role: "sf_c_20260713t120000_w0_r1_sabc_n123_q0"
            for role in COMPARISON_ROLES
        },
        seeds={"round": 7, "schema": 11, "query": 13},
        setup_sql=(
            "CREATE TABLE `t0` (`id` BIGINT PRIMARY KEY);",
            "INSERT INTO `t0` VALUES (1),(2);",
        ),
        query_sql="SELECT `id` FROM `t0` ORDER BY 1",
        query_limits={"timeout_seconds": 15, "row_limit": 10_000, "byte_limit": 32 << 20},
        payload_sha256="c" * 64,
        original_verdict="result_mismatch",
        first_difference={"category": "rows", "pair": "custom_off/custom_on"},
        statistics={"custom_off_rows": 2, "custom_on_rows": 1},
        configuration_fingerprints={
            role: f"fingerprint-{role.value}" for role in COMPARISON_ROLES
        },
        results={
            role: {
                "role": role.value,
                "status": "success",
                "columns": [{"name": "id", "type_code": 8}],
                "rows": [[1], [2]] if role is not NodeRole.CUSTOM_ON else [[1]],
                "elapsed_ns": 1_000_000,
            }
            for role in COMPARISON_ROLES
        },
        requires_same_session=False,
    )


def test_pass_is_only_a_compact_fsynced_event(tmp_path: Path) -> None:
    writer = CaseBundleWriter(tmp_path)

    writer.write_pass(_pass())

    records = read_jsonl(tmp_path / "events.jsonl")
    assert len(records) == 1
    assert records[0]["type"] == "pass"
    assert records[0]["row_count"] == 1
    assert not list(tmp_path.rglob("*.result.json.gz"))
    assert not (tmp_path / "passes").exists()


def test_finding_atomically_publishes_manifest_and_both_full_results(
    tmp_path: Path,
) -> None:
    writer = CaseBundleWriter(tmp_path)
    finding = _finding()

    published = writer.write_finding(finding)

    assert published == tmp_path / "findings" / finding.case_id
    assert not list((tmp_path / "findings").glob(".*.tmp-*"))
    manifest = json.loads((published / "manifest.json").read_text())
    assert manifest["case_id"] == finding.case_id
    assert manifest["schema_version"] == 2
    assert "setup_sql" not in manifest
    assert "setup_sql" not in manifest["replay"]
    assert manifest["replay"]["setup_sql_ref"]["path"] == "setup.sql.jsonl.gz"
    assert manifest["replay"]["query_sql_ref"]["path"] == "query.sql.jsonl.gz"
    assert manifest["replay"]["query_limits"] == dict(finding.query_limits)
    assert set(manifest["result_files"]) == {role.value for role in COMPARISON_ROLES}
    result_files = sorted(published.glob("*.result.json.gz"))
    assert len(result_files) == 2
    assert all(path.read_bytes()[4:8] == b"\x00\x00\x00\x00" for path in result_files)
    decoded = {
        path.name.split(".", 1)[0]: json.loads(gzip.decompress(path.read_bytes()))
        for path in result_files
    }
    assert decoded["custom_off"]["rows"] == [[1], [2]]
    assert decoded["custom_on"]["rows"] == [[1]]
    stored = ArtifactReader(tmp_path).get_finding(finding.case_id)
    assert stored.setup_sql == finding.setup_sql
    assert stored.query_sql == finding.query_sql
    records = read_jsonl(tmp_path / "events.jsonl")
    assert records[-1]["type"] == "finding"
    assert records[-1]["case_id"] == finding.case_id
    assert records[-1]["classification"] == "result_mismatch"
    assert "errno" not in records[-1]
    assert "sqlstate" not in records[-1]
    assert "reason" not in records[-1]


def test_finding_setup_larger_than_old_64_mib_manifest_limit_round_trips(
    tmp_path: Path,
) -> None:
    huge_statement = "INSERT INTO `t0` VALUES ('" + ("x" * (65 << 20)) + "')"
    finding = replace(
        _finding("case_large_setup_1"),
        setup_sql=("CREATE TABLE `t0` (`v` LONGTEXT)", huge_statement),
    )

    published = CaseBundleWriter(tmp_path).write_finding(finding)

    assert (published / "manifest.json").stat().st_size < 1_000_000
    assert (published / "case.sql").stat().st_size < 1024
    stored = ArtifactReader(tmp_path).get_finding(finding.case_id)
    assert stored.setup_sql == finding.setup_sql


def test_legacy_v1_inline_replay_sql_remains_readable(tmp_path: Path) -> None:
    finding = _finding("case_legacy_v1_1")
    published = CaseBundleWriter(tmp_path).write_finding(finding)
    manifest_path = published / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 1
    manifest["replay"].pop("setup_sql_ref")
    manifest["replay"].pop("query_sql_ref")
    manifest["replay"]["setup_sql"] = list(finding.setup_sql)
    manifest["replay"]["query_sql"] = finding.query_sql
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    stored = ArtifactReader(tmp_path).get_finding(finding.case_id)

    assert stored.setup_sql == finding.setup_sql
    assert stored.query_sql == finding.query_sql


def test_generator_contract_event_prefers_observed_error_identity(
    tmp_path: Path,
) -> None:
    finding = replace(
        _finding(),
        original_verdict="expected_error_mismatch",
        first_difference={
            "category": "generator_contract",
            "expected_error": {
                "errno": 1054,
                "kind": "unknown_column",
                "sqlstate": "42S22",
            },
            "observed_identities": [
                {"errno": 1064, "sqlstate": "42000"},
                {"errno": 1064, "sqlstate": "42000"},
            ],
            "reason": "expected identity did not match",
        },
    )

    published = CaseBundleWriter(tmp_path).write_finding(finding)

    event = read_jsonl(tmp_path / "events.jsonl")[-1]
    assert event["classification"] == "expected_error_mismatch"
    assert event["reason"] == "expected identity did not match"
    assert event["errno"] == 1064
    assert event["sqlstate"] == "42000"
    manifest = json.loads((published / "manifest.json").read_text())
    assert "classification" not in manifest
    assert "errno" not in manifest
    assert "sqlstate" not in manifest


def test_generator_contract_event_falls_back_to_expected_error_identity(
    tmp_path: Path,
) -> None:
    finding = replace(
        _finding(),
        original_verdict="expected_error_mismatch",
        first_difference={
            "category": "generator_contract",
            "expected_error": {
                "errno": 1054,
                "kind": "unknown_column",
                "sqlstate": "42S22",
            },
            "observed_identities": [None, None],
            "reason": "expected an error but all nodes succeeded",
        },
    )

    CaseBundleWriter(tmp_path).write_finding(finding)

    event = read_jsonl(tmp_path / "events.jsonl")[-1]
    assert event["errno"] == 1054
    assert event["sqlstate"] == "42S22"


def test_bundle_failure_before_replace_leaves_no_final_or_temp_directory(
    tmp_path: Path,
) -> None:
    calls = 0

    def fail_second_file_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated ENOSPC")
        os.fsync(fd)

    writer = CaseBundleWriter(tmp_path, fsync=fail_second_file_fsync)

    with pytest.raises(OSError, match="ENOSPC"):
        writer.write_finding(_finding())

    assert not (tmp_path / "findings" / "case_finding_1").exists()
    assert not list((tmp_path / "findings").glob(".*.tmp-*"))
    assert read_jsonl(tmp_path / "events.jsonl") == []


def test_duplicate_finding_never_overwrites_first_bundle(tmp_path: Path) -> None:
    writer = CaseBundleWriter(tmp_path)
    writer.write_finding(_finding())
    original = (tmp_path / "findings" / "case_finding_1" / "manifest.json").read_bytes()

    with pytest.raises(FileExistsError):
        writer.write_finding(_finding())

    assert (
        tmp_path / "findings" / "case_finding_1" / "manifest.json"
    ).read_bytes() == original
    assert len(read_jsonl(tmp_path / "events.jsonl")) == 1


def test_sensitive_keys_are_rejected_before_any_artifact_is_written(
    tmp_path: Path,
) -> None:
    finding = _finding()
    unsafe_results = dict(finding.results)
    unsafe_results[NodeRole.CUSTOM_OFF] = {
        "role": NodeRole.CUSTOM_OFF.value,
        "status": "success",
        "password": "must-never-land",
    }
    unsafe = FindingRecord(
        case_id=finding.case_id,
        run_id=finding.run_id,
        mode=finding.mode,
        databases=finding.databases,
        seeds=finding.seeds,
        setup_sql=finding.setup_sql,
        query_sql=finding.query_sql,
        query_limits=finding.query_limits,
        payload_sha256=finding.payload_sha256,
        original_verdict=finding.original_verdict,
        first_difference=finding.first_difference,
        statistics=finding.statistics,
        configuration_fingerprints=finding.configuration_fingerprints,
        results=unsafe_results,
        requires_same_session=finding.requires_same_session,
    )

    with pytest.raises(ValueError, match="sensitive"):
        CaseBundleWriter(tmp_path).write_finding(unsafe)

    assert not tmp_path.exists() or not list(tmp_path.rglob("*"))


def test_finding_rejects_result_payload_whose_role_does_not_match_file_role() -> None:
    finding = _finding()
    results = dict(finding.results)
    results[NodeRole.CUSTOM_OFF] = {
        "role": NodeRole.CUSTOM_ON.value,
        "status": "success",
    }

    with pytest.raises(ValueError, match="result role"):
        FindingRecord(
            case_id="case_bad_role_1",
            run_id=finding.run_id,
            mode=finding.mode,
            databases=finding.databases,
            seeds=finding.seeds,
            setup_sql=finding.setup_sql,
            query_sql=finding.query_sql,
            query_limits=finding.query_limits,
            payload_sha256=finding.payload_sha256,
            original_verdict=finding.original_verdict,
            first_difference=finding.first_difference,
            statistics=finding.statistics,
            configuration_fingerprints=finding.configuration_fingerprints,
            results=results,
        )


@pytest.mark.parametrize("case_id", ("../escape", "case/escape", ".hidden", ""))
def test_case_id_cannot_escape_or_hide_bundle_directory(case_id: str) -> None:
    with pytest.raises(ValueError, match="case_id"):
        _finding(case_id)


def test_reader_loads_finding_by_case_id_and_manifest_path(tmp_path: Path) -> None:
    published = CaseBundleWriter(tmp_path).write_finding(_finding())
    reader = ArtifactReader(tmp_path)

    by_id = reader.get_finding("case_finding_1")
    by_path = reader.get_finding(published / "manifest.json")

    assert by_id.manifest == by_path.manifest
    assert by_id.results == by_path.results
    assert by_id.path == published
    assert by_id.results[NodeRole.CUSTOM_ON]["rows"] == [[1]]


def test_reader_accepts_artifact_root_relative_manifest_path(tmp_path: Path) -> None:
    CaseBundleWriter(tmp_path).write_finding(_finding())

    stored = ArtifactReader(tmp_path).get_finding(
        Path("findings/case_finding_1/manifest.json")
    )

    assert stored.case_id == "case_finding_1"


def test_reader_rejects_result_path_traversal_in_manifest(tmp_path: Path) -> None:
    published = CaseBundleWriter(tmp_path).write_finding(_finding())
    manifest_path = published / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["result_files"]["custom_off"] = "../outside.result.json.gz"
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ArtifactValidationError, match="result_files"):
        ArtifactReader(tmp_path).get_finding("case_finding_1")


def test_reader_enforces_decompressed_result_limit(tmp_path: Path) -> None:
    published = CaseBundleWriter(tmp_path).write_finding(_finding())
    custom_off = published / "custom_off.result.json.gz"
    custom_off.write_bytes(gzip.compress(b'{"padding":"' + b"a" * 1024 + b'"}'))

    with pytest.raises(ArtifactValidationError, match="decompressed"):
        ArtifactReader(tmp_path, max_result_bytes=100).get_finding("case_finding_1")


def test_html_report_is_atomic_rebuildable_and_escapes_event_values(
    tmp_path: Path,
) -> None:
    writer = CaseBundleWriter(tmp_path)
    unsafe_query = "SELECT '<script>alert(1)</script>' ORDER BY 1"
    record = _pass()
    writer.write_pass(
        PassRecord(
            case_id=record.case_id,
            run_id=record.run_id,
            database=record.database,
            seed=record.seed,
            query_sql=unsafe_query,
            row_count=record.row_count,
            result_digest=record.result_digest,
            column_metadata_digest=record.column_metadata_digest,
            elapsed_ns_by_role=record.elapsed_ns_by_role,
            coverage_tags=record.coverage_tags,
        )
    )
    writer.write_finding(_finding())
    report_path = tmp_path / "reports" / "latest.html"

    written = HtmlReportBuilder(ArtifactReader(tmp_path)).write(report_path)
    html = written.read_text()

    assert written == report_path
    assert "Content-Security-Policy" in html
    assert "2 total cases" in html
    assert "1 findings" in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert not list(report_path.parent.glob(".*.tmp-*"))


def test_html_case_total_ignores_non_case_lifecycle_events(tmp_path: Path) -> None:
    JsonlWriter(tmp_path / "events.jsonl").append(
        {"type": "run_started", "run_id": "run_1"}
    )
    CaseBundleWriter(tmp_path).write_pass(_pass())

    html = HtmlReportBuilder(ArtifactReader(tmp_path)).render()

    assert "1 total cases" in html


def test_html_report_counts_performance_results_and_alerts(tmp_path: Path) -> None:
    writer = JsonlWriter(tmp_path / "events.jsonl")
    writer.append({"type": "performance_result", "case_id": "perf-pass"})
    writer.append({"type": "performance_alert", "case_id": "perf-alert"})
    writer.append(
        {
            "type": "performance_calibration_failure",
            "case_id": "perf-calibration",
            "failure_category": "setup_mismatch",
        }
    )

    html = HtmlReportBuilder(ArtifactReader(tmp_path)).render()

    assert "3 total cases" in html
    assert "2 findings" in html


def _typed_execution(role: NodeRole) -> NodeExecution:
    row = (
        2**64 - 1,
        b"\x00\xff",
        Decimal("12345678901234567890.123456789012345678901234567890"),
        date(2026, 7, 13),
        datetime(2026, 7, 13, 12, 34, 56, 123456),
        time(23, 59, 59, 999999),
        timedelta(days=-1, seconds=1, microseconds=2),
        -0.0,
        {"nested": [1, None, "x"]},
    )
    columns = tuple(
        ColumnMeta(f"c{index}", 253, True, False, index == 1)
        for index in range(len(row))
    )
    return NodeExecution.success(
        role=role,
        connection_id=100 + list(COMPARISON_ROLES).index(role),
        started_ns=10,
        ended_ns=20,
        columns=columns,
        rows=(row,),
        warnings=("diagnostic",),
    )


def test_node_execution_artifact_preserves_every_supported_wire_value_type(
    tmp_path: Path,
) -> None:
    encoded_by_role = {
        role: node_execution_to_artifact(_typed_execution(role))
        for role in COMPARISON_ROLES
    }
    finding = _finding()
    typed = FindingRecord(
        case_id="case_typed_1",
        run_id=finding.run_id,
        mode=finding.mode,
        databases=finding.databases,
        seeds=finding.seeds,
        setup_sql=finding.setup_sql,
        query_sql=finding.query_sql,
        query_limits=finding.query_limits,
        payload_sha256=finding.payload_sha256,
        original_verdict=finding.original_verdict,
        first_difference=finding.first_difference,
        statistics=finding.statistics,
        configuration_fingerprints=finding.configuration_fingerprints,
        results=encoded_by_role,
    )

    published = CaseBundleWriter(tmp_path).write_finding(typed)
    stored = ArtifactReader(tmp_path).get_finding(published / "manifest.json")
    encoded_row = stored.results[NodeRole.CUSTOM_OFF]["rows"][0]  # type: ignore[index]
    decoded = tuple(artifact_cell_to_value(cell) for cell in encoded_row)

    assert decoded == _typed_execution(NodeRole.CUSTOM_OFF).rows[0]
    assert stored.results[NodeRole.CUSTOM_OFF]["warnings"] == ["diagnostic"]


def test_artifact_package_exports_primary_public_contracts() -> None:
    assert ExportedCaseBundleWriter is CaseBundleWriter
