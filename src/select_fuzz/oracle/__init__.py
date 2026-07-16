"""Typed three-node correctness oracle."""

from select_fuzz.oracle.compare import (
    OracleResult,
    OracleVerdict,
    PairwiseComparison,
    compare_three_nodes,
)
from select_fuzz.oracle.errors import OracleCapacityError, OracleInputError
from select_fuzz.oracle.query_errors import (
    ErrorIdentity,
    QueryErrorAnalysis,
    QueryErrorDisposition,
    analyze_query_errors,
)

__all__ = [
    "OracleInputError",
    "OracleCapacityError",
    "OracleResult",
    "OracleVerdict",
    "PairwiseComparison",
    "ErrorIdentity",
    "QueryErrorAnalysis",
    "QueryErrorDisposition",
    "analyze_query_errors",
    "compare_three_nodes",
]
