"""Apply one immutable setup bundle to a MySQL node."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Protocol

from select_fuzz.config import COMPARISON_ROLES, NodeConfig, NodeRole
from select_fuzz.domain import ErrorInfo, ExecutionStatus
from select_fuzz.execution.protocols import ConnectionFactory, QuerySession
from select_fuzz.oracle.errors import normalize_error


INTERNAL_SETUP_ERRNO = 65010
_DATABASE_IDENTIFIER = re.compile(r"^sf_[a-z0-9_]{1,61}$")
_PAYLOAD_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DML_STATEMENT = re.compile(r"^\s*(?:INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)
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
            if (
                self.payload_sha256 is None
                or _PAYLOAD_SHA256.fullmatch(self.payload_sha256) is None
            ):
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


class LockstepSetupVerdict(StrEnum):
    READY = "ready"
    MISMATCH = "mismatch"
    REJECTED_GENERATION = "rejected_generation"
    INFRASTRUCTURE_PAUSE = "infrastructure_pause"


@dataclass(frozen=True, slots=True)
class SetupStatementNodeResult:
    role: NodeRole
    status: ExecutionStatus
    affected_rows: int | None = None
    error: ErrorInfo | None = None

    def __post_init__(self) -> None:
        if self.status is ExecutionStatus.SUCCESS:
            if self.error is not None:
                raise ValueError("successful setup statement cannot contain an error")
            return
        if self.status not in {ExecutionStatus.ERROR, ExecutionStatus.INFRA_ERROR}:
            raise ValueError("setup statement status is not supported")
        if self.error is None:
            raise ValueError("failed setup statement requires an error")
        if self.affected_rows is not None:
            raise ValueError("failed setup statement cannot contain affected rows")


@dataclass(frozen=True, slots=True)
class SetupStatementRecord:
    sql: str
    results: Mapping[NodeRole, SetupStatementNodeResult]


@dataclass(frozen=True, slots=True)
class LockstepSetupResult:
    verdict: LockstepSetupVerdict
    nodes: tuple[SetupNodeResult, ...]
    attempted_bundle_sql: tuple[str, ...]
    statement_records: tuple[SetupStatementRecord, ...]
    failing_sql: str | None = None


def _statement_failure(node: NodeConfig, error: Exception) -> SetupStatementNodeResult:
    database_error = _database_error(error)
    if (
        database_error is not None
        and database_error.errno not in _INFRA_MYSQL_ERRNOS
        and not 2000 <= database_error.errno < 3000
    ):
        return SetupStatementNodeResult(
            node.role,
            ExecutionStatus.ERROR,
            error=database_error,
        )
    return SetupStatementNodeResult(
        node.role,
        ExecutionStatus.INFRA_ERROR,
        error=database_error or _infra_error(error),
    )


def _statement_verdict(
    results: Mapping[NodeRole, SetupStatementNodeResult],
    *,
    compare_affected_rows: bool,
) -> LockstepSetupVerdict:
    ordered = tuple(results[role] for role in COMPARISON_ROLES)
    statuses = {result.status for result in ordered}
    if statuses == {ExecutionStatus.SUCCESS}:
        if not compare_affected_rows:
            return LockstepSetupVerdict.READY
        affected_rows = {result.affected_rows for result in ordered}
        if None not in affected_rows and len(affected_rows) == 1:
            return LockstepSetupVerdict.READY
        return LockstepSetupVerdict.MISMATCH
    if statuses == {ExecutionStatus.ERROR}:
        errors = tuple(result.error for result in ordered)
        if (
            all(error is not None for error in errors)
            and len({normalize_error(error) for error in errors if error is not None}) == 1
        ):
            return LockstepSetupVerdict.REJECTED_GENERATION
        return LockstepSetupVerdict.MISMATCH
    if statuses == {ExecutionStatus.INFRA_ERROR}:
        errors = tuple(result.error for result in ordered)
        if (
            all(error is not None for error in errors)
            and len({normalize_error(error) for error in errors if error is not None}) == 1
        ):
            return LockstepSetupVerdict.INFRASTRUCTURE_PAUSE
    return LockstepSetupVerdict.MISMATCH


def _setup_nodes_from_statement(
    results: Mapping[NodeRole, SetupStatementNodeResult],
    payload_sha256: str,
) -> tuple[SetupNodeResult, ...]:
    nodes: list[SetupNodeResult] = []
    for role in COMPARISON_ROLES:
        result = results[role]
        if result.status is ExecutionStatus.SUCCESS:
            nodes.append(SetupNodeResult(role, result.status, payload_sha256))
        else:
            nodes.append(SetupNodeResult(role, result.status, error=result.error))
    return tuple(nodes)


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
        if (
            not isinstance(bundle.payload_sha256, str)
            or _PAYLOAD_SHA256.fullmatch(bundle.payload_sha256) is None
        ):
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

    def apply_lockstep(
        self,
        nodes: Sequence[NodeConfig],
        database: str,
        bundle: SetupBundleLike,
        *,
        sessions: Mapping[NodeRole, QuerySession] | None = None,
    ) -> LockstepSetupResult:
        """Apply every setup statement to both comparison endpoints before advancing."""

        database = validate_database_name(database)
        by_role = {node.role: node for node in nodes}
        if len(nodes) != 2 or set(by_role) != set(COMPARISON_ROLES):
            raise ValueError("lockstep setup requires custom_off and custom_on")
        if (
            not isinstance(bundle.payload_sha256, str)
            or _PAYLOAD_SHA256.fullmatch(bundle.payload_sha256) is None
        ):
            raise ValueError("bundle payload_sha256 must be lowercase SHA-256")
        for statement in bundle.statements:
            if not isinstance(statement, str) or not statement.strip():
                raise ValueError("setup statements must be nonempty strings")

        stack = ExitStack()
        attempted: list[str] = []
        records: list[SetupStatementRecord] = []
        try:
            active_sessions = (
                {
                    role: stack.enter_context(
                        self._factory.query_session(by_role[role], "information_schema")
                    )
                    for role in COMPARISON_ROLES
                }
                if sessions is None
                else {role: sessions[role] for role in COMPARISON_ROLES}
            )

            def execute_statement(sql: str) -> dict[NodeRole, SetupStatementNodeResult]:
                def one(role: NodeRole) -> SetupStatementNodeResult:
                    try:
                        cursor = active_sessions[role].execute(sql)
                        try:
                            affected_rows = getattr(cursor, "affected_rows", None)
                        finally:
                            cursor.close()
                        return SetupStatementNodeResult(
                            role,
                            ExecutionStatus.SUCCESS,
                            affected_rows=affected_rows,
                        )
                    except Exception as error:
                        return _statement_failure(by_role[role], error)

                with ThreadPoolExecutor(
                    max_workers=2, thread_name_prefix="sf-setup-statement"
                ) as pool:
                    futures = {role: pool.submit(one, role) for role in COMPARISON_ROLES}
                    return {role: futures[role].result() for role in COMPARISON_ROLES}

            setup_sequence = (
                (f"CREATE DATABASE IF NOT EXISTS `{database}`", False, False),
                (f"USE `{database}`", False, False),
                *(
                    (statement, True, bool(_DML_STATEMENT.match(statement)))
                    for statement in bundle.statements
                ),
            )
            for sql, is_bundle_statement, compare_affected_rows in setup_sequence:
                if is_bundle_statement:
                    attempted.append(sql)
                results = execute_statement(sql)
                records.append(SetupStatementRecord(sql, results))
                verdict = _statement_verdict(
                    results,
                    compare_affected_rows=compare_affected_rows,
                )
                if verdict is not LockstepSetupVerdict.READY:
                    return LockstepSetupResult(
                        verdict,
                        _setup_nodes_from_statement(results, bundle.payload_sha256),
                        tuple(attempted),
                        tuple(records),
                        sql,
                    )
        except Exception as error:
            failure = {
                role: _statement_failure(by_role[role], error)
                for role in COMPARISON_ROLES
            }
            return LockstepSetupResult(
                LockstepSetupVerdict.INFRASTRUCTURE_PAUSE,
                _setup_nodes_from_statement(failure, bundle.payload_sha256),
                tuple(attempted),
                tuple(records),
                records[-1].sql if records else None,
            )
        finally:
            stack.close()

        nodes_result = tuple(
            SetupNodeResult(role, ExecutionStatus.SUCCESS, bundle.payload_sha256)
            for role in COMPARISON_ROLES
        )
        return LockstepSetupResult(
            LockstepSetupVerdict.READY,
            nodes_result,
            tuple(attempted),
            tuple(records),
        )


__all__ = [
    "INTERNAL_SETUP_ERRNO",
    "LockstepSetupResult",
    "LockstepSetupVerdict",
    "MySQLSetupRunner",
    "SetupBundleLike",
    "SetupNodeResult",
    "SetupStatementNodeResult",
    "SetupStatementRecord",
    "validate_database_name",
]
