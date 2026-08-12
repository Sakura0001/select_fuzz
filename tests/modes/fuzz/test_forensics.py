from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from select_fuzz.modes.fuzz.forensics import (
    FuzzErrorAggregator,
    capture_exception_evidence,
    error_fingerprint,
)


class _ConnectorFailure(RuntimeError):
    errno = -1
    sqlstate = "HY000"


def _raise_chained_failure() -> None:
    try:
        raise ValueError("底层协议状态损坏 connection 418")
    except ValueError as cause:
        raise _ConnectorFailure("Unread result found after 31.25 seconds") from cause


def test_capture_exception_evidence_preserves_original_chain_and_traceback() -> None:
    try:
        _raise_chained_failure()
    except _ConnectorFailure as error:
        evidence = capture_exception_evidence(error, "fetch")
    else:  # pragma: no cover - test helper must raise
        pytest.fail("test helper did not raise")

    assert evidence["failure_stage"] == "fetch"
    exception = evidence["exception"]
    assert exception["type"] == "_ConnectorFailure"
    assert exception["module"] == __name__
    assert exception["message"] == "Unread result found after 31.25 seconds"
    assert "_ConnectorFailure('Unread result found" in exception["repr"]
    assert exception["args"] == ("Unread result found after 31.25 seconds",)
    assert exception["errno"] == -1
    assert exception["sqlstate"] == "HY000"
    assert exception["relation"] == "root"
    assert evidence["exception_chain"][1]["type"] == "ValueError"
    assert evidence["exception_chain"][1]["relation"] == "cause"
    assert any(
        frame["function"] == "_raise_chained_failure"
        for frame in evidence["traceback_frames"]
    )


def test_capture_exception_evidence_is_bounded() -> None:
    error = RuntimeError("x" * 10_000, "y" * 10_000)

    evidence = capture_exception_evidence(error, "execute")

    exception = evidence["exception"]
    assert len(exception["message"]) == 4096
    assert len(exception["repr"]) == 4096
    assert all(len(value) <= 4096 for value in exception["args"])


def _evidence(message: str, *, stage: str = "execute", errno: int = -1):
    error = _ConnectorFailure(message)
    error.errno = errno
    return capture_exception_evidence(error, stage)


def test_error_fingerprint_normalizes_dynamic_connection_and_duration() -> None:
    first = _evidence(
        "Unread result on connection 418 at 192.168.10.20 after 31.25 seconds"
    )
    second = _evidence(
        "Unread result on connection 991 at 192.168.10.21 after 42.75 seconds"
    )

    assert error_fingerprint(first) == error_fingerprint(second)
    assert len(error_fingerprint(first)) == 12
    assert error_fingerprint(first) != error_fingerprint(
        _evidence("Socket is closed", stage="execute")
    )
    assert error_fingerprint(first) != error_fingerprint(
        _evidence(first["exception"]["message"], stage="fetch")
    )
    assert error_fingerprint(first) != error_fingerprint(
        _evidence(first["exception"]["message"], errno=2013)
    )


def test_error_aggregator_tracks_first_sample_period_and_suppression() -> None:
    current = [0]
    aggregator = FuzzErrorAggregator(clock_ns=lambda: current[0])
    evidence = _evidence("Unread result on connection 418")

    first = aggregator.record(
        evidence=evidence,
        worker="db0:reader-replica:0",
        database="sf_f_case",
        endpoint="replica",
        sql="SELECT 1",
        timed_out=False,
        connection_lost=False,
    )
    for ordinal in range(9):
        current[0] += 1_000_000_000
        repeated = aggregator.record(
            evidence=evidence,
            worker=f"db0:reader-replica:{ordinal % 3}",
            database="sf_f_case",
            endpoint="replica",
            sql="SELECT 1",
            timed_out=ordinal == 0,
            connection_lost=ordinal == 1,
        )

    snapshot = aggregator.snapshot(interval_seconds=10)

    assert first.is_new is True
    assert first.write_operation_event is True
    assert repeated.is_new is False
    assert repeated.write_operation_event is False
    assert snapshot["total_count"] == 10
    assert snapshot["interval_count"] == 10
    assert snapshot["rate_per_second"] == 1.0
    assert snapshot["fingerprint_count"] == 1
    top = snapshot["top"][0]
    assert top["fingerprint"] == first.fingerprint
    assert top["total_count"] == 10
    assert top["worker_count"] == 3
    assert top["database_count"] == 1
    assert top["endpoints"] == ("replica",)
    assert top["timed_out_count"] == 1
    assert top["connection_lost_count"] == 1
    assert top["sample_sql"] == "SELECT 1"
    assert aggregator.snapshot(interval_seconds=5)["interval_count"] == 0


def test_error_aggregator_emits_representative_after_suppression_window() -> None:
    current = [0]
    aggregator = FuzzErrorAggregator(clock_ns=lambda: current[0])
    evidence = _evidence("Unread result")
    aggregator.record(
        evidence=evidence,
        worker="reader-0",
        database="sf_f_case",
        endpoint="replica",
        sql="SELECT 1",
        timed_out=False,
        connection_lost=False,
    )
    current[0] = 1_000_000_000
    aggregator.record(
        evidence=evidence,
        worker="reader-0",
        database="sf_f_case",
        endpoint="replica",
        sql="SELECT 2",
        timed_out=False,
        connection_lost=False,
    )
    current[0] = 31_000_000_000

    decision = aggregator.record(
        evidence=evidence,
        worker="reader-0",
        database="sf_f_case",
        endpoint="replica",
        sql="SELECT 3",
        timed_out=False,
        connection_lost=False,
    )

    assert decision.write_operation_event is True
    assert decision.suppressed_repeats == 1


def test_error_aggregator_bounds_active_fingerprints_and_uses_other_bucket() -> None:
    aggregator = FuzzErrorAggregator(clock_ns=lambda: 0, max_fingerprints=64)

    for ordinal in range(70):
        aggregator.record(
            evidence=_evidence(f"distinct root cause {ordinal}"),
            worker=f"reader-{ordinal}",
            database="sf_f_case",
            endpoint="replica",
            sql=f"SELECT {ordinal}",
            timed_out=False,
            connection_lost=False,
        )

    snapshot = aggregator.snapshot(interval_seconds=5)

    assert snapshot["fingerprint_count"] == 64
    assert snapshot["other_count"] == 6
    assert len(snapshot["fingerprints"]) == 64
    assert all(len(item["recent_samples"]) <= 3 for item in snapshot["fingerprints"])


def test_error_aggregator_counts_concurrent_workers_exactly() -> None:
    aggregator = FuzzErrorAggregator()
    evidence = _evidence("shared connector failure")

    def record_worker(worker_id: int) -> None:
        for _ in range(100):
            aggregator.record(
                evidence=evidence,
                worker=f"reader-{worker_id}",
                database="sf_f_case",
                endpoint="replica",
                sql="SELECT 1",
                timed_out=False,
                connection_lost=False,
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        tuple(pool.map(record_worker, range(8)))

    snapshot = aggregator.snapshot(interval_seconds=1)
    assert snapshot["total_count"] == 800
    assert snapshot["interval_count"] == 800
    assert snapshot["top"][0]["worker_count"] == 8
