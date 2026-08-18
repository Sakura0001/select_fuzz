from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from select_fuzz.artifacts.bundle import CaseBundleWriter, FindingRecord
from select_fuzz.api.events import EventBroker, EventHistoryExpired, encode_sse
from select_fuzz.api.read_index import ReadIndex
from select_fuzz.config import COMPARISON_ROLES


def test_sse_encoding_and_resume_from_last_event_id() -> None:
    broker = EventBroker(history_limit=3, queue_size=2, heartbeat_seconds=0.01)
    one = broker.publish("run.state", {"run_id": "r1", "state": "running"})
    two = broker.publish("finding.created", {"id": "case-1"})
    assert one.sequence == 1 and two.sequence == 2
    assert encode_sse(two).startswith(b"id: 2\nevent: finding.created\n")

    async def resume() -> int:
        stream = broker.subscribe(after=1)
        event = await anext(stream)
        await stream.aclose()
        return event.sequence

    assert asyncio.run(resume()) == 2


def test_expired_history_and_slow_client_queue_are_bounded() -> None:
    broker = EventBroker(history_limit=2, queue_size=1, heartbeat_seconds=1)
    broker.publish("event", {"n": 1})
    broker.publish("event", {"n": 2})
    broker.publish("event", {"n": 3})
    with pytest.raises(EventHistoryExpired):
        broker.replay(after=0)

    async def slow_client_is_dropped() -> bool:
        stream = broker.subscribe(after=3)
        waiting = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        broker.publish("event", {"n": 4})
        broker.publish("event", {"n": 5})
        first = await waiting
        assert first.sequence == 4
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        return True

    assert asyncio.run(slow_client_is_dropped())


def test_subscription_registration_is_atomic_and_sequence_survives_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        state = tmp_path / "state.sqlite3"
        broker = EventBroker(state, history_limit=10)
        broker.publish("one", {"n": 1})
        subscription = broker.open_subscription(after=0)
        broker.publish("two", {"n": 2})
        assert (await anext(subscription)).sequence == 1
        assert (await anext(subscription)).sequence == 2
        await subscription.aclose()

        reopened = EventBroker(state, history_limit=10)
        assert reopened.sequence == 2
        assert [event.sequence for event in reopened.replay(0)] == [1, 2]

    asyncio.run(scenario())


def test_sqlite_index_rebuilds_from_jsonl_and_ignores_torn_tail(tmp_path: Path) -> None:
    facts = tmp_path / "events.jsonl"
    facts.write_bytes(
        b'{' + b'"sequence":1,"kind":"finding.created","payload":'
        + json.dumps(
            {
                "id": "case-7",
                "run_id": "run-1",
                "mode": "correctness",
                "severity": "high",
                "node": "custom_on",
                "feature": "cte",
                "errno": 1064,
                "occurred_at": "2026-07-13T00:00:00Z",
            },
            separators=(",", ":"),
        ).encode()
        + b'}\n{"sequence":2'
    )
    index = ReadIndex(tmp_path / "read.sqlite3")
    assert index.rebuild(facts) == 1
    page = index.list_findings(mode="correctness", severity="high", node="custom_on")
    assert [item["id"] for item in page] == ["case-7"]

    (tmp_path / "read.sqlite3").unlink()
    assert index.rebuild(facts) == 1
    assert index.get_finding("case-7")["feature"] == "cte"  # type: ignore[index]


def test_read_index_refresh_projects_only_new_committed_facts(tmp_path: Path) -> None:
    facts = tmp_path / "events.jsonl"
    first = {
        "sequence": 1, "kind": "finding.created",
        "payload": {"id": "case-1", "run_id": "r", "mode": "correctness",
                    "severity": "high", "occurred_at": "2026-07-13T00:00:01Z"},
    }
    second = {
        "sequence": 2, "kind": "finding.created",
        "payload": {"id": "case-2", "run_id": "r", "mode": "correctness",
                    "severity": "high", "occurred_at": "2026-07-13T00:00:02Z"},
    }
    facts.write_text(json.dumps(first) + "\n", encoding="utf-8")
    index = ReadIndex(tmp_path / "read.sqlite3")
    assert index.rebuild(facts) == 1
    with facts.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(second) + "\n")
    assert index.refresh(facts) == 1
    assert [row["id"] for row in index.list_findings()] == ["case-2", "case-1"]


def test_read_index_projects_authoritative_correctness_and_performance_events(
    tmp_path: Path,
) -> None:
    facts = tmp_path / "events.jsonl"
    facts.write_text(
        json.dumps(
            {
                "type": "finding",
                "case_id": "case-correctness",
                "run_id": "run-c",
                "mode": "correctness",
                "original_verdict": "result_mismatch",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "performance_alert",
                "case_id": "case-performance",
                "run_id": "run-p",
                "verdict": "perf_alert",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "performance_calibration_failure",
                "case_id": "case-calibration",
                "run_id": "run-p",
                "failure_category": "setup_mismatch",
                "occurred_at": "2026-07-13T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    index = ReadIndex(tmp_path / "read.sqlite3")

    assert index.rebuild(facts) == 3
    assert index.get_finding("case-correctness")["mode"] == "correctness"  # type: ignore[index]
    assert index.get_finding("case-performance")["mode"] == "performance"  # type: ignore[index]
    calibration = index.get_finding("case-calibration")
    assert calibration is not None
    assert calibration["mode"] == "performance"
    assert calibration["severity"] == "setup_mismatch"


def test_read_index_filters_writer_generator_findings_by_errno_and_classification(
    tmp_path: Path,
) -> None:
    database = "sf_c_20260713t120000_w0_r1_sabc_n123_q0"
    record = FindingRecord(
        case_id="case_generator_error",
        run_id="run_generator_error",
        mode="correctness",
        databases={role: database for role in COMPARISON_ROLES},
        seeds={"query": 1},
        setup_sql=("CREATE TABLE `t` (`id` INT);",),
        query_sql="SELECT missing FROM `t` ORDER BY 1",
        query_limits={"timeout_seconds": 1, "row_limit": 10, "byte_limit": 1024},
        payload_sha256="a" * 64,
        original_verdict="unexpected_valid_error",
        first_difference={
            "category": "generator_contract",
            "expected_error": None,
            "observed_identities": [
                {"errno": 1054, "sqlstate": "42S22"} for _ in COMPARISON_ROLES
            ],
            "reason": "a valid-lane query returned an error on every node",
        },
        statistics={},
        configuration_fingerprints={role: "fingerprint" for role in COMPARISON_ROLES},
        results={
            role: {
                "role": role.value,
                "status": "error",
                "error": {"errno": 1054, "sqlstate": "42S22"},
            }
            for role in COMPARISON_ROLES
        },
    )
    CaseBundleWriter(tmp_path).write_finding(record)
    index = ReadIndex(tmp_path / "read.sqlite3")

    assert index.rebuild(tmp_path / "events.jsonl") == 1
    page = index.list_findings(
        classification="unexpected_valid_error",
        errno=1054,
    )

    assert len(page) == 1
    assert page[0]["id"] == "case_generator_error"
    assert page[0]["classification"] == "unexpected_valid_error"
    assert page[0]["errno"] == 1054
    assert page[0]["sqlstate"] == "42S22"
    assert page[0]["reason"] == (
        "a valid-lane query returned an error on every node"
    )
    assert index.list_findings(classification="result_mismatch", errno=1054) == []
