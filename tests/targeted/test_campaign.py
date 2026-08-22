from __future__ import annotations

from select_fuzz.targeted_campaign import (
    _comparison,
    classify_connection_event,
    compare_result_payloads,
    make_database_name,
)


def test_non_timeout_lost_connection_is_a_crash_candidate() -> None:
    assert classify_connection_event(
        status="infra_error",
        errno=2013,
        watchdog_fired=False,
    ) == "crash_candidate"


def test_watchdog_lost_connection_is_not_a_crash_candidate() -> None:
    assert classify_connection_event(
        status="timeout",
        errno=2013,
        watchdog_fired=True,
    ) == "timeout_connection"


def test_connection_open_lost_connection_is_infrastructure() -> None:
    assert classify_connection_event(
        status="error",
        errno=2013,
        watchdog_fired=False,
        stage="connection_open",
    ) == "connection_lost_infra"


def test_other_errors_are_preserved_as_database_errors() -> None:
    assert classify_connection_event(
        status="error",
        errno=1054,
        watchdog_fired=False,
    ) == "database_error"


def test_database_names_are_unique_and_safe() -> None:
    first = make_database_name("worker 1", 7, 123)
    second = make_database_name("worker 1", 7, 124)
    assert first != second
    assert first.startswith("sf_t_")
    assert len(first) <= 64
    assert all(char.isalnum() or char == "_" for char in first)


def test_result_comparison_reports_rows_and_metadata_separately() -> None:
    assert compare_result_payloads(
        {"status": "success", "columns": [{"type_code": 3}], "rows": [[1]]},
        {"status": "success", "columns": [{"type_code": 3}], "rows": [[2]]},
    ) == (False, "rows")
    assert compare_result_payloads(
        {"status": "success", "columns": [{"type_code": 3}], "rows": [[1]]},
        {"status": "success", "columns": [{"type_code": 8}], "rows": [[1]]},
    ) == (False, "metadata")


def test_feature_only_probe_still_surfaces_a_one_sided_crash() -> None:
    crashing = {
        "status": "success",
        "queries": [
            {
                "status": "error",
                "error": {"classification": "crash_candidate"},
            }
        ],
    }
    healthy = {"status": "success", "queries": [{"status": "success"}]}
    result = _comparison("flashback", healthy, crashing)
    assert result["category"] == "crash_candidate"
    assert result["crash_roles"] == ["custom_on"]


def test_connection_open_loss_is_an_infrastructure_pause_not_a_finding() -> None:
    off = {
        "status": "setup_error",
        "setup_error": {
            "classification": "connection_lost_infra",
            "errno": 2013,
        },
        "queries": [],
    }
    on = {"status": "success", "setup_error": None, "queries": []}
    assert _comparison("pq_parallel", off, on) == {
        "matched": True,
        "category": "infrastructure_pause",
        "infrastructure_roles": ["custom_off"],
    }


def test_optimizer_switch_capability_gap_is_not_a_finding() -> None:
    off = {
        "setup_error": {"errno": 1193},
        "queries": [],
    }
    on = {"setup_error": None, "queries": [{"status": "success"}]}
    assert _comparison("optimizer_switch", off, on) == {
        "matched": True,
        "category": "capability_probe",
    }


def test_pq_probe_without_crash_is_a_capability_observation() -> None:
    off = {"setup_error": {"errno": 1193}, "queries": []}
    on = {"setup_error": {"errno": 1227}, "queries": []}
    assert _comparison("pq_parallel", off, on) == {
        "matched": True,
        "category": "capability_probe",
    }


def test_feature_only_remote_database_error_is_not_hidden_as_capability() -> None:
    off = {"setup_error": {"errno": 1064}, "queries": []}
    on = {
        "setup_error": None,
        "queries": [
            {
                "status": "error",
                "error": {"errno": 7625, "classification": "database_error"},
                "columns": [],
                "rows": [],
            }
        ],
    }
    assert _comparison("flashback", off, on) == {
        "matched": False,
        "category": "error",
    }


def test_feature_only_case_compares_results_when_both_nodes_support_it() -> None:
    off = {
        "setup_error": None,
        "queries": [{"status": "success", "columns": [], "rows": [[1]]}],
    }
    on = {
        "setup_error": None,
        "queries": [{"status": "success", "columns": [], "rows": [[2]]}],
    }
    assert _comparison("second_level_partition", off, on) == {
        "matched": False,
        "category": "rows",
    }
