from __future__ import annotations

import pytest

from select_fuzz.generation.schema import IndexDef, IndexKind, IndexPart
from select_fuzz.validation.generator_adapter import ProductionGeneratorAdapter
from select_fuzz.validation.models import Reachability
from select_fuzz.validation.reaudit_worker import run_isolated_reaudit
from select_fuzz.validation.reachability import CapabilityAuditor, GeneratedWitness
from select_fuzz.validation.signature import SignatureExtractor


def test_real_catalog_schema_and_query_generator_produce_directed_witness() -> None:
    adapter = ProductionGeneratorAdapter()
    signature = adapter.signature_for_feature("grouping_aggregate_having")
    capability = adapter.capability_for_feature("grouping_aggregate_having")

    witness = adapter.generate_for_validation("grouping_aggregate_having", seed=0)
    result = CapabilityAuditor().audit(signature, capability, generator=adapter, budget=3)

    assert isinstance(witness, GeneratedWitness)
    assert witness.sql.startswith("SELECT")
    assert "GROUP BY" in witness.sql
    assert result.status is Reachability.SUPPORTED
    assert result.witness_feature_id == "grouping_aggregate_having"


def test_catalog_ast_terms_are_normalized_for_discovered_shape_matching() -> None:
    adapter = ProductionGeneratorAdapter()
    signature = adapter.signature_for_feature("json_create_extract")
    capability = adapter.find_capability(signature)

    assert {"select", "function_expression", "json_function"} <= set(signature.nodes)
    assert capability.feature_id == "json_create_extract"
    assert capability.evidence_ready is True


def test_unverified_catalog_evidence_remains_blocked() -> None:
    adapter = ProductionGeneratorAdapter()
    signature = adapter.signature_for_feature("cte_nonrecursive")
    capability = adapter.capability_for_feature("cte_nonrecursive")
    result = CapabilityAuditor().audit(signature, capability, generator=adapter)
    assert result.status is Reachability.BLOCKED_EVIDENCE


def test_isolated_reaudit_loads_a_fresh_compatible_generator_graph() -> None:
    signature = ProductionGeneratorAdapter().signature_for_feature("grouping_aggregate_having")
    result = run_isolated_reaudit(signature, budget=3, timeout_s=30)

    assert result.status is Reachability.SUPPORTED


def test_isolated_reaudit_does_not_invalidate_parent_schema_enum_identity() -> None:
    signature = SignatureExtractor("8.0.41").extract("SELECT 1 ORDER BY 1")
    run_isolated_reaudit(signature, budget=3, timeout_s=30)

    primary = IndexDef(
        "PRIMARY",
        (IndexPart(column_name="id"),),
        unique=True,
        primary=True,
    )

    assert primary.kind is IndexKind.BTREE


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT t.id, u.id FROM t INNER JOIN u ON t.id = u.id ORDER BY 1, 2",
        "SELECT id FROM t UNION SELECT id FROM u ORDER BY 1",
        "SELECT CASE id WHEN 0 THEN 1 ELSE 2 END FROM t ORDER BY 1",
    ],
)
def test_discovered_relation_and_ordering_requirements_match_real_catalog(
    sql: str,
) -> None:
    adapter = ProductionGeneratorAdapter()
    signature = SignatureExtractor("8.0.41").extract(sql)
    capability = adapter.find_capability(signature)

    result = CapabilityAuditor().audit(
        signature,
        capability,
        generator=adapter,
        budget=3,
    )

    assert result.status is Reachability.SUPPORTED, (
        capability.feature_id,
        result.reasons,
    )


@pytest.mark.parametrize(
    ("sql", "expected_status"),
    [
        ("SELECT COUNT(*)", Reachability.SUPPORTED),
        ("SELECT t.id FROM t LEFT JOIN u ON t.id = u.id", Reachability.SUPPORTED),
        (
            "SELECT t.id FROM t LEFT JOIN u ON t.id = u.id WHERE EXISTS (SELECT 1 FROM u)",
            Reachability.SUPPORTED,
        ),
        ("VALUES ROW(1)", Reachability.SUPPORTED),
        (
            "SELECT * FROM JSON_TABLE('[1]', '$[*]' COLUMNS(value INT PATH '$')) AS jt",
            Reachability.BLOCKED_EVIDENCE,
        ),
    ],
)
def test_actionable_discovered_shapes_have_real_dynamic_witnesses(
    sql: str, expected_status: Reachability
) -> None:
    adapter = ProductionGeneratorAdapter()
    signature = SignatureExtractor("8.0.41").extract(sql)
    capability = adapter.find_capability(signature)

    result = CapabilityAuditor().audit(
        signature,
        capability,
        generator=adapter,
        budget=3,
    )

    assert result.status is expected_status, (
        signature.nodes,
        signature.requirements,
        capability.feature_id,
        result.reasons,
    )


@pytest.mark.parametrize(
    ("sql", "expected_status"),
    [
        ("VALUES ROW(1) LIMIT 1", Reachability.SUPPORTED),
        (
            "SELECT CAST(t.id AS SIGNED) FROM t INNER JOIN u ON t.id = u.id",
            Reachability.SUPPORTED,
        ),
        (
            "SELECT 1 INTERSECT SELECT 1 EXCEPT SELECT 2",
            Reachability.BLOCKED_EVIDENCE,
        ),
        ("SELECT * FROM (SELECT id FROM t) AS d", Reachability.SUPPORTED),
        (
            "SELECT id FROM t WHERE EXISTS (SELECT 1 FROM u) ORDER BY 1 LIMIT 1",
            Reachability.SUPPORTED,
        ),
        ("SELECT (SELECT 1) LIMIT 1", Reachability.SUPPORTED),
        ("SELECT 1 GROUP BY 1 WITH ROLLUP", Reachability.SUPPORTED),
        (
            "SELECT t.id FROM t INNER JOIN u ON t.id = u.id WHERE EXISTS (SELECT 1 FROM u)",
            Reachability.SUPPORTED,
        ),
    ],
)
def test_discovered_composite_shapes_use_real_directed_variants(
    sql: str, expected_status: Reachability
) -> None:
    adapter = ProductionGeneratorAdapter()
    signature = SignatureExtractor("8.0.41").extract(sql)
    capability = adapter.find_capability(signature)

    result = CapabilityAuditor().audit(
        signature,
        capability,
        generator=adapter,
        budget=3,
    )

    assert result.status is expected_status, (
        signature.nodes,
        signature.requirements,
        capability.feature_id,
        result.reasons,
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM t LIMIT 10",
        "SELECT 1",
    ],
)
def test_discovered_limit_and_scalar_literal_are_reachable(sql: str) -> None:
    adapter = ProductionGeneratorAdapter()
    signature = SignatureExtractor("8.0.41").extract(sql)
    capability = adapter.find_capability(signature)

    result = CapabilityAuditor().audit(
        signature,
        capability,
        generator=adapter,
        budget=3,
    )

    assert result.status is Reachability.SUPPORTED, (
        capability.feature_id,
        result.reasons,
    )


@pytest.mark.parametrize(
    ("sql", "expected_feature"),
    [
        ("TABLE t ORDER BY 1", "validation_table_only"),
        (
            "TABLE t UNION VALUES ROW(1) ORDER BY 1",
            "validation_table_values_union_distinct",
        ),
        (
            "SELECT 1 WHERE EXISTS (TABLE t) ORDER BY 1",
            "validation_table_subquery",
        ),
    ],
)
def test_explicit_table_discovery_routes_to_real_directed_witness(
    sql: str, expected_feature: str
) -> None:
    adapter = ProductionGeneratorAdapter()
    signature = SignatureExtractor("8.0.41").extract(sql)
    capability = adapter.find_capability(signature)

    witness = adapter.generate_for_validation(capability.feature_id, seed=0)

    assert capability.feature_id == expected_feature
    assert "explicit_table" in witness.signature.nodes
    assert witness.sql.endswith("ORDER BY 1") or "ORDER BY 1," in witness.sql


def test_table_in_subquery_is_not_claimed_by_an_exists_witness() -> None:
    adapter = ProductionGeneratorAdapter()
    signature = SignatureExtractor("8.0.41").extract(
        "SELECT 1 WHERE 1 IN (TABLE one_col) ORDER BY 1"
    )
    capability = adapter.find_capability(signature)
    result = CapabilityAuditor().audit(signature, capability, generator=adapter, budget=4)

    assert "subquery_in_table" in signature.nodes
    assert result.status is Reachability.GAP
    assert any("subquery_in_table" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "sql",
    [
        "(TABLE one_col) ORDER BY 1",
        "SELECT (TABLE one_col) AS x ORDER BY 1",
    ],
)
def test_generic_table_subquery_is_not_claimed_by_an_exists_witness(sql: str) -> None:
    adapter = ProductionGeneratorAdapter()
    signature = SignatureExtractor("8.0.41").extract(sql)
    capability = adapter.find_capability(signature)
    result = CapabilityAuditor().audit(signature, capability, generator=adapter, budget=4)

    assert "subquery" in signature.nodes
    assert "subquery_exists" not in signature.nodes
    assert result.status is Reachability.GAP


def test_table_values_union_distinct_uses_a_distinct_witness() -> None:
    adapter = ProductionGeneratorAdapter()
    signature = SignatureExtractor("8.0.41").extract(
        "TABLE one_col UNION DISTINCT VALUES ROW(1) ORDER BY 1"
    )
    capability = adapter.find_capability(signature)
    witness = adapter.generate_for_validation(capability.feature_id, seed=0)
    result = CapabilityAuditor().audit(signature, capability, generator=adapter, budget=4)

    assert capability.feature_id == "validation_table_values_union_distinct"
    assert "set_union_distinct" in witness.signature.nodes
    assert "set_union_all" not in witness.signature.nodes
    assert result.status is Reachability.SUPPORTED


@pytest.mark.parametrize(
    ("sql", "expected_feature", "expected_node"),
    [
        (
            "SELECT 1 ORDER BY 1 LIMIT 0",
            "validation_scalar_limit_zero",
            "limit_zero",
        ),
        (
            "SELECT id FROM t ORDER BY 1 LIMIT 2 OFFSET 1",
            "validation_table_offset_limit",
            "offset",
        ),
    ],
)
def test_limit_boundary_discovery_routes_to_real_directed_witness(
    sql: str, expected_feature: str, expected_node: str
) -> None:
    adapter = ProductionGeneratorAdapter()
    signature = SignatureExtractor("8.0.41").extract(sql)
    capability = adapter.find_capability(signature)
    witness = adapter.generate_for_validation(capability.feature_id, seed=0)

    assert capability.feature_id == expected_feature
    assert expected_node in witness.signature.nodes


def test_comma_limit_offset_routes_to_offset_not_limit_zero() -> None:
    adapter = ProductionGeneratorAdapter()
    signature = SignatureExtractor("8.0.41").extract("SELECT id FROM t ORDER BY 1 LIMIT 0, 10")
    capability = adapter.find_capability(signature)

    assert capability.feature_id == "validation_table_offset_limit"


@pytest.mark.parametrize(
    ("sql", "expected_feature"),
    [
        ("SELECT id FROM t ORDER BY 1 LIMIT 0", "validation_table_limit_zero"),
        (
            "SELECT 1 ORDER BY 1 LIMIT 2 OFFSET 1",
            "validation_scalar_offset_limit",
        ),
    ],
)
def test_limit_requirement_routing_produces_same_domain_witness(
    sql: str, expected_feature: str
) -> None:
    adapter = ProductionGeneratorAdapter()
    signature = SignatureExtractor("8.0.41").extract(sql)
    capability = adapter.find_capability(signature)
    result = CapabilityAuditor().audit(signature, capability, generator=adapter, budget=4)

    assert capability.feature_id == expected_feature
    assert result.status is Reachability.SUPPORTED, result.reasons


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM t ORDER BY 1 LIMIT 10, 0",
        "SELECT id FROM t ORDER BY 1 LIMIT 0 OFFSET 10",
    ],
)
def test_zero_limit_with_offset_routes_to_combined_real_witness(sql: str) -> None:
    adapter = ProductionGeneratorAdapter()
    signature = SignatureExtractor("8.0.41").extract(sql)
    capability = adapter.find_capability(signature)

    witness = adapter.generate_for_validation(capability.feature_id, seed=0)
    result = CapabilityAuditor().audit(
        signature,
        capability,
        generator=adapter,
        budget=4,
    )

    assert capability.feature_id == "validation_table_offset_limit_zero"
    assert {"limit_zero", "offset"} <= set(witness.signature.nodes)
    assert result.status is Reachability.SUPPORTED


def test_scalar_zero_limit_with_offset_routes_to_scalar_real_witness() -> None:
    adapter = ProductionGeneratorAdapter()
    signature = SignatureExtractor("8.0.41").extract("SELECT 1 ORDER BY 1 LIMIT 0 OFFSET 10")
    capability = adapter.find_capability(signature)
    result = CapabilityAuditor().audit(
        signature,
        capability,
        generator=adapter,
        budget=4,
    )

    assert capability.feature_id == "validation_scalar_offset_limit_zero"
    assert result.status is Reachability.SUPPORTED


def test_derived_explicit_column_discovery_routes_to_real_directed_witness() -> None:
    adapter = ProductionGeneratorAdapter()
    signature = SignatureExtractor("8.0.41").extract(
        "SELECT d.x1 FROM (SELECT id FROM t) AS d (x1) ORDER BY 1"
    )
    capability = adapter.find_capability(signature)

    witness = adapter.generate_for_validation(capability.feature_id, seed=0)

    assert capability.feature_id == "validation_derived_explicit_columns"
    assert capability.evidence_ready is True
    assert "derived_explicit_columns" in witness.signature.nodes
    assert " AS `d` (`dq1`" in witness.sql
    result = CapabilityAuditor().audit(
        signature,
        capability,
        generator=adapter,
        budget=4,
    )
    assert result.status is Reachability.SUPPORTED, result.reasons


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT d.x1 FROM (VALUES ROW(1)) AS d (x1) ORDER BY 1",
        "SELECT d.x1 FROM (TABLE one_col) AS d (x1) ORDER BY 1",
    ],
)
def test_unimplemented_derived_body_kinds_remain_explicit_gaps(sql: str) -> None:
    adapter = ProductionGeneratorAdapter()
    signature = SignatureExtractor("8.0.41").extract(sql)
    capability = adapter.find_capability(signature)
    result = CapabilityAuditor().audit(signature, capability, generator=adapter, budget=4)

    assert {"derived_table", "derived_explicit_columns"} <= set(signature.nodes)
    assert result.status is Reachability.GAP
