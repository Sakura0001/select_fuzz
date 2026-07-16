from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml
from yaml.constructor import ConstructorError

import select_fuzz.generation.catalog as catalog_module
from select_fuzz.generation.catalog import CapabilityStatus, FeatureCatalog
from select_fuzz.generation.catalog_schema import (
    ALLOWED_CATEGORIES,
    CatalogError,
    REVIEWED_CATALOG_SHA256,
    REVIEWED_FEATURE_IDS,
    REVIEWED_SOURCE_IDS,
    REVIEWED_VARIANT_IDS,
    canonical_catalog_sha256,
    load_and_validate_catalog,
    load_catalog_text,
    validate_catalog,
)


CATALOG_PATH = Path(__file__).resolve().parents[2] / "catalog" / "mysql-8.0.41-query-shapes.yaml"


def _catalog() -> Mapping[str, object]:
    return load_and_validate_catalog(CATALOG_PATH)


def _mutated_catalog() -> dict[str, object]:
    return copy.deepcopy(dict(_catalog()))


def _features(catalog: Mapping[str, object]) -> list[dict[str, object]]:
    return catalog["features"]  # type: ignore[return-value]


def _sources(catalog: Mapping[str, object]) -> list[dict[str, object]]:
    return catalog["sources"]  # type: ignore[return-value]


def _variants(feature: Mapping[str, object]) -> list[dict[str, object]]:
    return feature["variants"]  # type: ignore[return-value]


def test_catalog_v2_contract_locks_the_reviewed_manifest_exactly() -> None:
    catalog = _catalog()
    features = _features(catalog)
    sources = _sources(catalog)

    assert catalog["schema_version"] == 2
    assert catalog["target_version"] == "8.0.41"
    assert {source["source_id"] for source in sources} == REVIEWED_SOURCE_IDS
    assert {feature["feature_id"] for feature in features} == REVIEWED_FEATURE_IDS
    assert {
        variant["variant_id"] for feature in features for variant in _variants(feature)
    } == REVIEWED_VARIANT_IDS
    assert len(REVIEWED_SOURCE_IDS) == 23
    assert len(REVIEWED_FEATURE_IDS) == 19
    assert len(REVIEWED_VARIANT_IDS) == 64
    assert {feature["category"] for feature in features} == ALLOWED_CATEGORIES
    assert canonical_catalog_sha256(catalog) == REVIEWED_CATALOG_SHA256


def test_source_hash_scope_and_refresh_state_are_explicit() -> None:
    sources = _sources(_catalog())
    verified = {source["source_id"] for source in sources if source["lock_state"] == "verified"}

    assert verified == REVIEWED_SOURCE_IDS
    for source in sources:
        expected_scope = (
            "response_bytes" if source["kind"] == "exact_source" else "docs_body_text_v1"
        )
        assert source["hash_scope"] == expected_scope
        if source["lock_state"] == "verified":
            assert isinstance(source["content_sha256"], str)
        else:
            assert source["lock_state"] == "refresh_required"
            assert source["content_sha256"] is None


def test_duplicate_yaml_key_is_rejected() -> None:
    text = CATALOG_PATH.read_text(encoding="utf-8")
    with pytest.raises(ConstructorError, match="duplicate key"):
        load_catalog_text("schema_version: 2\n" + text)


def test_future_syntax_version_is_rejected() -> None:
    catalog = _mutated_catalog()
    _variants(_features(catalog)[0])[0]["min_version"] = "8.0.99"
    with pytest.raises(CatalogError, match="future version"):
        validate_catalog(catalog)


@pytest.mark.parametrize(
    ("field", "payload"),
    [
        ("ast_nodes", ["select * from t"]),
        ("variant_id", "select"),
        ("guards", ["where x = 1"]),
    ],
)
def test_executable_sql_text_in_structure_fields_is_rejected(
    field: str,
    payload: object,
) -> None:
    catalog = _mutated_catalog()
    _variants(_features(catalog)[0])[0][field] = payload
    with pytest.raises(CatalogError):
        validate_catalog(catalog)


def test_unknown_category_is_rejected() -> None:
    catalog = _mutated_catalog()
    _features(catalog)[0]["category"] = "future_syntax"
    with pytest.raises(CatalogError, match="unknown feature.category"):
        validate_catalog(catalog)


def test_unknown_schema_key_is_rejected() -> None:
    catalog = _mutated_catalog()
    _features(catalog)[0]["description"] = "free text"
    with pytest.raises(CatalogError, match="feature keys"):
        validate_catalog(catalog)


def test_missing_per_variant_evidence_is_rejected() -> None:
    catalog = _mutated_catalog()
    _variants(_features(catalog)[0])[0]["evidence"] = []
    with pytest.raises(CatalogError, match="evidence must be a non-empty list"):
        validate_catalog(catalog)


@pytest.mark.parametrize("record_kind", ["source", "feature", "variant"])
def test_unreviewed_manifest_id_is_rejected(record_kind: str) -> None:
    catalog = _mutated_catalog()
    if record_kind == "source":
        _sources(catalog)[0]["source_id"] = "unreviewed_source"
    elif record_kind == "feature":
        _features(catalog)[0]["feature_id"] = "unreviewed_feature"
    else:
        _variants(_features(catalog)[0])[0]["variant_id"] = "unreviewed_variant"

    with pytest.raises(CatalogError, match="reviewed .* manifest"):
        validate_catalog(catalog)


def test_source_url_must_equal_the_reviewed_canonical_url() -> None:
    catalog = _mutated_catalog()
    _sources(catalog)[0]["url"] = "https://dev.mysql.com/doc/refman/8.0/en/select.html"

    with pytest.raises(CatalogError, match="reviewed source manifest"):
        validate_catalog(catalog)


def test_exact_source_cannot_claim_the_document_text_hash_scope() -> None:
    catalog = _mutated_catalog()
    _sources(catalog)[0]["hash_scope"] = "docs_body_text_v1"

    with pytest.raises(CatalogError, match="must use hash_scope response_bytes"):
        validate_catalog(catalog)


def test_reviewed_catalog_digest_rejects_shape_valid_semantic_edits() -> None:
    catalog = _mutated_catalog()
    _variants(_features(catalog)[0])[0]["weight"] = 9

    with pytest.raises(CatalogError, match="reviewed canonical catalog digest"):
        validate_catalog(catalog)

    catalog = _mutated_catalog()
    _sources(catalog)[0]["content_sha256"] = "0" * 64
    with pytest.raises(CatalogError, match="reviewed canonical catalog digest"):
        validate_catalog(catalog)


def test_regex_locator_must_not_match_empty_text() -> None:
    catalog = _mutated_catalog()
    release = next(source for source in _sources(catalog) if source["source_id"] == "release_8001")
    release["locators"]["common_table_expressions"]["pattern"] = ".*"

    with pytest.raises(CatalogError, match="must not match empty text"):
        validate_catalog(catalog)


def test_every_evidence_locator_has_one_machine_verifiable_manifest() -> None:
    catalog = _catalog()
    source_locators = {source["source_id"]: source["locators"] for source in _sources(catalog)}
    referenced: set[tuple[object, object]] = set()

    for feature in _features(catalog):
        for record in [feature, *_variants(feature)]:
            for evidence in record["evidence"]:  # type: ignore[union-attr]
                source_id = evidence["source_id"]
                locator = evidence["locator"]
                referenced.add((source_id, locator))
                manifest = source_locators[source_id][locator]  # type: ignore[index]
                assert manifest["match_kind"] in {"literal", "regex"}
                assert manifest["pattern"]

    manifested = {
        (source_id, locator)
        for source_id, locators in source_locators.items()
        for locator in locators  # type: ignore[union-attr]
    }
    assert manifested == referenced


def test_feature_catalog_round_trips_all_64_variant_rows() -> None:
    document = _catalog()
    expected = []
    for feature in _features(document):
        for variant in _variants(feature):
            expected.append(
                (
                    variant["variant_id"],
                    feature["category"],
                    tuple(int(part) for part in variant["min_version"].split(".")),
                    frozenset(variant["profiles"]),
                    frozenset(variant["guards"]),
                    tuple((item["source_id"], item["locator"]) for item in variant["evidence"]),
                )
            )

    loaded = FeatureCatalog.from_yaml(CATALOG_PATH)
    actual = [
        (
            spec.feature_id,
            spec.family,
            spec.min_version,
            spec.compatible_profiles,
            spec.guards,
            tuple((item.source_id, item.locator) for item in spec.evidence),
        )
        for spec in loaded
    ]

    assert len(actual) == 64
    assert actual == expected


def test_feature_catalog_from_yaml_calls_the_production_validator(
    tmp_path: Path,
) -> None:
    catalog = _mutated_catalog()
    _features(catalog)[0]["category"] = "silently_ignored_category"
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(catalog, sort_keys=False), encoding="utf-8")

    with pytest.raises(CatalogError, match="unknown feature.category"):
        FeatureCatalog.from_yaml(path)


def test_loaded_catalog_is_distinct_from_generator_supported_registry() -> None:
    loaded = FeatureCatalog.from_yaml(CATALOG_PATH)

    assert len(loaded) == 64
    assert loaded.signature_targets(version=(8, 0, 41)) == ()
    assert len(loaded.catalogued_gaps(version=(8, 0, 41))) == 64
    assert {spec.capability_status for spec in loaded} == {CapabilityStatus.CATALOGUED_GAP}

    registered = FeatureCatalog.from_yaml(
        CATALOG_PATH,
        generator_supported_ids=frozenset({"select_query_specification"}),
    )
    assert [spec.feature_id for spec in registered.signature_targets(version=(8, 0, 41))] == [
        "select_query_specification"
    ]
    assert (
        registered.directed_target("select_query_specification").capability_status
        is CapabilityStatus.GENERATOR_SUPPORTED
    )
    assert registered.directed_target("select_query_specification").evidence_lock_ready


def test_verified_parent_and_variant_evidence_can_be_scheduled() -> None:
    registered = FeatureCatalog.from_yaml(
        CATALOG_PATH,
        generator_supported_ids=frozenset({"cte_recursive"}),
    )

    spec = registered.directed_target("cte_recursive")
    assert spec.capability_status is CapabilityStatus.GENERATOR_SUPPORTED
    assert spec.evidence_lock_ready
    assert [item.feature_id for item in registered.signature_targets(version=(8, 0, 41))] == [
        "cte_recursive"
    ]
    assert registered.evidence_lock_gaps(version=(8, 0, 41)) == ()


def test_refresh_required_parent_or_variant_evidence_cannot_be_scheduled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _mutated_catalog()
    release = next(
        source for source in _sources(catalog) if source["source_id"] == "release_8001"
    )
    release["lock_state"] = "refresh_required"
    release["content_sha256"] = None
    monkeypatch.setattr(
        catalog_module,
        "load_and_validate_catalog",
        lambda _path: catalog,
    )

    registered = FeatureCatalog.from_yaml(
        CATALOG_PATH,
        generator_supported_ids=frozenset({"cte_recursive"}),
    )

    spec = registered.directed_target("cte_recursive")
    assert spec.capability_status is CapabilityStatus.GENERATOR_SUPPORTED
    assert not spec.evidence_lock_ready
    assert registered.signature_targets(version=(8, 0, 41)) == ()
    assert [gap.feature_id for gap in registered.evidence_lock_gaps(version=(8, 0, 41))] == [
        "cte_recursive"
    ]


def test_generator_registry_rejects_an_unreviewed_variant() -> None:
    with pytest.raises(CatalogError, match="registry contains unknown variants"):
        FeatureCatalog.from_yaml(
            CATALOG_PATH,
            generator_supported_ids=frozenset({"unreviewed_renderer"}),
        )
