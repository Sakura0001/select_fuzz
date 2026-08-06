"""Independent per-database setup and first replica visibility wait."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import time
from typing import Callable

from select_fuzz.config import FuzzConfig, NodeConfig
from select_fuzz.execution.protocols import ConnectionFactory, QuerySession
from select_fuzz.execution.replication import MARKER_DDL_SQL, MARKER_TABLE, marker_upsert_sql
from select_fuzz.execution.setup import validate_database_name
from select_fuzz.generation.query_grammar import GrammarSchema
from select_fuzz.modes.fuzz.dml import FuzzTable
from select_fuzz.modes.fuzz.schema import build_table_specs, initial_insert_sql
from select_fuzz.modes.fuzz.sql_log import FuzzSqlRecorder


_EVENT_ERROR_MESSAGE_ATTRIBUTE = "_select_fuzz_event_error_message"


def _event_error_message(error: Exception) -> str:
    message = getattr(error, _EVENT_ERROR_MESSAGE_ATTRIBUTE, None)
    return message if isinstance(message, str) else str(error)


def _execute(session: QuerySession, sql: str) -> None:
    cursor = session.execute(sql)
    try:
        while cursor.fetchmany(128):
            pass
    finally:
        cursor.close()


@dataclass(frozen=True, slots=True)
class FuzzDatabaseSchema:
    database: str
    grammar_schema: GrammarSchema
    dml_tables: tuple[FuzzTable, ...]
    initial_high_watermark: int


class FuzzMaterializer:
    def __init__(
        self,
        factory: ConnectionFactory,
        primary: NodeConfig,
        replica: NodeConfig,
        config: FuzzConfig,
        *,
        replica_sync_timeout_seconds: float = 30.0,
        sleeper: Callable[[float], None] = time.sleep,
        sql_recorder: FuzzSqlRecorder | None = None,
    ) -> None:
        if replica_sync_timeout_seconds <= 0:
            raise ValueError("replica_sync_timeout_seconds must be positive")
        self._factory = factory
        self._primary = primary
        self._replica = replica
        self._config = config
        self._replica_sync_timeout_seconds = float(replica_sync_timeout_seconds)
        self._sleeper = sleeper
        self._sql_recorder = sql_recorder

    def materialize(self, database: str, *, seed: int) -> FuzzDatabaseSchema:
        database = validate_database_name(database)
        tables = tuple(f"fuzz_t{index}" for index in range(self._config.initial_tables))
        table_specs = build_table_specs(tables, self._config, seed=seed)
        with self._factory.query_session(self._primary, "information_schema") as session:
            create_database_sql = f"CREATE DATABASE IF NOT EXISTS `{database}`"
            use_database_sql = f"USE `{database}`"
            self._record_schema(database, create_database_sql)
            self._record_schema(database, use_database_sql)
            _execute(session, create_database_sql)
            _execute(session, use_database_sql)
            for table_index, spec in enumerate(table_specs):
                create_table_sql = spec.create_sql()
                insert_rows_sql = initial_insert_sql(
                    spec,
                    self._config.initial_rows_per_table,
                    seed + table_index,
                )
                self._record_schema(database, create_table_sql)
                self._record_schema(database, insert_rows_sql)
                _execute(session, create_table_sql)
                _execute(session, insert_rows_sql)
            self._record_schema(database, MARKER_DDL_SQL)
            marker_sql = marker_upsert_sql(0)
            self._record_schema(database, marker_sql)
            _execute(session, MARKER_DDL_SQL)
            _execute(session, marker_sql)
        self._wait_for_replica(database)
        grammar_tables = tuple(spec.grammar_table() for spec in table_specs)
        return FuzzDatabaseSchema(
            database,
            GrammarSchema(grammar_tables),
            tuple(
                FuzzTable(
                    table,
                    "id",
                    ("tenant_id", "amount", "status", "updated_at", "payload"),
                )
                for table in tables
            ),
            self._config.initial_rows_per_table,
        )

    def _record_schema(self, database: str, sql: str) -> None:
        if self._sql_recorder is not None:
            self._sql_recorder.record_schema(database, sql)

    def _wait_for_replica(self, database: str) -> None:
        deadline = time.monotonic() + self._replica_sync_timeout_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with self._factory.control_session(self._replica, database) as session:
                    cursor = session.execute(
                        f"SELECT `batch_sequence` FROM `{MARKER_TABLE}` "
                        "WHERE `marker_id` = 1"
                    )
                    try:
                        rows = cursor.fetchmany(2)
                    finally:
                        cursor.close()
                if rows == ((0,),):
                    return
                last_error = None
            except Exception as error:
                last_error = error
            self._sleeper(0.1)
        detail = "主节点同步标记在备节点尚不可见"
        event_detail = "replication marker not visible"
        if last_error is not None:
            detail = f"最后一次探测异常={type(last_error).__name__}：{last_error}"
            event_detail = (
                f"last probe error={type(last_error).__name__}: {last_error}"
            )
        timeout_error = TimeoutError(
            "等待备节点同步超时：已等待 "
            f"{self._replica_sync_timeout_seconds:g} 秒；"
            f"数据库={database}；{detail}"
        )
        event_message = (
            "replica synchronization timeout after "
            f"{self._replica_sync_timeout_seconds:g} seconds; "
            f"database={database}; {event_detail}"
        )
        setattr(timeout_error, _EVENT_ERROR_MESSAGE_ATTRIBUTE, event_message)
        raise timeout_error


def fuzz_database_name(run_id: str, ordinal: int, *, generation: int = 0) -> str:
    if ordinal < 0:
        raise ValueError("fuzz database ordinal must be nonnegative")
    if generation < 0:
        raise ValueError("fuzz database generation must be nonnegative")
    digest = sha256(f"{run_id}:{generation}:{ordinal}".encode()).hexdigest()[:16]
    return validate_database_name(f"sf_f_{digest}_g{generation}_{ordinal}")


__all__ = [
    "FuzzDatabaseSchema",
    "FuzzMaterializer",
    "fuzz_database_name",
]
