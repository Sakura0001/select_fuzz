from __future__ import annotations

import json
from multiprocessing import get_context
import os
from pathlib import Path
from threading import Event, Lock, Thread

import pytest

import select_fuzz.generation.coverage as coverage_module
from select_fuzz.generation.catalog import FeatureCatalog, FeatureSpec
from select_fuzz.generation.coverage import (
    CoverageLedger,
    CoveragePlanExhaustedError,
    CoverageScheduler,
    WeightedCoverageScheduler,
)


def _spec(feature_id: str, *, weight: float = 1.0) -> FeatureSpec:
    return FeatureSpec(
        feature_id=feature_id,
        family=feature_id.split("_", 1)[0],
        min_version=(8, 0, 0),
        compatible_profiles=frozenset({"regular_innodb"}),
        weight=weight,
        ast_nodes=frozenset({"query_expression"}),
        guards=frozenset({"read_only_select"}),
    )


def _checkpoint_from_process(path: str, worker: int) -> None:
    ledger = CoverageLedger(path)
    ledger.record("shared", hits=worker + 1)
    ledger.record(f"worker_{worker}")
    ledger.checkpoint()


def test_weighted_scheduler_is_seeded_random_with_bounded_debt_boost(
    tmp_path: Path,
) -> None:
    ledger = CoverageLedger(
        tmp_path / "coverage.json",
        counts={"shape_saturated": 10, "shape_debt": 0, "shape_heavy": 10},
    )
    catalog = FeatureCatalog(
        (
            _spec("shape_saturated", weight=1),
            _spec("shape_debt", weight=1),
            _spec("shape_heavy", weight=5),
        )
    )
    first = WeightedCoverageScheduler(
        catalog=catalog,
        ledger=ledger,
        min_hits=10,
        version=(8, 0, 41),
        schedule_seed=20260715,
        plan_start_ordinal=100,
        max_debt_boost=4.0,
    )
    second = WeightedCoverageScheduler(
        catalog=catalog,
        ledger=ledger,
        min_hits=10,
        version=(8, 0, 41),
        schedule_seed=20260715,
        plan_start_ordinal=100,
        max_debt_boost=4.0,
    )

    selected = [first.choose(case_ordinal=ordinal).feature_id for ordinal in range(100, 1100)]

    assert selected == [
        second.choose(case_ordinal=ordinal).feature_id for ordinal in range(100, 1100)
    ]
    assert set(selected) == {"shape_saturated", "shape_debt", "shape_heavy"}
    assert selected.count("shape_debt") > selected.count("shape_saturated")
    assert selected.count("shape_heavy") > selected.count("shape_debt")


def test_scheduler_strictly_prefers_largest_coverage_debt(tmp_path: Path) -> None:
    ledger = CoverageLedger(tmp_path / "coverage.json")
    ledger.record("join_inner", hits=10)
    ledger.record("cte_recursive", hits=0)
    scheduler = CoverageScheduler(
        catalog=FeatureCatalog((_spec("join_inner"), _spec("cte_recursive"))),
        ledger=ledger,
        min_hits=10,
        version=(8, 0, 41),
    )

    assert scheduler.choose(case_ordinal=0).feature_id == "cte_recursive"


def test_checkpoint_roundtrip_is_canonical_and_keeps_counts(tmp_path: Path) -> None:
    path = tmp_path / "coverage.json"
    ledger = CoverageLedger(path)
    ledger.record("window_rows", hits=3)
    ledger.record("join_inner", hits=2)
    ledger.checkpoint()

    restored = CoverageLedger.load(path)
    assert restored.hits("window_rows") == 3
    assert json.loads(path.read_text(encoding="utf-8"))["counts"] == {
        "join_inner": 2,
        "window_rows": 3,
    }


def test_failed_atomic_replace_preserves_previous_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "coverage.json"
    ledger = CoverageLedger(path)
    ledger.record("window_rows", hits=1)
    ledger.checkpoint()
    previous = path.read_bytes()
    ledger.record("window_rows", hits=1)

    def fail_replace(source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                     destination: str | bytes | os.PathLike[str] | os.PathLike[bytes]) -> None:
        del source, destination
        raise OSError("injected replace failure")

    monkeypatch.setattr("select_fuzz.generation.coverage.os.replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        ledger.checkpoint()

    assert path.read_bytes() == previous
    assert not list(tmp_path.glob(".coverage.json.*.tmp"))


def test_post_replace_directory_fsync_error_does_not_double_count_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "coverage.json"
    ledger = CoverageLedger(path)
    ledger.record("window_rows")
    original_fsync = os.fsync
    fsync_calls = 0

    def fail_directory_fsync(fd: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError("injected directory fsync failure")
        original_fsync(fd)

    monkeypatch.setattr("select_fuzz.generation.coverage.os.fsync", fail_directory_fsync)
    with pytest.raises(OSError, match="directory fsync"):
        ledger.checkpoint()

    monkeypatch.setattr("select_fuzz.generation.coverage.os.fsync", original_fsync)
    ledger.checkpoint()
    assert CoverageLedger.load(path).hits("window_rows") == 1


def test_windows_directory_flush_does_not_call_unsupported_os_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coverage_module.sys, "platform", "win32")

    def forbidden_open(*_args: object, **_kwargs: object) -> int:
        pytest.fail("Windows checkpoint must not open a directory with os.open")

    monkeypatch.setattr(coverage_module.os, "open", forbidden_open)
    coverage_module._fsync_parent_directory(tmp_path)


def test_concurrent_recording_never_loses_hits(tmp_path: Path) -> None:
    ledger = CoverageLedger(tmp_path / "coverage.json")
    threads = [Thread(target=lambda: ledger.record("join_inner", hits=1000)) for _ in range(10)]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert ledger.hits("join_inner") == 10_000


def test_independent_ledger_instances_merge_deltas_at_checkpoint(tmp_path: Path) -> None:
    path = tmp_path / "coverage.json"
    first = CoverageLedger(path)
    second = CoverageLedger(path)
    first.record("join_inner", hits=2)
    second.record("join_inner", hits=3)
    second.record("cte_recursive", hits=1)

    first.checkpoint()
    second.checkpoint()

    restored = CoverageLedger.load(path)
    assert restored.snapshot() == {"cte_recursive": 1, "join_inner": 5}


def test_processes_checkpoint_the_same_ledger_without_lost_updates(tmp_path: Path) -> None:
    path = tmp_path / "coverage.json"
    context = get_context("spawn")
    processes = [
        context.Process(target=_checkpoint_from_process, args=(str(path), worker))
        for worker in range(12)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)

    assert all(not process.is_alive() for process in processes)
    assert all(process.exitcode == 0 for process in processes)
    counts = CoverageLedger.load(path).snapshot()
    assert counts["shared"] == sum(range(1, 13))
    assert {key for key in counts if key.startswith("worker_")} == {
        f"worker_{worker}" for worker in range(12)
    }


@pytest.mark.parametrize("feature_id,hits", [(1, 1), ("feature", 1.5), ("feature", True)])
def test_record_rejects_values_that_would_corrupt_checkpoint(
    tmp_path: Path, feature_id: object, hits: object
) -> None:
    ledger = CoverageLedger(tmp_path / "coverage.json")
    with pytest.raises((TypeError, ValueError)):
        ledger.record(feature_id, hits=hits)  # type: ignore[arg-type]


def test_concurrent_checkpoints_cannot_replace_newer_state_with_older_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "coverage.json"
    ledger = CoverageLedger(path)
    ledger.record("join_inner")
    original_replace = os.replace
    first_waiting = Event()
    release_first = Event()
    second_reached_replace = Event()
    call_lock = Lock()
    calls = 0

    def ordered_replace(source: Path, destination: Path) -> None:
        nonlocal calls
        with call_lock:
            calls += 1
            call_number = calls
        if call_number == 1:
            first_waiting.set()
            assert release_first.wait(timeout=2)
        else:
            second_reached_replace.set()
        original_replace(source, destination)

    monkeypatch.setattr("select_fuzz.generation.coverage.os.replace", ordered_replace)
    first = Thread(target=ledger.checkpoint)
    first.start()
    assert first_waiting.wait(timeout=2)
    ledger.record("join_inner")
    second = Thread(target=ledger.checkpoint)
    second.start()

    # Without full checkpoint serialization, the newer writer reaches replace now.
    second_reached_replace.wait(timeout=0.1)
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive() and not second.is_alive()
    assert CoverageLedger.load(path).hits("join_inner") == 2


def test_case_ordinal_schedule_is_deterministic_under_concurrent_completion(
    tmp_path: Path,
) -> None:
    specs = (_spec("feature_a"), _spec("feature_b"))
    scheduler = CoverageScheduler(
        catalog=FeatureCatalog(specs),
        ledger=CoverageLedger(tmp_path / "coverage.json"),
        min_hits=5,
        version=(8, 0, 41),
        schedule_seed=99,
    )
    selected: dict[int, str] = {}
    selected_lock = Lock()

    def select(ordinal: int) -> None:
        feature_id = scheduler.choose(case_ordinal=ordinal).feature_id
        with selected_lock:
            selected[ordinal] = feature_id

    threads = [Thread(target=select, args=(ordinal,)) for ordinal in reversed(range(10))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    expected = {
        ordinal: scheduler.choose(case_ordinal=ordinal).feature_id for ordinal in range(10)
    }
    assert selected == expected
    assert set(selected.values()) == {"feature_a", "feature_b"}


def test_scheduler_stops_replaying_the_frozen_debt_plan_after_it_is_exhausted(
    tmp_path: Path,
) -> None:
    ledger = CoverageLedger(tmp_path / "coverage.json")
    ledger.record("feature_b")
    scheduler = CoverageScheduler(
        catalog=FeatureCatalog(
            (_spec("feature_a", weight=1), _spec("feature_b", weight=100))
        ),
        ledger=ledger,
        min_hits=1,
        version=(8, 0, 41),
        schedule_seed=0,
    )

    assert scheduler.choose(case_ordinal=0).feature_id == "feature_a"
    ledger.record("feature_a")
    assert scheduler.choose(case_ordinal=1).feature_id == "feature_b"


def test_exhausted_debt_plan_requires_replan_when_a_hit_was_not_recorded(
    tmp_path: Path,
) -> None:
    scheduler = CoverageScheduler(
        catalog=FeatureCatalog((_spec("feature_a"),)),
        ledger=CoverageLedger(tmp_path / "coverage.json"),
        min_hits=1,
        version=(8, 0, 41),
    )

    assert scheduler.choose(case_ordinal=0).feature_id == "feature_a"
    with pytest.raises(CoveragePlanExhaustedError):
        scheduler.choose(case_ordinal=1)

    replanned = scheduler.replan(start_case_ordinal=1)
    assert replanned.plan_start_ordinal == 1
    assert replanned.choose(case_ordinal=1).feature_id == "feature_a"
    ledger = replanned.ledger
    ledger.record("feature_a")
    assert replanned.cycle_complete()


def test_replan_keeps_global_case_ordinals_separate_from_batch_ordinals(
    tmp_path: Path,
) -> None:
    ledger = CoverageLedger(tmp_path / "coverage.json")
    first = CoverageScheduler(
        catalog=FeatureCatalog((_spec("feature_a"), _spec("feature_b"))),
        ledger=ledger,
        min_hits=1,
        version=(8, 0, 41),
        schedule_seed=8,
        plan_start_ordinal=40,
    )
    selected = first.choose(case_ordinal=40)
    ledger.record(selected.feature_id)

    with pytest.raises(ValueError, match="plan end"):
        first.replan(start_case_ordinal=41)

    second = first.replan(start_case_ordinal=42)
    with pytest.raises(ValueError, match="predates"):
        second.choose(case_ordinal=40)
    remaining = second.choose(case_ordinal=42)
    assert remaining.feature_id != selected.feature_id
    ledger.record(remaining.feature_id)
    assert second.cycle_complete()
    assert first.plan_end_ordinal == second.plan_start_ordinal == 42


def test_new_scheduler_advances_to_the_next_cumulative_coverage_cycle(
    tmp_path: Path,
) -> None:
    ledger = CoverageLedger(tmp_path / "coverage.json")
    ledger.record("feature_a", hits=2)
    ledger.record("feature_b", hits=2)

    scheduler = CoverageScheduler(
        catalog=FeatureCatalog((_spec("feature_a"), _spec("feature_b"))),
        ledger=ledger,
        min_hits=2,
        version=(8, 0, 41),
    )

    assert scheduler.cycle_number == 2
    assert scheduler.cycle_target_hits == 4
    assert scheduler.planned_case_count == 4
    assert not scheduler.cycle_complete()


def test_catalog_exposes_versioned_directed_signature_targets() -> None:
    catalog = FeatureCatalog(
        (
            _spec("join_inner"),
            FeatureSpec(
                feature_id="future_shape",
                family="set_operation",
                min_version=(8, 0, 99),
                compatible_profiles=frozenset({"regular_innodb"}),
                ast_nodes=frozenset({"set_operation"}),
                guards=frozenset({"read_only_select"}),
            ),
        )
    )

    targets = catalog.signature_targets(version=(8, 0, 41))
    assert [target.feature_id for target in targets] == ["join_inner"]
    assert catalog.directed_target("join_inner").feature_id == "join_inner"
    with pytest.raises(KeyError):
        catalog.directed_target("missing")


def test_official_catalog_v2_is_consumable_when_present() -> None:
    path = Path(__file__).resolve().parents[2] / "catalog" / "mysql-8.0.41-query-shapes.yaml"
    catalog = FeatureCatalog.from_yaml(
        path,
        generator_supported_ids=frozenset(
            {"select_query_specification", "cte_recursive"}
        ),
    )
    targets = {spec.feature_id for spec in catalog.signature_targets(version=(8, 0, 41))}
    evidence_gaps = {
        spec.feature_id for spec in catalog.evidence_lock_gaps(version=(8, 0, 41))
    }

    assert targets == {"select_query_specification", "cte_recursive"}
    assert evidence_gaps == set()
    assert len(catalog.catalogued_gaps(version=(8, 0, 41))) == 62
