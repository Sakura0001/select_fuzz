from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from select_fuzz.base_tables import (
    available_base_table_generator_versions,
    normalize_base_table_seed,
)
from select_fuzz.sqlgen.registry import (
    available_crud_generator_versions,
    available_query_generator_versions,
)
from select_fuzz.sqlgen.seeds import normalize_uint64_seed


class TaskCreateRequest(BaseModel):
    node_name: str = Field(..., description="节点名称")
    host: str = Field(..., description="数据库地址")
    port: int = Field(..., ge=1, le=65535, description="数据库端口")
    username: str = Field(..., description="数据库用户")
    password: str = Field(..., description="数据库密码")
    database: str = Field(default="test", description="数据库名")
    jump_host: Optional[str] = Field(default=None, description="跳板机配置名")
    replica_host: Optional[str] = Field(default=None, description="备节点数据库地址")
    replica_port: Optional[int] = Field(default=None, ge=1, le=65535, description="备节点数据库端口")
    thread_count: int = Field(default=16, ge=1, le=128, description="备节点查询线程数")
    enable_crud: bool = Field(default=False, strict=True, description="是否启用逐表 CRUD")
    query_seed: Optional[str] = Field(default=None, description="查询生成器复现种子")
    query_generator_version: Optional[str] = Field(default=None, description="查询生成器版本")
    crud_seed: Optional[str] = Field(default=None, description="CRUD 生成器复现种子")
    crud_generator_version: Optional[str] = Field(default=None, description="CRUD 生成器版本")
    expand_base_table_columns: bool = Field(default=False, strict=True, description="是否扩展每张内置基表的列")
    base_table_seed: Optional[str] = Field(default=None, description="基表扩列复现种子")
    base_table_generator_version: Optional[str] = Field(default=None, description="基表生成器版本")

    @field_validator("base_table_seed", "base_table_generator_version", mode="before")
    @classmethod
    def normalize_empty_reproduction_field(cls, value: object, info):
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            field_name = "基表种子" if info.field_name == "base_table_seed" else "基表生成器版本"
            raise ValueError(f"{field_name}必须使用字符串传输")
        return value

    @field_validator(
        "query_seed",
        "query_generator_version",
        "crud_seed",
        "crud_generator_version",
        mode="before",
    )
    @classmethod
    def normalize_task_reproduction_field(cls, value: object, info):
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ValueError(f"{info.field_name} 必须使用字符串传输")
        return value

    @model_validator(mode="after")
    def validate_base_table_reproduction_fields(self) -> "TaskCreateRequest":
        if self.replica_port is not None and self.replica_host is None:
            raise ValueError("填写 replica_port 时必须同时填写 replica_host")
        if self.query_seed is not None:
            self.query_seed = normalize_uint64_seed(self.query_seed)
        if self.crud_seed is not None:
            self.crud_seed = normalize_uint64_seed(self.crud_seed)
        if (
            self.query_generator_version is not None
            and self.query_generator_version not in available_query_generator_versions()
        ):
            raise ValueError(f"未知查询生成器版本：{self.query_generator_version}")
        if (
            self.crud_generator_version is not None
            and self.crud_generator_version not in available_crud_generator_versions()
        ):
            raise ValueError(f"未知 CRUD 生成器版本：{self.crud_generator_version}")
        if not self.enable_crud and (
            self.crud_seed is not None or self.crud_generator_version is not None
        ):
            raise ValueError("关闭 CRUD 时，CRUD 种子和生成器版本必须为空")
        if not self.expand_base_table_columns:
            if self.base_table_seed is not None or self.base_table_generator_version is not None:
                raise ValueError("关闭扩展基表列时，基表种子和生成器版本必须为空")
            return self

        if self.base_table_seed is not None:
            self.base_table_seed = normalize_base_table_seed(self.base_table_seed)
        if (
            self.base_table_generator_version is not None
            and self.base_table_generator_version not in available_base_table_generator_versions()
        ):
            raise ValueError(f"未知基表生成器版本：{self.base_table_generator_version}")
        return self


class TaskResponse(BaseModel):
    task_id: str
    node_name: str
    target: str
    primary_target: str = ""
    replica_target: str = ""
    replica_host: Optional[str] = None
    replica_port: Optional[int] = None
    status: str
    phase: str = "新建"
    last_error: Optional[str] = None
    database: str = "test"
    jump_host: Optional[str] = None
    thread_count: int = 16
    enable_crud: bool = False
    query_seed: Optional[str] = None
    query_generator_version: Optional[str] = None
    crud_seed: Optional[str] = None
    crud_generator_version: Optional[str] = None
    query_worker_total: int = 0
    crud_worker_total: int = 0
    worker_total: int = 0
    sql_total: int = 0
    success_query_total: int = 0
    failed_query_total: int = 0
    ordinary_error_total: int = 0
    insert_success_total: int = 0
    insert_failed_total: int = 0
    update_success_total: int = 0
    update_failed_total: int = 0
    delete_success_total: int = 0
    delete_failed_total: int = 0
    crud_success_total: int = 0
    crud_failed_total: int = 0
    primary_reconnect_total: int = 0
    replica_reconnect_total: int = 0
    primary_reconnecting: int = 0
    replica_reconnecting: int = 0
    lost_connection_total: int = 0
    sql_rate: float = 0
    worker_states: list[dict] = Field(default_factory=list)
    expand_base_table_columns: bool = False
    base_table_seed: Optional[str] = None
    base_table_generator_version: Optional[str] = None


class JumpHostRequest(BaseModel):
    name: str
    host: str
    port: int = 22
    username: str
    password: Optional[str] = None
    private_key_path: Optional[str] = None
