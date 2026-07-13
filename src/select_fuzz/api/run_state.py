"""SQLite-backed durable run state."""

from __future__ import annotations

from datetime import UTC, datetime
import builtins
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import RLock
from uuid import uuid4

from select_fuzz.api.contracts import ReplayView, RunCreate, RunView


class IdempotencyConflict(ValueError):
    pass


class RunStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS run (
                id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
                fingerprint TEXT NOT NULL, state TEXT NOT NULL, request_json TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, version INTEGER NOT NULL,
                pid INTEGER, process_identity TEXT, exit_code INTEGER)"""
            )
            existing = {row[1] for row in db.execute("PRAGMA table_info(run)")}
            for name, sql_type in (
                ("pid", "INTEGER"), ("process_identity", "TEXT"), ("exit_code", "INTEGER")
            ):
                if name not in existing:
                    db.execute(f"ALTER TABLE run ADD COLUMN {name} {sql_type}")
            db.execute(
                """CREATE TABLE IF NOT EXISTS replay_job (
                id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
                fingerprint TEXT NOT NULL, case_id TEXT NOT NULL, state TEXT NOT NULL,
                result_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _payload(request: RunCreate) -> str:
        return json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _view(row: sqlite3.Row) -> RunView:
        return RunView(
            id=row["id"],
            state=row["state"],
            request=RunCreate.model_validate_json(row["request_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            version=row["version"],
            pid=row["pid"],
            process_identity=row["process_identity"],
            exit_code=row["exit_code"],
        )

    def create_once(self, request: RunCreate, idempotency_key: str) -> tuple[RunView, bool]:
        payload = self._payload(request)
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM run WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if row is not None:
                if row["fingerprint"] != fingerprint:
                    raise IdempotencyConflict(idempotency_key)
                return self._view(row), False
            run_id = "run-" + uuid4().hex
            db.execute(
                """INSERT INTO run
                (id,idempotency_key,fingerprint,state,request_json,created_at,updated_at,version)
                VALUES (?,?,?,?,?,?,?,?)""",
                (run_id, idempotency_key, fingerprint, "queued", payload, now, now, 0),
            )
            row = db.execute("SELECT * FROM run WHERE id=?", (run_id,)).fetchone()
            assert row is not None
            return self._view(row), True

    def get(self, run_id: str) -> RunView | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM run WHERE id=?", (run_id,)).fetchone()
        return None if row is None else self._view(row)

    def list(self, *, limit: int = 100, offset: int = 0) -> list[RunView]:
        if not 1 <= limit <= 201 or offset < 0:
            raise ValueError("invalid pagination")
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM run ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._view(row) for row in rows]

    def set_state(self, run_id: str, state: str) -> RunView | None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE run SET state=?, updated_at=?, version=version+1 WHERE id=?",
                (state, now, run_id),
            )
            row = db.execute("SELECT * FROM run WHERE id=?", (run_id,)).fetchone()
        return None if row is None else self._view(row)

    def set_process(
        self,
        run_id: str,
        *,
        pid: int | None,
        process_identity: str | None,
        state: str,
        exit_code: int | None = None,
    ) -> RunView | None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as db:
            db.execute(
                """UPDATE run SET state=?,pid=?,process_identity=?,exit_code=?,
                updated_at=?,version=version+1 WHERE id=?""",
                (state, pid, process_identity, exit_code, now, run_id),
            )
            row = db.execute("SELECT * FROM run WHERE id=?", (run_id,)).fetchone()
        return None if row is None else self._view(row)

    def active(self) -> builtins.list[RunView]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM run WHERE state IN ('starting','running','stopping','recovering')"
            ).fetchall()
        return [self._view(row) for row in rows]

    @staticmethod
    def _replay_view(row: sqlite3.Row) -> ReplayView:
        result = None if row["result_json"] is None else json.loads(row["result_json"])
        return ReplayView(
            id=row["id"], case_id=row["case_id"], state=row["state"],
            result=result, created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def create_replay_once(self, case_id: str, key: str) -> tuple[ReplayView, bool]:
        fingerprint = hashlib.sha256(case_id.encode()).hexdigest()
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM replay_job WHERE idempotency_key=?", (key,)
            ).fetchone()
            if row is not None:
                if row["fingerprint"] != fingerprint:
                    raise IdempotencyConflict(key)
                return self._replay_view(row), False
            replay_id = "replay-" + uuid4().hex
            db.execute(
                "INSERT INTO replay_job VALUES (?,?,?,?,?,?,?,?)",
                (replay_id, key, fingerprint, case_id, "queued", None, now, now),
            )
            row = db.execute("SELECT * FROM replay_job WHERE id=?", (replay_id,)).fetchone()
            assert row is not None
            return self._replay_view(row), True

    def get_replay(self, replay_id: str) -> ReplayView | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM replay_job WHERE id=?", (replay_id,)).fetchone()
        return None if row is None else self._replay_view(row)

    def set_replay(
        self, replay_id: str, state: str, result: dict[str, object] | None = None
    ) -> ReplayView | None:
        now = datetime.now(UTC).isoformat()
        payload = None if result is None else json.dumps(result, sort_keys=True, separators=(",", ":"))
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE replay_job SET state=?,result_json=?,updated_at=? WHERE id=?",
                (state, payload, now, replay_id),
            )
            row = db.execute("SELECT * FROM replay_job WHERE id=?", (replay_id,)).fetchone()
        return None if row is None else self._replay_view(row)

    def list_replays(self, *, limit: int = 20) -> builtins.list[ReplayView]:
        if not 1 <= limit <= 200:
            raise ValueError("invalid replay limit")
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM replay_job ORDER BY created_at DESC,id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._replay_view(row) for row in rows]
