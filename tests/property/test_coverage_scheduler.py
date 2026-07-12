from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from hypothesis import given, strategies as st

from select_fuzz.generation.catalog import FeatureCatalog, FeatureSpec
from select_fuzz.generation.coverage import CoverageLedger, CoverageScheduler


@given(
    feature_count=st.integers(min_value=1, max_value=20),
    min_hits=st.integers(min_value=1, max_value=20),
    seed=st.integers(min_value=0, max_value=2**32 - 1),
)
def test_debt_scheduler_completes_every_enabled_feature(
    feature_count: int, min_hits: int, seed: int
) -> None:
    specs = tuple(
        FeatureSpec(
            feature_id=f"feature_{index}",
            family="property",
            min_version=(8, 0, 0),
            compatible_profiles=frozenset({"regular_innodb"}),
            ast_nodes=frozenset({"query_expression"}),
            guards=frozenset({"read_only_select"}),
            weight=float(index + 1),
        )
        for index in range(feature_count)
    )
    with TemporaryDirectory() as directory:
        ledger = CoverageLedger(Path(directory) / "coverage.json")
        scheduler = CoverageScheduler(
            catalog=FeatureCatalog(specs),
            ledger=ledger,
            min_hits=min_hits,
            version=(8, 0, 41),
            schedule_seed=seed,
        )
        for ordinal in range(feature_count * min_hits):
            selected = scheduler.choose(case_ordinal=ordinal)
            ledger.record(selected.feature_id)

        assert scheduler.cycle_complete()
        assert all(ledger.hits(spec.feature_id) == min_hits for spec in specs)
