from __future__ import annotations

import random

import pytest

from select_fuzz.generation.catalog import CapabilityStatus, FeatureSpec
from select_fuzz.generation.query import (
    EvidenceGateError,
    QueryBudget,
    QueryGenerator,
    TargetNotReachable,
    UnsupportedQueryFeature,
)
from select_fuzz.generation.schema import (
    ColumnDef,
    IndexDef,
    IndexPart,
    SchemaManifest,
    SchemaProfile,
    TableDef,
)


TABLE = TableDef(
    "items",
    False,
    (ColumnDef("id", "BIGINT", False),),
    (
        IndexDef(
            "PRIMARY",
            (IndexPart(column_name="id"),),
            unique=True,
            primary=True,
        ),
    ),
)
MANIFEST = SchemaManifest(
    SchemaProfile.REGULAR_INNODB, "query_boundary", 1, (TABLE,)
)


def target(
    feature_id: str = "select_query_specification",
    *,
    status: CapabilityStatus = CapabilityStatus.GENERATOR_SUPPORTED,
    ready: bool = True,
    version: tuple[int, int, int] = (8, 0, 0),
    profiles: frozenset[str] = frozenset({SchemaProfile.REGULAR_INNODB.value}),
) -> FeatureSpec:
    return FeatureSpec(
        feature_id,
        "query",
        version,
        profiles,
        frozenset({"query_expression"}),
        frozenset({"read_only_select"}),
        capability_status=status,
        evidence_lock_ready=ready,
        unverified_evidence_sources=frozenset() if ready else frozenset({"source"}),
    )


def test_query_request_gate_rejects_runtime_type_registry_evidence_version_and_profile_errors() -> None:
    generator = QueryGenerator()
    cases = [
        (target(), True, 0, TypeError),
        (target(), 1, -1, ValueError),
        (target("unsupported_feature"), 1, 0, UnsupportedQueryFeature),
        (
            target(status=CapabilityStatus.CATALOGUED_GAP),
            1,
            0,
            UnsupportedQueryFeature,
        ),
        (target(ready=False), 1, 0, EvidenceGateError),
        (target(version=(8, 0, 42)), 1, 0, UnsupportedQueryFeature),
        (
            target(profiles=frozenset({SchemaProfile.PARTITIONED_INNODB.value})),
            1,
            0,
            TargetNotReachable,
        ),
    ]
    for candidate, seed, ordinal, error in cases:
        with pytest.raises(error):
            generator._validate_request(
                MANIFEST, target=candidate, seed=seed, case_ordinal=ordinal
            )


def test_row_estimates_reject_unknown_noninteger_boolean_and_negative_values() -> None:
    budget = QueryBudget(default_rows_per_table=7)
    assert QueryGenerator._row_estimates(MANIFEST, None, budget) == {"items": 7}
    with pytest.raises(ValueError, match="unknown tables"):
        QueryGenerator._row_estimates(MANIFEST, {"missing": 1}, budget)
    for value in (True, -1, 1.5):
        with pytest.raises(ValueError, match="nonnegative integers"):
            QueryGenerator._row_estimates(MANIFEST, {"items": value}, budget)


def test_generation_rejects_non_lane_values_and_mismatched_top_n_targets() -> None:
    generator = QueryGenerator()
    with pytest.raises(TypeError, match="lane"):
        generator.generate(MANIFEST, target=target(), seed=1, lane="valid")
    with pytest.raises(TargetNotReachable, match="top-N"):
        generator._build_feature(
            MANIFEST,
            feature_id="select_parenthesized",
            rng=random.Random(1),
            rows={"items": 1},
            budget=QueryBudget(),
            require_top_n=True,
            directed_variant=None,
            free_random=False,
        )
