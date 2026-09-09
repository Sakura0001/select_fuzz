"""Parallel Query (PQ) hardening harness for the self-developed MySQL-compatible engine.

This package implements three PQ-focused modes that all enforce "trigger PQ
first" before measuring or comparing:

- ``fast``       : generate SQL -> EXPLAIN -> confirm PQ -> execute -> verify
                  (PQ vs non-PQ). Tailored to produce PQ-likely SQL.
- ``compare``   : same SQL against preconfigured PQ and serial endpoints.
- ``performance``: confirm PQ triggered, record dop / PQ time / non-PQ time /
                  speedup / rows.

Every real run requires an explicitly configured serial comparison endpoint.
Runtime parameters are inherited and observed without harness-issued SETs;
normal driver charset negotiation and autocommit behavior are retained.
The harness reuses ``mysql.connector`` and the oracle tolerance semantics.
"""

from __future__ import annotations

from select_fuzz.pq.config import PQConfig
from select_fuzz.pq.detector import PQMarkers, detect_pq, pq_triggered
from select_fuzz.pq.oracle import CompareOutcome, compare_result_sets

__all__ = [
    "CompareOutcome",
    "PQConfig",
    "PQMarkers",
    "compare_result_sets",
    "detect_pq",
    "pq_triggered",
]
