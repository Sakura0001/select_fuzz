from __future__ import annotations

from pathlib import Path

from select_fuzz.artifacts.bundle import CaseBundleWriter


def test_round_script_contains_only_header_comments_and_actual_single_line_sql(
    tmp_path: Path,
) -> None:
    writer = CaseBundleWriter(tmp_path)
    database = "sf_round_interleaved_1"
    path = writer.begin_round_sql(
        2,
        database=database,
        setup_sql=("CREATE TABLE `t0` (\n `id` BIGINT PRIMARY KEY\n)",),
        queries=(),
        metadata={"round_seed": 41},
    )
    writer.append_round_sql(2, database, "SELECT\n  COUNT(*)\nFROM `t0`")
    writer.append_round_sql(2, database, "SELECT 1")
    writer.append_round_dml_batch(
        2,
        database,
        (
            "START TRANSACTION",
            "UPDATE `t0`\nSET `id` = `id` + 1 LIMIT 12",
            "COMMIT",
        ),
    )
    writer.append_round_sql(2, database, "SELECT 2")

    lines = path.read_text(encoding="utf-8").splitlines()
    first_sql = next(index for index, line in enumerate(lines) if not line.startswith("--"))

    assert all(not line.startswith("--") for line in lines[first_sql:])
    assert "CREATE TABLE `t0` ( `id` BIGINT PRIMARY KEY );" in lines
    first_query = lines.index("SELECT COUNT(*) FROM `t0`;")
    assert lines[first_query + 1] == "SELECT 1;"
    start = lines.index("START TRANSACTION;")
    assert lines[start - 1] == ""
    assert lines[start + 1] == "UPDATE `t0` SET `id` = `id` + 1 LIMIT 12;"
    assert lines[start + 3] == ""
    assert lines[start + 4] == "SELECT 2;"
