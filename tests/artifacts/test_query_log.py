from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from select_fuzz.artifacts import WorkerQueryLogWriter, read_jsonl


def _record(ordinal: int, record_type: str = "query_attempt_started") -> dict[str, object]:
    return {
        "case_ordinal": ordinal,
        "query_sql": f"SELECT {ordinal} ORDER BY 1",
        "schema_version": 1,
        "type": record_type,
    }


def test_worker_query_log_uses_one_independent_file_per_worker(tmp_path: Path) -> None:
    writer = WorkerQueryLogWriter(tmp_path / "sql")

    writer.append(0, _record(1))
    writer.append(1, _record(2))
    writer.append(0, _record(1, "query_attempt_finished"))

    assert read_jsonl(tmp_path / "sql" / "worker-000.jsonl") == [
        _record(1),
        _record(1, "query_attempt_finished"),
    ]
    assert read_jsonl(tmp_path / "sql" / "worker-001.jsonl") == [_record(2)]


def test_worker_query_log_concurrent_appends_do_not_drop_or_interleave_records(
    tmp_path: Path,
) -> None:
    writer = WorkerQueryLogWriter(tmp_path / "sql")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda ordinal: writer.append(0, _record(ordinal)), range(128)))

    records = read_jsonl(tmp_path / "sql" / "worker-000.jsonl")
    assert len(records) == 128
    assert {record["case_ordinal"] for record in records} == set(range(128))
    assert all(record["type"] == "query_attempt_started" for record in records)


@pytest.mark.parametrize("worker_id", (-1, True, 1.5, "0"))
def test_worker_query_log_rejects_invalid_worker_ids(
    tmp_path: Path,
    worker_id: object,
) -> None:
    writer = WorkerQueryLogWriter(tmp_path / "sql")

    with pytest.raises(ValueError, match="worker_id"):
        writer.append(worker_id, _record(1))  # type: ignore[arg-type]

    assert not (tmp_path / "sql").exists()


def test_started_record_remains_readable_without_a_finished_pair(tmp_path: Path) -> None:
    writer = WorkerQueryLogWriter(tmp_path / "sql")

    writer.append(7, _record(41))

    assert read_jsonl(writer.path_for(7)) == [_record(41)]
