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
    private_key_path: Optional[str] = None


@dataclass(frozen=True)
class TargetNodeConfig:
    name: str
    host: str
    port: int
    username: str
    password: str
    database: str
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
    lost_connection_dedup_minutes: int = 10
    recovery_probe_seconds: int = 60
    jump_hosts: List[JumpHostConfig] = field(default_factory=list)
    target_nodes: List[TargetNodeConfig] = field(default_factory=list)

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_sql_dir", Path(self.base_sql_dir))
        object.__setattr__(self, "sql_log_dir", Path(self.sql_log_dir))


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
