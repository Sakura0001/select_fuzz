from pathlib import Path

from select_fuzz.config import (
    JumpHostConfig,
    RuntimeConfig,
    TargetNodeConfig,
    load_runtime_config,
)


def test_runtime_config_使用中文默认配置并解析基表目录(tmp_path: Path) -> None:
    base_dir = tmp_path / "sql_base_tables"
    base_dir.mkdir()

    config = RuntimeConfig(base_sql_dir=base_dir)

    assert config.project_name == "sql_fuzz"
    assert config.display_name == "SQL 模糊测试控制台"
    assert config.base_sql_dir == base_dir
    assert config.sql_log_dir.name == "logs"
    assert config.lost_connection_dedup_minutes == 10
    assert config.recovery_probe_seconds == 60


def test_节点配置支持跳板机引用() -> None:
    jump = JumpHostConfig(
        name="jump-prod",
        host="10.2.0.8",
        port=22,
        username="ops",
        private_key_path="/Users/yuyu/.ssh/id_rsa",
    )
    node = TargetNodeConfig(
        name="polardb-node-a",
        host="172.18.4.12",
        port=3306,
        username="fuzz",
        password="secret",
        database="select_fuzz",
        jump_host="jump-prod",
    )

    assert jump.name == node.jump_host
    assert node.address == "172.18.4.12:3306"


def test_从_yaml_读取运行配置(tmp_path: Path) -> None:
    config_file = tmp_path / "运行配置.yaml"
    base_dir = tmp_path / "base"
    config_file.write_text(
        "\n".join(
            [
                "project_name: sql_fuzz",
                "display_name: SQL 模糊测试控制台",
                f"base_sql_dir: {base_dir}",
                "sql_log_dir: logs/custom",
                "lost_connection_dedup_minutes: 10",
                "recovery_probe_seconds: 60",
                "jump_hosts:",
                "  - name: jump-prod",
                "    host: 10.2.0.8",
                "    port: 22",
                "    username: ops",
                "target_nodes:",
                "  - name: node-a",
                "    host: 172.18.4.12",
                "    port: 3306",
                "    username: fuzz",
                "    password: secret",
                "    database: select_fuzz",
                "    jump_host: jump-prod",
            ]
        ),
        encoding="utf-8",
    )

    config = load_runtime_config(config_file)

    assert config.base_sql_dir == base_dir
    assert config.sql_log_dir == Path("logs/custom")
    assert config.jump_hosts[0].name == "jump-prod"
    assert config.target_nodes[0].jump_host == "jump-prod"
