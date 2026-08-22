from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from select_fuzz.artifacts.bundle import (
    CaseBundleWriter,
    FindingRecord,
    PassRecord,
    _canonical_json,
    _encode_artifact_value,
    artifact_cell_to_value,
    node_execution_to_artifact,
)
from select_fuzz.config import COMPARISON_ROLES, NodeRole


DIGEST = "a" * 64
DATABASE = "sf_c_20260713t120000_w0_r1_sabc_n123_q0"
ROLES = {role: DATABASE for role in COMPARISON_ROLES}
RESULTS = {
    role: {"role": role.value, "status": "success"}
    for role in COMPARISON_ROLES
}


def pass_record(**overrides: object) -> PassRecord:
    values: dict[str, object] = {
        "case_id": "case_1",
        "run_id": "run_1",
        "database": DATABASE,
        "seed": 1,
        "query_sql": "SELECT 1 ORDER BY 1",
        "row_count": 1,
        "result_digest": DIGEST,
        "column_metadata_digest": DIGEST,
        "elapsed_ns_by_role": {role: 1 for role in COMPARISON_ROLES},
        "coverage_tags": ("select",),
    }
    values.update(overrides)
    return PassRecord(**values)


def finding_record(**overrides: object) -> FindingRecord:
    values: dict[str, object] = {
        "case_id": "case_1",
        "run_id": "run_1",
        "mode": "correctness",
        "databases": ROLES,
        "seeds": {"query": 1},
        "setup_sql": ("CREATE TABLE `t` (`id` INT);",),
        "query_sql": "SELECT 1 ORDER BY 1",
        "query_limits": {"timeout_seconds": 1, "row_limit": 1, "byte_limit": 1},
        "payload_sha256": DIGEST,
        "original_verdict": "mismatch",
        "first_difference": {},
        "statistics": {},
        "configuration_fingerprints": {
            role: "fingerprint" for role in COMPARISON_ROLES
        },
        "results": RESULTS,
    }
    values.update(overrides)
    return FindingRecord(**values)


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        "text",
        1,
        1.25,
        Decimal("1.20"),
        b"bytes",
        datetime(2026, 7, 13, 1, 2, 3),
        date(2026, 7, 13),
        time(1, 2, 3),
        timedelta(days=-1, seconds=1),
        {"key": [1, None]},
        (1, 2),
        {"a", "b"},
    ],
)
def test_artifact_cell_codec_round_trips_supported_types(value: object) -> None:
    encoded = _encode_artifact_value(value)
    decoded = artifact_cell_to_value(encoded)
    if isinstance(value, dict):
        assert dict(decoded) == {"key": (1, None)}
    elif isinstance(value, list):
        assert decoded == tuple(value)
    else:
        assert decoded == value


@pytest.mark.parametrize(
    ("operation", "error"),
    [
        (lambda: _encode_artifact_value(float("nan")), ValueError),
        (lambda: _encode_artifact_value({1: "bad"}), TypeError),
        (lambda: _encode_artifact_value(object()), TypeError),
        (lambda: artifact_cell_to_value(1), ValueError),
        (lambda: artifact_cell_to_value({"$type": "sequence", "items": {}}), ValueError),
        (lambda: artifact_cell_to_value({"$type": "mapping", "items": {}}), ValueError),
        (
            lambda: artifact_cell_to_value({"$type": "mapping", "items": [[1, "value"]]}),
            ValueError,
        ),
        (lambda: artifact_cell_to_value({"$type": "unknown"}), ValueError),
        (lambda: node_execution_to_artifact("execution"), TypeError),
        (lambda: _canonical_json(object()), TypeError),
        (lambda: _canonical_json("too large", max_bytes=1), ValueError),
    ],
)
def test_artifact_codec_rejects_lossy_or_malformed_values(
    operation: object, error: type[Exception]
) -> None:
    assert callable(operation)
    with pytest.raises(error):
        operation()


@pytest.mark.parametrize(
    "overrides",
    [
        {"seed": True},
        {"query_sql": ""},
        {"row_count": True},
        {"row_count": -1},
        {"result_digest": "bad"},
        {"elapsed_ns_by_role": {NodeRole.BASELINE: 1}},
        {"elapsed_ns_by_role": {role: -1 for role in COMPARISON_ROLES}},
        {"coverage_tags": ("",)},
    ],
)
def test_pass_records_reject_invalid_compact_event_values(overrides: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        pass_record(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"mode": "other"},
        {"databases": {NodeRole.BASELINE: DATABASE}},
        {"seeds": {}},
        {"seeds": {"query": True}},
        {"setup_sql": ()},
        {"setup_sql": ("",)},
        {"query_sql": ""},
        {"query_limits": []},
        {"query_limits": {"timeout_seconds": 1}},
        {
            "query_limits": {
                "timeout_seconds": 1,
                "row_limit": True,
                "byte_limit": 1,
            }
        },
        {
            "query_limits": {
                "timeout_seconds": 1,
                "row_limit": 1,
                "byte_limit": 1,
                "extra": 1,
            }
        },
        {"original_verdict": ""},
        {"first_difference": []},
        {"configuration_fingerprints": {role: "" for role in COMPARISON_ROLES}},
        {"results": {role: [] for role in COMPARISON_ROLES}},
        {
            "results": {
                role: {"role": role.value, "status": "unknown"}
                for role in COMPARISON_ROLES
            }
        },
        {"requires_same_session": 1},
    ],
)
def test_finding_records_reject_incomplete_or_inconsistent_replay_data(
    overrides: dict[str, object],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        finding_record(**overrides)


@pytest.mark.parametrize(
    ("original_verdict", "first_difference"),
    [
        (
            "expected_error_mismatch",
            {
                "category": "generator_contract",
                "expected_error": None,
                "observed_identities": [
                    {"errno": 1064, "sqlstate": "42000"},
                    {"errno": 1064, "sqlstate": "42000"},
                ],
            },
        ),
        (
            "expected_error_mismatch",
            {
                "category": "generator_contract",
                "expected_error": None,
                "observed_identities": [
                    {"errno": True, "sqlstate": "42000"},
                    None,
                ],
                "reason": "invalid observed errno",
            },
        ),
        (
            "expected_error_mismatch",
            {
                "category": "generator_contract",
                "expected_error": {
                    "errno": 1054,
                    "kind": "unknown_column",
                    "sqlstate": "invalid",
                },
                "observed_identities": [None, None],
                "reason": "invalid expected SQLSTATE",
            },
        ),
        (
            "expected_error_mismatch",
            {
                "category": "generator_contract",
                "expected_error": {"errno": 1054, "sqlstate": "42S22"},
                "observed_identities": [None, None],
                "reason": "missing expected kind",
            },
        ),
        (
            "expected_error_mismatch",
            {
                "category": "generator_contract",
                "expected_error": {
                    "errno": 1054,
                    "kind": "not_a_kind",
                    "sqlstate": "42S22",
                },
                "observed_identities": [None, None],
                "reason": "invalid expected kind",
            },
        ),
        (
            "expected_error_mismatch",
            {
                "category": "generator_contract",
                "expected_error": None,
                "observed_identities": [None, None],
                "reason": "missing expected contract",
            },
        ),
        (
            "unexpected_valid_error",
            {
                "category": "generator_contract",
                "expected_error": {
                    "errno": 1054,
                    "kind": "unknown_column",
                    "sqlstate": "42S22",
                },
                "observed_identities": [None, None],
                "reason": "valid query cannot expect an error",
            },
        ),
        (
            "result_mismatch",
            {
                "category": "generator_contract",
                "expected_error": None,
                "observed_identities": [None, None],
                "reason": "generator details require a generator verdict",
            },
        ),
        ("unexpected_valid_error", {}),
    ],
)
def test_generator_contract_details_are_validated_before_event_projection(
    original_verdict: str,
    first_difference: dict[str, object],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        finding_record(
            original_verdict=original_verdict,
            first_difference=first_difference,
        )


@pytest.mark.parametrize(
    ("original_verdict", "expected_error"),
    [
        ("unexpected_valid_error", None),
        (
            "expected_error_mismatch",
            {
                "errno": 1054,
                "kind": "unknown_column",
                "sqlstate": "42S22",
            },
        ),
    ],
)
def test_generator_contract_verdicts_accept_replay_readable_details(
    original_verdict: str,
    expected_error: object,
) -> None:
    record = finding_record(
        original_verdict=original_verdict,
        first_difference={
            "category": "generator_contract",
            "expected_error": expected_error,
            "observed_identities": [None, None],
            "reason": "closed generator contract",
        },
    )

    assert record.original_verdict == original_verdict


def test_bundle_writer_requires_typed_records(tmp_path: Path) -> None:
    writer = CaseBundleWriter(tmp_path)
    with pytest.raises(TypeError, match="PassRecord"):
        writer.write_pass("pass")
    with pytest.raises(TypeError, match="FindingRecord"):
        writer.write_finding("finding")
    assert pass_record().to_event()["type"] == "pass"
    assert finding_record().manifest()["schema_version"] == 2
