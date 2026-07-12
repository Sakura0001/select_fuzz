"""Race-safe query watchdog using an independent MySQL control connection."""

from __future__ import annotations

import math
from collections.abc import Callable
from threading import Event, Lock, Thread

from select_fuzz.config import NodeConfig
from select_fuzz.execution.protocols import ControlConnectionFactory


class KillHandle:
    """A single statement deadline whose worker is always joined before reuse."""

    def __init__(
        self,
        factory: ControlConnectionFactory,
        node: NodeConfig,
        database: str,
        connection_id: int,
        timeout_s: float,
        statement_token: object,
        fallback_abort: Callable[[], None] | None,
        statement_done: Event | None,
        kill_grace_s: float,
    ) -> None:
        self._factory = factory
        self._node = node
        self._database = database
        self._connection_id = connection_id
        self._timeout_s = timeout_s
        self._statement_token = statement_token
        self._fallback_abort = fallback_abort
        self._statement_done = statement_done
        self._kill_grace_s = kill_grace_s
        self._wake = Event()
        self._lock = Lock()
        self._action = "waiting"
        self._timed_out = False
        self._fired = False
        self._kill_error_type: str | None = None
        self._thread = Thread(
            target=self._worker,
            name=f"select-fuzz-kill-{node.role.value}-{connection_id}",
            daemon=True,
        )
        self._thread.start()

    def _require_token(self, statement_token: object | None) -> None:
        if statement_token is not None and statement_token is not self._statement_token:
            raise ValueError("statement token does not own this watchdog handle")

    def _worker(self) -> None:
        woke = self._wake.wait(self._timeout_s)
        with self._lock:
            if self._action == "cancelled":
                return
            if self._action == "manual_kill":
                pass
            elif not woke and self._action == "waiting":
                self._action = "timeout_kill"
                self._timed_out = True
            else:  # pragma: no cover - defensive state invariant
                return
            self._fired = True
        kill_failed = False
        try:
            with self._factory.control_session(self._node, self._database) as session:
                cursor = session.execute(f"KILL QUERY {self._connection_id}")
                try:
                    cursor.fetchmany(1)
                finally:
                    cursor.close()
        except Exception as error:  # connector failures are diagnostics, not thread crashes
            with self._lock:
                self._kill_error_type = type(error).__name__
            kill_failed = True
        if self._fallback_abort is None:
            return
        should_abort = kill_failed or self._action == "manual_kill"
        if (
            not should_abort
            and self._statement_done is not None
            and not self._statement_done.wait(self._kill_grace_s)
        ):
            should_abort = True
        if should_abort:
            try:
                self._fallback_abort()
            except Exception as error:
                with self._lock:
                    if self._kill_error_type is None:
                        self._kill_error_type = type(error).__name__

    def cancel(self, *, statement_token: object | None = None) -> None:
        """Cancel the deadline and synchronously join any in-flight KILL."""

        self._require_token(statement_token)
        with self._lock:
            if self._action == "waiting":
                self._action = "cancelled"
                self._wake.set()
        self._thread.join()

    def trigger(self, *, statement_token: object | None = None) -> None:
        """Abort now for a non-timeout reason such as a result-size ceiling."""

        self._require_token(statement_token)
        with self._lock:
            if self._action == "waiting":
                self._action = "manual_kill"
                self._wake.set()
        self._thread.join()

    @property
    def timed_out(self) -> bool:
        with self._lock:
            return self._timed_out

    @property
    def fired(self) -> bool:
        with self._lock:
            return self._fired

    @property
    def kill_error_type(self) -> str | None:
        with self._lock:
            return self._kill_error_type

    @property
    def thread_alive(self) -> bool:
        return self._thread.is_alive()


class KillQueryWatchdog:
    """Factory for one-shot handles; SQL interpolation is limited to a checked int."""

    def __init__(
        self,
        factory: ControlConnectionFactory,
        *,
        kill_grace_s: float = 1.0,
    ) -> None:
        if (
            not isinstance(kill_grace_s, (int, float))
            or isinstance(kill_grace_s, bool)
            or not math.isfinite(kill_grace_s)
            or kill_grace_s <= 0
        ):
            raise ValueError("kill_grace_s must be a finite positive number")
        self._factory = factory
        self._kill_grace_s = float(kill_grace_s)

    def arm(
        self,
        node: NodeConfig,
        database: str,
        connection_id: int,
        timeout_s: float,
        *,
        statement_token: object | None = None,
        fallback_abort: Callable[[], None] | None = None,
        statement_done: Event | None = None,
    ) -> KillHandle:
        if (
            not isinstance(connection_id, int)
            or isinstance(connection_id, bool)
            or connection_id <= 0
        ):
            raise ValueError("connection_id must be a positive integer")
        if (
            not isinstance(timeout_s, (int, float))
            or isinstance(timeout_s, bool)
            or not math.isfinite(timeout_s)
            or timeout_s <= 0
        ):
            raise ValueError("timeout_s must be a finite positive number")
        if not isinstance(database, str) or not database:
            raise ValueError("database must not be empty")
        token = object() if statement_token is None else statement_token
        return KillHandle(
            self._factory,
            node,
            database,
            connection_id,
            float(timeout_s),
            token,
            fallback_abort,
            statement_done,
            self._kill_grace_s,
        )


__all__ = ["KillHandle", "KillQueryWatchdog"]
