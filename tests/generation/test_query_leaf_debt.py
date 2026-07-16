from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from select_fuzz.generation.catalog import CapabilityStatus, FeatureCatalog, FeatureSpec
from select_fuzz.generation.coverage import CoverageLedger, CoverageScheduler
from select_fuzz.generation.function_registry import DETERMINISTIC_FUNCTION_SIGNATURES
from select_fuzz.generation.query import QueryBatchPlanner, QueryGenerator, QueryLane
from select_fuzz.generation.query_safety import ReadOnlyValidator
from select_fuzz.generation.schema import (
    ColumnDef,
    IndexDef,
    IndexExpression,
    IndexKind,
    IndexPart,
    SchemaManifest,
    SchemaProfile,
    SortDirection,
    TableDef,
)


def _target(feature_id: str) -> FeatureSpec:
    return FeatureSpec(
        feature_id,
        "query",
        (8, 0, 41),
        frozenset({SchemaProfile.REGULAR_INNODB.value}),
        frozenset({"query_expression"}),
        frozenset({"read_only_select"}),
        capability_status=CapabilityStatus.GENERATOR_SUPPORTED,
        evidence_lock_ready=True,
    )


def _manifest() -> SchemaManifest:
    tables = tuple(
        TableDef(
            f"t{ordinal}",
            False,
            (
                ColumnDef("id", "BIGINT UNSIGNED", False),
                ColumnDef(
                    "payload",
                    "VARCHAR(64)",
                    True,
                    "utf8mb4",
                    "utf8mb4_0900_ai_ci",
                ),
                ColumnDef("amount", "INT", True),
                ColumnDef("created_at", "DATETIME(6)", True),
            ),
            (
                IndexDef(
                    "PRIMARY",
                    (IndexPart(column_name="id"),),
                    unique=True,
                    primary=True,
                ),
                IndexDef(
                    "ix_id_desc",
                    (IndexPart(column_name="id", direction=SortDirection.DESC),),
                ),
                IndexDef(
                    "ix_payload_prefix",
                    (IndexPart(column_name="payload", prefix_length=8),),
                ),
                IndexDef(
                    "ix_payload_lower",
                    (IndexPart(expression=IndexExpression.lower_char("payload", 32)),),
                    kind=IndexKind.FUNCTIONAL,
                ),
            ),
        )
        for ordinal in range(3)
    )
    return SchemaManifest(
        SchemaProfile.REGULAR_INNODB,
        "select_query_specification",
        8_041,
        tables,
    )


def _scheduler(
    *,
    target: FeatureSpec,
    ledger: CoverageLedger,
    min_hits: int,
) -> CoverageScheduler:
    return CoverageScheduler(
        catalog=FeatureCatalog((target,)),
        ledger=ledger,
        min_hits=min_hits,
        version=(8, 0, 41),
        profiles=frozenset({SchemaProfile.REGULAR_INNODB.value}),
        schedule_seed=80_410,
    )


def test_function_leaf_registry_covers_every_signature_and_null_position() -> None:
    leaves = QueryGenerator().directed_leaf_variants("function_deterministic_scalar")
    variants = {leaf.variant_id for leaf in leaves}
    expected = {signature.signature_id for signature in DETERMINISTIC_FUNCTION_SIGNATURES} | {
        f"{signature.signature_id}_null_{position}"
        for signature in DETERMINISTIC_FUNCTION_SIGNATURES
        for position in signature.null_argument_positions
    }

    assert expected <= variants
    assert len({leaf.coverage_tag for leaf in leaves}) == len(leaves)
    assert all(
        leaf.coverage_tag == f"query_leaf:function_deterministic_scalar:{leaf.variant_id}"
        for leaf in leaves
    )


def test_planner_prioritizes_the_persisted_least_hit_leaf(tmp_path: Path) -> None:
    generator = QueryGenerator()
    target = _target("function_aggregate")
    leaves = generator.directed_leaf_variants(target.feature_id)
    missing = next(leaf for leaf in leaves if leaf.variant_id == "aggregate_all_null")
    ledger = CoverageLedger(tmp_path / "coverage.json")
    for leaf in leaves:
        if leaf != missing:
            ledger.record(leaf.coverage_tag, hits=2)
    ledger.checkpoint()
    restored = CoverageLedger.load(ledger.path)

    query = QueryBatchPlanner(generator).plan(
        _manifest(),
        scheduler=_scheduler(
            target=target,
            ledger=restored,
            min_hits=1,
        ),
        run_seed=71,
        start_case_ordinal=0,
        queries_per_round=1,
        lane=QueryLane.VALID,
        estimated_rows_by_table={"t0": 3, "t1": 3, "t2": 3},
    )[0]

    assert missing.coverage_tag in query.feature_tags
    assert "WHERE (`t`.`amount` IS NULL)" in query.sql


def test_batch_leaf_reservations_spread_equal_debt_before_repeating(
    tmp_path: Path,
) -> None:
    generator = QueryGenerator()
    target = _target("function_aggregate")
    ledger = CoverageLedger(tmp_path / "coverage.json")
    leaves = generator.directed_leaf_variants(target.feature_id)
    leaf_tags = {leaf.coverage_tag for leaf in leaves}

    queries = QueryBatchPlanner(generator).plan(
        _manifest(),
        scheduler=_scheduler(
            target=target,
            ledger=ledger,
            min_hits=4,
        ),
        run_seed=73,
        start_case_ordinal=0,
        queries_per_round=4,
        lane=QueryLane.VALID,
        estimated_rows_by_table={"t0": 3, "t1": 3, "t2": 3},
    )

    selected = [query.feature_tags & leaf_tags for query in queries]
    assert all(len(tags) == 1 for tags in selected)
    assert len(set().union(*selected)) == 4


def test_planner_covers_all_function_signature_and_null_witness_leaves(
    tmp_path: Path,
) -> None:
    generator = QueryGenerator()
    target = _target("function_deterministic_scalar")
    leaves = generator.directed_leaf_variants(target.feature_id)
    function_variants = {
        signature.signature_id for signature in DETERMINISTIC_FUNCTION_SIGNATURES
    } | {
        f"{signature.signature_id}_null_{position}"
        for signature in DETERMINISTIC_FUNCTION_SIGNATURES
        for position in signature.null_argument_positions
    }
    function_tags = {leaf.coverage_tag for leaf in leaves if leaf.variant_id in function_variants}
    ledger = CoverageLedger(tmp_path / "coverage.json")
    for leaf in leaves:
        if leaf.variant_id not in function_variants:
            ledger.record(leaf.coverage_tag)

    queries = QueryBatchPlanner(generator).plan(
        _manifest(),
        scheduler=_scheduler(
            target=target,
            ledger=ledger,
            min_hits=len(function_tags),
        ),
        run_seed=79,
        start_case_ordinal=0,
        queries_per_round=len(function_tags),
        lane=QueryLane.VALID,
        estimated_rows_by_table={"t0": 3, "t1": 3, "t2": 3},
    )

    observed = set().union(*(query.feature_tags & function_tags for query in queries))
    assert len(function_tags) == 335
    assert observed == function_tags


def test_nonvalid_lanes_do_not_claim_a_directed_leaf() -> None:
    generator = QueryGenerator()
    target = _target("function_aggregate")
    leaf = generator.directed_leaf_variants(target.feature_id)[0]

    for lane in (QueryLane.FREE_RANDOM, QueryLane.NEGATIVE):
        generated = generator.generate(
            _manifest(),
            target=target,
            seed=83,
            lane=lane,
            directed_variant=leaf.variant_id,
            estimated_rows_by_table={"t0": 3, "t1": 3, "t2": 3},
        )
        assert leaf.coverage_tag not in generated.feature_tags
        assert not generated.coverage_eligible


def test_unreachable_leaf_falls_back_within_the_same_feature(tmp_path: Path) -> None:
    generator = QueryGenerator()
    target = _target("join_inner_cross_straight")
    manifest = _manifest()
    manifest = replace(
        manifest,
        tables=tuple(
            replace(
                table,
                columns=tuple(
                    replace(column, nullable=False) if column.name == "amount" else column
                    for column in table.columns
                ),
            )
            for table in manifest.tables
        ),
    )
    leaves = generator.directed_leaf_variants(target.feature_id)
    unreachable = next(leaf for leaf in leaves if leaf.variant_id == "nullable_key_both")
    ledger = CoverageLedger(tmp_path / "coverage.json")
    for leaf in leaves:
        if leaf != unreachable:
            ledger.record(leaf.coverage_tag)

    generated = QueryBatchPlanner(generator).plan(
        manifest,
        scheduler=_scheduler(target=target, ledger=ledger, min_hits=1),
        run_seed=89,
        start_case_ordinal=0,
        queries_per_round=1,
        lane=QueryLane.VALID,
        estimated_rows_by_table={"t0": 3, "t1": 3, "t2": 3},
    )[0]

    assert unreachable.coverage_tag not in generated.feature_tags
    assert any(leaf.coverage_tag in generated.feature_tags for leaf in leaves)
    ReadOnlyValidator().validate_text(generated.sql)


def test_unreachable_single_debt_target_falls_back_to_compatible_catalog_target(
    tmp_path: Path,
) -> None:
    generator = QueryGenerator()
    unreachable = _target("regression_8041_desc_pk_index_merge")
    fallback = _target("select_query_specification")
    manifest = _manifest()
    manifest = replace(
        manifest,
        tables=tuple(
            replace(
                table,
                indexes=tuple(index for index in table.indexes if index.primary),
            )
            for table in manifest.tables
        ),
    )
    ledger = CoverageLedger(tmp_path / "coverage.json")
    ledger.record(fallback.feature_id)
    scheduler = CoverageScheduler(
        catalog=FeatureCatalog((unreachable, fallback)),
        ledger=ledger,
        min_hits=1,
        version=(8, 0, 41),
        profiles=frozenset({SchemaProfile.REGULAR_INNODB.value}),
        schedule_seed=80_410,
    )

    assert scheduler.planned_case_count == 1
    assert scheduler.choose(case_ordinal=0).feature_id == unreachable.feature_id

    generated = QueryBatchPlanner(generator).plan(
        manifest,
        scheduler=scheduler,
        run_seed=91,
        start_case_ordinal=0,
        queries_per_round=1,
        lane=QueryLane.VALID,
        estimated_rows_by_table={"t0": 3, "t1": 3, "t2": 3},
        allow_compatible_fallback=True,
    )[0]

    assert generated.target_feature_id == fallback.feature_id
    ReadOnlyValidator().validate_text(generated.sql)


def test_every_registered_leaf_has_a_reachable_read_only_witness() -> None:
    generator = QueryGenerator()
    manifest = _manifest()
    rows = {table.name: 3 for table in manifest.tables}

    for target in generator.feature_catalog():
        leaves = generator.directed_leaf_variants(target.feature_id)
        for ordinal, leaf in enumerate(leaves):
            generated = generator.generate(
                manifest,
                target=_target(target.feature_id),
                seed=ordinal,
                case_ordinal=ordinal,
                lane=QueryLane.VALID,
                directed_variant=leaf.variant_id,
                estimated_rows_by_table=rows,
            )
            assert leaf.coverage_tag in generated.feature_tags
            ReadOnlyValidator().validate_text(generated.sql)
