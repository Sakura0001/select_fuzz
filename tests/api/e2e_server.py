"""Credential-free real FastAPI fixture for Playwright."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile

import uvicorn

from select_fuzz.api.app import create_app
from select_fuzz.api.contracts import RunCreate
from select_fuzz.api.run_state import RunStore
from select_fuzz.api.supervisor import SubprocessSupervisor


class ReplayFixture:
    async def execute(self, case_id: str) -> dict[str, object]:
        return {"case_id": case_id, "status": "reproduced", "database": "e2e_replay"}


root = Path(tempfile.mkdtemp(prefix="select-fuzz-e2e-"))


class WorkerCommand:
    def build(self, run_id: str, request: RunCreate) -> tuple[str, ...]:
        del run_id, request
        return (sys.executable, "-c", "import time; time.sleep(60)")


app = create_app(
    state_path=root / "state.sqlite3",
    artifact_root=root / "artifacts",
    supervisor=SubprocessSupervisor(
        RunStore(root / "state.sqlite3"),
        WorkerCommand(),
        grace_seconds=0.2,
    ),
    replay_executor=ReplayFixture(),
)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
