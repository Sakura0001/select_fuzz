from __future__ import annotations

from dataclasses import dataclass

from select_fuzz.validation.models import FeatureSignature, Reachability
from select_fuzz.validation.reachability import (
    CapabilityAuditor,
    CatalogCapability,
)


@dataclass
class FakeGenerator:
    outputs: tuple[str, ...]

    def generate_for_validation(self, feature_id: str, seed: int) -> str | None:
        assert feature_id == "window_feature"
        return self.outputs[seed] if seed < len(self.outputs) else None


def test_static_missing_shape_is_gap() -> None:
    target = FeatureSignature("8.0.41", ("select", "window"), ("table",))
    catalog = CatalogCapability(
        feature_id="window_feature",
        nodes=frozenset({"select"}),
        requirements=frozenset({"table"}),
        evidence_ready=True,
    )

    result = CapabilityAuditor().audit(target, catalog)

    assert result.status is Reachability.GAP
    assert result.reasons == ("missing nodes: window",)


def test_unverified_official_evidence_blocks_support_claim() -> None:
    target = FeatureSignature("8.0.41", ("select", "window"), ("table",))
    catalog = CatalogCapability(
        feature_id="window_feature",
        nodes=frozenset(target.nodes),
        requirements=frozenset(target.requirements),
        evidence_ready=False,
        evidence_ids=("manual_window",),
    )

    result = CapabilityAuditor().audit(target, catalog)

    assert result.status is Reachability.BLOCKED_EVIDENCE
    assert "manual_window" in result.reasons[0]


def test_dynamic_probe_must_resynthesize_the_target_shape() -> None:
    target = FeatureSignature(
        "8.0.41",
        ("select", "window", "window_order"),
        ("table", "unique_tiebreaker"),
    )
    catalog = CatalogCapability(
        feature_id="window_feature",
        nodes=frozenset(target.nodes),
        requirements=frozenset(target.requirements),
        evidence_ready=True,
    )
    generator = FakeGenerator(
        outputs=(
            "SELECT id FROM t ORDER BY 1",
            "SELECT ROW_NUMBER() OVER (ORDER BY id) FROM t ORDER BY 1",
        )
    )

    result = CapabilityAuditor().audit(target, catalog, generator=generator, budget=2)

    assert result.status is Reachability.SUPPORTED
    assert result.witness_seed == 1
    assert result.witness_feature_id == "window_feature"


def test_no_dynamic_witness_remains_a_gap() -> None:
    target = FeatureSignature("8.0.41", ("select", "window"), ("table",))
    catalog = CatalogCapability(
        feature_id="window_feature",
        nodes=frozenset(target.nodes),
        requirements=frozenset(target.requirements),
        evidence_ready=True,
    )

    result = CapabilityAuditor().audit(
        target,
        catalog,
        generator=FakeGenerator(("SELECT id FROM t ORDER BY 1",)),
        budget=3,
    )

    assert result.status is Reachability.GAP
    assert result.reasons == ("directed generator produced no matching witness",)
