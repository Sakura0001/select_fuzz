"""Explicit product scope for production SELECT coverage scheduling."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from enum import StrEnum
from types import MappingProxyType

from select_fuzz.generation.catalog import FeatureCatalog


class QueryExclusionReason(StrEnum):
    JSON = "json"
    FULLTEXT = "fulltext"
    SPATIAL = "spatial"


class QueryCoverageScope:
    """Keep user exclusions explicit without deleting legacy renderers."""

    __slots__ = ("excluded_profile_reasons", "exclusion_reasons")

    def __init__(
        self,
        exclusion_reasons: Mapping[str, QueryExclusionReason],
        *,
        excluded_profile_reasons: Mapping[str, QueryExclusionReason] | None = None,
    ) -> None:
        normalized: dict[str, QueryExclusionReason] = {}
        for feature_id, reason in exclusion_reasons.items():
            if not isinstance(feature_id, str) or not feature_id:
                raise TypeError("excluded feature IDs must be nonempty strings")
            if not isinstance(reason, QueryExclusionReason):
                raise TypeError("every excluded feature requires a typed reason")
            normalized[feature_id] = reason
        self.exclusion_reasons: Mapping[str, QueryExclusionReason] = MappingProxyType(normalized)
        normalized_profiles: dict[str, QueryExclusionReason] = {}
        for profile, reason in (excluded_profile_reasons or {}).items():
            if not isinstance(profile, str) or not profile:
                raise TypeError("excluded profiles must be nonempty strings")
            if not isinstance(reason, QueryExclusionReason):
                raise TypeError("every excluded profile requires a typed reason")
            normalized_profiles[profile] = reason
        self.excluded_profile_reasons: Mapping[str, QueryExclusionReason] = MappingProxyType(
            normalized_profiles
        )

    @property
    def excluded_feature_ids(self) -> frozenset[str]:
        return frozenset(self.exclusion_reasons)

    @property
    def excluded_families(self) -> frozenset[str]:
        """Feature-family names the grammar generator must not emit."""

        return frozenset(
            reason.value
            for reason in (
                *self.exclusion_reasons.values(),
                *self.excluded_profile_reasons.values(),
            )
        )

    def validate_catalog(self, catalog: FeatureCatalog) -> None:
        catalog_ids = {spec.feature_id for spec in catalog}
        unknown = self.excluded_feature_ids - catalog_ids
        if unknown:
            raise ValueError(f"query scope excludes unknown catalog IDs: {sorted(unknown)}")
        catalog_profiles = {profile for spec in catalog for profile in spec.compatible_profiles}
        unknown_profiles = set(self.excluded_profile_reasons) - catalog_profiles
        if unknown_profiles:
            raise ValueError(f"query scope excludes unknown profiles: {sorted(unknown_profiles)}")

    def filter_catalog(self, catalog: FeatureCatalog) -> FeatureCatalog:
        self.validate_catalog(catalog)
        excluded_profiles = set(self.excluded_profile_reasons)
        scoped = []
        for spec in catalog:
            if spec.feature_id in self.excluded_feature_ids:
                continue
            compatible_profiles = spec.compatible_profiles - excluded_profiles
            if compatible_profiles:
                scoped.append(replace(spec, compatible_profiles=frozenset(compatible_profiles)))
        return FeatureCatalog(scoped)


DEFAULT_QUERY_SCOPE = QueryCoverageScope(
    {
        "function_fulltext_spatial": QueryExclusionReason.FULLTEXT,
        "index_fulltext": QueryExclusionReason.FULLTEXT,
        "index_multivalue": QueryExclusionReason.JSON,
        "index_spatial": QueryExclusionReason.SPATIAL,
        "json_create_extract": QueryExclusionReason.JSON,
        "json_member_overlap": QueryExclusionReason.JSON,
        "json_schema_validation": QueryExclusionReason.JSON,
        "json_table_columns": QueryExclusionReason.JSON,
        "json_table_implicit_lateral": QueryExclusionReason.JSON,
        "json_value_scalar": QueryExclusionReason.JSON,
        "scene_fulltext": QueryExclusionReason.FULLTEXT,
        "scene_json_multivalue": QueryExclusionReason.JSON,
        "scene_spatial": QueryExclusionReason.SPATIAL,
    },
    excluded_profile_reasons={
        "fulltext_innodb": QueryExclusionReason.FULLTEXT,
        "json_multivalue_innodb": QueryExclusionReason.JSON,
        "spatial_innodb": QueryExclusionReason.SPATIAL,
    },
)


__all__ = [
    "DEFAULT_QUERY_SCOPE",
    "QueryCoverageScope",
    "QueryExclusionReason",
]
