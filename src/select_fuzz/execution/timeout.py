"""Race-safe query watchdog using an independent MySQL control connection."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from heapq import heapify, heappop, heappush
from itertools import count
from threading import Condition, Event, Lock, Thread

from select_fuzz.config import NodeConfig
from select_fuzz.execution.protocols import ControlConnectionFactory


_DEADLINE_COMPACT_INTERVAL = 256


class _DeadlineScheduler:
    """One lazy daemon schedules normal statement deadlines process-wide."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._deadlines: list[tuple[float, int, KillHandle]] = []
        self._sequence = count()
        self._thread: Thread | None = None
        self._stale_notifications = 0

    def schedule(self, handle: KillHandle, deadline: float) -> None:
        with self._condition:
            if self._thread is None or not self._thread.is_alive():
                self._thread = Thread(
                    target=self._run,
                    name="select-fuzz-deadline-scheduler",
                    daemon=True,
                )
                self._thread.start()
            heappush(self._deadlines, (deadline, next(self._sequence), handle))
            self._condition.notify()

    def notify(self) -> None:
        with self._condition:
            self._stale_notifications += 1
            if self._stale_notifications >= _DEADLINE_COMPACT_INTERVAL:
                self._deadlines = [
                    item for item in self._deadlines if item[2]._is_waiting()
                ]
                heapify(self._deadlines)
                self._stale_notifications = 0
            self._condition.notify()

    def _run(self) -> None:
        while True:
            due: KillHandle | None = None
            with self._condition:
                while due is None:
                    while self._deadlines and not self._deadlines[0][2]._is_waiting():
                        heappop(self._deadlines)
                    if not self._deadlines:
                        self._condition.wait()
                        continue
                    deadline, _sequence, handle = self._deadlines[0]
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        self._condition.wait(remaining)
                        continue
                    heappop(self._deadlines)
                    if handle._claim_timeout():
                        due = handle
            try:
                due._start_action()
            except BaseException:
                # One action thread failing to start must not disable deadlines
                # for every other statement in this process. The handle records
                # its own diagnostic and completion state in _start_action().
                continue


_DEADLINE_SCHEDULER = _DeadlineScheduler()


class KillHandle:
    """One centrally scheduled deadline with action threads only when fired."""

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
        scheduler: _DeadlineScheduler,
    ) -> None:
        self._factory = factory
        self._node = node
        self._database = database
        self._connection_id = connection_id
        self._statement_token = statement_token
        self._fallback_abort = fallback_abort
        self._statement_done = statement_done
        self._kill_grace_s = kill_grace_s
        self._lock = Lock()
        self._action = "waiting"
        self._timed_out = False
        self._fired = False
        self._kill_error_type: str | None = None
        self._kill_finished = Event()
        self._completed = Event()
        self._scheduler = scheduler
        scheduler.schedule(self, time.monotonic() + timeout_s)

    def _require_token(self, statement_token: object | None) -> None:
        if statement_token is not None and statement_token is not self._statement_token:
            raise ValueError("statement token does not own this watchdog handle")

    def _is_waiting(self) -> bool:
        with self._lock:
            return self._action == "waiting"

    def _claim_timeout(self) -> bool:
        with self._lock:
            if self._action != "waiting":
                return False
            self._action = "timeout_kill"
            self._timed_out = True
            self._fired = True
            return True

    def _start_action(self) -> None:
        try:
            thread = Thread(
                target=self._perform_action,
                name=(
                    f"select-fuzz-kill-{self._node.role.value}-"
                    f"{self._connection_id}"
                ),
                daemon=True,
            )
            thread.start()
        except BaseException as error:
            with self._lock:
                self._kill_error_type = type(error).__name__
            try:
                if self._fallback_abort is not None:
                    self._abort_connection()
            finally:
                self._completed.set()
            raise

    def _perform_action(self) -> None:
        try:
            with self._lock:
                manual_kill = self._action == "manual_kill"
            kill_thread: Thread | None = None
            try:
                candidate = Thread(
                    target=self._kill_query,
                    name=f"select-fuzz-kill-control-{self._connection_id}",
                    daemon=True,
                )
                candidate.start()
                kill_thread = candidate
            except BaseException as error:
                with self._lock:
                    self._kill_error_type = type(error).__name__
                self._kill_finished.set()
            if self._fallback_abort is not None:
                should_abort = manual_kill
                if not manual_kill:
                    deadline = time.monotonic() + self._kill_grace_s
                    while True:
                        if (
                            self._statement_done is not None
                            and self._statement_done.is_set()
                        ):
                            break
                        with self._lock:
                            kill_failed = (
                                self._kill_finished.is_set()
                                and self._kill_error_type is not None
                            )
                        if kill_failed or time.monotonic() >= deadline:
                            should_abort = True
                            break
                        if self._statement_done is None:
                            self._kill_finished.wait(
                                min(0.01, max(0.0, deadline - time.monotonic()))
                            )
                        else:
                            self._statement_done.wait(
                                min(0.01, max(0.0, deadline - time.monotonic()))
                            )
                if should_abort:
                    if not self._abort_connection():
                        self._kill_connection()
            # Do not permit connection reuse until an in-flight KILL has completed:
            # a delayed KILL QUERY could otherwise target the next statement on the
            # same connection ID.
            if kill_thread is not None:
                kill_thread.join()
        finally:
            self._completed.set()

    def _kill_query(self) -> None:
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
        finally:
            self._kill_finished.set()

    def _abort_connection(self) -> bool:
        assert self._fallback_abort is not None
        try:
            self._fallback_abort()
            return True
        except Exception as error:
            with self._lock:
                if self._kill_error_type is None:
                    self._kill_error_type = type(error).__name__
            return False

    def _kill_connection(self) -> None:
        try:
            with self._factory.control_session(self._node, self._database) as session:
                cursor = session.execute(f"KILL CONNECTION {self._connection_id}")
                try:
                    cursor.fetchmany(1)
                finally:
                    cursor.close()
        except Exception as error:
            with self._lock:
                if self._kill_error_type is None:
                    self._kill_error_type = type(error).__name__

    def cancel(self, *, statement_token: object | None = None) -> None:
        """Cancel the deadline and wait for any already-fired KILL."""

        self._require_token(statement_token)
        cancelled = False
        with self._lock:
            if self._action == "waiting":
                self._action = "cancelled"
                self._completed.set()
                cancelled = True
        if cancelled:
            self._scheduler.notify()
        self._completed.wait()

    def trigger(self, *, statement_token: object | None = None) -> None:
        """Abort now for a non-timeout reason such as a result-size ceiling."""

        self._require_token(statement_token)
        should_start = False
        with self._lock:
            if self._action == "waiting":
                self._action = "manual_kill"
                self._fired = True
                should_start = True
        if should_start:
            self._scheduler.notify()
            self._start_action()
        self._completed.wait()

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
        return not self._completed.is_set()


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
            _DEADLINE_SCHEDULER,
        )


__all__ = ["KillHandle", "KillQueryWatchdog"]
