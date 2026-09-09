from __future__ import annotations

import pytest

from select_fuzz.generation.pq_eligibility import PqEligibilityValidator
from select_fuzz.validation.generator_adapter import ProductionGeneratorAdapter, _VALIDATION_SQL
from select_fuzz.validation.models import Reachability
from select_fuzz.validation.reachability import CapabilityAuditor
from select_fuzz.validation.signature import SignatureExtractor


def test_directed_validation_sql_contains_only_pq_construction_paths() -> None:
    for sql in _VALIDATION_SQL.values():
        PqEligibilityValidator().validate_text(sql)


@pytest.mark.parametrize(
    "feature_id",
    [
        "cte_recursive",
        "window_frames",
        "json_create_extract",
        "scene_temporary",
        "function_fulltext_spatial",
    ],
)
def test_unsupported_catalog_feature_fails_before_schema_or_query_search(
    feature_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = ProductionGeneratorAdapter()

    def forbidden_search(*args: object, **kwargs: object) -> None:
        raise AssertionError("an unsupported feature must not enter witness search")

    monkeypatch.setattr(adapter.schema_generator, "generate", forbidden_search)
    with pytest.raises(ValueError, match="PQ"):
        adapter.generate_for_validation(feature_id, seed=1)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT COUNT(*)",
        "VALUES ROW(1)",
        "SELECT id FROM t LIMIT 0",
        "SELECT id FROM t LEFT JOIN u ON t.id = u.id",
        "SELECT id FROM t WHERE EXISTS (SELECT id FROM u)",
        "SELECT SUM(id) OVER() FROM t",
    ],
)
def test_unsupported_discovery_is_a_gap_without_a_generator_probe(sql: str) -> None:
    adapter = ProductionGeneratorAdapter()
    signature = SignatureExtractor("8.0.41").extract(sql)
    capability = adapter.find_capability(signature)
    result = CapabilityAuditor().audit(signature, capability, generator=None)
    assert result.status is Reachability.GAP
    assert capability.feature_id == "pq_excluded"
    assert "directed generator probe is required" not in result.reasons
