from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from typing import Any

import pytest

from select_fuzz.artifacts.jsonl import (
    JsonlCorruptionError,
    JsonlWriter,
    MAX_JSONL_RECORD_BYTES,
    read_jsonl,
)


def test_jsonl_fsyncs_before_publish(tmp_path: Path) -> None:
    events: list[str] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    writer = JsonlWriter(
        tmp_path / "events.jsonl",
        fsync=recording_fsync,
        on_publish=lambda record: events.append(f"publish:{record['case_id']}"),
    )

    writer.append({"type": "finding", "case_id": "c1"})

    assert events == ["fsync", "publish:c1"]
    assert read_jsonl(tmp_path / "events.jsonl") == [
        {"case_id": "c1", "type": "finding"}
    ]


def test_jsonl_concurrent_appends_remain_complete_and_noninterleaved(
    tmp_path: Path,
) -> None:
    writer = JsonlWriter(tmp_path / "events.jsonl")

    def append(worker: int) -> None:
        for sequence in range(100):
            writer.append({"worker": worker, "sequence": sequence})

    with ThreadPoolExecutor(max_workers=10) as pool:
        tuple(pool.map(append, range(10)))

    records = read_jsonl(tmp_path / "events.jsonl")
    assert len(records) == 1000
    assert {(record["worker"], record["sequence"]) for record in records} == {
        (worker, sequence) for worker in range(10) for sequence in range(100)
    }


@pytest.mark.parametrize(
    "tail",
    (
        b'{"case_id":"torn"',
        b'{"case_id":"complete_but_missing_newline"}',
        b"\xf0\x9f",
    ),
)
def test_reader_ignores_only_an_unterminated_final_tail(
    tmp_path: Path, tail: bytes
) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"case_id":"kept"}\n' + tail)

    assert read_jsonl(path) == [{"case_id": "kept"}]


@pytest.mark.parametrize(
    "payload",
    (
        b'{"case_id":"kept"}\nnot-json\n',
        b'not-json\n{"case_id":"later"}\n',
        b'{"case_id":"kept"}\n\xff\n',
    ),
)
def test_reader_rejects_any_corrupt_terminated_line(
    tmp_path: Path, payload: bytes
) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(payload)

    with pytest.raises(JsonlCorruptionError):
        read_jsonl(path)


def test_reader_returns_empty_for_missing_log(tmp_path: Path) -> None:
    assert read_jsonl(tmp_path / "missing.jsonl") == []


@pytest.mark.parametrize("record", ([1, 2], {"elapsed": float("nan")}))
def test_writer_rejects_non_mapping_or_non_json_values(
    tmp_path: Path, record: Any
) -> None:
    writer = JsonlWriter(tmp_path / "events.jsonl")

    with pytest.raises((TypeError, ValueError)):
        writer.append(record)

    assert read_jsonl(tmp_path / "events.jsonl") == []


def test_fsync_failure_prevents_publish_callback(tmp_path: Path) -> None:
    published: list[object] = []

    def fail_fsync(fd: int) -> None:
        raise OSError("simulated ENOSPC")

    writer = JsonlWriter(
        tmp_path / "events.jsonl",
        fsync=fail_fsync,
        on_publish=published.append,
    )

    with pytest.raises(OSError, match="ENOSPC"):
        writer.append({"type": "finding", "case_id": "c1"})

    assert published == []


def test_oversized_unterminated_tail_is_rejected_instead_of_read_unbounded(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(b"x" * (MAX_JSONL_RECORD_BYTES + 1))

    with pytest.raises(JsonlCorruptionError, match="8 MiB"):
        read_jsonl(path)


@pytest.mark.parametrize("sensitive_key", ("password", "token", "credentials"))
def test_generic_jsonl_writer_rejects_sensitive_keys_before_creating_file(
    tmp_path: Path, sensitive_key: str
) -> None:
    path = tmp_path / "events.jsonl"

    with pytest.raises(ValueError, match="sensitive"):
        JsonlWriter(path).append(
            {"type": "run_started", "config": {sensitive_key: "do-not-store"}}
        )

    assert not path.exists()
