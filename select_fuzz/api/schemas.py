from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class TaskCreateRequest(BaseModel):
    node_name: str = Field(..., description="节点名称")
    host: str = Field(..., description="数据库地址")
    port: int = Field(..., description="数据库端口")
    username: str = Field(..., description="数据库用户")
    password: str = Field(..., description="数据库密码")
    database: str = Field(default="test", description="数据库名")
    jump_host: Optional[str] = Field(default=None, description="跳板机配置名")
    thread_count: int = Field(default=1, ge=1, le=128, description="并发查询线程数")


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
    lost_connection_total: int = 0
    sql_rate: float = 0
    worker_states: list[dict] = Field(default_factory=list)


class JumpHostRequest(BaseModel):
    name: str
    host: str
    port: int = 22
    username: str
    password: Optional[str] = None
    private_key_path: Optional[str] = None
