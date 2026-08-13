from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from select_fuzz.base_tables import (
    available_base_table_generator_versions,
    normalize_base_table_seed,
)


class TaskCreateRequest(BaseModel):
    node_name: str = Field(..., description="节点名称")
    host: str = Field(..., description="数据库地址")
    port: int = Field(..., description="数据库端口")
    username: str = Field(..., description="数据库用户")
    password: str = Field(..., description="数据库密码")
    database: str = Field(default="test", description="数据库名")
    jump_host: Optional[str] = Field(default=None, description="跳板机配置名")
    thread_count: int = Field(default=1, ge=1, le=128, description="并发查询线程数")
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

    @model_validator(mode="after")
    def validate_base_table_reproduction_fields(self) -> "TaskCreateRequest":
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
    status: str
    phase: str = "新建"
    last_error: Optional[str] = None
    database: str = "test"
    jump_host: Optional[str] = None
    thread_count: int = 1
    sql_total: int = 0
    success_query_total: int = 0
    failed_query_total: int = 0
    ordinary_error_total: int = 0
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
