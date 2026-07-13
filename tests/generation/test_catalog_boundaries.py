from __future__ import annotations

from collections.abc import Callable

import pytest

from select_fuzz.generation.catalog import (
    CapabilityStatus,
    FeatureCatalog,
    FeatureSpec,
    _records,
    _string_set,
)
from select_fuzz.generation.catalog_schema import CatalogError


def spec(**overrides: object) -> FeatureSpec:
    values: dict[str, object] = {
        "feature_id": "feature",
        "family": "query",
        "min_version": (8, 0, 0),
        "compatible_profiles": frozenset({"regular_innodb"}),
        "ast_nodes": frozenset({"query_expression"}),
        "guards": frozenset({"read_only_select"}),
    }
    values.update(overrides)
    return FeatureSpec(**values)


@pytest.mark.parametrize(
    ("factory", "error"),
    [
        (lambda: _string_set([], "items"), CatalogError),
        (lambda: _records([], "records"), CatalogError),
        (lambda: _records([1], "records"), CatalogError),
        (lambda: spec(feature_id="Bad"), CatalogError),
        (lambda: spec(min_version=(8, 0)), ValueError),
        (lambda: spec(min_version=(8, 0, -1)), ValueError),
        (lambda: spec(compatible_profiles=frozenset()), ValueError),
        (lambda: spec(ast_nodes=frozenset()), ValueError),
        (lambda: spec(guards=frozenset()), ValueError),
        (
            lambda: spec(
                evidence_lock_ready=True,
                unverified_evidence_sources=frozenset({"source"}),
            ),
            ValueError,
        ),
        (lambda: spec(weight=0), ValueError),
        (lambda: FeatureCatalog((spec(), spec())), ValueError),
    ],
)
def test_catalog_value_objects_reject_invalid_registry_states(
    factory: Callable[[], object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        factory()


def test_catalog_filters_supported_gap_evidence_version_and_profiles() -> None:
    supported = spec(feature_id="supported")
    gap = spec(feature_id="gap", capability_status=CapabilityStatus.CATALOGUED_GAP)
    blocked = spec(
        feature_id="blocked",
        evidence_lock_ready=False,
        unverified_evidence_sources=frozenset({"source"}),
    )
    future = spec(feature_id="future", min_version=(8, 0, 42))
    catalog = FeatureCatalog((supported, gap, blocked, future))

    assert tuple(catalog) == (supported, gap, blocked, future)
    assert len(catalog) == 4
    assert catalog.signature_targets(version=(8, 0, 41)) == (supported,)
    assert catalog.catalogued_gaps(version=(8, 0, 41)) == (gap,)
    assert catalog.evidence_lock_gaps(version=(8, 0, 41)) == (blocked,)
    assert catalog.signature_targets(
        version=(8, 0, 41), profiles=frozenset({"partitioned_innodb"})
    ) == ()
    assert catalog.directed_target("supported") is supported
