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


def _comparison_nodes() -> list[dict[str, object]]:
    return [
        {
            "role": "custom_off",
            "host": "127.0.0.1",
            "port": 3307,
            "username_env": "SELECT_FUZZ_MYSQL_USER",
            "password_env": "SELECT_FUZZ_MYSQL_PASSWORD",
        },
        {
            "role": "custom_on",
            "host": "127.0.0.1",
            "port": 3308,
            "username_env": "SELECT_FUZZ_MYSQL_USER",
            "password_env": "SELECT_FUZZ_MYSQL_PASSWORD",
        },
    ]


@pytest.mark.parametrize("mode", ["correctness", "performance"])
def test_comparison_modes_load_exactly_two_flat_endpoints(
    tmp_path: Path,
    mode: str,
) -> None:
    config_path = tmp_path / "comparison.yaml"
    config_path.write_text(
        yaml.safe_dump({"mode": mode, "nodes": _comparison_nodes()}),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert [node.role for node in config.comparison_nodes] == [
        NodeRole.CUSTOM_OFF,
        NodeRole.CUSTOM_ON,
    ]
    assert [node.port for node in config.comparison_nodes] == [3307, 3308]


def test_comparison_mode_rejects_old_six_endpoint_topology(tmp_path: Path) -> None:
    config_path = tmp_path / "old-comparison.yaml"
    config_path.write_text(
        yaml.safe_dump({"mode": "correctness", "nodes": _topology()}),
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigLoadError,
        match="对比模式必须配置 custom_off 和 custom_on 两个单实例 endpoint",
    ):
        load_config(config_path)


def test_comparison_mode_rejects_duplicate_endpoint(tmp_path: Path) -> None:
    nodes = _comparison_nodes()
    nodes[1]["port"] = 3307
    config_path = tmp_path / "duplicate-comparison.yaml"
    config_path.write_text(
        yaml.safe_dump({"mode": "performance", "nodes": nodes}),
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigLoadError,
        match="custom_off 和 custom_on 必须使用不同的 host/port",
    ):
        load_config(config_path)


def test_comparison_mode_rejects_replica_parameter_file_before_reading_it(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "parameters-comparison.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "mode": "performance",
                "nodes": _comparison_nodes(),
                "replica_parameters_file": "does-not-exist.yaml",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigLoadError,
        match="两实例对比模式不使用备库参数文件",
    ):
        load_config(config_path)


def test_fuzz_loads_six_endpoints_and_relative_replica_parameter_file(
    tmp_path: Path,
) -> None:
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
                "mode": "fuzz",
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
                    "mode": "fuzz",
                    "nodes": _topology(),
                    "replica_parameters_file": parameter_path.name,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigLoadError, match="replica parameters"):
        load_config(config_path)
