"""Append-only fsynced JSON Lines storage."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import fcntl
import json
import os
from pathlib import Path
from threading import Lock
from typing import Any


MAX_JSONL_RECORD_BYTES = 8 * 1024 * 1024
_SENSITIVE_KEYS = frozenset(
    {
        "credential",
        "credentials",
        "password",
        "password_env",
        "passwd",
        "secret",
        "token",
        "username",
        "username_env",
    }
)


class JsonlCorruptionError(ValueError):
    """A newline-terminated record is corrupt and cannot be ignored safely."""


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def assert_no_sensitive_keys(value: object, path: str = "record") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} contains a non-string JSON key")
            normalized = key.casefold().replace("-", "_")
            if normalized in _SENSITIVE_KEYS:
                raise ValueError(f"{path} contains forbidden sensitive key {key!r}")
            assert_no_sensitive_keys(child, f"{path}.{key}")
    elif isinstance(value, (tuple, list, set, frozenset)):
        for index, child in enumerate(value):
            assert_no_sensitive_keys(child, f"{path}[{index}]")


def _encode_record(record: Mapping[str, object]) -> tuple[bytes, dict[str, object]]:
    if not isinstance(record, Mapping):
        raise TypeError("JSONL records must be mappings")
    assert_no_sensitive_keys(record)
    try:
        encoded = json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise type(error)(f"record is not strict JSON: {error}") from error
    if len(encoded) + 1 > MAX_JSONL_RECORD_BYTES:
        raise ValueError("JSONL record exceeds the 8 MiB safety limit")
    decoded = json.loads(encoded, parse_constant=_reject_constant)
    if not isinstance(decoded, dict):  # pragma: no cover - Mapping encodes to object
        raise TypeError("JSONL records must encode as objects")
    return encoded + b"\n", decoded


def _write_all(fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(fd, payload[offset:])
        if written <= 0:  # pragma: no cover - OS contract defense
            raise OSError("append returned no progress")
        offset += written


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class JsonlWriter:
    """Serialize thread/process appenders and publish only after file durability."""

    def __init__(
        self,
        path: str | Path,
        *,
        fsync: Callable[[int], None] = os.fsync,
        on_publish: Callable[[Mapping[str, object]], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self._fsync = fsync
        self._on_publish = on_publish
        self._lock = Lock()

    def append(self, record: Mapping[str, object]) -> None:
        payload, published = _encode_record(record)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            existed = self.path.exists()
            descriptor = os.open(
                self.path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                _write_all(descriptor, payload)
                self._fsync(descriptor)
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
            if not existed:
                _fsync_directory(self.path.parent)
            if self._on_publish is not None:
                self._on_publish(published)


def read_jsonl(path: str | Path) -> list[dict[str, object]]:
    """Read committed lines, ignoring only one unterminated final tail."""

    source = Path(path)
    if not source.exists():
        return []
    records: list[dict[str, object]] = []
    with source.open("rb") as stream:
        line_number = 0
        while True:
            line = stream.readline(MAX_JSONL_RECORD_BYTES + 1)
            if not line:
                break
            line_number += 1
            if len(line) > MAX_JSONL_RECORD_BYTES:
                raise JsonlCorruptionError(
                    f"JSONL line {line_number} exceeds the 8 MiB safety limit"
                )
            if not line.endswith(b"\n"):
                break
            try:
                decoded: Any = json.loads(
                    line[:-1].decode("utf-8"),
                    parse_constant=_reject_constant,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                raise JsonlCorruptionError(
                    f"corrupt newline-terminated JSONL line {line_number}"
                ) from error
            if not isinstance(decoded, dict):
                raise JsonlCorruptionError(
                    f"JSONL line {line_number} must contain an object"
                )
            records.append(decoded)
    return records


__all__ = [
    "JsonlCorruptionError",
    "JsonlWriter",
    "MAX_JSONL_RECORD_BYTES",
    "assert_no_sensitive_keys",
    "read_jsonl",
]
