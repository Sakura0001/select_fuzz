from pathlib import Path

import pytest

from select_fuzz.modes.fuzz.sql_log import FuzzSqlRecorder


def test_fuzz_sql_recorder_writes_schema_and_worker_files(tmp_path: Path) -> None:
    recorder = FuzzSqlRecorder(tmp_path / "sql")

    recorder.record_schema("sf_f_case_0", "CREATE TABLE `fuzz_t0` (`id` BIGINT)")
    recorder.record_query(
        "sf_f_case_0",
        "reader-primary",
        0,
        "SELECT 1",
    )

    assert (tmp_path / "sql" / "fuzz_schema_sf_f_case_0.sql").read_text() == (
        "CREATE TABLE `fuzz_t0` (`id` BIGINT);\n"
    )
    assert (tmp_path / "sql" / "fuzz_sf_f_case_0_reader-primary_000.sql").read_text() == (
        "SELECT 1;\n"
    )


def test_fuzz_sql_recorder_rejects_unsafe_path_components(tmp_path: Path) -> None:
    recorder = FuzzSqlRecorder(tmp_path / "sql")

    with pytest.raises(ValueError, match="unsafe SQL artifact path"):
        recorder.record_schema("../outside", "SELECT 1")
