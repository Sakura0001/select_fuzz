from __future__ import annotations

import re

import pytest

from select_fuzz.generation.query import (
    QueryGenerator,
    QueryLane,
    TargetNotReachable,
)
from select_fuzz.generation.query_scope import (
    DEFAULT_QUERY_SCOPE,
    QueryCoverageScope,
    QueryExclusionReason,
)
from select_fuzz.generation.schema import (
    IndexKind,
    SchemaGenerator,
    SchemaLimits,
)


USER_EXCLUDED_FEATURE_IDS = frozenset(
    {
        "function_fulltext_spatial",
        "index_fulltext",
        "index_multivalue",
        "index_spatial",
        "json_create_extract",
        "json_member_overlap",
        "json_schema_validation",
        "json_table_columns",
        "json_table_implicit_lateral",
        "json_value_scalar",
        "scene_fulltext",
        "scene_json_multivalue",
        "scene_spatial",
    }
)


def test_default_query_scope_excludes_only_user_declared_families() -> None:
    catalog = QueryGenerator.feature_catalog()

    assert DEFAULT_QUERY_SCOPE.excluded_feature_ids == USER_EXCLUDED_FEATURE_IDS
    assert set(DEFAULT_QUERY_SCOPE.exclusion_reasons) == USER_EXCLUDED_FEATURE_IDS
    assert {
        DEFAULT_QUERY_SCOPE.exclusion_reasons[feature_id]
        for feature_id in USER_EXCLUDED_FEATURE_IDS
    } == {
        QueryExclusionReason.JSON,
        QueryExclusionReason.FULLTEXT,
        QueryExclusionReason.SPATIAL,
    }
    assert USER_EXCLUDED_FEATURE_IDS < {spec.feature_id for spec in catalog}


def test_default_query_scope_filters_both_catalog_rows_and_scheduling_targets() -> None:
    catalog = QueryGenerator.feature_catalog()
    scoped = DEFAULT_QUERY_SCOPE.filter_catalog(catalog)

    assert {spec.feature_id for spec in scoped} == {
        spec.feature_id for spec in catalog if spec.feature_id not in USER_EXCLUDED_FEATURE_IDS
    }
    assert not USER_EXCLUDED_FEATURE_IDS.intersection(
        spec.feature_id for spec in scoped.signature_targets(version=(8, 0, 41))
    )
    assert {
        "fulltext_innodb",
        "json_multivalue_innodb",
        "spatial_innodb",
    }.isdisjoint(profile for spec in scoped for profile in spec.compatible_profiles)
    temporal = scoped.directed_target("type_temporal_json_spatial")
    assert temporal.compatible_profiles == frozenset({"regular_innodb"})


def test_query_scope_rejects_unknown_or_unexplained_exclusions() -> None:
    catalog = QueryGenerator.feature_catalog()

    DEFAULT_QUERY_SCOPE.validate_catalog(catalog)
    with pytest.raises(TypeError, match="typed reason"):
        QueryCoverageScope({"json_create_extract": "json"})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="unknown catalog IDs"):
        QueryCoverageScope({"missing_feature": QueryExclusionReason.JSON}).validate_catalog(catalog)
    with pytest.raises(ValueError, match="unknown profiles"):
        QueryCoverageScope(
            {},
            excluded_profile_reasons={"missing_profile": QueryExclusionReason.SPATIAL},
        ).validate_catalog(catalog)


def test_every_default_scoped_query_and_schema_omits_excluded_families() -> None:
    generator = QueryGenerator()
    catalog = DEFAULT_QUERY_SCOPE.filter_catalog(generator.feature_catalog())
    schemas = SchemaGenerator()
    limits = SchemaLimits(max_tables=3, max_columns=7)

    for ordinal, target in enumerate(catalog.signature_targets(version=(8, 0, 41))):
        for attempt in range(32):
            seed = 80_410_000 + ordinal * 32 + attempt
            manifest = schemas.generate(target, seed=seed, limits=limits)
            try:
                generated = generator.generate(
                    manifest,
                    target=target,
                    seed=seed + 1,
                    lane=QueryLane.VALID,
                    estimated_rows_by_table={table.name: 8 for table in manifest.tables},
                )
            except TargetNotReachable:
                continue
            break
        else:
            pytest.fail(f"scoped target is unreachable: {target.feature_id}")

        assert not re.search(r"\b(?:JSON_[A-Z0-9_]*|ST_[A-Z0-9_]*)\s*\(", generated.sql)
        assert "MATCH(" not in generated.sql.upper()
        assert all(
            index.kind not in {IndexKind.FULLTEXT, IndexKind.MULTIVALUE, IndexKind.SPATIAL}
            for table in manifest.tables
            for index in table.indexes
        )
