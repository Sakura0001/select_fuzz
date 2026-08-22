"""Immutable values crossing generator, executor, oracle, and control-plane boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import re
from types import MappingProxyType
from typing import Literal

from select_fuzz.config import NodeRole


class ExecutionStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    INFRA_ERROR = "infra_error"


_SQLSTATE = re.compile(r"^[0-9A-Z]{5}$")


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("immutable payload mappings require string keys")
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(child) for child in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(child) for child in value)
    if isinstance(value, bytearray):
        return bytes(value)
    return value


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    frozen = _freeze(value)
    if not isinstance(frozen, Mapping):  # pragma: no cover - helper contract
        raise TypeError("expected a mapping payload")
    return frozen


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    errno: int
    sqlstate: str
    message: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.errno, int)
            or isinstance(self.errno, bool)
            or not 0 <= self.errno <= 0xFFFF
        ):
            raise ValueError("errno must be an unsigned 16-bit integer")
        if not isinstance(self.sqlstate, str) or not _SQLSTATE.fullmatch(self.sqlstate):
            raise ValueError("sqlstate must contain five uppercase alphanumeric characters")
        if not isinstance(self.message, str):
            raise TypeError("message must be a string")


@dataclass(frozen=True, slots=True)
class ColumnMeta:
    name: str
    type_code: int
    nullable: bool
    unsigned: bool
    binary: bool
    character_set_id: int | None = None
    column_length: int | None = None
    decimals: int | None = None
    flags: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("column name must not be empty")
        if (
            not isinstance(self.type_code, int)
            or isinstance(self.type_code, bool)
            or not 0 <= self.type_code <= 0xFF
        ):
            raise ValueError("type_code must be an unsigned 8-bit integer")
        for field_name in ("nullable", "unsigned", "binary"):
            if not isinstance(getattr(self, field_name), bool):
                raise TypeError(f"{field_name} must be a bool")
        bounded_fields = (
            ("character_set_id", self.character_set_id, 0xFFFF),
            ("column_length", self.column_length, 0xFFFFFFFF),
            ("decimals", self.decimals, 0xFF),
            ("flags", self.flags, 0xFFFF),
        )
        for field_name, value, maximum in bounded_fields:
            if value is not None and (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 <= value <= maximum
            ):
                raise ValueError(
                    f"{field_name} must be an unsigned protocol integer when present"
                )


@dataclass(frozen=True, slots=True)
class NodeExecution:
    role: NodeRole
    status: ExecutionStatus
    started_ns: int
    ended_ns: int
    connection_id: int | None
    affected_rows: int | None = None
    columns: tuple[ColumnMeta, ...] = ()
    rows: tuple[tuple[object, ...], ...] = ()
    error: ErrorInfo | None = None
    warnings: tuple[str, ...] = ()
    watchdog_fired: bool = False
    watchdog_error_type: str | None = None
    connection_reusable: bool = True
    performance_payload: Mapping[str, object] | None = None
    failure_evidence: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "columns", tuple(self.columns))
        object.__setattr__(
            self,
            "rows",
            tuple(tuple(_freeze(cell) for cell in row) for row in self.rows),
        )
        object.__setattr__(self, "warnings", tuple(self.warnings))
        if not isinstance(self.connection_reusable, bool):
            raise TypeError("connection_reusable must be a bool")
        if self.watchdog_error_type is not None and (
            not isinstance(self.watchdog_error_type, str)
            or not self.watchdog_error_type
        ):
            raise TypeError("watchdog_error_type must be a nonempty string when present")
        if self.started_ns < 0 or self.ended_ns < self.started_ns:
            raise ValueError("ended_ns must be greater than or equal to started_ns")
        if self.connection_id is not None and self.connection_id <= 0:
            raise ValueError("connection_id must be positive when present")
        if self.affected_rows is not None and (
            not isinstance(self.affected_rows, int)
            or isinstance(self.affected_rows, bool)
            or self.affected_rows < 0
        ):
            raise ValueError("affected_rows must be nonnegative when present")
        if self.status is ExecutionStatus.SUCCESS and self.error is not None:
            raise ValueError("successful execution cannot contain an error")
        if self.status is not ExecutionStatus.SUCCESS and self.error is None:
            raise ValueError("non-successful execution requires an error")
        if self.status is not ExecutionStatus.SUCCESS and self.rows:
            raise ValueError("rows are only valid for successful execution")
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("result row width must match column metadata width")
        if self.performance_payload is not None:
            object.__setattr__(
                self,
                "performance_payload",
                _freeze_mapping(self.performance_payload),
            )
        if self.failure_evidence is not None:
            object.__setattr__(
                self,
                "failure_evidence",
                _freeze_mapping(self.failure_evidence),
            )

    @property
    def elapsed_ns(self) -> int:
        return self.ended_ns - self.started_ns

    @classmethod
    def success(
        cls,
        *,
        role: NodeRole,
        connection_id: int | None,
        started_ns: int,
        ended_ns: int,
        columns: tuple[ColumnMeta, ...] = (),
        rows: tuple[tuple[object, ...], ...] = (),
        warnings: tuple[str, ...] = (),
        connection_reusable: bool = True,
        performance_payload: Mapping[str, object] | None = None,
        affected_rows: int | None = None,
    ) -> NodeExecution:
        return cls(
            role=role,
            status=ExecutionStatus.SUCCESS,
            started_ns=started_ns,
            ended_ns=ended_ns,
            connection_id=connection_id,
            affected_rows=affected_rows,
            columns=columns,
            rows=rows,
            warnings=warnings,
            connection_reusable=connection_reusable,
            performance_payload=performance_payload,
        )

    @classmethod
    def failure(
        cls,
        *,
        role: NodeRole,
        status: ExecutionStatus,
        started_ns: int,
        ended_ns: int,
        connection_id: int | None,
        error: ErrorInfo,
        rows: tuple[tuple[object, ...], ...] = (),
        warnings: tuple[str, ...] = (),
        watchdog_fired: bool = False,
        watchdog_error_type: str | None = None,
        connection_reusable: bool = True,
        performance_payload: Mapping[str, object] | None = None,
        affected_rows: int | None = None,
        failure_evidence: Mapping[str, object] | None = None,
    ) -> NodeExecution:
        if status is ExecutionStatus.SUCCESS:
            raise ValueError("failure status cannot be success")
        return cls(
            role=role,
            status=status,
            started_ns=started_ns,
            ended_ns=ended_ns,
            connection_id=connection_id,
            affected_rows=affected_rows,
            rows=rows,
            error=error,
            warnings=warnings,
            watchdog_fired=watchdog_fired,
            watchdog_error_type=watchdog_error_type,
            connection_reusable=connection_reusable,
            performance_payload=performance_payload,
            failure_evidence=failure_evidence,
        )


@dataclass(frozen=True, slots=True)
class RunRequest:
    run_id: str
    mode: Literal["correctness", "performance", "fuzz"]
    seed: int
    workers: int
    rounds: int | None
    queries_per_round: int

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("run_id must not be empty")
        if self.mode not in ("correctness", "performance", "fuzz"):
            raise ValueError("mode must be correctness, performance, or fuzz")
        if self.workers <= 0:
            raise ValueError("workers must be positive")
        if self.mode == "performance" and self.workers != 1:
            raise ValueError("performance mode requires one worker")
        if self.rounds is not None and self.rounds <= 0:
            raise ValueError("rounds must be positive when supplied")
        if self.queries_per_round <= 0:
            raise ValueError("queries_per_round must be positive")


@dataclass(frozen=True, slots=True)
class RunEvent:
    run_id: str
    sequence: int
    kind: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("run_id must not be empty")
        if self.sequence < 0:
            raise ValueError("sequence must be nonnegative")
        if not self.kind:
            raise ValueError("kind must not be empty")
        object.__setattr__(self, "payload", _freeze_mapping(self.payload))
