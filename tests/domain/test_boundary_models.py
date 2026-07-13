from __future__ import annotations

from collections.abc import Callable

import pytest

from select_fuzz.config import NodeRole
from select_fuzz.domain.models import (
    ErrorInfo,
    ExecutionStatus,
    NodeExecution,
    RunEvent,
    RunRequest,
    _freeze,
)
from select_fuzz.domain.values import SeedTree, deterministic_id, stable_fingerprint


ERROR = ErrorInfo(1, "HY000", "error")


@pytest.mark.parametrize(
    ("factory", "error"),
    [
        (lambda: _freeze({1: "bad"}), TypeError),
        (
            lambda: NodeExecution.success(
                role=NodeRole.BASELINE,
                connection_id=1,
                started_ns=0,
                ended_ns=1,
                connection_reusable=1,
            ),
            TypeError,
        ),
        (
            lambda: NodeExecution.failure(
                role=NodeRole.BASELINE,
                status=ExecutionStatus.ERROR,
                started_ns=0,
                ended_ns=1,
                connection_id=1,
                error=ERROR,
                watchdog_error_type="",
            ),
            TypeError,
        ),
        (
            lambda: NodeExecution.failure(
                role=NodeRole.BASELINE,
                status=ExecutionStatus.ERROR,
                started_ns=0,
                ended_ns=1,
                connection_id=0,
                error=ERROR,
            ),
            ValueError,
        ),
        (
            lambda: NodeExecution(
                NodeRole.BASELINE, ExecutionStatus.ERROR, 0, 1, 1
            ),
            ValueError,
        ),
        (
            lambda: NodeExecution.failure(
                role=NodeRole.BASELINE,
                status=ExecutionStatus.SUCCESS,
                started_ns=0,
                ended_ns=1,
                connection_id=1,
                error=ERROR,
            ),
            ValueError,
        ),
        (lambda: RunRequest("", "correctness", 1, 1, 1, 1), ValueError),
        (lambda: RunRequest("run", "other", 1, 1, 1, 1), ValueError),
        (lambda: RunRequest("run", "correctness", 1, 0, 1, 1), ValueError),
        (lambda: RunRequest("run", "correctness", 1, 1, 0, 1), ValueError),
        (lambda: RunEvent("", 0, "kind", {}), ValueError),
        (lambda: RunEvent("run", 0, "", {}), ValueError),
        (lambda: deterministic_id("Bad", 1), ValueError),
        (lambda: stable_fingerprint(float("nan")), ValueError),
        (lambda: stable_fingerprint({1: "bad"}), TypeError),
        (lambda: stable_fingerprint({1, 2}), TypeError),
    ],
)
def test_domain_boundaries_reject_ambiguous_or_invalid_runtime_values(
    factory: Callable[[], object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        factory()


def test_freezing_and_fingerprinting_cover_supported_collection_and_scalar_types() -> None:
    assert _freeze(bytearray(b"x")) == b"x"
    assert _freeze({"values": {1, 2}}) == {"values": frozenset({1, 2})}
    values = [None, True, 1, "one", 1.25, b"bytes", {}, [], ()]
    fingerprints = {stable_fingerprint(value) for value in values}
    assert len(fingerprints) == len(values)
    assert SeedTree(1).derive("path", 1, b"bytes") == SeedTree(1).derive(
        "path", 1, b"bytes"
    )
