from __future__ import annotations

import pytest

from select_fuzz.validation.generator_adapter import ProductionGeneratorAdapter
from select_fuzz.validation.models import Reachability
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


def test_reload_from_disk_rebuilds_compatible_generator_class_graph() -> None:
    adapter = ProductionGeneratorAdapter.reload_from_disk()
    signature = adapter.signature_for_feature("grouping_aggregate_having")
    result = CapabilityAuditor().audit(
        signature,
        adapter.capability_for_feature("grouping_aggregate_having"),
        generator=adapter,
        budget=3,
    )
    assert result.status is Reachability.SUPPORTED


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
