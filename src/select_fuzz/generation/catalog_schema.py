"""Fail-closed schema and source-lock validation for the official SQL catalog."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import NoReturn, cast
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import yaml
from yaml.constructor import ConstructorError


Version = tuple[int, int, int]
TARGET_VERSION: Version = (8, 0, 41)
CHECKED_AT = "2026-07-12"

TOP_LEVEL_KEYS = {
    "schema_version",
    "target_product",
    "target_version",
    "raw_web_sql_policy",
    "checked_at",
    "guard_definitions",
    "profile_definitions",
    "sources",
    "features",
}
SOURCE_KEYS = {
    "source_id",
    "kind",
    "version",
    "url",
    "hash_scope",
    "lock_state",
    "content_sha256",
    "checked_at",
    "locators",
}
LOCATOR_KEYS = {"match_kind", "pattern"}
FEATURE_KEYS = {
    "feature_id",
    "category",
    "min_version",
    "ast_nodes",
    "guards",
    "profiles",
    "weight",
    "evidence",
    "variants",
}
VARIANT_KEYS = {
    "variant_id",
    "min_version",
    "ast_nodes",
    "guards",
    "profiles",
    "weight",
    "evidence",
}
EVIDENCE_KEYS = {"source_id", "locator"}

ALLOWED_CATEGORIES = frozenset(
    {
        "case",
        "cte",
        "derived_lateral",
        "discovery_source",
        "functions_operators",
        "grouping_rollup",
        "index_constraint",
        "join",
        "json_function",
        "json_table",
        "optimizer_hint",
        "partition_selection",
        "regression_seed",
        "scene_constraint",
        "select",
        "set_operation",
        "subquery",
        "type_constraint",
        "window",
    }
)
SYNTAX_CATEGORIES = ALLOWED_CATEGORIES - {
    "discovery_source",
    "index_constraint",
    "scene_constraint",
    "type_constraint",
}
ALLOWED_SOURCE_KINDS = frozenset(
    {
        "exact_source",
        "manual_snapshot",
        "release_note",
        "version_reference_snapshot",
    }
)
ALLOWED_HASH_SCOPES = frozenset({"response_bytes", "docs_body_text_v1"})
ALLOWED_LOCK_STATES = frozenset({"verified", "refresh_required"})
ALLOWED_GUARDS = frozenset(
    {
        "alias_required",
        "arity_equal",
        "bounded_cardinality",
        "bounded_recursion",
        "compatible_collation",
        "compatible_types",
        "config_fingerprint_equal",
        "deterministic_expression",
        "exact_grouping",
        "existing_index",
        "existing_partition",
        "fixed_srid",
        "foreign_key_compatible",
        "frame_bounds_valid",
        "fulltext_compatible",
        "json_path_valid",
        "lateral_direction_valid",
        "multivalue_compatible",
        "no_duplicate_json_keys",
        "no_external_json_ref",
        "no_nondeterministic_function",
        "no_unsupported_window_clause",
        "only_full_group_by_legal",
        "partition_unique_key_complete",
        "read_only_select",
        "same_session",
        "scalar_cardinality",
        "shallow_complexity",
        "stable_ordering",
        "termination_predicate",
        "version_available",
    }
)
ALLOWED_PROFILES = frozenset(
    {
        "foreign_key_graph",
        "fulltext_innodb",
        "json_multivalue_innodb",
        "partitioned_innodb",
        "regular_innodb",
        "spatial_innodb",
        "temporary_innodb",
    }
)
ALLOWED_AST_NODES = frozenset(
    {
        "aggregate_expression",
        "anti_join",
        "case_expression",
        "common_table_expression",
        "derived_table",
        "explicit_partition",
        "explicit_table",
        "frame_clause",
        "function_expression",
        "grouping_clause",
        "hint_comment",
        "joined_table",
        "json_table_function",
        "lateral_derived_table",
        "parenthesized_query_expression",
        "predicate_expression",
        "query_expression",
        "query_specification",
        "recursive_common_table_expression",
        "row_constructor",
        "scene_profile",
        "set_operation",
        "subquery_expression",
        "table_value_constructor",
        "type_domain",
        "window_clause",
        "window_function",
    }
)

REVIEWED_SOURCE_MANIFEST: Mapping[str, tuple[str, str, str]] = {
    "grammar_8041": (
        "exact_source",
        "8.0.41",
        "https://raw.githubusercontent.com/mysql/mysql-server/mysql-8.0.41/sql/sql_yacc.yy",
    ),
    "parse_tree_8041": (
        "exact_source",
        "8.0.41",
        "https://raw.githubusercontent.com/mysql/mysql-server/mysql-8.0.41/sql/parse_tree_nodes.h",
    ),
    **{
        f"release_80{int(version.rsplit('.', maxsplit=1)[1]):02d}": (
            "release_note",
            version,
            f"https://dev.mysql.com/doc/relnotes/mysql/8.0/en/news-{version.replace('.', '-')}.html",
        )
        for version in (
            "8.0.1",
            "8.0.2",
            "8.0.4",
            "8.0.13",
            "8.0.14",
            "8.0.17",
            "8.0.19",
            "8.0.20",
            "8.0.21",
            "8.0.22",
            "8.0.31",
            "8.0.41",
        )
    },
    "manual_create_index": (
        "manual_snapshot",
        "8.0",
        "https://dev.mysql.com/doc/refman/8.0/en/create-index.html",
    ),
    "manual_partition_limits": (
        "manual_snapshot",
        "8.0",
        "https://dev.mysql.com/doc/refman/8.0/en/partitioning-limitations.html",
    ),
    "manual_foreign_keys": (
        "manual_snapshot",
        "8.0",
        "https://dev.mysql.com/doc/refman/8.0/en/create-table-foreign-keys.html",
    ),
    "manual_fulltext": (
        "manual_snapshot",
        "8.0",
        "https://dev.mysql.com/doc/refman/8.0/en/fulltext-restrictions.html",
    ),
    "manual_spatial": (
        "manual_snapshot",
        "8.0",
        "https://dev.mysql.com/doc/refman/8.0/en/spatial-index-optimization.html",
    ),
    "manual_data_types": (
        "manual_snapshot",
        "8.0",
        "https://dev.mysql.com/doc/refman/8.0/en/data-types.html",
    ),
    "manual_storage": (
        "manual_snapshot",
        "8.0",
        "https://dev.mysql.com/doc/refman/8.0/en/storage-requirements.html",
    ),
    "manual_innodb_limits": (
        "manual_snapshot",
        "8.0",
        "https://dev.mysql.com/doc/refman/8.0/en/innodb-limits.html",
    ),
    "version_builtin": (
        "version_reference_snapshot",
        "8.0",
        "https://dev.mysql.com/doc/mysqld-version-reference/en/built-in-functions.html",
    ),
}
REVIEWED_SOURCE_IDS = frozenset(REVIEWED_SOURCE_MANIFEST)
REVIEWED_SOURCE_URLS = frozenset(item[2] for item in REVIEWED_SOURCE_MANIFEST.values())
REVIEWED_FEATURE_IDS = frozenset(
    {
        "case_control_flow",
        "cte_family",
        "derived_lateral_family",
        "function_discovery",
        "functions_operators_family",
        "grouping_rollup_family",
        "index_profiles",
        "join_family",
        "json_function_family",
        "json_table_family",
        "optimizer_hint_family",
        "partition_selection_family",
        "regression_8041_family",
        "scene_compatibility",
        "select_family",
        "set_operation_family",
        "subquery_family",
        "type_domains",
        "window_family",
    }
)
REVIEWED_VARIANT_IDS = frozenset(
    {
        "case_searched",
        "case_simple",
        "cte_nonrecursive",
        "cte_recursive",
        "derived_explicit_columns",
        "derived_regular",
        "function_aggregate",
        "function_deterministic_scalar",
        "function_fulltext_spatial",
        "function_version_import",
        "grouping_aggregate_having",
        "grouping_with_rollup",
        "index_descending",
        "index_fulltext",
        "index_functional",
        "index_multivalue",
        "index_prefix",
        "index_spatial",
        "join_inner_cross_straight",
        "join_outer_natural",
        "json_create_extract",
        "json_member_overlap",
        "json_schema_validation",
        "json_table_columns",
        "json_table_implicit_lateral",
        "json_value_scalar",
        "lateral_correlated",
        "optimizer_hint_derived_pushdown",
        "optimizer_hint_index_level",
        "optimizer_hint_join_order",
        "partition_explicit_selection",
        "regression_8041_antijoin_spill_null_key",
        "regression_8041_desc_pk_index_merge",
        "regression_8041_distinct_not_in",
        "regression_8041_hint_lexer",
        "regression_8041_rollup_row_comparator",
        "regression_8041_subquery_materialization",
        "regression_8041_union_chain_flatten",
        "regression_8041_union_view_charset",
        "scene_foreign_key",
        "scene_fulltext",
        "scene_json_multivalue",
        "scene_partitioned",
        "scene_regular",
        "scene_spatial",
        "scene_temporary",
        "select_parenthesized",
        "select_nested_parenthesized_top_n",
        "select_query_specification",
        "set_except",
        "set_branch_local_top_n",
        "set_intersect",
        "set_table_values",
        "set_union",
        "table_explicit",
        "table_subquery_exists",
        "table_values_union",
        "subquery_quantified",
        "subquery_result_kinds",
        "type_numeric_boundaries",
        "type_string_lob_boundaries",
        "type_temporal_json_spatial",
        "window_frames",
        "window_inline_named",
    }
)
REVIEWED_CATALOG_SHA256 = "93ce862b4ec6474c1c1dc9dceb29a9e5b33be6e7e6c002c447782c9d3af05007"

SQL_KEYWORDS = frozenset(
    {
        "all",
        "and",
        "case",
        "cross",
        "except",
        "exists",
        "from",
        "group",
        "having",
        "in",
        "inner",
        "intersect",
        "join",
        "lateral",
        "left",
        "limit",
        "not",
        "on",
        "or",
        "order",
        "outer",
        "partition",
        "right",
        "select",
        "union",
        "using",
        "where",
        "window",
        "with",
    }
)
ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class CatalogError(ValueError):
    """The catalog cannot safely be consumed."""


class SourceLockError(CatalogError):
    """A locked source or locator no longer matches its reviewed bytes."""


class _DocsBodyParser(HTMLParser):
    """Extract one official docs-body while excluding dynamic page chrome."""

    _IGNORED_TAGS = frozenset({"script", "style", "noscript", "svg"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.body_count = 0
        self._body_div_depth = 0
        self._ignored_depth = 0
        self._in_title = False
        self.title_parts: list[str] = []
        self.body_parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized_tag = tag.lower()
        attributes = dict(attrs)
        if normalized_tag == "title":
            self._in_title = True
        if normalized_tag == "div" and attributes.get("id") == "docs-body":
            self.body_count += 1
            if self._body_div_depth == 0:
                self._body_div_depth = 1
                self.body_parts.append(" ")
                return
        if self._body_div_depth > 0:
            if normalized_tag == "div":
                self._body_div_depth += 1
            if normalized_tag in self._IGNORED_TAGS:
                self._ignored_depth += 1
            elif self._ignored_depth == 0:
                self.body_parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        if normalized_tag == "title":
            self._in_title = False
        if self._body_div_depth <= 0:
            return
        if normalized_tag in self._IGNORED_TAGS and self._ignored_depth > 0:
            self._ignored_depth -= 1
        elif self._ignored_depth == 0:
            self.body_parts.append(" ")
        if normalized_tag == "div":
            self._body_div_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._body_div_depth > 0 and self._ignored_depth == 0:
            self.body_parts.append(data)


def _normalize_visible_text(parts: list[str]) -> str:
    joined = " ".join(parts)
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", joined)).strip()


def canonical_catalog_sha256(catalog: Mapping[str, object]) -> str:
    """Hash the complete YAML data model with deterministic JSON encoding."""

    payload = json.dumps(
        catalog,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def canonicalize_source_content(
    *,
    source_kind: str,
    hash_scope: str,
    content: bytes,
) -> bytes:
    """Return deterministic bytes for hashing and locator matching."""

    if not isinstance(content, bytes):
        raise SourceLockError("source content must be bytes")
    if hash_scope == "response_bytes":
        if source_kind != "exact_source":
            raise SourceLockError("response_bytes is permitted only for exact_source")
        return content
    if hash_scope != "docs_body_text_v1" or source_kind == "exact_source":
        raise SourceLockError(f"invalid hash scope {hash_scope!r} for {source_kind}")
    try:
        html = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SourceLockError("official documentation response is not UTF-8") from error
    parser = _DocsBodyParser()
    try:
        parser.feed(html)
        parser.close()
    except (AssertionError, ValueError) as error:
        raise SourceLockError("official documentation HTML is malformed") from error
    if parser.body_count != 1:
        raise SourceLockError(
            f"official documentation must contain exactly one docs-body; got {parser.body_count}"
        )
    title = _normalize_visible_text(parser.title_parts)
    title_lower = title.lower()
    error_markers = ("access denied", "robot", "captcha", "not found", "error")
    if not title.startswith("MySQL :: MySQL ") or any(
        marker in title_lower for marker in error_markers
    ):
        raise SourceLockError(f"unexpected official documentation title: {title!r}")
    required_title = {
        "release_note": "8.0 Release Notes",
        "manual_snapshot": "8.0 Reference Manual",
        "version_reference_snapshot": "Version Reference",
    }.get(source_kind)
    if required_title is None or required_title not in title:
        raise SourceLockError(
            f"official documentation title does not match {source_kind}: {title!r}"
        )
    body = _normalize_visible_text(parser.body_parts)
    if not body:
        raise SourceLockError("official documentation docs-body is empty")
    return body.encode("utf-8")


class DuplicateKeySafeLoader(yaml.SafeLoader):
    """SafeLoader variant that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: DuplicateKeySafeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


DuplicateKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _fail(message: str) -> NoReturn:
    raise CatalogError(message)


def _require_exact_keys(
    record: Mapping[str, object], expected: set[str], label: str
) -> None:
    if set(record) != expected:
        _fail(f"{label} keys: expected {sorted(expected)}, got {sorted(record)}")


def _as_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        _fail(f"{label} must be a string-keyed mapping")
    return cast(Mapping[str, object], value)


def _as_record_list(value: object, label: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or not value:
        _fail(f"{label} must be a non-empty list")
    return [_as_mapping(item, label) for item in value]


def parse_version(value: object, label: str) -> Version:
    if not isinstance(value, str):
        _fail(f"{label} must be a string")
    pieces = value.split(".")
    if len(pieces) != 3 or not all(piece.isdigit() for piece in pieces):
        _fail(f"{label} must use major.minor.patch")
    return int(pieces[0]), int(pieces[1]), int(pieces[2])


def _identifier(
    value: object,
    label: str,
    allowed: frozenset[str] | None = None,
) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        _fail(f"{label} must be a snake_case identifier")
    if allowed is not None:
        if value not in allowed:
            _fail(f"unknown {label}: {value}")
        return value
    if value in SQL_KEYWORDS:
        _fail(f"{label} contains executable SQL text")
    return value


def _identifier_list(
    value: object, label: str, allowed: frozenset[str]
) -> list[str]:
    if not isinstance(value, list) or not value:
        _fail(f"{label} must be a non-empty list")
    result = [_identifier(item, label, allowed) for item in value]
    if len(result) != len(set(result)):
        _fail(f"{label} must not contain duplicates")
    return result


def _validate_locator_manifest(value: object, label: str) -> Mapping[str, object]:
    locators = _as_mapping(value, label)
    if not locators:
        _fail(f"{label} must be a non-empty mapping")
    for locator, raw_manifest in locators.items():
        _identifier(locator, f"{label}.locator")
        manifest = _as_mapping(raw_manifest, f"{label}.{locator}")
        _require_exact_keys(manifest, LOCATOR_KEYS, f"{label}.{locator}")
        if manifest["match_kind"] not in {"literal", "regex"}:
            _fail(f"{label}.{locator}.match_kind must be literal or regex")
        pattern = manifest["pattern"]
        if not isinstance(pattern, str) or not pattern or len(pattern) > 2000:
            _fail(f"{label}.{locator}.pattern must be a non-empty bounded string")
        if manifest["match_kind"] == "regex":
            try:
                compiled = re.compile(pattern)
            except re.error as error:
                raise CatalogError(f"{label}.{locator}.pattern is invalid regex") from error
            if compiled.search("") is not None:
                _fail(f"{label}.{locator}.pattern must not match empty text")
    return locators


def _validate_evidence(
    value: object,
    label: str,
    sources: Mapping[str, Mapping[str, object]],
) -> list[Mapping[str, object]]:
    records = _as_record_list(value, label)
    for record in records:
        _require_exact_keys(record, EVIDENCE_KEYS, label)
        source_id = _identifier(record["source_id"], f"{label}.source_id")
        if source_id not in sources:
            _fail(f"{label} references unknown source {source_id}")
        locator = _identifier(record["locator"], f"{label}.locator")
        locators = cast(Mapping[str, object], sources[source_id]["locators"])
        if locator not in locators:
            _fail(f"{label} references unknown locator {source_id}.{locator}")
    return records


def _validate_structure_record(
    record: Mapping[str, object],
    *,
    label: str,
    id_key: str,
    sources: Mapping[str, Mapping[str, object]],
    require_exact_source: bool,
) -> tuple[str, list[Mapping[str, object]]]:
    record_id = _identifier(record[id_key], f"{label}.{id_key}")
    version = parse_version(record["min_version"], f"{label}.min_version")
    if version > TARGET_VERSION:
        _fail(f"{label} claims future version {record['min_version']}")
    _identifier_list(record["ast_nodes"], f"{label}.ast_nodes", ALLOWED_AST_NODES)
    _identifier_list(record["guards"], f"{label}.guards", ALLOWED_GUARDS)
    _identifier_list(record["profiles"], f"{label}.profiles", ALLOWED_PROFILES)
    weight = record["weight"]
    if not isinstance(weight, int) or isinstance(weight, bool) or not 1 <= weight <= 100:
        _fail(f"{label}.weight must be an integer from 1 to 100")
    evidence = _validate_evidence(record["evidence"], f"{label}.evidence", sources)
    evidence_sources = {cast(str, item["source_id"]) for item in evidence}
    evidence_kinds = {sources[source_id]["kind"] for source_id in evidence_sources}
    if require_exact_source and "exact_source" not in evidence_kinds:
        _fail(f"{label} lacks exact-tag grammar or parse-tree evidence")
    if version > (5, 7, 0):
        matching_release = any(
            sources[source_id]["kind"] == "release_note"
            and parse_version(sources[source_id]["version"], "source.version") == version
            for source_id in evidence_sources
        )
        if not matching_release:
            _fail(f"{label} lacks same-version release-note evidence")
    return record_id, evidence


def validate_catalog(catalog: Mapping[str, object]) -> None:
    """Validate the complete reviewed v2 catalog, rejecting every unknown extension."""

    _require_exact_keys(catalog, TOP_LEVEL_KEYS, "catalog")
    if catalog["schema_version"] != 2:
        _fail("schema_version must be 2; extensions require an explicit schema bump")
    if catalog["target_product"] != "MySQL Community Server":
        _fail("unexpected target_product")
    if catalog["target_version"] != "8.0.41":
        _fail("target_version must be 8.0.41")
    if catalog["raw_web_sql_policy"] != "signatures_only_never_execute":
        _fail("raw web SQL policy must forbid execution")
    if catalog["checked_at"] != CHECKED_AT:
        _fail("checked_at must pin this snapshot")
    if catalog["guard_definitions"] != sorted(ALLOWED_GUARDS):
        _fail("guard_definitions must equal the reviewed guard enum")
    if catalog["profile_definitions"] != sorted(ALLOWED_PROFILES):
        _fail("profile_definitions must equal the reviewed profile enum")

    sources: dict[str, Mapping[str, object]] = {}
    for record in _as_record_list(catalog["sources"], "sources"):
        _require_exact_keys(record, SOURCE_KEYS, "source")
        source_id = _identifier(record["source_id"], "source.source_id")
        if source_id in sources:
            _fail(f"duplicate source_id {source_id}")
        kind = _identifier(record["kind"], "source.kind", ALLOWED_SOURCE_KINDS)
        version = record["version"]
        if kind in {"exact_source", "release_note"}:
            parse_version(version, "source.version")
        elif version != "8.0":
            _fail("rolling source.version must be 8.0")
        url = record["url"]
        if not isinstance(url, str):
            _fail("source.url must be a string")
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.query or parsed.fragment or parsed.username:
            _fail("source.url must be a canonical https URL")
        hash_scope = _identifier(
            record["hash_scope"], "source.hash_scope", ALLOWED_HASH_SCOPES
        )
        expected_scope = (
            "response_bytes" if kind == "exact_source" else "docs_body_text_v1"
        )
        if hash_scope != expected_scope:
            _fail(f"source {source_id} must use hash_scope {expected_scope}")
        lock_state = _identifier(
            record["lock_state"], "source.lock_state", ALLOWED_LOCK_STATES
        )
        content_sha256 = record["content_sha256"]
        if lock_state == "verified":
            if not isinstance(content_sha256, str) or not HASH_RE.fullmatch(content_sha256):
                _fail("verified source.content_sha256 must be lowercase SHA-256")
        elif content_sha256 is not None:
            _fail("refresh_required source.content_sha256 must be null")
        if record["checked_at"] != CHECKED_AT:
            _fail("every source must pin checked_at")
        _validate_locator_manifest(record["locators"], f"source.{source_id}.locators")
        sources[source_id] = record

    if frozenset(sources) != REVIEWED_SOURCE_IDS:
        _fail("source IDs must equal the reviewed source manifest")
    for source_id, expected in REVIEWED_SOURCE_MANIFEST.items():
        record = sources[source_id]
        actual = (record["kind"], record["version"], record["url"])
        if actual != expected:
            _fail(f"source {source_id} differs from the reviewed source manifest")

    feature_ids: set[str] = set()
    variant_ids: set[str] = set()
    seen_categories: set[str] = set()
    referenced_locators: dict[str, set[str]] = {source_id: set() for source_id in sources}
    for feature in _as_record_list(catalog["features"], "features"):
        _require_exact_keys(feature, FEATURE_KEYS, "feature")
        category = _identifier(feature["category"], "feature.category", ALLOWED_CATEGORIES)
        seen_categories.add(category)
        feature_id, feature_evidence = _validate_structure_record(
            feature,
            label=f"feature.{feature['feature_id']}",
            id_key="feature_id",
            sources=sources,
            require_exact_source=category in SYNTAX_CATEGORIES,
        )
        if feature_id in feature_ids:
            _fail(f"duplicate feature_id {feature_id}")
        feature_ids.add(feature_id)
        for item in feature_evidence:
            referenced_locators[cast(str, item["source_id"])].add(
                cast(str, item["locator"])
            )

        for variant in _as_record_list(feature["variants"], f"feature.{feature_id}.variants"):
            _require_exact_keys(variant, VARIANT_KEYS, "variant")
            variant_id, variant_evidence = _validate_structure_record(
                variant,
                label=f"variant.{variant['variant_id']}",
                id_key="variant_id",
                sources=sources,
                require_exact_source=category in SYNTAX_CATEGORIES,
            )
            if variant_id in variant_ids:
                _fail(f"duplicate variant_id {variant_id}")
            variant_ids.add(variant_id)
            for item in variant_evidence:
                referenced_locators[cast(str, item["source_id"])].add(
                    cast(str, item["locator"])
                )

    if seen_categories != ALLOWED_CATEGORIES:
        _fail("catalog must contain every reviewed category")
    if frozenset(feature_ids) != REVIEWED_FEATURE_IDS:
        _fail("feature IDs must equal the reviewed feature manifest")
    if frozenset(variant_ids) != REVIEWED_VARIANT_IDS:
        _fail("variant IDs must equal the reviewed variant manifest")
    for source_id, record in sources.items():
        manifested = set(cast(Mapping[str, object], record["locators"]))
        if manifested != referenced_locators[source_id]:
            _fail(f"source {source_id} locators must exactly match evidence references")
    if canonical_catalog_sha256(catalog) != REVIEWED_CATALOG_SHA256:
        _fail("catalog differs from the reviewed canonical catalog digest")


def load_catalog_text(text: str) -> Mapping[str, object]:
    """Safely parse and strictly validate catalog YAML text."""

    loader = DuplicateKeySafeLoader(text)
    try:
        loaded = loader.get_single_data()
    finally:
        loader.dispose()  # type: ignore[no-untyped-call]
    catalog = _as_mapping(loaded, "catalog")
    validate_catalog(catalog)
    return catalog


def load_and_validate_catalog(path: str | Path) -> Mapping[str, object]:
    """Read a catalog from disk and apply the same strict production validator."""

    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise CatalogError("unable to read feature catalog") from error
    try:
        return load_catalog_text(text)
    except yaml.YAMLError as error:
        raise CatalogError("unable to parse feature catalog") from error


@dataclass(frozen=True, slots=True)
class SourceLockReport:
    sources_checked: int
    locators_checked: int


@dataclass(frozen=True, slots=True)
class SourceLockCandidate:
    source_id: str
    content_sha256: str
    locators_checked: int


FetchBytes = Callable[[str], bytes]


def _fetch_url_bytes(url: str) -> bytes:
    if url not in REVIEWED_SOURCE_URLS:
        raise SourceLockError(f"refusing unreviewed source URL: {url}")
    # The strict allowlist above excludes file/custom schemes and arbitrary hosts.
    request = Request(  # noqa: S310
        url,
        headers={
            "Accept": "*/*",
            # Pin reviewed representation headers so exact-source verification
            # does not vary with Python's default user agent.
            "User-Agent": "curl/8.7.1",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310
            if response.geturl() != url:
                raise SourceLockError(f"canonical source redirected: {url}")
            content = response.read()
            if not isinstance(content, bytes):
                raise SourceLockError(f"locked source did not return bytes: {url}")
            return content
    except SourceLockError:
        raise
    except OSError as error:
        raise SourceLockError(f"unable to fetch locked source {url}") from error


def inspect_catalog_source_locks(
    catalog_or_path: Mapping[str, object] | str | Path,
    *,
    fetch_bytes: FetchBytes | None = None,
) -> tuple[SourceLockCandidate, ...]:
    """Fetch and validate stable scopes, returning reviewable digest candidates."""

    if isinstance(catalog_or_path, (str, Path)):
        catalog = load_and_validate_catalog(catalog_or_path)
    else:
        catalog = catalog_or_path
    source_records = _as_record_list(catalog.get("sources"), "sources")
    fetch = fetch_bytes or _fetch_url_bytes
    candidates: list[SourceLockCandidate] = []
    for record in source_records:
        source_id = _identifier(record.get("source_id"), "source.source_id")
        url = record.get("url")
        source_kind = record.get("kind")
        hash_scope = record.get("hash_scope")
        if not isinstance(url, str):
            raise SourceLockError(f"{source_id} lacks a URL")
        if not isinstance(source_kind, str) or not isinstance(hash_scope, str):
            raise SourceLockError(f"{source_id} lacks source kind or hash scope")
        content = fetch(url)
        if not isinstance(content, bytes):
            raise SourceLockError(f"{source_id} fetcher must return bytes")
        scoped_content = canonicalize_source_content(
            source_kind=source_kind,
            hash_scope=hash_scope,
            content=content,
        )
        actual_hash = hashlib.sha256(scoped_content).hexdigest()
        text = scoped_content.decode("utf-8", errors="replace")
        locators = _as_mapping(record.get("locators"), f"source.{source_id}.locators")
        source_locators_checked = 0
        for locator, raw_manifest in locators.items():
            manifest = _as_mapping(raw_manifest, f"source.{source_id}.locators.{locator}")
            kind = manifest.get("match_kind")
            pattern = manifest.get("pattern")
            if kind not in {"literal", "regex"} or not isinstance(pattern, str):
                raise SourceLockError(f"{source_id}.{locator} has an invalid locator manifest")
            try:
                found = pattern in text if kind == "literal" else re.search(pattern, text) is not None
            except re.error as error:
                raise SourceLockError(f"{source_id}.{locator} has invalid regex") from error
            if not found:
                raise SourceLockError(f"{source_id} locator {locator} did not match hash scope")
            source_locators_checked += 1
        candidates.append(
            SourceLockCandidate(
                source_id=source_id,
                content_sha256=actual_hash,
                locators_checked=source_locators_checked,
            )
        )
    return tuple(candidates)


def verify_catalog_source_lock(
    catalog_or_path: Mapping[str, object] | str | Path,
    *,
    fetch_bytes: FetchBytes | None = None,
) -> SourceLockReport:
    """Verify every reviewed digest and locator, refusing incomplete locks."""

    if isinstance(catalog_or_path, (str, Path)):
        catalog = load_and_validate_catalog(catalog_or_path)
    else:
        catalog = catalog_or_path
    source_records = _as_record_list(catalog.get("sources"), "sources")
    pending_sources = [
        _identifier(record.get("source_id"), "source.source_id")
        for record in source_records
        if record.get("lock_state") != "verified"
    ]
    if pending_sources:
        raise SourceLockError(
            f"source lock requires refresh: {', '.join(sorted(pending_sources))}"
        )
    expected_hashes: dict[str, str] = {}
    for record in source_records:
        source_id = _identifier(record.get("source_id"), "source.source_id")
        expected_hash = record.get("content_sha256")
        if not isinstance(expected_hash, str):
            raise SourceLockError(f"{source_id} lacks a SHA-256 lock")
        expected_hashes[source_id] = expected_hash
    candidates = inspect_catalog_source_locks(catalog, fetch_bytes=fetch_bytes)
    for candidate in candidates:
        expected_hash = expected_hashes[candidate.source_id]
        if candidate.content_sha256 != expected_hash:
            raise SourceLockError(
                f"{candidate.source_id} SHA-256 mismatch: expected {expected_hash}, "
                f"got {candidate.content_sha256}"
            )
    return SourceLockReport(
        sources_checked=len(candidates),
        locators_checked=sum(candidate.locators_checked for candidate in candidates),
    )
