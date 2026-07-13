"""Composable CPU-dense performance testing pipeline."""

from select_fuzz.performance.calibration import (
    CalibrationDivergence,
    CalibrationEngine,
    CalibrationExhausted,
    CalibrationFailureKind,
    CalibrationInfrastructurePause,
    CalibrationTerminated,
    CostModel,
    ReferenceAnalyzer,
)
from select_fuzz.performance.execution import FormalRunner, classify_execution
from select_fuzz.performance.models import (
    Assessment,
    CalibrationAttempt,
    FormalRun,
    FrozenCase,
    Measurement,
    Outcome,
    PerformancePolicy,
    ScaleKnobs,
    Verdict,
    WorkloadScale,
)
from select_fuzz.performance.oracle import assess
from select_fuzz.performance.service import PerformanceService
from select_fuzz.performance.templates import (
    CpuDenseGroupSortTemplate,
    CpuDenseJoinTemplate,
    CpuDenseRangeSortTemplate,
    CpuDenseScanTemplate,
    CpuDenseSetupManifest,
    CpuDenseWindowTemplate,
)
from select_fuzz.performance.tree import (
    Family,
    PlanParseError,
    ShapeBoundary,
    TreeNode,
    TreePlan,
    parse_tree,
)

__all__ = [
    "Assessment",
    "CalibrationAttempt",
    "CalibrationDivergence",
    "CalibrationEngine",
    "CalibrationExhausted",
    "CalibrationFailureKind",
    "CalibrationInfrastructurePause",
    "CalibrationTerminated",
    "CostModel",
    "CpuDenseScanTemplate",
    "CpuDenseSetupManifest",
    "CpuDenseRangeSortTemplate",
    "CpuDenseJoinTemplate",
    "CpuDenseGroupSortTemplate",
    "CpuDenseWindowTemplate",
    "Family",
    "FormalRun",
    "FormalRunner",
    "FrozenCase",
    "Measurement",
    "Outcome",
    "PerformancePolicy",
    "PerformanceService",
    "PlanParseError",
    "ReferenceAnalyzer",
    "ScaleKnobs",
    "ShapeBoundary",
    "TreeNode",
    "TreePlan",
    "Verdict",
    "WorkloadScale",
    "assess",
    "classify_execution",
    "parse_tree",
]
