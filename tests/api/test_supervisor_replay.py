from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import time

from select_fuzz.api.contracts import RunCreate, RunView
from select_fuzz.api.run_state import RunStore
from select_fuzz.api.replays import ProductionReplayExecutor
from select_fuzz.api.supervisor import SelectFuzzCommandBuilder, SubprocessSupervisor


class SleepCommand:
    def __init__(self, seconds: float, exit_code: int = 0) -> None:
        self.seconds = seconds
        self.exit_code = exit_code

    def build(self, run_id: str, request: RunCreate) -> tuple[str, ...]:
        del run_id, request
        return (
            sys.executable,
            "-c",
            f"import time; time.sleep({self.seconds}); raise SystemExit({self.exit_code})",
        )


class TestIdentity:
    def identity(self, pid: int) -> str:
        return f"test-process-{pid}"


def _created(store: RunStore, key: str = "subprocess-key") -> str:
    record, _ = store.create_once(RunCreate(mode="correctness"), key)
    return record.id


def test_real_subprocess_reaches_running_then_completed(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = RunStore(tmp_path / "state.sqlite3")
        run_id = _created(store)
        supervisor = SubprocessSupervisor(
            store, SleepCommand(0.05), grace_seconds=0.1, identity_probe=TestIdentity()
        )
        await supervisor.start(run_id, RunCreate(mode="correctness"))
        running = store.get(run_id)
        assert running is not None and running.state == "running"
        assert running.pid is not None and running.process_identity
        await supervisor.wait(run_id)
        completed = store.get(run_id)
        assert completed is not None and completed.state == "completed"
        assert completed.exit_code == 0

    asyncio.run(scenario())


def test_stop_escalates_to_kill_and_is_idempotent(tmp_path: Path) -> None:
    class IgnoreTerm:
        def build(self, run_id: str, request: RunCreate) -> tuple[str, ...]:
            del run_id, request
            return (
                sys.executable,
                "-c",
                "import signal,time; signal.signal(signal.SIGTERM, lambda *_: None); time.sleep(30)",
            )

    async def scenario() -> None:
        store = RunStore(tmp_path / "state.sqlite3")
        run_id = _created(store, "kill-subprocess")
        supervisor = SubprocessSupervisor(
            store, IgnoreTerm(), grace_seconds=0.02, identity_probe=TestIdentity()
        )
        await supervisor.start(run_id, RunCreate(mode="correctness"))
        await asyncio.sleep(0.05)
        one, two = await asyncio.gather(supervisor.stop(run_id), supervisor.stop(run_id))
        assert one.state == two.state == "stopped"
        assert store.get(run_id).exit_code is not None  # type: ignore[union-attr]

    asyncio.run(scenario())


def test_recovery_marks_identity_mismatch_orphaned(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = RunStore(tmp_path / "state.sqlite3")
        run_id = _created(store, "recover-subprocess")
        store.set_process(run_id, pid=999_999, process_identity="missing", state="running")
        supervisor = SubprocessSupervisor(store, SleepCommand(1), grace_seconds=0.01)
        await supervisor.recover()
        record = store.get(run_id)
        assert record is not None and record.state == "orphaned"

    asyncio.run(scenario())


def test_real_cli_command_builder_uses_supported_flags_without_credentials(tmp_path: Path) -> None:
    builder = SelectFuzzCommandBuilder(
        tmp_path / "config.yaml", tmp_path / "artifacts", executable=("select-fuzz",)
    )
    argv = builder.build(
        "run-7",
        RunCreate(
            mode="performance",
            workers=1,
            rounds=2,
            seed=9,
            timeout_seconds=15,
            degradation_ratio=0.25,
            data_rows_min=1234,
            data_rows_max=5678,
        ),
    )
    assert argv[:3] == ("select-fuzz", "run", "--mode")
    assert "--rounds" in argv and "--artifacts" in argv
    assert argv[argv.index("--timeout-seconds") + 1] == "15.0"
    assert argv[argv.index("--degradation-ratio") + 1] == "0.25"
    assert argv[argv.index("--data-rows-min") + 1] == "1234"
    assert argv[argv.index("--data-rows-max") + 1] == "5678"
    assert all("password" not in part.casefold() for part in argv)


def test_app_returns_supervisor_state_instead_of_forcing_running(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from select_fuzz.api.app import create_app

    class StartingSupervisor:
        def bind_store(self, store: RunStore) -> None:
            self.store = store

        async def start(self, run_id: str, request: RunCreate) -> RunView:
            del request
            record = self.store.set_state(run_id, "starting")
            assert record is not None
            return record

        async def stop(self, run_id: str) -> RunView:
            record = self.store.set_state(run_id, "stopped")
            assert record is not None
            return record

        async def recover(self) -> None:
            return None

    client = TestClient(
        create_app(
            state_path=tmp_path / "state.sqlite3",
            artifact_root=tmp_path / "artifacts",
            supervisor=StartingSupervisor(),
        ),
        base_url="http://127.0.0.1",
    )
    response = client.post(
        "/api/v1/runs",
        headers={"Idempotency-Key": "starting-state"},
        json={"mode": "correctness"},
    )
    assert response.status_code == 202
    assert response.json()["state"] == "starting"


class ImmediateReplay:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, case_id: str) -> dict[str, object]:
        self.calls.append(case_id)
        return {"case_id": case_id, "status": "reproduced", "database": "replay_db"}


def test_production_replay_executor_calls_sync_replay_service() -> None:
    class Service:
        def replay(self, case_id: str) -> dict[str, object]:
            return {"case_id": case_id, "status": "reproduced"}

    result = asyncio.run(ProductionReplayExecutor(Service()).execute("case-8"))
    assert result == {"case_id": "case-8", "status": "reproduced"}


def test_post_replay_is_durable_and_idempotent(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from select_fuzz.api.app import create_app
    from select_fuzz.api.supervisor import InMemoryProcessSupervisor

    executor = ImmediateReplay()
    app = create_app(
        state_path=tmp_path / "state.sqlite3",
        artifact_root=tmp_path / "artifacts",
        supervisor=InMemoryProcessSupervisor(),
        replay_executor=executor,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"Idempotency-Key": "replay-case-7"}
        first = client.post("/api/v1/replays", headers=headers, json={"case_id": "case-7"})
        second = client.post("/api/v1/replays", headers=headers, json={"case_id": "case-7"})
        assert first.status_code == second.status_code == 202
        assert first.json()["id"] == second.json()["id"]
        replay_id = first.json()["id"]
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            detail = client.get(f"/api/v1/replays/jobs/{replay_id}").json()
            if detail["state"] == "reproduced":
                break
            time.sleep(0.01)
        assert detail["result"]["database"] == "replay_db"
        assert executor.calls == ["case-7"]
