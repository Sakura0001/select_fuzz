from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from select_fuzz.config import (
    AppConfig,
    ConfigLoadError,
    CorrectnessConfig,
    NodeConfig,
    NodePreflight,
    NodeRole,
    PerformanceConfig,
    RunMode,
    evaluate_preflight,
    load_config,
    resolve_credentials,
)


def _node(role: str, port: int) -> dict[str, object]:
    return {
        "role": role,
        "host": "127.0.0.1",
        "port": port,
        "username_env": "SELECT_FUZZ_MYSQL_USER",
        "password_env": "SELECT_FUZZ_MYSQL_PASSWORD",
    }


def _config_data(**correctness: object) -> dict[str, object]:
    return {
        "mode": "correctness",
        "nodes": [
            _node("baseline", 3306),
            _node("custom_off", 3307),
            _node("custom_on", 3308),
        ],
        "correctness": correctness,
    }


def _write_config(tmp_path: Path, data: Mapping[str, object]) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(dict(data), sort_keys=False), encoding="utf-8")
    return path


def test_mode_defaults_cli_override_and_secret_never_enters_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SELECT_FUZZ_MYSQL_USER", "runtime-user")
    monkeypatch.setenv("SELECT_FUZZ_MYSQL_PASSWORD", "runtime-secret")

    config = load_config(_write_config(tmp_path, _config_data(workers=12)), cli={"workers": 8})

    assert config.mode is RunMode.CORRECTNESS
    assert config.correctness.workers == 8
    assert config.correctness.queries_per_round == 1000
    assert config.correctness.timeout_seconds == 15.0
    assert config.performance.workers == 1
    assert config.performance.queries_per_round == 100
    assert config.performance.regression_threshold == 0.20
    assert config.performance.calibration_max_seconds == 12.0
    assert config.performance.formal_timeout_seconds == 15.0
    assert "runtime-secret" not in config.model_dump_json()
    assert "runtime-secret" not in repr(config)


def test_cli_overrides_follow_the_selected_mode(tmp_path: Path) -> None:
    config = load_config(
        _write_config(tmp_path, _config_data()),
        cli={"mode": "performance", "queries_per_round": 321, "timeout_seconds": 44},
    )

    assert config.mode is RunMode.PERFORMANCE
    assert config.performance.workers == 1
    assert config.performance.queries_per_round == 321
    assert config.performance.formal_timeout_seconds == 44
    assert config.correctness.queries_per_round == 1000


def test_none_cli_mode_means_no_override(tmp_path: Path) -> None:
    data = _config_data()
    data["mode"] = "performance"

    config = load_config(
        _write_config(tmp_path, data),
        cli={"mode": None, "queries_per_round": 321},
    )

    assert config.mode is RunMode.PERFORMANCE
    assert config.performance.queries_per_round == 321


def test_performance_workers_must_equal_one() -> None:
    with pytest.raises(ValidationError):
        PerformanceConfig(workers=2)

    with pytest.raises(ValidationError):
        PerformanceConfig(calibration_runs_per_reference=2)


def test_statement_timeouts_share_the_ui_and_connector_safety_ceiling() -> None:
    assert CorrectnessConfig(timeout_seconds=300).timeout_seconds == 300
    assert PerformanceConfig(formal_timeout_seconds=300).formal_timeout_seconds == 300

    with pytest.raises(ValidationError):
        CorrectnessConfig(timeout_seconds=300.001)
    with pytest.raises(ValidationError):
        PerformanceConfig(formal_timeout_seconds=300.001)


def test_correctness_row_range_is_configurable_and_ordered(tmp_path: Path) -> None:
    config = load_config(
        _write_config(tmp_path, _config_data()),
        cli={"min_rows_per_table": 37, "max_rows_per_table": 419},
    )

    assert config.correctness.min_rows_per_table == 37
    assert config.correctness.max_rows_per_table == 419

    with pytest.raises(ValidationError, match="min_rows_per_table"):
        CorrectnessConfig(min_rows_per_table=500, max_rows_per_table=100)


@pytest.mark.parametrize(
    "nodes",
    [
        [_node("baseline", 3306), _node("custom_off", 3307)],
        [
            _node("baseline", 3306),
            _node("custom_off", 3307),
            _node("custom_off", 3308),
        ],
        [
            _node("baseline", 3306),
            _node("custom_off", 3306),
            _node("custom_on", 3308),
        ],
    ],
)
def test_configuration_requires_three_roles_and_unique_endpoints(
    nodes: list[dict[str, object]],
) -> None:
    with pytest.raises(ValidationError):
        AppConfig(nodes=nodes)


def test_models_forbid_unknown_fields_and_invalid_environment_names() -> None:
    with pytest.raises(ValidationError):
        CorrectnessConfig(workers=10, typo_field=True)

    with pytest.raises(ValidationError):
        NodeConfig(
            role=NodeRole.BASELINE,
            host="db.example",
            port=3306,
            password_env="not a valid env name",
        )


def test_load_rejects_literal_password_without_echoing_it(tmp_path: Path) -> None:
    literal = "do-not-echo-this-value"
    data = _config_data()
    nodes = data["nodes"]
    assert isinstance(nodes, list)
    nodes[0]["password"] = literal

    with pytest.raises(ConfigLoadError) as exc_info:
        load_config(_write_config(tmp_path, data))

    assert literal not in str(exc_info.value)
    assert literal not in repr(exc_info.value)


def test_direct_model_validation_hides_invalid_secret_input() -> None:
    literal = "direct-model-secret-must-not-leak"
    data = _config_data()
    nodes = data["nodes"]
    assert isinstance(nodes, list)
    nodes[0]["password"] = literal

    with pytest.raises(ValidationError) as exc_info:
        AppConfig.model_validate(data)

    assert literal not in str(exc_info.value)
    assert literal not in repr(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_credentials_are_resolved_only_on_request_and_are_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = NodeConfig(role=NodeRole.BASELINE, host="db.example", port=3306)
    assert not hasattr(node, "username")
    assert not hasattr(node, "password")

    monkeypatch.setenv("SELECT_FUZZ_MYSQL_USER", "runtime-user")
    monkeypatch.setenv("SELECT_FUZZ_MYSQL_PASSWORD", "runtime-secret")
    credentials = resolve_credentials(node)

    assert credentials.username.get_secret_value() == "runtime-user"
    assert credentials.password.get_secret_value() == "runtime-secret"
    assert "runtime-user" not in repr(credentials)
    assert "runtime-secret" not in repr(credentials)
    assert "runtime-user" not in credentials.model_dump_json()
    assert "runtime-secret" not in credentials.model_dump_json()


def test_missing_credential_error_names_only_the_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = NodeConfig(role=NodeRole.BASELINE, host="db.example", port=3306)
    monkeypatch.delenv("SELECT_FUZZ_MYSQL_USER", raising=False)
    monkeypatch.setenv("SELECT_FUZZ_MYSQL_PASSWORD", "must-not-leak")

    with pytest.raises(ConfigLoadError) as exc_info:
        resolve_credentials(node)

    assert "SELECT_FUZZ_MYSQL_USER" in str(exc_info.value)
    assert "must-not-leak" not in str(exc_info.value)


def test_preflight_configuration_differences_warn_but_missing_access_is_fatal() -> None:
    snapshots = (
        NodePreflight(
            role=NodeRole.BASELINE,
            config_fingerprint="baseline-fingerprint",
            capabilities={"cte", "window"},
            permissions={"SELECT", "CREATE"},
            role_probe_matches=True,
        ),
        NodePreflight(
            role=NodeRole.CUSTOM_OFF,
            config_fingerprint="off-fingerprint",
            capabilities={"cte"},
            permissions={"SELECT", "CREATE"},
            role_probe_matches=False,
        ),
        NodePreflight(
            role=NodeRole.CUSTOM_ON,
            config_fingerprint="on-fingerprint",
            capabilities={"cte", "window"},
            permissions={"SELECT"},
            role_probe_matches=None,
        ),
    )

    report = evaluate_preflight(
        snapshots,
        required_capabilities={"cte", "window"},
        required_permissions={"SELECT", "CREATE"},
    )

    assert not report.can_start
    assert {issue.code for issue in report.warnings} >= {
        "configuration_difference",
        "role_probe_mismatch",
        "role_probe_missing",
    }
    assert {issue.code for issue in report.fatals} == {
        "missing_capability",
        "missing_permission",
    }


def test_preflight_rejects_duplicate_role_observations() -> None:
    snapshots = tuple(
        NodePreflight(role=role, config_fingerprint=f"{role.value}-fingerprint")
        for role in (
            NodeRole.BASELINE,
            NodeRole.BASELINE,
            NodeRole.CUSTOM_OFF,
            NodeRole.CUSTOM_ON,
        )
    )

    report = evaluate_preflight(snapshots)

    assert not report.can_start
    assert "duplicate_node_observation" in {issue.code for issue in report.fatals}


def test_preflight_warns_when_configuration_fingerprint_is_missing() -> None:
    snapshots = tuple(NodePreflight(role=role) for role in NodeRole)

    report = evaluate_preflight(snapshots)

    assert "configuration_fingerprint_missing" in {
        issue.code for issue in report.warnings
    }


def test_role_probe_fields_must_be_configured_together() -> None:
    with pytest.raises(ValidationError):
        NodeConfig(
            role=NodeRole.BASELINE,
            host="db.example",
            port=3306,
            role_probe_sql="SELECT @@version",
        )


def test_example_configuration_loads_and_contains_no_password_literal() -> None:
    repository = Path(__file__).resolve().parents[2]
    example = repository / "config" / "example.yaml"

    config = load_config(example)

    assert {node.role for node in config.nodes} == set(NodeRole)
    document = example.read_text(encoding="utf-8")
    assert "password:" not in document
    assert "password_env:" in document
