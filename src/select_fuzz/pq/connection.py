"""Bounded text-protocol sessions using preconfigured database parameters."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Any, cast

import mysql.connector
from mysql.connector.abstracts import MySQLConnectionAbstract

from select_fuzz.pq.config import Endpoint, PQConfig, require_serial_endpoint, validate_database

_FALLBACK = re.compile(
    r"(?:fallback|fall\s+back|retry|retried|re.?execut).*(?:non.?pq|serial|without.*parallel)"
    r"|(?:non.?pq|serial).*(?:fallback|fall\s+back|retry)", re.I,
)
_SESSION_VARIABLES: tuple[str, ...] = (
    "version", "sql_mode", "time_zone", "max_execution_time", "collation_connection",
)
_PQ_SESSION_VARIABLES: tuple[str, ...] = (
    "force_parallel_execute", "pq_master_enable", "parallel_max_threads",
    "parallel_memory_limit", "pq_support_features_switch", "parallel_default_dop",
    "parallel_cost_threshold", "parallel_tuple_cost", "parallel_setup_cost",
    "parallel_rows_threshold", "parallel_correlated_subquery", "parallel_limit_no_order_by",
    "pq_group_having", "innodb_parallel_select_count", "parallel_fail_retry",
    "parallel_graceful_fallback", "parallel_queue_timeout",
)


@dataclass(frozen=True, slots=True)
class QueryResult:
    status: str
    rows: tuple[tuple[Any, ...], ...]
    columns: tuple[str, ...]
    type_codes: tuple[int, ...]
    error: str = ""
    errno: int = 0
    elapsed_ns: int = 0
    complete: bool = True
    total_rows: int | None = None
    warnings: tuple[tuple[Any, ...], ...] = ()
    fallback: bool = False
    warnings_complete: bool = True

    @property
    def row_count(self) -> int:
        return self.total_rows if self.total_rows is not None else len(self.rows)

    @property
    def elapsed_ms(self) -> float:
        return self.elapsed_ns / 1_000_000.0


class PQConnection:
    """A dedicated, non-prepared session with read-only parameter observations.

    PQ, serial and reference sessions inherit their runtime settings. Driver
    autocommit and charset negotiation retain their normal connection behavior.
    """

    def __init__(self, endpoint: Endpoint, *, dop: int, autocommit: bool = True,
                 query_timeout_ms: int = 60_000, reference: bool = False) -> None:
        if query_timeout_ms <= 0 or not 0 <= dop <= 256:
            raise ValueError("positive timeout and DOP in 0..256 required")
        self._endpoint = endpoint
        self._dop = int(dop)
        self._query_timeout_ms = int(query_timeout_ms)
        self._reference = reference
        self._autocommit = autocommit
        self._database: str | None = None
        self._broken = False
        self.setup_notes: list[str] = []
        self.session_values: dict[str, str] = {}
        self._conn = self._open(autocommit=autocommit)

    def _open(self, *, autocommit: bool) -> MySQLConnectionAbstract:
        kwargs = {k: v for k, v in self._endpoint.as_kwargs().items() if k != "database"}
        # Endpoint kwargs never include pooling options, so connect returns a direct session.
        conn = cast(MySQLConnectionAbstract, mysql.connector.connect(
            **kwargs, connection_timeout=5,
            read_timeout=max(1, math.ceil(self._query_timeout_ms / 1000) + 5),
            write_timeout=max(1, math.ceil(self._query_timeout_ms / 1000) + 5),
            charset="utf8mb4", get_warnings=False,
        ))
        cur = None
        try:
            conn.autocommit = autocommit
            cur = conn.cursor()
            # A single metadata read records the actual feature switches and
            # inherited values, including force_parallel_execute and thresholds.
            names = _SESSION_VARIABLES
            if not self._reference:
                names += _PQ_SESSION_VARIABLES
            cur.execute("SHOW SESSION VARIABLES WHERE Variable_name IN (" +
                        ",".join(f"'{n}'" for n in names) + ")")
            self.session_values = {str(k).lower(): str(v) for k, v in cur.fetchall()}
        except BaseException:
            conn.close()
            raise
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception:
                    pass
        return conn

    @property
    def dop(self) -> int | None:
        """Observed session DOP, when available; this is not worker evidence."""
        actual = self.session_values.get("parallel_default_dop", "")
        return int(actual) if actual.isdecimal() else None

    def set_dop(self, dop: int) -> None:
        raise ValueError("DOP is preconfigured; this harness cannot change runtime parameters")

    def _ensure_connected(self) -> None:
        if self._broken:
            self._conn = self._open(autocommit=self._autocommit)
            self._broken = False
            if self._database:
                self.use_database(self._database)

    def use_database(self, database: str) -> None:
        validate_database(database)
        self.run_script([f"USE `{database}`"])
        self._database = database

    def execute(self, sql: str, *, row_limit: int = 50_000) -> QueryResult:
        """Fetch to EOF with bounded retention and time; never claim prefix equality.

        Timing includes execute and complete result transfer, but excludes SHOW
        WARNINGS. Excess rows are drained and counted without retaining them.
        """
        if row_limit < 1:
            raise ValueError("row_limit must be positive")
        start = time.perf_counter_ns()
        deadline = start + self._query_timeout_ms * 1_000_000
        cur = None
        try:
            self._ensure_connected()
            cur = self._conn.cursor()
            cur.execute(sql)
            if time.perf_counter_ns() >= deadline:
                raise TimeoutError("query execution exceeded deadline")
            desc = cur.description or ()
            columns = tuple(d[0] for d in desc)
            type_codes = tuple(int(d[1]) for d in desc)
            rows: list[tuple[Any, ...]] = []
            total = 0
            while desc:
                batch = cur.fetchmany(min(1024, row_limit + 1))
                if time.perf_counter_ns() >= deadline:
                    raise TimeoutError("query result transfer exceeded deadline")
                if not batch:
                    break
                total += len(batch)
                if len(rows) < row_limit:
                    rows.extend(tuple(r) for r in batch[:row_limit - len(rows)])
            elapsed = time.perf_counter_ns() - start
            warnings: tuple[tuple[Any, ...], ...] = ()
            warnings_complete = True
            # Must be the next command: intervening SELECT/EXPLAIN/SET can
            # replace the statement's diagnostics area.
            try:
                cur.execute("SHOW WARNINGS")
                warnings = tuple(tuple(r) for r in cur.fetchall())
            except Exception:
                warnings_complete = False
            return QueryResult(
                "OK", tuple(rows), columns, type_codes, elapsed_ns=elapsed,
                complete=total <= row_limit, total_rows=total, warnings=warnings,
                fallback=any(_FALLBACK.search(str(w[-1])) for w in warnings if w),
                warnings_complete=warnings_complete,
            )
        except Exception as exc:
            # A fetch/transport failure can leave unread data. Drop this session;
            # the next attempt inherits fresh settings and restores only USE.
            self._broken = True
            try:
                self._conn.close()
            except Exception:
                pass
            errno = int(getattr(exc, "errno", 0) or 0)
            return QueryResult(
                "ERROR", (), (), (), error=f"{type(exc).__name__}[{errno}]: {exc}",
                errno=errno, elapsed_ns=time.perf_counter_ns() - start,
                complete=False, warnings_complete=False,
            )
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception:
                    # Do not replace a fetch error with 'Unread result found'.
                    pass

    def execute_scalar_text(self, sql: str) -> str:
        r = self.execute(sql)
        if r.status != "OK" or not r.complete:
            return f"EXPLAIN-ERR: {r.error or 'incomplete result'}"
        return "\n".join(" ".join(str(c) for c in row if c is not None) for row in r.rows)

    def run_script(self, statements: list[str]) -> None:
        self._ensure_connected()
        cur = self._conn.cursor()
        try:
            for stmt in statements:
                if not stmt.strip():
                    continue
                cur.execute(stmt)
                if cur.description is not None:
                    # The default cursor returns tuples; dictionary cursors are never requested.
                    rows = cast(list[tuple[Any, ...]], cur.fetchall())
                    if stmt.lstrip().upper().startswith("ANALYZE TABLE"):
                        errors = [r for r in rows if len(r) >= 4 and str(r[2]).lower() == "error"]
                        if errors:
                            raise RuntimeError(f"ANALYZE failed: {errors}")
            if not self._conn.autocommit:
                self._conn.commit()
        finally:
            cur.close()

    def snapshot(self) -> dict[str, Any]:
        return {"host": self._endpoint.host, "port": self._endpoint.port,
                "database": self._database, "dop": self.dop, "expected_dop": self._dop,
                "reference": self._reference, "session": dict(self.session_values),
                "parameter_policy": "preconfigured_read_only",
                "setup_notes": list(self.setup_notes)}

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


def open_pair(config: PQConfig) -> tuple[PQConnection, PQConnection]:
    serial_endpoint = require_serial_endpoint(config)
    timeout_ms = max(1, int(config.query_timeout_seconds * 1000))
    pq = PQConnection(config.endpoint, dop=config.dop_on, query_timeout_ms=timeout_ms)
    try:
        seq = PQConnection(serial_endpoint, dop=config.dop_off, query_timeout_ms=timeout_ms)
    except BaseException:
        pq.close()
        raise
    return pq, seq


__all__ = ["PQConnection", "QueryResult", "open_pair"]
