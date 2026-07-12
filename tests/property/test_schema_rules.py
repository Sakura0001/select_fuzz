from __future__ import annotations

from hypothesis import given, settings, strategies as st

from select_fuzz.generation.catalog import FeatureSpec
from select_fuzz.generation.schema import SchemaGenerator, SchemaLimits, SchemaProfile
from select_fuzz.generation.schema_rules import SchemaRules


LEGAL_LAYOUTS = tuple(
    (profile, row_format, page_size)
    for profile in SchemaProfile
    for row_format in ("DYNAMIC", "COMPACT", "REDUNDANT", "COMPRESSED")
    for page_size in (4096, 8192, 16_384, 32_768, 65_536)
    if not (row_format == "COMPRESSED" and page_size > 16_384)
    and not (profile is SchemaProfile.TEMPORARY_INNODB and row_format == "COMPRESSED")
)


@settings(max_examples=10_000, deadline=None)
@given(
    seed=st.integers(min_value=0, max_value=2**63 - 1),
    layout=st.sampled_from(LEGAL_LAYOUTS),
    max_tables=st.integers(min_value=2, max_value=8),
    max_columns=st.integers(min_value=3, max_value=16),
    lane_ticket=st.integers(min_value=0, max_value=9),
)
def test_ten_thousand_legal_lane_schemas_obey_every_mysql_rule(
    seed: int,
    layout: tuple[SchemaProfile, str, int],
    max_tables: int,
    max_columns: int,
    lane_ticket: int,
) -> None:
    profile, row_format, page_size = layout
    target = FeatureSpec(
        feature_id="property_target",
        family="property",
        min_version=(8, 0, 0),
        compatible_profiles=frozenset({profile.value}),
        ast_nodes=frozenset({"query_expression"}),
        guards=frozenset({"read_only_select"}),
    )
    limits = SchemaLimits(
        max_tables=max_tables,
        max_columns=max_columns,
        row_format=row_format,
        page_size=page_size,
    )

    boundary_ordinal = (
        seed % len(SchemaGenerator.executable_boundary_pool(limits))
        if profile is SchemaProfile.REGULAR_INNODB and lane_ticket == 0
        else None
    )
    manifest = SchemaGenerator().generate(
        target,
        seed=seed,
        limits=limits,
        boundary_ordinal=boundary_ordinal,
    )

    SchemaRules.mysql_8041().validate(manifest, limits=limits)
    assert 1 <= len(manifest.tables) <= max_tables
    assert all(2 <= len(table.columns) <= max_columns for table in manifest.tables)


@given(seed=st.integers(min_value=0, max_value=2**63 - 1))
def test_hierarchical_seed_paths_make_target_and_limits_part_of_identity(seed: int) -> None:
    generator = SchemaGenerator()
    regular = FeatureSpec(
        feature_id="regular_target",
        family="property",
        min_version=(8, 0, 0),
        compatible_profiles=frozenset({SchemaProfile.REGULAR_INNODB.value}),
        ast_nodes=frozenset({"query_expression"}),
        guards=frozenset({"read_only_select"}),
    )

    first = generator.generate(regular, seed=seed, limits=SchemaLimits(max_tables=2))
    second = generator.generate(regular, seed=seed, limits=SchemaLimits(max_tables=3))

    assert first.canonical_bytes() != second.canonical_bytes()
