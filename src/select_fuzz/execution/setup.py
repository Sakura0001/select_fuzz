"""Apply one immutable setup bundle to a MySQL node."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
import re
from typing import Protocol

from select_fuzz.config import NodeConfig, NodeRole
from select_fuzz.domain import ErrorInfo, ExecutionStatus
from select_fuzz.execution.protocols import ConnectionFactory, QuerySession


INTERNAL_SETUP_ERRNO = 65010
_DATABASE_IDENTIFIER = re.compile(r"^sf_[a-z0-9_]{1,61}$")
_PAYLOAD_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INFRA_MYSQL_ERRNOS = frozenset(
    {
        1040,  # too many connections
        1042,  # hostname resolution failure
        1043,  # bad handshake
        1044,  # database access denied
        1045,  # authentication denied
        1053,  # server shutdown in progress
        1129,  # host blocked
        1130,  # host not allowed
        *range(1152, 1162),  # packet/read/write transport failures
        1184,  # aborted connection
        1189,  # network read error
        1190,  # network read interrupted
        1203,  # max user connections
        1205,  # lock wait timeout
        1213,  # deadlock
        1226,  # user resource limit
        1317,  # interrupted statement
        3024,  # statement execution timeout
    }
)


class SetupBundleLike(Protocol):
    @property
    def requires_same_session(self) -> bool: ...

    @property
    def payload_sha256(self) -> str: ...

    @property
    def statements(self) -> tuple[str, ...]: ...


def validate_database_name(database: object) -> str:
    if not isinstance(database, str) or _DATABASE_IDENTIFIER.fullmatch(database) is None:
        raise ValueError(
            "database must be a safe lowercase sf_ product identifier of at most 64 bytes"
        )
    return database


def _database_error(error: Exception) -> ErrorInfo | None:
    errno = getattr(error, "errno", None)
    sqlstate = getattr(error, "sqlstate", None)
    message = getattr(error, "msg", None)
    if (
        isinstance(errno, int)
        and not isinstance(errno, bool)
        and 0 <= errno <= 0xFFFF
        and isinstance(sqlstate, str)
        and isinstance(message, str)
    ):
        try:
            return ErrorInfo(errno, sqlstate, message)
        except (TypeError, ValueError):
            return None
    return None


def _infra_error(error: Exception) -> ErrorInfo:
    return ErrorInfo(
        INTERNAL_SETUP_ERRNO,
        "HY000",
        f"setup session failed: {type(error).__name__}",
    )


def _execute_and_close(session: QuerySession, sql: str) -> None:
    cursor = session.execute(sql)
    try:
        return None
    finally:
        cursor.close()


@dataclass(frozen=True, slots=True)
class SetupNodeResult:
    role: NodeRole
    status: ExecutionStatus
    payload_sha256: str | None = None
    error: ErrorInfo | None = None

    def __post_init__(self) -> None:
        if self.status is ExecutionStatus.SUCCESS:
            if self.payload_sha256 is None or _PAYLOAD_SHA256.fullmatch(
                self.payload_sha256
            ) is None:
                raise ValueError("successful setup requires a lowercase payload SHA-256")
            if self.error is not None:
                raise ValueError("successful setup cannot contain an error")
            return
        if self.status not in {ExecutionStatus.ERROR, ExecutionStatus.INFRA_ERROR}:
            raise ValueError("setup status must be success, error, or infra_error")
        if self.error is None:
            raise ValueError("failed setup requires an error")
        if self.payload_sha256 is not None:
            raise ValueError("failed setup cannot contain a payload SHA-256")


class MySQLSetupRunner:
    """Apply database creation, session selection, DDL, and inserts."""

    def __init__(self, factory: ConnectionFactory) -> None:
        self._factory = factory

    def apply(
        self,
        node: NodeConfig,
        database: str,
        bundle: SetupBundleLike,
        *,
        session: QuerySession | None = None,
    ) -> SetupNodeResult:
        database = validate_database_name(database)
        if not isinstance(bundle.payload_sha256, str) or _PAYLOAD_SHA256.fullmatch(
            bundle.payload_sha256
        ) is None:
            raise ValueError("bundle payload_sha256 must be lowercase SHA-256")
        manager = (
            self._factory.query_session(node, "information_schema")
            if session is None
            else nullcontext(session)
        )
        try:
            with manager as active_session:
                _execute_and_close(
                    active_session,
                    f"CREATE DATABASE IF NOT EXISTS `{database}`",
                )
                _execute_and_close(active_session, f"USE `{database}`")
                for statement in bundle.statements:
                    if not isinstance(statement, str) or not statement.strip():
                        raise ValueError("setup statements must be nonempty strings")
                    _execute_and_close(active_session, statement)
        except Exception as error:
            database_error = _database_error(error)
            if (
                database_error is not None
                and database_error.errno not in _INFRA_MYSQL_ERRNOS
                and not 2000 <= database_error.errno < 3000
            ):
                return SetupNodeResult(
                    role=node.role,
                    status=ExecutionStatus.ERROR,
                    error=database_error,
                )
            return SetupNodeResult(
                role=node.role,
                status=ExecutionStatus.INFRA_ERROR,
                error=database_error or _infra_error(error),
            )
        return SetupNodeResult(
            role=node.role,
            status=ExecutionStatus.SUCCESS,
            payload_sha256=bundle.payload_sha256,
        )


__all__ = [
    "INTERNAL_SETUP_ERRNO",
    "MySQLSetupRunner",
    "SetupBundleLike",
    "SetupNodeResult",
    "validate_database_name",
]
