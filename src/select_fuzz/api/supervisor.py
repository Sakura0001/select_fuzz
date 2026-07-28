"""Injectable process supervision with durable identity and restart recovery."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable, Sequence
import hashlib
import os
import signal
import subprocess
from pathlib import Path
import sys
from typing import Protocol

from select_fuzz.api.contracts import RunCreate, RunView
from select_fuzz.api.run_state import RunStore


class CommandBuilder(Protocol):
    def build(self, run_id: str, request: RunCreate) -> Sequence[str]: ...


class SelectFuzzCommandBuilder:
    """Build argv for the real CLI without copying credentials into process arguments."""

    def __init__(
        self,
        config_path: str | Path,
        artifact_root: str | Path,
        *,
        executable: Sequence[str] = (sys.executable, "-m", "select_fuzz"),
    ) -> None:
        self._config = str(Path(config_path).resolve())
        self._artifacts = Path(artifact_root).resolve()
        self._executable = tuple(executable)

    def build(self, run_id: str, request: RunCreate) -> tuple[str, ...]:
        argv = [
            *self._executable, "run", "--mode", request.mode, "--config", self._config,
            "--seed", str(request.seed), "--workers", str(request.workers),
            "--queries-per-round", str(request.queries_per_round),
            "--timeout-seconds", str(request.timeout_seconds),
            "--degradation-ratio", str(request.degradation_ratio),
            "--data-rows-min", str(request.data_rows_min),
            "--data-rows-max", str(request.data_rows_max),
            "--artifacts", str(self._artifacts / run_id),
        ]
        if request.rounds is not None:
            argv.extend(("--rounds", str(request.rounds)))
        if request.duration_seconds is not None:
            argv.extend(("--duration-seconds", str(request.duration_seconds)))
        if request.mode == "fuzz":
            argv.extend(
                (
                    "--databases",
                    str(request.databases),
                    "--writer-threads-per-database",
                    str(request.writer_threads_per_database),
                    "--reader-threads-per-database",
                    str(request.reader_threads_per_database),
                )
            )
        return tuple(argv)


class ProcessSupervisor(Protocol):
    def bind_store(self, store: RunStore) -> None: ...

    def bind_event_publisher(
        self, publisher: Callable[[str, dict[str, object]], object]
    ) -> None: ...

    async def start(self, run_id: str, request: RunCreate) -> RunView: ...

    async def stop(self, run_id: str) -> RunView: ...

    async def recover(self) -> None: ...


class ProcessIdentityProbe:
    """Fingerprint a PID without persisting command arguments or secrets."""

    def identity(self, pid: int) -> str | None:
        try:
            completed = subprocess.run(
                ("/bin/ps", "-o", "lstart=,command=", "-p", str(pid)),
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        value = completed.stdout.strip()
        if completed.returncode != 0 or not value:
            return None
        return hashlib.sha256(f"{pid}:{value}".encode()).hexdigest()


class _ProcessHandle(Protocol):
    pid: int

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


class _RecoveredHandle:
    def __init__(self, pid: int, poll_seconds: float = 0.05) -> None:
        self.pid = pid
        self._poll_seconds = poll_seconds

    def terminate(self) -> None:
        os.kill(self.pid, signal.SIGTERM)

    def kill(self) -> None:
        os.kill(self.pid, signal.SIGKILL)

    async def wait(self) -> int:
        while True:
            try:
                os.kill(self.pid, 0)
            except ProcessLookupError:
                return -1
            await asyncio.sleep(self._poll_seconds)


class SubprocessSupervisor:
    def __init__(
        self,
        store: RunStore,
        commands: CommandBuilder,
        *,
        grace_seconds: float = 5.0,
        identity_probe: ProcessIdentityProbe | None = None,
    ) -> None:
        if grace_seconds <= 0:
            raise ValueError("grace_seconds must be positive")
        self._store = store
        self._commands = commands
        self._grace_seconds = grace_seconds
        self._identity = identity_probe or ProcessIdentityProbe()
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._handles: dict[str, _ProcessHandle] = {}
        self._watchers: dict[str, asyncio.Task[None]] = {}
        self._publish_event: Callable[[str, dict[str, object]], object] | None = None

    def bind_store(self, store: RunStore) -> None:
        self._store = store

    def bind_event_publisher(
        self, publisher: Callable[[str, dict[str, object]], object]
    ) -> None:
        self._publish_event = publisher

    def _publish(self, record: RunView) -> None:
        if self._publish_event is not None:
            self._publish_event("run.state", {"run_id": record.id, "state": record.state})

    async def start(self, run_id: str, request: RunCreate) -> RunView:
        async with self._locks[run_id]:
            record = self._store.get(run_id)
            if record is None:
                raise KeyError(run_id)
            if record.state != "queued":
                return record
            starting = self._store.set_state(run_id, "starting")
            assert starting is not None
            self._publish(starting)
            command = tuple(self._commands.build(run_id, request))
            if not command or any(not isinstance(part, str) or not part for part in command):
                failed = self._store.set_state(run_id, "failed")
                assert failed is not None
                self._publish(failed)
                raise ValueError("command builder returned an invalid argv")
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception:
                failed = self._store.set_state(run_id, "failed")
                assert failed is not None
                self._publish(failed)
                raise
            identity = await asyncio.to_thread(self._identity.identity, process.pid)
            if identity is None:
                process.kill()
                await process.wait()
                failed = self._store.set_process(
                    run_id, pid=process.pid, process_identity=None, state="failed",
                    exit_code=process.returncode,
                )
                assert failed is not None
                self._publish(failed)
                raise RuntimeError("spawned worker identity could not be verified")
            self._handles[run_id] = process
            running = self._store.set_process(
                run_id, pid=process.pid, process_identity=identity, state="running"
            )
            assert running is not None
            self._publish(running)
            self._watchers[run_id] = asyncio.create_task(self._watch(run_id, process))
            return running

    async def _watch(self, run_id: str, handle: _ProcessHandle) -> None:
        try:
            exit_code = await handle.wait()
            async with self._locks[run_id]:
                record = self._store.get(run_id)
                if record is None or record.state in {
                    "stopped", "completed", "failed", "orphaned"
                }:
                    return
                state = "stopped" if record.state == "stopping" else (
                    "completed" if exit_code == 0 else "failed"
                )
                finished = self._store.set_process(
                    run_id, pid=record.pid, process_identity=record.process_identity,
                    state=state, exit_code=exit_code,
                )
                assert finished is not None
                self._publish(finished)
        finally:
            self._handles.pop(run_id, None)
            if self._watchers.get(run_id) is asyncio.current_task():
                self._watchers.pop(run_id, None)

    async def wait(self, run_id: str) -> None:
        watcher = self._watchers.get(run_id)
        if watcher is not None:
            await watcher

    async def stop(self, run_id: str) -> RunView:
        async with self._locks[run_id]:
            record = self._store.get(run_id)
            if record is None:
                raise KeyError(run_id)
            if record.state in {"stopped", "completed", "failed", "orphaned"}:
                return record
            stopping = self._store.set_state(run_id, "stopping")
            assert stopping is not None
            self._publish(stopping)
            handle = self._handles.get(run_id)
            if handle is None:
                orphaned = self._store.set_state(run_id, "orphaned")
                assert orphaned is not None
                self._publish(orphaned)
                return orphaned
            try:
                handle.terminate()
            except ProcessLookupError:
                pass
            try:
                exit_code = await asyncio.wait_for(handle.wait(), self._grace_seconds)
            except TimeoutError:
                try:
                    handle.kill()
                except ProcessLookupError:
                    pass
                exit_code = await handle.wait()
            stopped = self._store.set_process(
                run_id, pid=record.pid, process_identity=record.process_identity,
                state="stopped", exit_code=exit_code,
            )
            assert stopped is not None
            self._publish(stopped)
            return stopped

    async def recover(self) -> None:
        resume_stops: list[str] = []
        for record in self._store.active():
            async with self._locks[record.id]:
                was_stopping = record.state == "stopping"
                recovering = self._store.set_state(record.id, "recovering")
                assert recovering is not None
                self._publish(recovering)
                identity = (
                    None if record.pid is None
                    else await asyncio.to_thread(self._identity.identity, record.pid)
                )
                if identity is None or identity != record.process_identity or record.pid is None:
                    orphaned = self._store.set_state(record.id, "orphaned")
                    assert orphaned is not None
                    self._publish(orphaned)
                    continue
                handle = _RecoveredHandle(record.pid)
                self._handles[record.id] = handle
                running = self._store.set_process(
                    record.id, pid=record.pid, process_identity=identity, state="running"
                )
                assert running is not None
                self._publish(running)
                self._watchers[record.id] = asyncio.create_task(self._watch(record.id, handle))
                if was_stopping:
                    resume_stops.append(record.id)
        for run_id in resume_stops:
            await self.stop(run_id)


class InMemoryProcessSupervisor:
    """Deterministic fake; production composition must use SubprocessSupervisor."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.stopped: list[str] = []
        self._store: RunStore | None = None
        self._publish_event: Callable[[str, dict[str, object]], object] | None = None

    def bind_store(self, store: RunStore) -> None:
        self._store = store

    def bind_event_publisher(
        self, publisher: Callable[[str, dict[str, object]], object]
    ) -> None:
        self._publish_event = publisher

    async def start(self, run_id: str, request: RunCreate) -> RunView:
        del request
        self.started.append(run_id)
        assert self._store is not None
        record = self._store.set_state(run_id, "running")
        assert record is not None
        if self._publish_event is not None:
            self._publish_event("run.state", {"run_id": run_id, "state": "running"})
        return record

    async def stop(self, run_id: str) -> RunView:
        if run_id not in self.stopped:
            self.stopped.append(run_id)
        assert self._store is not None
        record = self._store.set_state(run_id, "stopped")
        assert record is not None
        if self._publish_event is not None:
            self._publish_event("run.state", {"run_id": run_id, "state": "stopped"})
        return record

    async def recover(self) -> None:
        return None
