from __future__ import annotations

import re

import pytest

from select_fuzz.generation.catalog import FeatureCatalog
from select_fuzz.generation.catalog_schema import REVIEWED_VARIANT_IDS
from select_fuzz.generation.query_scope import (
    DEFAULT_QUERY_SCOPE,
    QueryCoverageScope,
    QueryExclusionReason,
)
from select_fuzz.generation.query_grammar import (
    GrammarColumn,
    GrammarQueryGenerator,
    GrammarSchema,
    GrammarTable,
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


def _catalog() -> FeatureCatalog:
    return FeatureCatalog.default(generator_supported_ids=REVIEWED_VARIANT_IDS)


def test_default_query_scope_excludes_only_user_declared_families() -> None:
    catalog = _catalog()

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
    catalog = _catalog()
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
    catalog = _catalog()

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


def test_default_scope_also_constrains_grammar_random_candidates() -> None:
    generator = GrammarQueryGenerator()
    schema = GrammarSchema(
        (
            GrammarTable(
                "t0",
                (
                    GrammarColumn("id", "BIGINT"),
                    GrammarColumn("txt", "VARCHAR(64)"),
                    GrammarColumn("doc", "JSON"),
                    GrammarColumn("shape", "POINT"),
                ),
            ),
        )
    )

    candidates = tuple(
        generator.generate(
            schema,
            seed=80_410_000 + seed,
            excluded_families=DEFAULT_QUERY_SCOPE.excluded_families,
        ).sql
        for seed in range(500)
    )

    assert candidates
    assert all(
        re.search(r"\b(?:JSON_[A-Z0-9_]*|ST_[A-Z0-9_]*)\s*\(", sql) is None
        and re.search(r"\bAS\s+JSON\b", sql, re.IGNORECASE) is None
        and "JSON_TABLE" not in sql.upper()
        and "MATCH" not in sql.upper()
        for sql in candidates
    )
