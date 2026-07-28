"""Stable HTTP contracts kept independent from the concrete fuzzing engine."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class RunCreate(BaseModel):
    mode: Literal["correctness", "performance", "fuzz"]
    seed: int = Field(default=0, ge=0)
    workers: int | None = Field(default=None, ge=1, le=64)
    rounds: int | None = Field(default=None, ge=1)
    queries_per_round: int | None = Field(default=None, ge=1)
    timeout_seconds: float | None = Field(default=None, gt=0, le=300)
    degradation_ratio: float = Field(default=0.2, gt=0)
    data_rows_min: int = Field(default=100, ge=1)
    data_rows_max: int = Field(default=10_000, ge=1)
    duration_seconds: float | None = Field(default=None, gt=0)
    databases: int = Field(default=1, ge=1, le=32)
    writer_threads_per_database: int = Field(default=2, ge=1, le=64)
    reader_threads_per_database: int = Field(default=6, ge=3, le=192)

    @model_validator(mode="after")
    def defaults_for_mode(self) -> RunCreate:
        if self.workers is None:
            self.workers = 1 if self.mode in {"performance", "fuzz"} else 10
        if self.queries_per_round is None:
            self.queries_per_round = (
                100 if self.mode == "performance" else 1 if self.mode == "fuzz" else 1000
            )
        if self.timeout_seconds is None:
            self.timeout_seconds = 15.0
        if self.mode == "performance" and self.workers != 1:
            raise ValueError("performance mode requires workers=1")
        if self.mode == "fuzz" and self.workers != 1:
            raise ValueError("fuzz mode uses one coordinator worker")
        if self.mode == "fuzz" and self.reader_threads_per_database % 3 != 0:
            raise ValueError("fuzz reader_threads_per_database must be divisible by 3")
        if self.data_rows_max < self.data_rows_min:
            raise ValueError("data_rows_max must be greater than or equal to data_rows_min")
        return self


class RunView(BaseModel):
    id: str
    state: Literal[
        "queued", "starting", "running", "stopping", "recovering", "orphaned",
        "stopped", "completed", "failed"
    ]
    request: RunCreate
    created_at: str
    updated_at: str
    version: int
    pid: int | None = None
    process_identity: str | None = None
    exit_code: int | None = None


class ReplayCreate(BaseModel):
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,127}$")


class ReplayView(BaseModel):
    id: str
    case_id: str
    state: Literal["queued", "running", "reproduced", "not_reproduced", "failed"]
    created_at: str
    updated_at: str
    result: dict[str, object] | None = None


class RunPage(BaseModel):
    items: list[RunView]
    next_cursor: str | None = None


class ProblemField(BaseModel):
    pointer: str
    message: str


class Problem(BaseModel):
    type: str
    title: str
    status: int
    detail: str
    instance: str
    request_id: str
    errors: list[ProblemField] = Field(default_factory=list)
