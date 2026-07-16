from __future__ import annotations

from dataclasses import dataclass

import pytest

from select_fuzz.generation.mutation import MutationBatchGenerator, MutationOperation
from select_fuzz.generation.mutation import MutationBatch, MutationStatement


@dataclass(frozen=True)
class _Column:
    name: str


@dataclass(frozen=True)
class _Table:
    name: str
    columns: tuple[_Column, ...]


@dataclass(frozen=True)
class _Schema:
    tables: tuple[_Table, ...]


@dataclass(frozen=True)
class _Data:
    table_order: tuple[str, ...]


@dataclass(frozen=True)
class _Setup:
    schema: _Schema
    data: _Data


@dataclass(frozen=True)
class _CountedData:
    table_order: tuple[str, ...]
    rows_by_table: dict[str, tuple[tuple[int, ...], ...]]


def _setup() -> _Setup:
    return _Setup(
        _Schema(
            (
                _Table("t0", (_Column("id"), _Column("payload"))),
                _Table("t1", (_Column("id"), _Column("parent_id"))),
            )
        ),
        _Data(("t0", "t1")),
    )


def test_batch_is_deterministic_bounded_and_uses_real_identifiers() -> None:
    generator = MutationBatchGenerator()

    first = generator.generate(_setup(), seed=41, sequence=3)
    second = generator.generate(_setup(), seed=41, sequence=3)

    assert first == second
    assert 1 <= len(first.statements) <= 3
    assert 12 <= first.target_rows <= 50
    assert all("\n" not in statement.sql for statement in first.statements)
    assert all("`t0`" in statement.sql or "`t1`" in statement.sql for statement in first.statements)


def test_operation_weight_is_two_to_one_to_one_over_deterministic_seed_sample() -> None:
    generator = MutationBatchGenerator()
    counts = {operation: 0 for operation in MutationOperation}
    for seed in range(2_000):
        batch = generator.generate(_setup(), seed=seed, sequence=seed + 1)
        for statement in batch.statements:
            counts[statement.operation] += 1
    total = sum(counts.values())

    assert 0.46 <= counts[MutationOperation.INSERT] / total <= 0.54
    assert 0.21 <= counts[MutationOperation.UPDATE] / total <= 0.29
    assert 0.21 <= counts[MutationOperation.DELETE] / total <= 0.29


def test_low_row_setup_falls_back_to_exact_cardinality_insert() -> None:
    setup = _Setup(
        _Schema((_Table("t0", (_Column("id"), _Column("payload"))),)),
        _CountedData(("t0",), {"t0": ((1, 7),)}),  # type: ignore[arg-type]
    )

    batch = MutationBatchGenerator().generate(setup, seed=9, sequence=1)

    assert all(statement.operation is MutationOperation.INSERT for statement in batch.statements)
    assert all("LIMIT 1) AS `sf_source`" in statement.sql for statement in batch.statements)
    assert all("UNION ALL" in statement.sql for statement in batch.statements)

    empty = _Setup(
        _Schema((_Table("t0", (_Column("id"),)),)),
        _CountedData(("t0",), {"t0": ()}),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="populated table"):
        MutationBatchGenerator().generate(empty, seed=9, sequence=1)


def test_batch_model_rejects_invalid_count_total_and_sequence() -> None:
    valid = MutationStatement(MutationOperation.UPDATE, "UPDATE t SET id = id", 12)
    too_small = MutationStatement(MutationOperation.DELETE, "DELETE FROM t LIMIT 1", 1)
    with pytest.raises(ValueError, match="sequence"):
        MutationBatch(1, 0, (valid,))
    with pytest.raises(ValueError):
        MutationBatch(1, 1, ())
    with pytest.raises(ValueError):
        MutationBatch(1, 1, (valid,) * 4)
    with pytest.raises(ValueError, match="12 to 50"):
        MutationBatch(1, 1, (too_small,))


def test_statement_and_generator_reject_invalid_boundaries() -> None:
    with pytest.raises(ValueError, match="physical line"):
        MutationStatement(MutationOperation.UPDATE, "UPDATE t\nSET id = 1", 1)
    with pytest.raises(ValueError, match="target_rows"):
        MutationStatement(MutationOperation.UPDATE, "UPDATE t SET id = 1", 0)
    generator = MutationBatchGenerator()
    with pytest.raises(TypeError, match="seed"):
        generator.generate(_setup(), seed=True, sequence=1)
    with pytest.raises(ValueError, match="sequence"):
        generator.generate(_setup(), seed=1, sequence=0)
    empty = _Setup(_Schema(()), _Data(()))
    with pytest.raises(ValueError, match="at least one table"):
        generator.generate(empty, seed=1, sequence=1)
    no_id = _Setup(_Schema((_Table("t", (_Column("payload"),)),)), _Data(("t",)))
    with pytest.raises(ValueError, match="id column"):
        generator.generate(no_id, seed=1, sequence=1)
