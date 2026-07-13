"""Transactional checkpoint state with an append-only, fsynced event ledger."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any

from select_fuzz.validation.models import (
    EpochCheckpoint,
    FeatureSignature,
    GapRecord,
    Reachability,
    ReachabilityResult,
    SourceCandidate,
)


class LedgerCorruptionError(RuntimeError):
    pass


class ValidationLedger:
    """SQLite is authoritative; JSONL is an auditable append-only projection."""

    def __init__(self, db_path: Path, events_path: Path) -> None:
        self.db_path = db_path
        self.events_path = events_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        events_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()
        self._flush_outbox()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=30.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    def _migrate(self) -> None:
        with self._connect() as db, db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    version INTEGER NOT NULL
                );
                INSERT INTO schema_meta(version)
                    SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_meta);
                CREATE TABLE IF NOT EXISTS gaps (
                    signature_key TEXT PRIMARY KEY,
                    priority TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    discovered_at TEXT NOT NULL,
                    regression_completed INTEGER NOT NULL DEFAULT 0
                        CHECK (regression_completed IN (0, 1)),
                    resolved INTEGER NOT NULL DEFAULT 0
                        CHECK (resolved IN (0, 1))
                );
                CREATE TABLE IF NOT EXISTS signatures (
                    signature_key TEXT PRIMARY KEY,
                    first_run_id TEXT NOT NULL,
                    first_epoch INTEGER NOT NULL,
                    version TEXT,
                    nodes_json TEXT,
                    requirements_json TEXT,
                    source_sha256 TEXT
                );
                CREATE TABLE IF NOT EXISTS sources (
                    url TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    PRIMARY KEY(url, content_sha256)
                );
                CREATE TABLE IF NOT EXISTS audits (
                    run_id TEXT NOT NULL,
                    epoch INTEGER NOT NULL,
                    signature_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    witness_seed INTEGER,
                    witness_feature_id TEXT,
                    PRIMARY KEY(run_id, epoch, signature_key)
                );
                CREATE TABLE IF NOT EXISTS source_queue (
                    url TEXT PRIMARY KEY,
                    discovered_from TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS validation_errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    epoch INTEGER NOT NULL,
                    error_type TEXT NOT NULL,
                    message TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    run_id TEXT PRIMARY KEY,
                    epoch INTEGER NOT NULL,
                    source_cursor TEXT NOT NULL,
                    unique_signatures INTEGER NOT NULL,
                    gaps INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    elapsed_s REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS checkpoint_history (
                    run_id TEXT NOT NULL,
                    epoch INTEGER NOT NULL,
                    source_cursor TEXT NOT NULL,
                    unique_signatures INTEGER NOT NULL,
                    gaps INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    elapsed_s REAL NOT NULL,
                    PRIMARY KEY(run_id, epoch)
                );
                CREATE TABLE IF NOT EXISTS event_outbox (
                    event_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    emitted INTEGER NOT NULL DEFAULT 0
                        CHECK (emitted IN (0, 1))
                );
                """
            )
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(checkpoints)").fetchall()
            }
            if "elapsed_s" not in columns:
                db.execute(
                    "ALTER TABLE checkpoints ADD COLUMN elapsed_s REAL NOT NULL DEFAULT 0"
                )
            db.execute(
                """
                INSERT OR IGNORE INTO checkpoint_history(
                    run_id, epoch, source_cursor, unique_signatures, gaps,
                    updated_at, elapsed_s
                )
                SELECT run_id, epoch, source_cursor, unique_signatures, gaps,
                    updated_at, elapsed_s FROM checkpoints
                """
            )
            gap_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(gaps)").fetchall()
            }
            if "regression_completed" not in gap_columns:
                db.execute(
                    "ALTER TABLE gaps ADD COLUMN regression_completed "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            if "resolved" not in gap_columns:
                db.execute("ALTER TABLE gaps ADD COLUMN resolved INTEGER NOT NULL DEFAULT 0")
            signature_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(signatures)").fetchall()
            }
            for name, declaration in (
                ("version", "TEXT"),
                ("nodes_json", "TEXT"),
                ("requirements_json", "TEXT"),
                ("source_sha256", "TEXT"),
            ):
                if name not in signature_columns:
                    db.execute(f"ALTER TABLE signatures ADD COLUMN {name} {declaration}")

    def enqueue_source(self, url: str, *, discovered_from: str) -> bool:
        with self._connect() as db, db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO source_queue(url, discovered_from) VALUES (?, ?)",
                (url, discovered_from),
            ).rowcount
        return bool(inserted)

    def claim_source(self) -> tuple[str, int] | None:
        with self._connect() as db, db:
            row = db.execute(
                """
                SELECT url, attempts FROM source_queue
                WHERE status IN ('pending', 'retry')
                ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, rowid LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE source_queue SET status='claimed', attempts=attempts+1 WHERE url=?",
                (row["url"],),
            )
        return row["url"], int(row["attempts"]) + 1

    def queued_source_urls(self) -> tuple[str, ...]:
        with self._connect() as db:
            rows = db.execute("SELECT url FROM source_queue ORDER BY rowid").fetchall()
        return tuple(row["url"] for row in rows)

    def recover_claimed_sources(self) -> int:
        with self._connect() as db, db:
            updated = db.execute(
                "UPDATE source_queue SET status='retry' WHERE status='claimed'"
            ).rowcount
        return int(updated)

    def complete_source(self, url: str) -> None:
        with self._connect() as db, db:
            db.execute(
                "UPDATE source_queue SET status='complete', last_error=NULL WHERE url=?",
                (url,),
            )

    def retry_source(self, url: str, *, error: str) -> None:
        with self._connect() as db, db:
            db.execute(
                "UPDATE source_queue SET status='retry', last_error=? WHERE url=?",
                (error[:500], url),
            )

    def record_gap(self, gap: GapRecord) -> bool:
        event_id = f"gap:{gap.signature_key}:{gap.status.value}"
        payload = {
            "event_id": event_id,
            "type": "gap_recorded",
            "signature_key": gap.signature_key,
            "priority": gap.priority,
            "status": gap.status.value,
            "reasons": list(gap.reasons),
            "discovered_at": gap.discovered_at.isoformat(),
        }
        with self._connect() as db, db:
            existing = db.execute(
                "SELECT * FROM gaps WHERE signature_key = ?", (gap.signature_key,)
            ).fetchone()
            if existing is not None:
                current = Reachability(existing["status"])
                if current is Reachability.GAP and gap.status is Reachability.BLOCKED_EVIDENCE:
                    return False
                unchanged = (
                    current is gap.status
                    and existing["priority"] == gap.priority
                    and tuple(json.loads(existing["reasons_json"])) == gap.reasons
                    and not bool(existing["resolved"])
                )
                if unchanged:
                    return False
                updated = db.execute(
                    """
                    UPDATE gaps SET priority=?, status=?, reasons_json=?, discovered_at=?,
                        regression_completed=?, resolved=0 WHERE signature_key=?
                    """,
                    (
                        gap.priority,
                        gap.status.value,
                        json.dumps(gap.reasons, separators=(",", ":")),
                        gap.discovered_at.isoformat(),
                        int(gap.status is Reachability.BLOCKED_EVIDENCE),
                        gap.signature_key,
                    ),
                ).rowcount
                transition_id = f"gap-transition:{gap.signature_key}:{gap.status.value}"
                payload["event_id"] = transition_id
                payload["type"] = "gap_transitioned"
                self._enqueue(db, transition_id, payload)
                inserted = updated
            else:
                inserted = db.execute(
                """
                INSERT OR IGNORE INTO gaps(
                    signature_key, priority, status, reasons_json, discovered_at,
                    regression_completed
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    gap.signature_key,
                    gap.priority,
                    gap.status.value,
                    json.dumps(gap.reasons, separators=(",", ":")),
                    gap.discovered_at.isoformat(),
                    int(gap.status is Reachability.BLOCKED_EVIDENCE),
                ),
                ).rowcount
                if inserted:
                    self._enqueue(db, event_id, payload)
        self._flush_outbox()
        return bool(inserted)

    def record_signature(
        self,
        signature: FeatureSignature | str,
        *,
        run_id: str,
        epoch: int,
        source_sha256: str | None = None,
    ) -> bool:
        if epoch < 0:
            raise ValueError("epoch must be nonnegative")
        signature_key = signature.key if isinstance(signature, FeatureSignature) else signature
        with self._connect() as db, db:
            inserted = db.execute(
                """
                INSERT OR IGNORE INTO signatures(
                    signature_key, first_run_id, first_epoch, version, nodes_json,
                    requirements_json, source_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signature_key,
                    run_id,
                    epoch,
                    signature.version if isinstance(signature, FeatureSignature) else None,
                    json.dumps(signature.nodes) if isinstance(signature, FeatureSignature) else None,
                    json.dumps(signature.requirements)
                    if isinstance(signature, FeatureSignature)
                    else None,
                    source_sha256,
                ),
            ).rowcount
        return bool(inserted)

    def record_source(self, source: SourceCandidate) -> bool:
        with self._connect() as db, db:
            inserted = db.execute(
                """
                INSERT OR IGNORE INTO sources(url, content_sha256, fetched_at, media_type)
                VALUES (?, ?, ?, ?)
                """,
                (
                    source.url,
                    source.content_sha256,
                    source.fetched_at.isoformat(),
                    source.media_type,
                ),
            ).rowcount
        return bool(inserted)

    def list_sources(self) -> tuple[SourceCandidate, ...]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM sources ORDER BY url, content_sha256").fetchall()
        return tuple(
            SourceCandidate(
                row["url"],
                row["content_sha256"],
                datetime.fromisoformat(row["fetched_at"]),
                row["media_type"],
            )
            for row in rows
        )

    def list_signatures(self) -> tuple[FeatureSignature, ...]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM signatures WHERE version IS NOT NULL ORDER BY signature_key"
            ).fetchall()
        return tuple(
            FeatureSignature(
                row["version"],
                tuple(json.loads(row["nodes_json"])),
                tuple(json.loads(row["requirements_json"])),
            )
            for row in rows
        )

    def record_audit(self, result: ReachabilityResult, *, run_id: str, epoch: int) -> None:
        with self._connect() as db, db:
            db.execute(
                """
                INSERT INTO audits(
                    run_id, epoch, signature_key, status, reasons_json,
                    witness_seed, witness_feature_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, epoch, signature_key) DO UPDATE SET
                    status=excluded.status, reasons_json=excluded.reasons_json,
                    witness_seed=excluded.witness_seed,
                    witness_feature_id=excluded.witness_feature_id
                """,
                (
                    run_id,
                    epoch,
                    result.signature_key,
                    result.status.value,
                    json.dumps(result.reasons),
                    result.witness_seed,
                    result.witness_feature_id,
                ),
            )

    def list_audits(self) -> tuple[ReachabilityResult, ...]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT audit.* FROM audits AS audit
                JOIN (
                    SELECT signature_key, MAX(epoch) AS latest_epoch
                    FROM audits GROUP BY signature_key
                ) AS latest
                ON latest.signature_key = audit.signature_key
                AND latest.latest_epoch = audit.epoch
                ORDER BY audit.signature_key
                """
            ).fetchall()
        return tuple(
            ReachabilityResult(
                row["signature_key"],
                Reachability(row["status"]),
                tuple(json.loads(row["reasons_json"])),
                row["witness_seed"],
                row["witness_feature_id"],
            )
            for row in rows
        )

    def record_error(
        self, *, run_id: str, epoch: int, error_type: str, message: str
    ) -> None:
        sanitized = re.sub(
            r"(?i)(password|token|secret|key)=([^\s&]+)", r"\1=<redacted>", message
        )
        sanitized = re.sub(r"(https://[^\s?]+)\?[^\s]+", r"\1?<redacted>", sanitized)
        with self._connect() as db, db:
            db.execute(
                """
                INSERT INTO validation_errors(run_id, epoch, error_type, message)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, epoch, error_type[:100], sanitized[:1000]),
            )

    def list_errors(self) -> tuple[dict[str, object], ...]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT run_id, epoch, error_type, message FROM validation_errors ORDER BY id"
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def list_checkpoints(self, run_id: str) -> tuple[EpochCheckpoint, ...]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM checkpoint_history WHERE run_id=? ORDER BY epoch",
                (run_id,),
            ).fetchall()
        return tuple(self._checkpoint_from_row(row) for row in rows)

    def needs_regression(self, signature_key: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT regression_completed FROM gaps WHERE signature_key = ?",
                (signature_key,),
            ).fetchone()
        if row is None:
            raise KeyError(signature_key)
        return not bool(row["regression_completed"])

    def mark_regression_complete(self, signature_key: str) -> bool:
        event_id = f"regression-complete:{signature_key}"
        payload = {
            "event_id": event_id,
            "type": "regression_completed",
            "signature_key": signature_key,
        }
        with self._connect() as db, db:
            updated = db.execute(
                """
                UPDATE gaps SET regression_completed = 1, resolved = 1
                WHERE signature_key = ? AND regression_completed = 0
                """,
                (signature_key,),
            ).rowcount
            if updated:
                self._enqueue(db, event_id, payload)
        self._flush_outbox()
        return bool(updated)

    def resolve_gap(self, signature_key: str) -> bool:
        event_id = f"gap-resolved:{signature_key}"
        with self._connect() as db, db:
            updated = db.execute(
                """
                UPDATE gaps SET resolved=1, regression_completed=1
                WHERE signature_key=? AND resolved=0
                """,
                (signature_key,),
            ).rowcount
            if updated:
                self._enqueue(
                    db,
                    event_id,
                    {
                        "event_id": event_id,
                        "type": "gap_resolved",
                        "signature_key": signature_key,
                    },
                )
        self._flush_outbox()
        return bool(updated)

    def signature_count(self) -> int:
        with self._connect() as db:
            row = db.execute("SELECT COUNT(*) AS count FROM signatures").fetchone()
        return int(row["count"])

    def checkpoint(self, checkpoint: EpochCheckpoint) -> bool:
        event_id = f"checkpoint:{checkpoint.run_id}:{checkpoint.epoch}"
        payload = {
            "event_id": event_id,
            "type": "checkpoint",
            **{
                key: (value.isoformat() if isinstance(value, datetime) else value)
                for key, value in asdict(checkpoint).items()
            },
        }
        with self._connect() as db, db:
            existing = db.execute(
                "SELECT * FROM checkpoints WHERE run_id = ?", (checkpoint.run_id,)
            ).fetchone()
            if existing is not None:
                existing_checkpoint = self._checkpoint_from_row(existing)
                if checkpoint.epoch < existing_checkpoint.epoch:
                    raise ValueError("checkpoint epoch cannot move backwards")
                if checkpoint.epoch == existing_checkpoint.epoch:
                    if checkpoint != existing_checkpoint:
                        raise ValueError("same epoch cannot contain different checkpoint state")
                    return False
            db.execute(
                """
                INSERT INTO checkpoints(
                    run_id, epoch, source_cursor, unique_signatures, gaps, updated_at, elapsed_s
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    epoch=excluded.epoch,
                    source_cursor=excluded.source_cursor,
                    unique_signatures=excluded.unique_signatures,
                    gaps=excluded.gaps,
                    updated_at=excluded.updated_at,
                    elapsed_s=excluded.elapsed_s
                """,
                (
                    checkpoint.run_id,
                    checkpoint.epoch,
                    checkpoint.source_cursor,
                    checkpoint.unique_signatures,
                    checkpoint.gaps,
                    checkpoint.updated_at.isoformat(),
                    checkpoint.elapsed_s,
                ),
            )
            db.execute(
                """
                INSERT OR REPLACE INTO checkpoint_history(
                    run_id, epoch, source_cursor, unique_signatures, gaps,
                    updated_at, elapsed_s
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint.run_id,
                    checkpoint.epoch,
                    checkpoint.source_cursor,
                    checkpoint.unique_signatures,
                    checkpoint.gaps,
                    checkpoint.updated_at.isoformat(),
                    checkpoint.elapsed_s,
                ),
            )
            self._enqueue(db, event_id, payload)
        self._flush_outbox()
        return True

    def latest_checkpoint(self, run_id: str) -> EpochCheckpoint | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM checkpoints WHERE run_id = ?", (run_id,)
            ).fetchone()
        return None if row is None else self._checkpoint_from_row(row)

    def list_gaps(self) -> tuple[GapRecord, ...]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM gaps WHERE resolved = 0 ORDER BY signature_key"
            ).fetchall()
        return tuple(
            GapRecord(
                signature_key=row["signature_key"],
                priority=row["priority"],
                status=Reachability(row["status"]),
                reasons=tuple(json.loads(row["reasons_json"])),
                discovered_at=datetime.fromisoformat(row["discovered_at"]),
            )
            for row in rows
        )

    def iter_events(self) -> Iterator[dict[str, Any]]:
        if not self.events_path.exists():
            return
        lines = self.events_path.read_bytes().splitlines(keepends=True)
        seen: set[str] = set()
        index = 0
        while index < len(lines):
            raw_line = lines[index]
            try:
                event = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                if index == len(lines) - 1 and not raw_line.endswith(b"\n"):
                    break
                if index + 1 < len(lines):
                    try:
                        recovery = json.loads(lines[index + 1])
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        recovery = None
                    if isinstance(recovery, dict) and recovery.get("type") == "corrupt_tail":
                        index += 2
                        continue
                raise LedgerCorruptionError(f"invalid JSONL event at line {index + 1}") from exc
            event_id = event.get("event_id")
            if not isinstance(event_id, str):
                raise LedgerCorruptionError(f"event at line {index + 1} has no event_id")
            if event_id in seen:
                index += 1
                continue
            seen.add(event_id)
            yield event
            index += 1

    @staticmethod
    def _checkpoint_from_row(row: sqlite3.Row) -> EpochCheckpoint:
        return EpochCheckpoint(
            run_id=row["run_id"],
            epoch=row["epoch"],
            source_cursor=row["source_cursor"],
            unique_signatures=row["unique_signatures"],
            gaps=row["gaps"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
            elapsed_s=row["elapsed_s"],
        )

    @staticmethod
    def _enqueue(db: sqlite3.Connection, event_id: str, payload: dict[str, Any]) -> None:
        db.execute(
            "INSERT OR IGNORE INTO event_outbox(event_id, payload_json) VALUES (?, ?)",
            (event_id, json.dumps(payload, sort_keys=True, separators=(",", ":"))),
        )

    def _flush_outbox(self) -> None:
        with self._connect() as db:
            rows = db.execute(
                "SELECT event_id, payload_json FROM event_outbox WHERE emitted = 0 ORDER BY rowid"
            ).fetchall()
        for row in rows:
            self._append_fsync(row["payload_json"].encode() + b"\n")
            with self._connect() as db, db:
                db.execute(
                    "UPDATE event_outbox SET emitted = 1 WHERE event_id = ?",
                    (row["event_id"],),
                )

    def _append_fsync(self, payload: bytes) -> None:
        with self.events_path.open("a+b", buffering=0) as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                if size:
                    stream.seek(-1, os.SEEK_END)
                    terminated = stream.read(1) == b"\n"
                    stream.seek(0, os.SEEK_END)
                    if not terminated:
                        tail_start = max(0, size - 256)
                        stream.seek(tail_start)
                        tail_digest = sha256(stream.read()).hexdigest()
                        stream.seek(0, os.SEEK_END)
                        recovery = json.dumps(
                            {
                                "event_id": f"corrupt-tail:{size}:{tail_digest}",
                                "type": "corrupt_tail",
                                "offset": size,
                                "tail_sha256": tail_digest,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                        stream.write(b"\n" + recovery + b"\n")
                stream.write(payload)
                os.fsync(stream.fileno())
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


__all__ = ["LedgerCorruptionError", "ValidationLedger"]
