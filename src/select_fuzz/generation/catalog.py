"""Machine-readable feature catalog consumed by scheduling and research audits."""

from __future__ import annotations

from importlib import resources
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

from select_fuzz.generation.catalog_schema import (
    CatalogError,
    REVIEWED_VARIANT_IDS,
    Version,
    load_and_validate_catalog,
    parse_version,
)


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise CatalogError(f"{label} must be a snake_case identifier")
    return value


def _string_set(value: object, label: str) -> frozenset[str]:
    if not isinstance(value, list) or not value:
        raise CatalogError(f"{label} must be a non-empty list")
    return frozenset(_identifier(item, label) for item in value)


def _records(value: object, label: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or not value:
        raise CatalogError(f"{label} must be a non-empty list")
    if not all(isinstance(item, Mapping) for item in value):
        raise CatalogError(f"{label} must contain mappings")
    return cast(list[Mapping[str, object]], value)


class CapabilityStatus(StrEnum):
    """Whether an internal generator registry can currently render a catalog row."""

    GENERATOR_SUPPORTED = "generator_supported"
    CATALOGUED_GAP = "catalogued_gap"


# The generator implementation has not landed yet. New entries must be registered
# here only when a renderer and its tests exist; catalog loading alone is not support.
GENERATOR_SUPPORTED_VARIANT_IDS: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    source_id: str
    locator: str

    def __post_init__(self) -> None:
        _identifier(self.source_id, "source_id")
        _identifier(self.locator, "locator")


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    feature_id: str
    family: str
    min_version: Version
    compatible_profiles: frozenset[str]
    ast_nodes: frozenset[str]
    guards: frozenset[str]
    capability_status: CapabilityStatus = CapabilityStatus.GENERATOR_SUPPORTED
    evidence_lock_ready: bool = True
    unverified_evidence_sources: frozenset[str] = frozenset()
    weight: float = 1.0
    evidence: tuple[EvidenceRef, ...] = ()
    source_feature_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.feature_id, "feature_id")
        _identifier(self.family, "family")
        if len(self.min_version) != 3 or any(part < 0 for part in self.min_version):
            raise ValueError("min_version must contain three nonnegative integers")
        if not self.compatible_profiles:
            raise ValueError("compatible_profiles must not be empty")
        if not self.ast_nodes:
            raise ValueError("ast_nodes must not be empty")
        if not self.guards:
            raise ValueError("guards must not be empty")
        if self.evidence_lock_ready == bool(self.unverified_evidence_sources):
            raise ValueError(
                "evidence_lock_ready must be false exactly when evidence sources are unverified"
            )
        if self.weight <= 0:
            raise ValueError("weight must be positive")


class FeatureCatalog:
    """Immutable loaded rows with generator-supported targets kept explicit."""

    def __init__(self, specs: Iterable[FeatureSpec]):
        self._specs = tuple(specs)
        self._by_id = {spec.feature_id: spec for spec in self._specs}
        if len(self._by_id) != len(self._specs):
            raise ValueError("feature IDs must be unique")

    def __iter__(self) -> Iterator[FeatureSpec]:
        return iter(self._specs)

    def __len__(self) -> int:
        return len(self._specs)

    @staticmethod
    def _compatible(
        spec: FeatureSpec,
        *,
        version: Version,
        profiles: frozenset[str] | None,
    ) -> bool:
        return spec.min_version <= version and (
            profiles is None or bool(spec.compatible_profiles & profiles)
        )

    def signature_targets(
        self,
        *,
        version: Version,
        profiles: frozenset[str] | None = None,
    ) -> tuple[FeatureSpec, ...]:
        """Return only rows backed by the current generator registry."""

        return tuple(
            spec
            for spec in self._specs
            if spec.capability_status is CapabilityStatus.GENERATOR_SUPPORTED
            and spec.evidence_lock_ready
            and self._compatible(spec, version=version, profiles=profiles)
        )

    def catalogued_gaps(
        self,
        *,
        version: Version,
        profiles: frozenset[str] | None = None,
    ) -> tuple[FeatureSpec, ...]:
        """Return loaded rows that still lack a registered generator renderer."""

        return tuple(
            spec
            for spec in self._specs
            if spec.capability_status is CapabilityStatus.CATALOGUED_GAP
            and self._compatible(spec, version=version, profiles=profiles)
        )

    def evidence_lock_gaps(
        self,
        *,
        version: Version,
        profiles: frozenset[str] | None = None,
    ) -> tuple[FeatureSpec, ...]:
        """Return registered renderers blocked by unverified evidence sources."""

        return tuple(
            spec
            for spec in self._specs
            if spec.capability_status is CapabilityStatus.GENERATOR_SUPPORTED
            and not spec.evidence_lock_ready
            and self._compatible(spec, version=version, profiles=profiles)
        )

    def directed_target(self, feature_id: str) -> FeatureSpec:
        return self._by_id[feature_id]

    @classmethod
    def default(
        cls,
        *,
        generator_supported_ids: frozenset[str] | None = None,
    ) -> FeatureCatalog:
        """Load the canonical catalog from package data or a source checkout."""

        packaged = resources.files("select_fuzz").joinpath(
            "data", "mysql-8.0.41-query-shapes.yaml"
        )
        if packaged.is_file():
            with resources.as_file(packaged) as catalog_path:
                return cls.from_yaml(
                    catalog_path,
                    generator_supported_ids=generator_supported_ids,
                )

        checkout_path = (
            Path(__file__).resolve().parents[3]
            / "catalog"
            / "mysql-8.0.41-query-shapes.yaml"
        )
        if not checkout_path.is_file():
            raise CatalogError("canonical MySQL 8.0.41 catalog is unavailable")
        return cls.from_yaml(
            checkout_path,
            generator_supported_ids=generator_supported_ids,
        )

    @classmethod
    def from_yaml(
        cls,
        path: str | Path,
        *,
        generator_supported_ids: frozenset[str] | None = None,
    ) -> FeatureCatalog:
        """Load only after the shared production schema validator accepts the file."""

        document = load_and_validate_catalog(path)
        supported_ids = (
            GENERATOR_SUPPORTED_VARIANT_IDS
            if generator_supported_ids is None
            else generator_supported_ids
        )
        unknown_supported = supported_ids - REVIEWED_VARIANT_IDS
        if unknown_supported:
            raise CatalogError(
                f"generator registry contains unknown variants: {sorted(unknown_supported)}"
            )

        source_ready = {
            _identifier(source["source_id"], "source.source_id"): (
                source["lock_state"] == "verified"
            )
            for source in _records(document["sources"], "sources")
        }

        specs: list[FeatureSpec] = []
        for feature in _records(document["features"], "features"):
            parent_id = _identifier(feature["feature_id"], "feature.feature_id")
            family = _identifier(feature["category"], "feature.category")
            parent_evidence_sources = {
                _identifier(item["source_id"], "evidence.source_id")
                for item in _records(feature["evidence"], "feature.evidence")
            }
            for variant in _records(feature["variants"], f"{parent_id}.variants"):
                variant_id = _identifier(variant["variant_id"], "variant.variant_id")
                evidence = tuple(
                    EvidenceRef(
                        source_id=_identifier(item["source_id"], "evidence.source_id"),
                        locator=_identifier(item["locator"], "evidence.locator"),
                    )
                    for item in _records(variant["evidence"], "variant.evidence")
                )
                weight = variant["weight"]
                if not isinstance(weight, int) or isinstance(weight, bool):
                    raise CatalogError("variant.weight must be an integer")
                all_evidence_sources = parent_evidence_sources | {
                    item.source_id for item in evidence
                }
                unverified_sources = frozenset(
                    source_id
                    for source_id in all_evidence_sources
                    if not source_ready[source_id]
                )
                specs.append(
                    FeatureSpec(
                        feature_id=variant_id,
                        family=family,
                        min_version=parse_version(
                            variant["min_version"], "variant.min_version"
                        ),
                        compatible_profiles=_string_set(
                            variant["profiles"], "variant.profiles"
                        ),
                        ast_nodes=_string_set(variant["ast_nodes"], "variant.ast_nodes"),
                        guards=_string_set(variant["guards"], "variant.guards"),
                        capability_status=(
                            CapabilityStatus.GENERATOR_SUPPORTED
                            if variant_id in supported_ids
                            else CapabilityStatus.CATALOGUED_GAP
                        ),
                        evidence_lock_ready=not unverified_sources,
                        unverified_evidence_sources=unverified_sources,
                        weight=float(weight),
                        evidence=evidence,
                        source_feature_id=parent_id,
                    )
                )
        return cls(specs)
