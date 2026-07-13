"""Disposable SQLite projection rebuilt from authoritative JSONL facts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3


class ReadIndex:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @staticmethod
    def _schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """CREATE TABLE finding (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, mode TEXT NOT NULL,
            severity TEXT NOT NULL, node TEXT, feature TEXT, errno INTEGER,
            occurred_at TEXT NOT NULL, summary_json TEXT NOT NULL, sequence INTEGER NOT NULL);
            CREATE INDEX finding_filters ON finding(mode,severity,node,feature,errno,occurred_at DESC);
            CREATE TABLE projection_meta(name TEXT PRIMARY KEY,value TEXT NOT NULL);"""
        )

    @staticmethod
    def _project(db: sqlite3.Connection, fact: object) -> tuple[int, int]:
        if not isinstance(fact, dict):
            return 0, 0
        sequence = fact.get("sequence")
        watermark = sequence if isinstance(sequence, int) else 0
        kind = fact.get("kind")
        fact_type = fact.get("type")
        payload = fact.get("payload")
        if kind != "finding.created" and fact_type not in {
            "finding",
            "performance_alert",
            "performance_calibration_failure",
        }:
            return 0, watermark
        if fact_type in {
            "finding",
            "performance_alert",
            "performance_calibration_failure",
        }:
            is_performance = fact_type in {
                "performance_alert",
                "performance_calibration_failure",
            }
            payload = {
                "id": fact.get("case_id"),
                "run_id": fact.get("run_id", ""),
                "mode": fact.get(
                    "mode", "performance" if is_performance else "correctness"
                ),
                "severity": fact.get(
                    "original_verdict",
                    fact.get(
                        "failure_category",
                        "perf_alert" if fact_type == "performance_alert" else "finding",
                    )
                    or "calibration_failure",
                ),
                "occurred_at": fact.get("occurred_at", ""),
                "record": fact,
            }
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
            return 0, watermark
        db.execute(
            "INSERT OR REPLACE INTO finding VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                payload["id"], payload.get("run_id", ""), payload.get("mode", ""),
                payload.get("severity", "unknown"), payload.get("node"),
                payload.get("feature"), payload.get("errno"), payload.get("occurred_at", ""),
                json.dumps(payload, sort_keys=True, separators=(",", ":")), watermark,
            ),
        )
        return 1, watermark

    def rebuild(self, facts_path: str | Path) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".new")
        temporary.unlink(missing_ok=True)
        count = 0
        watermark = 0
        source_offset = 0
        with sqlite3.connect(temporary) as db:
            self._schema(db)
            source = Path(facts_path)
            if source.exists():
                with source.open("rb") as stream:
                    while True:
                        raw = stream.readline()
                        if not raw:
                            break
                        if not raw.endswith(b"\n"):
                            break
                        try:
                            fact = json.loads(raw)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            break
                        projected, sequence = self._project(db, fact)
                        count += projected
                        watermark = max(watermark, sequence)
                        source_offset = stream.tell()
            db.execute("INSERT INTO projection_meta VALUES ('watermark',?)", (str(watermark),))
            db.execute(
                "INSERT INTO projection_meta VALUES ('source_offset',?)", (str(source_offset),)
            )
            db.commit()
        descriptor = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, self.path)
        return count

    def refresh(self, facts_path: str | Path) -> int:
        source = Path(facts_path)
        if not self.path.exists():
            return self.rebuild(source)
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT value FROM projection_meta WHERE name='source_offset'"
            ).fetchone()
            if row is None:
                return self.rebuild(source)
            offset = int(row[0])
            if not source.exists():
                return 0 if offset == 0 else self.rebuild(source)
            if source.stat().st_size < offset:
                return self.rebuild(source)
            watermark_row = db.execute(
                "SELECT value FROM projection_meta WHERE name='watermark'"
            ).fetchone()
            watermark = 0 if watermark_row is None else int(watermark_row[0])
            count = 0
            committed_offset = offset
            with source.open("rb") as stream:
                stream.seek(offset)
                while True:
                    raw = stream.readline()
                    if not raw or not raw.endswith(b"\n"):
                        break
                    try:
                        fact = json.loads(raw)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        break
                    projected, sequence = self._project(db, fact)
                    count += projected
                    watermark = max(watermark, sequence)
                    committed_offset = stream.tell()
            db.execute(
                "INSERT OR REPLACE INTO projection_meta VALUES ('watermark',?)",
                (str(watermark),),
            )
            db.execute(
                "INSERT OR REPLACE INTO projection_meta VALUES ('source_offset',?)",
                (str(committed_offset),),
            )
            db.commit()
            return count

    def list_findings(
        self,
        *,
        mode: str | None = None,
        severity: str | None = None,
        node: str | None = None,
        feature: str | None = None,
        errno: int | None = None,
        query: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, object]]:
        if not 1 <= limit <= 201 or offset < 0:
            raise ValueError("invalid finding pagination")
        clauses: list[str] = []
        values: list[object] = []
        for column, value in (
            ("mode", mode), ("severity", severity), ("node", node),
            ("feature", feature), ("errno", errno),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                values.append(value)
        if query is not None:
            clauses.append("summary_json LIKE ?")
            values.append("%" + query.replace("%", "\\%").replace("_", "\\_") + "%")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                "SELECT summary_json FROM finding" + where
                + " ORDER BY occurred_at DESC,id DESC LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def get_finding(self, finding_id: str) -> dict[str, object] | None:
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT summary_json FROM finding WHERE id=?", (finding_id,)).fetchone()
        return None if row is None else json.loads(row[0])
