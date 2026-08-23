from __future__ import annotations

from select_fuzz.targeted_campaign import (
    CampaignNode,
    _comparison,
    _kill_server_connection,
    build_target_cases,
    classify_connection_event,
    compare_result_payloads,
    make_database_name,
)


def test_target_cases_cover_every_ordered_second_level_partition_pair() -> None:
    partition_cases = [
        case for case in build_target_cases() if case.feature == "second_level_partition"
    ]

    assert len(partition_cases) == 16
    assert len({case.setup[0] for case in partition_cases}) == 16
    for case in partition_cases:
        assert "PARTITION BY" in case.setup[0]
        assert "SUBPARTITION BY" in case.setup[0]
        parent_clause = case.setup[0].split(" SUBPARTITION BY", 1)[0]
        if "PARTITION BY HASH" in parent_clause or "PARTITION BY KEY" in parent_clause:
            assert "PARTITION p0" not in case.setup[0]
        if "PARTITION BY LIST" in parent_clause:
            assert "11,0,30" not in case.setup[1]


class _ControlConnection:
    connection_id = 9001

    def __init__(self) -> None:
        self.sql: list[str] = []
        self.closed = False

    def cursor(self):  # type: ignore[no-untyped-def]
        connection = self

        class Cursor:
            with_rows = False

            def execute(self, sql: str) -> None:
                connection.sql.append(sql)

            def fetchall(self) -> list[object]:
                return []

            def close(self) -> None:
                return None

        return Cursor()

    def close(self) -> None:
        self.closed = True


def test_kill_server_connection_uses_separate_control_session(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    control = _ControlConnection()
    node = CampaignNode("custom_on", "127.0.0.1", 3306, "USER", "PASSWORD")
    monkeypatch.setattr(
        "select_fuzz.targeted_campaign._connect",
        lambda _node, _database: control,
    )

    result = _kill_server_connection(node, 41)

    assert result == {"attempted": True, "succeeded": True}
    assert control.sql == ["KILL CONNECTION 41"]
    assert control.closed is True


def test_non_timeout_lost_connection_is_infrastructure_not_a_finding() -> None:
    assert classify_connection_event(
        status="infra_error",
        errno=2013,
        watchdog_fired=False,
    ) == "connection_lost_infra"


def test_explicit_crash_candidate_is_preserved_even_with_other_side_infrastructure() -> None:
    crashing = {
        "status": "success",
        "queries": [
            {
                "status": "error",
                "error": {"classification": "crash_candidate"},
            }
        ],
    }
    disconnected = {
        "status": "success",
        "queries": [
            {
                "status": "error",
                "error": {
                    "classification": "connection_lost_infra",
                    "errno": 2013,
                },
            }
        ],
    }
    result = _comparison("flashback", disconnected, crashing)
    assert result["category"] == "crash_candidate"
    assert result["crash_roles"] == ["custom_on"]


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


def test_internal_temporary_table_full_is_a_resource_pause() -> None:
    assert classify_connection_event(
        status="error",
        errno=1114,
        watchdog_fired=False,
        message="The table '/var/lib/engine/tmp/#sql56c5_123a5_0' is full",
    ) == "resource_limit"


def test_user_table_full_remains_a_database_error() -> None:
    assert classify_connection_event(
        status="error",
        errno=1114,
        watchdog_fired=False,
        message="The table 'application_rows' is full",
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


def test_query_lost_connection_is_an_infrastructure_pause_not_a_finding() -> None:
    off = {
        "status": "success",
        "setup_error": None,
        "queries": [
            {
                "status": "error",
                "error": {
                    "classification": "connection_lost_infra",
                    "errno": 2013,
                },
            }
        ],
    }
    on = {"status": "success", "setup_error": None, "queries": [{"status": "success"}]}
    assert _comparison("flashback", off, on) == {
        "matched": True,
        "category": "infrastructure_pause",
        "infrastructure_roles": ["custom_off"],
    }


def test_internal_temporary_table_full_is_an_infrastructure_pause() -> None:
    off = {
        "status": "success",
        "setup_error": None,
        "queries": [
            {
                "status": "error",
                "error": {
                    "classification": "resource_limit",
                    "errno": 1114,
                    "message": "The table '/var/lib/engine/tmp/#sql56c5_123a5_0' is full",
                },
            }
        ],
    }
    on = {"status": "success", "setup_error": None, "queries": [{"status": "success"}]}

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
