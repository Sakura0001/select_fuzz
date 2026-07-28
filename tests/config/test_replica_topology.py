from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from select_fuzz.config import ConfigLoadError, NodeRole, load_config


def _endpoint(port: int) -> dict[str, object]:
    return {
        "host": "127.0.0.1",
        "port": port,
        "username_env": "SELECT_FUZZ_MYSQL_USER",
        "password_env": "SELECT_FUZZ_MYSQL_PASSWORD",
    }


def _topology() -> list[dict[str, object]]:
    return [
        {
            "role": role.value,
            "primary": _endpoint(33061 + index * 2),
            "replica": _endpoint(33062 + index * 2),
        }
        for index, role in enumerate(NodeRole)
    ]


def test_loads_six_endpoints_and_relative_replica_parameter_file(tmp_path: Path) -> None:
    parameter_path = tmp_path / "replica-parameters.yaml"
    parameter_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "replicas": {
                    "baseline": {"session_variables": {"optimizer_switch": "index_merge=off"}},
                    "custom_off": {"session_variables": {}},
                    "custom_on": {"session_variables": {"sql_safe_updates": 0}},
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "mode": "correctness",
                "nodes": _topology(),
                "replica_parameters_file": parameter_path.name,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert [node.port for node in config.primary_nodes] == [33061, 33063, 33065]
    assert [node.port for node in config.replica_nodes] == [33062, 33064, 33066]
    assert config.replica_parameters_file == parameter_path.resolve()
    assert config.replica_session_variables(NodeRole.BASELINE) == {
        "optimizer_switch": "index_merge=off"
    }
    assert len(config.replica_parameters_sha256) == 64
    assert config.replica_sync_timeout_seconds == 10


def test_rejects_duplicate_endpoint_across_primary_and_replica(tmp_path: Path) -> None:
    topology = _topology()
    topology[1]["replica"] = _endpoint(33061)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"nodes": topology}), encoding="utf-8")

    with pytest.raises(ConfigLoadError, match="Invalid configuration"):
        load_config(config_path)


def test_explicit_primary_and_replica_must_be_distinct(tmp_path: Path) -> None:
    topology = _topology()
    topology[0]["replica"] = _endpoint(33061)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"nodes": topology}), encoding="utf-8")

    with pytest.raises(ConfigLoadError, match="Invalid configuration"):
        load_config(config_path)


def test_fuzz_allows_one_routing_proxy_for_primary_and_replica(tmp_path: Path) -> None:
    proxy = {
        "host": "192.168.243.82",
        "port": 3306,
        "username_env": "SELECT_FUZZ_MYSQL_USER",
        "password_env": "SELECT_FUZZ_MYSQL_PASSWORD",
    }
    config_path = tmp_path / "fuzz.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "mode": "fuzz",
                "nodes": [
                    {"role": role.value, "primary": proxy, "replica": proxy}
                    for role in NodeRole
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.fuzz.target_role is NodeRole.CUSTOM_ON
    assert config.node_for(NodeRole.CUSTOM_ON).host == "192.168.243.82"
    assert config.replica_for(NodeRole.CUSTOM_ON).port == 3306


def test_fuzz_accepts_legacy_single_endpoint_entries(tmp_path: Path) -> None:
    config_path = tmp_path / "fuzz-legacy.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "mode": "fuzz",
                "nodes": [
                    {
                        "role": role.value,
                        "host": "192.168.243.82",
                        "port": 3306,
                    }
                    for role in NodeRole
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.node_for(NodeRole.CUSTOM_ON).port == config.replica_for(NodeRole.CUSTOM_ON).port


def test_replica_parameter_file_requires_exact_roles_and_scalar_values(
    tmp_path: Path,
) -> None:
    parameter_path = tmp_path / "replica-parameters.yaml"
    parameter_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "replicas": {
                    "baseline": {"session_variables": {"bad": [1, 2]}},
                    "custom_off": {"session_variables": {}},
                },
            }
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "nodes": _topology(),
                "replica_parameters_file": parameter_path.name,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigLoadError, match="replica parameters"):
        load_config(config_path)
