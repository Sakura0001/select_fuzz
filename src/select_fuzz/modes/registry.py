"""Single registration point for independently packaged run modes."""

from select_fuzz.modes.contracts import ModeDefinition
from select_fuzz.modes.correctness.entrypoint import build_correctness_runner
from select_fuzz.modes.fuzz import build_fuzz_runner
from select_fuzz.modes.performance.entrypoint import build_performance_runner


MODE_REGISTRY = {
    "correctness": ModeDefinition("correctness", "三库对比", build_correctness_runner),
    "performance": ModeDefinition("performance", "性能对比", build_performance_runner),
    "fuzz": ModeDefinition("fuzz", "并发 Fuzz", build_fuzz_runner),
}


__all__ = ["MODE_REGISTRY"]
