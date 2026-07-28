import pytest

from select_fuzz.config import FuzzConfig, NodeRole
from select_fuzz.modes.fuzz.models import FuzzConnectionLayout, FuzzRowBudget


def test_fuzz_defaults_target_custom_on_and_split_readers_one_to_two() -> None:
    config = FuzzConfig(databases=4, writer_threads_per_database=4, reader_threads_per_database=30)
    layout = FuzzConnectionLayout.from_config(config)

    assert config.target_role is NodeRole.CUSTOM_ON
    assert layout.primary_writers == 16
    assert layout.primary_readers == 40
    assert layout.replica_readers == 80
    assert layout.total_connections == 136


def test_fuzz_requires_reader_count_divisible_by_three() -> None:
    with pytest.raises(ValueError, match="divisible by 3"):
        FuzzConfig(reader_threads_per_database=10)


def test_fuzz_requires_at_least_fifty_columns_per_table() -> None:
    with pytest.raises(ValueError, match="greater than or equal to 50"):
        FuzzConfig(min_columns_per_table=49)

    with pytest.raises(ValueError, match="min_columns_per_table"):
        FuzzConfig(min_columns_per_table=60, max_columns_per_table=50)


def test_fuzz_rejects_connection_product_over_explicit_cap() -> None:
    config = FuzzConfig(
        databases=4,
        writer_threads_per_database=4,
        reader_threads_per_database=30,
        max_total_connections=100,
    )
    with pytest.raises(ValueError, match="max_total_connections"):
        FuzzConnectionLayout.from_config(config)


def test_fuzz_row_budget_caps_inserts_and_reconciles_real_affected_rows() -> None:
    budget = FuzzRowBudget(initial_rows=90, maximum_rows=100)
    assert budget.reserve_insert(25) == 10
    budget.reconcile_insert(10, 4)
    assert budget.current == 94
    budget.record_delete(3)
    assert budget.current == 91
