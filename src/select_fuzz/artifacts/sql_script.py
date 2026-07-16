"""Source-able SQL artifacts and append-only per-worker SQL audit logs."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import fcntl
import json
import math
import os
from pathlib import Path
import re
from threading import Lock
from uuid import uuid4

from select_fuzz.artifacts.jsonl import assert_no_sensitive_keys
from select_fuzz.execution.setup import validate_database_name


MAX_DIFF_ROWS = 100
MAX_DIFF_BYTES = 64 * 1024
_SAFE_DELIMITER = re.compile(r"^[!#$%&*+\-./:<=>?@^_|~]{1,16}$")
_METADATA_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_DELIMITER_DIRECTIVE = re.compile(r"(?im)^\s*DELIMITER(?:\s|$)")
_ROUTINE_START = re.compile(
    r"(?is)^\s*CREATE\s+(?:DEFINER\s*=\s*\S+\s+)?"
    r"(?:PROCEDURE|FUNCTION|TRIGGER|EVENT)\b"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

DEFAULT_SESSION_STATEMENTS = (
    "SET NAMES utf8mb4",
    "SET SESSION time_zone = '+00:00'",
)


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:  # pragma: no cover - defensive OS contract check
            raise OSError("SQL artifact write returned no progress")
        offset += written


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_file(
    path: Path,
    payload: bytes,
    *,
    fsync: Callable[[int], None],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        0o600,
    )
    try:
        _write_all(descriptor, payload)
        fsync(descriptor)
    except BaseException:
        try:
            os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _append_file(
    path: Path,
    payload: bytes,
    *,
    fsync: Callable[[int], None],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    descriptor = os.open(
        path,
        os.O_APPEND | os.O_CREAT | os.O_WRONLY,
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        _write_all(descriptor, payload)
        fsync(descriptor)
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
    if not existed:
        _fsync_directory(path.parent)


def _validate_sql(sql: object) -> str:
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("SQL must be a nonempty string")
    if "\x00" in sql:
        raise ValueError("SQL cannot contain NUL bytes")
    return sql.strip()


def _last_sql_token(sql: str) -> tuple[str | None, bool]:
    """Return the final token outside comments/quotes and final line-comment state."""

    state = "normal"
    last: str | None = None
    index = 0
    while index < len(sql):
        char = sql[index]
        following = sql[index + 1] if index + 1 < len(sql) else ""
        if state == "line_comment":
            if char in "\r\n":
                state = "normal"
            index += 1
            continue
        if state == "block_comment":
            if char == "*" and following == "/":
                state = "normal"
                index += 2
            else:
                index += 1
            continue
        if state in {"single_quote", "double_quote", "backtick"}:
            terminator = {
                "single_quote": "'",
                "double_quote": '"',
                "backtick": "`",
            }[state]
            if char == "\\" and state != "backtick" and following:
                index += 2
                continue
            if char == terminator:
                if following == terminator:
                    index += 2
                    continue
                last = char
                state = "normal"
            index += 1
            continue
        if char == "#":
            state = "line_comment"
            index += 1
            continue
        if (
            char == "-"
            and following == "-"
            and (index + 2 == len(sql) or sql[index + 2].isspace())
        ):
            state = "line_comment"
            index += 2
            continue
        if char == "/" and following == "*":
            state = "block_comment"
            index += 2
            continue
        if char == "'":
            last = char
            state = "single_quote"
        elif char == '"':
            last = char
            state = "double_quote"
        elif char == "`":
            last = char
            state = "backtick"
        elif not char.isspace():
            last = char
        index += 1
    return last, state == "line_comment"


def _render_statement(sql: object) -> str:
    statement = _validate_sql(sql)
    if _DELIMITER_DIRECTIVE.search(statement) is not None:
        raise ValueError("DELIMITER client commands require append_client_script()")
    final_token, final_line_comment = _last_sql_token(statement)
    if final_token != ";":
        statement += "\n;" if final_line_comment else ";"
    return statement + "\n"


def _render_single_line_statement(sql: object) -> str:
    """Render generated SQL as one physical line for round replay artifacts."""

    statement = re.sub(r"\s+", " ", _validate_sql(sql)).strip()
    if _DELIMITER_DIRECTIVE.search(statement) is not None:
        raise ValueError("DELIMITER client commands are not valid round statements")
    if not statement.endswith(";"):
        statement += ";"
    return statement + "\n"


def _render_client_script(sql: object) -> str:
    script = _validate_sql(sql)
    return script + "\n"


def _render_routine(sql: object, delimiter: object) -> str:
    routine = _validate_sql(sql)
    if (
        not isinstance(delimiter, str)
        or delimiter == ";"
        or _SAFE_DELIMITER.fullmatch(delimiter) is None
        or delimiter.startswith(("--", "/*"))
    ):
        raise ValueError("delimiter must be a safe punctuation token other than ';'")
    if delimiter in routine:
        raise ValueError("delimiter must not occur inside routine SQL")
    if _DELIMITER_DIRECTIVE.search(routine) is not None:
        raise ValueError("routine SQL cannot contain a DELIMITER client command")
    if routine.endswith(";"):
        routine = routine[:-1].rstrip()
    return f"DELIMITER {delimiter}\n{routine}{delimiter}\nDELIMITER ;\n"


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return {"$type": "float", "value": repr(value)}
    if isinstance(value, Decimal):
        return {"$type": "decimal", "value": str(value)}
    if isinstance(value, bytes):
        return {"$type": "bytes", "hex": value.hex()}
    if isinstance(value, (datetime, date, time)):
        return {"$type": type(value).__name__, "value": value.isoformat()}
    if isinstance(value, timedelta):
        return {
            "$type": "timedelta",
            "microseconds": (
                (value.days * 86_400 + value.seconds) * 1_000_000
                + value.microseconds
            ),
        }
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("summary mappings require string keys")
        return {key: _json_safe(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_json_safe(child) for child in value]
    raise TypeError(f"unsupported SQL artifact value: {type(value).__qualname__}")


def _render_metadata(metadata: Mapping[str, object] | None) -> str:
    if metadata is None:
        return ""
    if not isinstance(metadata, Mapping):
        raise TypeError("metadata must be a mapping")
    assert_no_sensitive_keys(metadata)
    lines: list[str] = []
    for key in sorted(metadata):
        if not isinstance(key, str) or _METADATA_KEY.fullmatch(key) is None:
            raise ValueError("metadata keys must be safe SQL comment labels")
        safe = _json_safe(metadata[key])
        value = safe if isinstance(safe, str) else json.dumps(
            safe,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        comment_value = " ".join(value.splitlines())
        lines.append(f"-- {key}: {comment_value}\n")
    return "".join(lines)


class SourceableSqlWriter:
    """Create one ordered, directly source-able MySQL reproduction script."""

    def __init__(
        self,
        path: str | Path,
        database: str,
        *,
        metadata: Mapping[str, object] | None = None,
        session_statements: Iterable[str] = DEFAULT_SESSION_STATEMENTS,
        reset_database: bool = True,
        fsync: Callable[[int], None] = os.fsync,
    ) -> None:
        self.path = Path(path)
        self.database = validate_database_name(database)
        self._fsync = fsync
        self._lock = Lock()
        if not isinstance(reset_database, bool):
            raise TypeError("reset_database must be a bool")
        prologue = "-- select-fuzz reproducible SQL\n"
        prologue += _render_metadata(metadata)
        for statement in session_statements:
            prologue += _render_statement(statement)
        if reset_database:
            prologue += _render_statement(f"DROP DATABASE IF EXISTS `{self.database}`")
        prologue += _render_statement(
            f"CREATE DATABASE IF NOT EXISTS `{self.database}`"
        )
        prologue += _render_statement(f"USE `{self.database}`")
        _replace_file(self.path, prologue.encode("utf-8"), fsync=self._fsync)

    def append_statement(self, sql: str) -> None:
        self._append(_render_statement(sql))

    def append_single_line_statement(self, sql: str) -> None:
        self._append(_render_single_line_statement(sql))

    def append_blank_line(self) -> None:
        self._append("\n")

    def append_client_script(self, sql: str) -> None:
        """Append a mysql-client script that may contain DELIMITER commands."""

        self._append(_render_client_script(sql))

    def append_routine(self, sql: str, *, delimiter: str = "$$") -> None:
        self._append(_render_routine(sql, delimiter))

    def append_comment(self, metadata: Mapping[str, object]) -> None:
        self._append(_render_metadata(metadata))

    def _append(self, payload: str) -> None:
        with self._lock:
            _append_file(self.path, payload.encode("utf-8"), fsync=self._fsync)


class WorkerSqlLogWriter:
    """Append complete executed SQL to one persistent file per worker."""

    def __init__(
        self,
        directory: str | Path,
        *,
        fsync: Callable[[int], None] = os.fsync,
    ) -> None:
        self.directory = Path(directory)
        self._fsync = fsync
        self._locks: dict[int, Lock] = {}
        self._locks_lock = Lock()

    def path_for(self, worker_id: int) -> Path:
        self._validate_worker_id(worker_id)
        return self.directory / f"worker-{worker_id:03d}.sql"

    def append(
        self,
        worker_id: int,
        sql: str,
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        payload = _render_metadata(metadata) + _render_statement(sql)
        self._append(worker_id, payload)

    def append_client_script(
        self,
        worker_id: int,
        sql: str,
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        payload = _render_metadata(metadata) + _render_client_script(sql)
        self._append(worker_id, payload)

    def append_routine(
        self,
        worker_id: int,
        sql: str,
        *,
        delimiter: str = "$$",
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        payload = _render_metadata(metadata) + _render_routine(sql, delimiter)
        self._append(worker_id, payload)

    def _append(self, worker_id: int, payload: str) -> None:
        path = self.path_for(worker_id)
        with self._locks_lock:
            lock = self._locks.setdefault(worker_id, Lock())
        with lock:
            _append_file(path, payload.encode("utf-8"), fsync=self._fsync)

    @staticmethod
    def _validate_worker_id(worker_id: int) -> None:
        if (
            not isinstance(worker_id, int)
            or isinstance(worker_id, bool)
            or worker_id < 0
        ):
            raise ValueError("worker_id must be a nonnegative integer")


def write_minimal_failure_script(
    path: str | Path,
    *,
    database: str,
    setup_statements: Iterable[str],
    failing_query: str,
    metadata: Mapping[str, object] | None = None,
) -> Path:
    """Write only the setup needed by one failing query and that query itself."""

    writer = SourceableSqlWriter(path, database, metadata=metadata)
    for statement in setup_statements:
        if _DELIMITER_DIRECTIVE.search(statement) is not None:
            writer.append_client_script(statement)
        elif _ROUTINE_START.search(statement) is not None:
            writer.append_routine(statement)
        else:
            writer.append_statement(statement)
    writer.append_statement(failing_query)
    return writer.path


def compact_result_summary(
    rows: Iterable[Sequence[object]],
    *,
    row_count: int | None = None,
    digest: str | None = None,
) -> dict[str, object]:
    """Include rows only when both the 100-row and 64-KiB limits permit it."""

    materialized = list(rows)
    if row_count is None:
        row_count = len(materialized)
    if (
        not isinstance(row_count, int)
        or isinstance(row_count, bool)
        or row_count < len(materialized)
    ):
        raise ValueError("row_count must be an integer no smaller than captured rows")
    if digest is not None and (
        not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
    ):
        raise ValueError("digest must be a lowercase SHA-256")
    summary: dict[str, object] = {"row_count": row_count}
    if digest is not None:
        summary["digest"] = digest
    if row_count <= MAX_DIFF_ROWS and len(materialized) == row_count:
        safe_rows = _json_safe(materialized)
        candidate = dict(summary)
        candidate["rows"] = safe_rows
        candidate["rows_truncated"] = False
        encoded = json.dumps(
            candidate,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) <= MAX_DIFF_BYTES:
            return candidate
    summary["rows_truncated"] = True
    return summary


def write_difference_summary(
    path: str | Path,
    summary: Mapping[str, object],
    *,
    fsync: Callable[[int], None] = os.fsync,
) -> Path:
    """Atomically publish a strict-JSON mismatch summary capped at 64 KiB."""

    if not isinstance(summary, Mapping):
        raise TypeError("difference summary must be a mapping")
    assert_no_sensitive_keys(summary)
    safe = _json_safe(summary)
    payload = (
        json.dumps(
            safe,
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if len(payload) > MAX_DIFF_BYTES:
        raise ValueError("difference summary exceeds the 64 KiB safety limit")
    destination = Path(path)
    _replace_file(destination, payload, fsync=fsync)
    return destination


__all__ = [
    "DEFAULT_SESSION_STATEMENTS",
    "MAX_DIFF_BYTES",
    "MAX_DIFF_ROWS",
    "SourceableSqlWriter",
    "WorkerSqlLogWriter",
    "compact_result_summary",
    "write_difference_summary",
    "write_minimal_failure_script",
]
