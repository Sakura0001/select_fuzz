"""Shared bounded execution layer for correctness and performance modes."""

from select_fuzz.execution.mysql import (
    INTERNAL_RESULT_LIMIT_ERRNO,
    INTERNAL_RUNNER_ERRNO,
    INTERNAL_WATCHDOG_TIMEOUT_ERRNO,
    MySQLConnectorFactory,
    NodeQueryRunner,
)
from select_fuzz.execution.protocols import (
    BarrierLike,
    ConnectionFactory,
    ControlConnectionFactory,
    CursorLike,
    QuerySession,
)
from select_fuzz.execution.timeout import KillHandle, KillQueryWatchdog

__all__ = [
    "BarrierLike",
    "ConnectionFactory",
    "ControlConnectionFactory",
    "CursorLike",
    "INTERNAL_RESULT_LIMIT_ERRNO",
    "INTERNAL_RUNNER_ERRNO",
    "INTERNAL_WATCHDOG_TIMEOUT_ERRNO",
    "KillHandle",
    "KillQueryWatchdog",
    "MySQLConnectorFactory",
    "NodeQueryRunner",
    "QuerySession",
]
