import re

from select_fuzz.modes.fuzz.dml import FuzzDmlGenerator, FuzzTable


def test_delete_is_always_bounded_to_one_or_at_most_one_hundred_rows() -> None:
    generator = FuzzDmlGenerator(
        (FuzzTable("orders", "id", ("amount", "status", "payload")),),
        batch_rows_min=100,
        batch_rows_max=100_000,
        delete_batch_rows_min=10,
        delete_batch_rows_max=100,
    )

    deletes = [
        generator.generate_delete(seed=seed, known_high_watermark=1_000_000).sql
        for seed in range(100)
    ]

    assert all(sql.startswith("DELETE FROM `orders`") for sql in deletes)
    assert all(" WHERE " in sql and "LIMIT " in sql for sql in deletes)
    limits = [int(re.search(r"LIMIT ([0-9]+)$", sql).group(1)) for sql in deletes]  # type: ignore[union-attr]
    assert min(limits) >= 1
    assert max(limits) <= 100


def test_delete_never_targets_the_seed_row_and_still_supports_point_delete() -> None:
    generator = FuzzDmlGenerator(
        (FuzzTable("orders", "id", ("amount", "status", "payload")),),
        batch_rows_min=10,
        batch_rows_max=100,
        delete_batch_rows_min=10,
        delete_batch_rows_max=20,
    )

    statements = [
        generator.generate_delete(seed=seed, known_high_watermark=100)
        for seed in range(100)
    ]

    assert all("`id` <> 1" in statement.sql for statement in statements)
    assert any(
        statement.target_rows == 1 and "`id` =" in statement.sql
        for statement in statements
    )
