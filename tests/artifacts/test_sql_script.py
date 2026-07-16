from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest

from select_fuzz.artifacts import (
    MAX_DIFF_BYTES,
    MAX_DIFF_ROWS,
    SourceableSqlWriter,
    WorkerSqlLogWriter,
    compact_result_summary,
    write_difference_summary,
    write_minimal_failure_script,
)


def test_sourceable_writer_emits_prologue_and_keeps_execution_order(
    tmp_path: Path,
) -> None:
    path = tmp_path / "round.sql"
    writer = SourceableSqlWriter(
        path,
        "sf_seed_41",
        metadata={"seed": 41, "note": "first\nround"},
    )

    writer.append_statement("CREATE TABLE t (id BIGINT PRIMARY KEY)")
    writer.append_statement("CREATE INDEX idx_t_id ON t (id);")
    writer.append_statement("INSERT INTO t VALUES (1)")
    writer.append_statement("SELECT id FROM t")

    payload = path.read_text(encoding="utf-8")
    assert payload.startswith("-- select-fuzz reproducible SQL\n")
    assert "-- note: first round\n" in payload
    assert "SET NAMES utf8mb4;\n" in payload
    assert "SET SESSION time_zone = '+00:00';\n" in payload
    assert "CREATE DATABASE IF NOT EXISTS `sf_seed_41`;\n" in payload
    assert "USE `sf_seed_41`;\n" in payload
    expected = [
        "CREATE TABLE t (id BIGINT PRIMARY KEY);",
        "CREATE INDEX idx_t_id ON t (id);",
        "INSERT INTO t VALUES (1);",
        "SELECT id FROM t;",
    ]
    assert [payload.index(statement) for statement in expected] == sorted(
        payload.index(statement) for statement in expected
    )
    assert ";;" not in payload


def test_sourceable_writer_handles_routine_and_raw_client_delimiters(
    tmp_path: Path,
) -> None:
    path = tmp_path / "procedures.sql"
    writer = SourceableSqlWriter(path, "sf_perf_1")

    writer.append_routine(
        "CREATE PROCEDURE fill_rows()\n"
        "BEGIN\n"
        "  INSERT INTO t VALUES (1);\n"
        "END;",
        delimiter="$$",
    )
    writer.append_client_script(
        "DELIMITER //\n"
        "CREATE PROCEDURE fill_more() BEGIN SELECT 2; END//\n"
        "DELIMITER ;"
    )
    writer.append_statement("CALL fill_rows()")

    assert path.read_text(encoding="utf-8").endswith(
        "DELIMITER $$\n"
        "CREATE PROCEDURE fill_rows()\n"
        "BEGIN\n"
        "  INSERT INTO t VALUES (1);\n"
        "END$$\n"
        "DELIMITER ;\n"
        "DELIMITER //\n"
        "CREATE PROCEDURE fill_more() BEGIN SELECT 2; END//\n"
        "DELIMITER ;\n"
        "CALL fill_rows();\n"
    )


@pytest.mark.parametrize("delimiter", (";", "", "a b", "--", "/*x*/"))
def test_routine_rejects_unsafe_delimiters(tmp_path: Path, delimiter: str) -> None:
    writer = SourceableSqlWriter(tmp_path / "routine.sql", "sf_routine")

    with pytest.raises(ValueError, match="delimiter"):
        writer.append_routine("CREATE PROCEDURE p() SELECT 1", delimiter=delimiter)


def test_worker_sql_log_appends_across_writer_instances_and_rounds(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "thread-sql"
    first_run = WorkerSqlLogWriter(directory)
    first_run.append(3, "CREATE TABLE t (id INT)", metadata={"round": "round-1"})
    first_run.append(3, "INSERT INTO t VALUES (1)")

    second_run = WorkerSqlLogWriter(directory)
    second_run.append(3, "SELECT * FROM t", metadata={"round": "round-2\ncontinued"})

    path = directory / "worker-003.sql"
    payload = path.read_text(encoding="utf-8")
    assert payload.count("CREATE TABLE t (id INT);") == 1
    assert payload.count("INSERT INTO t VALUES (1);") == 1
    assert payload.count("SELECT * FROM t;") == 1
    assert payload.index("CREATE TABLE") < payload.index("INSERT INTO") < payload.index("SELECT")
    assert "-- round: round-1\n" in payload
    assert "-- round: round-2 continued\n" in payload


def test_worker_sql_log_concurrent_records_are_not_interleaved(tmp_path: Path) -> None:
    writer = WorkerSqlLogWriter(tmp_path / "thread-sql")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda ordinal: writer.append(
                    0,
                    f"SELECT {ordinal} AS value",
                    metadata={"ordinal": ordinal},
                ),
                range(128),
            )
        )

    payload = writer.path_for(0).read_text(encoding="utf-8")
    assert payload.count("-- ordinal:") == 128
    assert payload.count(" AS value;") == 128
    for ordinal in range(128):
        assert payload.count(f"SELECT {ordinal} AS value;") == 1


@pytest.mark.parametrize("worker_id", (-1, True, 1.5, "0"))
def test_worker_sql_log_rejects_invalid_worker_ids(
    tmp_path: Path,
    worker_id: object,
) -> None:
    writer = WorkerSqlLogWriter(tmp_path / "thread-sql")

    with pytest.raises(ValueError, match="worker_id"):
        writer.append(worker_id, "SELECT 1")  # type: ignore[arg-type]

    assert not (tmp_path / "thread-sql").exists()


def test_minimal_failure_script_contains_setup_and_only_the_failing_query(
    tmp_path: Path,
) -> None:
    path = tmp_path / "case.sql"

    write_minimal_failure_script(
        path,
        database="sf_failure_7",
        setup_statements=(
            "CREATE TABLE t (id INT PRIMARY KEY)",
            "CREATE INDEX idx_t_id ON t (id)",
            "INSERT INTO t VALUES (1), (2)",
        ),
        failing_query="SELECT id FROM t WHERE id > 0 ORDER BY id",
        metadata={"seed": 7, "verdict": "content_mismatch"},
    )

    payload = path.read_text(encoding="utf-8")
    assert "CREATE DATABASE IF NOT EXISTS `sf_failure_7`;" in payload
    assert "CREATE TABLE t" in payload
    assert "CREATE INDEX idx_t_id" in payload
    assert "INSERT INTO t" in payload
    assert payload.count("SELECT id FROM t WHERE id > 0 ORDER BY id;") == 1
    assert "query_attempt" not in payload


def test_compact_result_summary_includes_only_small_results() -> None:
    rows = [(1, "one"), (2, "two")]

    assert compact_result_summary(rows, digest="a" * 64) == {
        "digest": "a" * 64,
        "row_count": 2,
        "rows": [[1, "one"], [2, "two"]],
        "rows_truncated": False,
    }

    too_many = compact_result_summary([(value,) for value in range(MAX_DIFF_ROWS + 1)])
    assert too_many == {"row_count": MAX_DIFF_ROWS + 1, "rows_truncated": True}

    too_large = compact_result_summary([("x" * MAX_DIFF_BYTES,)])
    assert too_large == {"row_count": 1, "rows_truncated": True}


def test_difference_summary_is_compact_strict_json(tmp_path: Path) -> None:
    path = tmp_path / "case.diff"
    summary = {
        "category": "row_count_mismatch",
        "first_difference": {"row": 1},
        "nodes": {
            "baseline": compact_result_summary([(1,), (2,)], digest="a" * 64),
            "custom_on": compact_result_summary([(1,)], digest="b" * 64),
        },
    }

    write_difference_summary(path, summary)

    payload = path.read_bytes()
    assert len(payload) <= MAX_DIFF_BYTES
    assert json.loads(payload) == summary


def test_difference_summary_rejects_oversized_payload(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="64 KiB"):
        write_difference_summary(
            tmp_path / "case.diff",
            {"category": "content_mismatch", "rows": "x" * MAX_DIFF_BYTES},
        )
