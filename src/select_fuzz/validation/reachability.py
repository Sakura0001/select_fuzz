"""Static evidence gating plus directed generator reachability auditing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from select_fuzz.validation.models import (
    FeatureSignature,
    Reachability,
    ReachabilityResult,
)
from select_fuzz.validation.signature import SignatureExtractor


@dataclass(frozen=True, slots=True)
class CatalogCapability:
    """Narrow adapter boundary from the product catalog into validation."""

    feature_id: str
    nodes: frozenset[str]
    requirements: frozenset[str]
    evidence_ready: bool
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.feature_id or not self.nodes:
            raise ValueError("feature_id and nodes must not be empty")
        if not self.evidence_ready and not self.evidence_ids:
            raise ValueError("blocked capabilities must identify evidence")


class ValidationGenerator(Protocol):
    """Adapter that re-synthesizes SQL; it must never return raw network text."""

    def generate_for_validation(
        self, feature_id: str, seed: int
    ) -> str | GeneratedWitness | None: ...


@dataclass(frozen=True, slots=True)
class GeneratedWitness:
    sql: str
    signature: FeatureSignature


class CapabilityAuditor:
    def __init__(self, *, extractor: SignatureExtractor | None = None) -> None:
        self.extractor = extractor or SignatureExtractor()

    def audit(
        self,
        signature: FeatureSignature,
        catalog: CatalogCapability,
        *,
        generator: ValidationGenerator | None = None,
        budget: int = 32,
    ) -> ReachabilityResult:
        if budget <= 0:
            raise ValueError("budget must be positive")
        missing_nodes = sorted(set(signature.nodes) - catalog.nodes)
        missing_requirements = sorted(set(signature.requirements) - catalog.requirements)
        reasons: list[str] = []
        if missing_nodes:
            reasons.append("missing nodes: " + ", ".join(missing_nodes))
        if missing_requirements:
            reasons.append("missing requirements: " + ", ".join(missing_requirements))
        if reasons:
            return ReachabilityResult(signature.key, Reachability.GAP, tuple(reasons))
        if not catalog.evidence_ready:
            evidence = ", ".join(sorted(catalog.evidence_ids))
            return ReachabilityResult(
                signature.key,
                Reachability.BLOCKED_EVIDENCE,
                ("official evidence is not locked: " + evidence,),
            )
        if generator is None:
            return ReachabilityResult(
                signature.key,
                Reachability.GAP,
                ("directed generator probe is required",),
            )

        for seed in range(budget):
            try:
                generated = generator.generate_for_validation(catalog.feature_id, seed)
                if generated is None:
                    continue
                witness = (
                    self.extractor.extract(generated.sql)
                    if isinstance(generated, GeneratedWitness)
                    else self.extractor.extract(generated)
                )
                if isinstance(generated, GeneratedWitness) and witness.key != generated.signature.key:
                    continue
            except (TypeError, ValueError):
                continue
            if set(signature.nodes) <= set(witness.nodes) and set(
                signature.requirements
            ) <= set(witness.requirements):
                return ReachabilityResult(
                    signature.key,
                    Reachability.SUPPORTED,
                    witness_seed=seed,
                    witness_feature_id=catalog.feature_id,
                )
        return ReachabilityResult(
            signature.key,
            Reachability.GAP,
            ("directed generator produced no matching witness",),
        )


__all__ = [
    "CapabilityAuditor",
    "CatalogCapability",
    "GeneratedWitness",
    "ValidationGenerator",
]
