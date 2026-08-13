from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


@dataclass(frozen=True)
class JumpHostConfig:
    name: str
    host: str
    port: int = 22
    username: str = ""
    password: Optional[str] = None
    private_key_path: Optional[str] = None


@dataclass(frozen=True)
class TargetNodeConfig:
    name: str
    host: str
    port: int
    username: str
    password: str
    database: str = "test"
    jump_host: Optional[str] = None

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class RuntimeConfig:
    project_name: str = "sql_fuzz"
    display_name: str = "SQL 模糊测试控制台"
    base_sql_dir: Path = Path("sql_base_tables")
    sql_log_dir: Path = Path("logs")
    failed_sql_dir: Path = Path("logs/failed_sql")
    lost_connection_dedup_minutes: int = 10
    recovery_probe_seconds: int = 60
    default_thread_count: int = 16
    jump_hosts: List[JumpHostConfig] = field(default_factory=list)
    target_nodes: List[TargetNodeConfig] = field(default_factory=list)

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_sql_dir", Path(self.base_sql_dir))
        object.__setattr__(self, "sql_log_dir", Path(self.sql_log_dir))
        object.__setattr__(self, "failed_sql_dir", Path(self.failed_sql_dir))
        if self.default_thread_count < 1:
            raise ValueError("默认线程数必须大于等于 1")


def _load_yaml(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as file_obj:
        data = yaml.safe_load(file_obj) or {}
    if not isinstance(data, dict):
        raise ValueError("运行配置文件必须是 YAML 对象")
    return data


def load_runtime_config(path: Path | str) -> RuntimeConfig:
    data = _load_yaml(Path(path))
    jump_hosts = [JumpHostConfig(**item) for item in data.pop("jump_hosts", [])]
    target_nodes = [TargetNodeConfig(**item) for item in data.pop("target_nodes", [])]
    return RuntimeConfig(
        **data,
        jump_hosts=jump_hosts,
        target_nodes=target_nodes,
    )
