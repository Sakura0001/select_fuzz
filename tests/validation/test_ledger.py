from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path

from select_fuzz.validation.ledger import ValidationLedger
from select_fuzz.validation.models import (
    EpochCheckpoint,
    FeatureSignature,
    GapRecord,
    Reachability,
    ReachabilityResult,
    SourceCandidate,
)


NOW = datetime(2026, 7, 13, tzinfo=UTC)


def _gap() -> GapRecord:
    signature = FeatureSignature("8.0.41", ("select", "window"), ("table",))
    return GapRecord(signature.key, "P1", Reachability.GAP, ("missing window",), NOW)


def _checkpoint(epoch: int, signatures: int = 1) -> EpochCheckpoint:
    return EpochCheckpoint("run-1", epoch, f"cursor-{epoch}", signatures, 1, NOW)


def test_gap_event_and_signature_count_are_idempotent_and_fsynced(
    tmp_path: Path, monkeypatch: object
) -> None:
    fsync_calls: list[int] = []
    original_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        original_fsync(fd)

    # pytest's MonkeyPatch is intentionally kept out of the production API type surface.
    monkeypatch.setattr(os, "fsync", recording_fsync)  # type: ignore[attr-defined]
    ledger = ValidationLedger(tmp_path / "state.db", tmp_path / "gaps.jsonl")
    gap = _gap()

    assert ledger.record_gap(gap) is True
    assert ledger.record_gap(gap) is False
    assert ledger.record_signature(gap.signature_key, run_id="run-1", epoch=1) is True
    assert ledger.record_signature(gap.signature_key, run_id="run-1", epoch=2) is False

    assert ledger.list_gaps() == (gap,)
    assert ledger.needs_regression(gap.signature_key) is True
    assert ledger.mark_regression_complete(gap.signature_key) is True
    assert ledger.mark_regression_complete(gap.signature_key) is False
    assert ledger.needs_regression(gap.signature_key) is False
    assert ledger.list_gaps() == ()
    assert ledger.record_gap(gap) is True
    assert ledger.list_gaps() == (gap,)
    events = tuple(ledger.iter_events())
    assert [event["type"] for event in events].count("gap_recorded") == 1
    assert fsync_calls


def test_atomic_checkpoint_resumes_latest_epoch_without_recounting(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    events = tmp_path / "events.jsonl"
    ledger = ValidationLedger(db, events)
    ledger.checkpoint(_checkpoint(1))
    ledger.checkpoint(_checkpoint(2, signatures=3))
    ledger.checkpoint(_checkpoint(2, signatures=3))

    resumed = ValidationLedger(db, events)

    assert resumed.latest_checkpoint("run-1") == _checkpoint(2, signatures=3)
    checkpoint_events = [
        event for event in resumed.iter_events() if event["type"] == "checkpoint"
    ]
    assert [event["epoch"] for event in checkpoint_events] == [1, 2]


def test_corrupt_jsonl_tail_does_not_corrupt_transactional_state(tmp_path: Path) -> None:
    ledger = ValidationLedger(tmp_path / "state.db", tmp_path / "events.jsonl")
    ledger.record_gap(_gap())
    ledger.checkpoint(_checkpoint(4))
    with (tmp_path / "events.jsonl").open("ab") as stream:
        stream.write(b'{"partial"')

    next_gap = GapRecord(
        FeatureSignature("8.0.41", ("select", "cte"), ("table",)).key,
        "P2",
        Reachability.GAP,
        ("missing cte",),
        NOW,
    )
    ledger.record_gap(next_gap)

    resumed = ValidationLedger(tmp_path / "state.db", tmp_path / "events.jsonl")

    assert resumed.latest_checkpoint("run-1") == _checkpoint(4)
    assert set(resumed.list_gaps()) == {_gap(), next_gap}
    assert all(isinstance(event, dict) for event in resumed.iter_events())
    assert len(tuple(resumed.iter_events())) == 3


def test_checkpoint_jsonl_has_stable_event_ids(tmp_path: Path) -> None:
    ledger = ValidationLedger(tmp_path / "state.db", tmp_path / "events.jsonl")
    ledger.checkpoint(_checkpoint(1))
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert rows[0]["event_id"] == "checkpoint:run-1:1"


def test_blocked_evidence_gap_does_not_request_generator_regression(tmp_path: Path) -> None:
    ledger = ValidationLedger(tmp_path / "state.db", tmp_path / "events.jsonl")
    signature = FeatureSignature("8.0.41", ("select", "cte"), ("table",))
    blocked = GapRecord(
        signature.key,
        "P1",
        Reachability.BLOCKED_EVIDENCE,
        ("source lock missing",),
        NOW,
    )
    ledger.record_gap(blocked)
    assert ledger.needs_regression(signature.key) is False


def test_ledger_persists_sources_signatures_and_audit_witnesses(tmp_path: Path) -> None:
    ledger = ValidationLedger(tmp_path / "state.db", tmp_path / "events.jsonl")
    source = SourceCandidate(
        "https://dev.mysql.com/doc/refman/8.0/en/select.html",
        "b" * 64,
        NOW,
        "text/html",
    )
    signature = FeatureSignature("8.0.41", ("select", "window"), ("table",))
    audit = ReachabilityResult(
        signature.key,
        Reachability.SUPPORTED,
        witness_seed=7,
        witness_feature_id="window_named_inline",
    )

    assert ledger.record_source(source) is True
    assert ledger.record_source(source) is False
    assert ledger.record_signature(
        signature, run_id="run-1", epoch=3, source_sha256=source.content_sha256
    ) is True
    ledger.record_audit(audit, run_id="run-1", epoch=3)

    assert ledger.list_sources() == (source,)
    assert ledger.list_signatures() == (signature,)
    assert ledger.list_audits() == (audit,)


def test_blocked_evidence_can_transition_to_actionable_gap(tmp_path: Path) -> None:
    ledger = ValidationLedger(tmp_path / "state.db", tmp_path / "events.jsonl")
    signature = FeatureSignature("8.0.41", ("select", "cte"), ("table",))
    blocked = GapRecord(
        signature.key,
        "P1",
        Reachability.BLOCKED_EVIDENCE,
        ("source lock missing",),
        NOW,
    )
    actionable = GapRecord(
        signature.key,
        "P1",
        Reachability.GAP,
        ("generator witness missing",),
        NOW,
    )
    ledger.record_gap(blocked)

    assert ledger.record_gap(actionable) is True
    assert ledger.list_gaps() == (actionable,)
    assert ledger.needs_regression(signature.key) is True


def test_normal_supported_audit_resolves_old_gap(tmp_path: Path) -> None:
    ledger = ValidationLedger(tmp_path / "state.db", tmp_path / "events.jsonl")
    gap = _gap()
    ledger.record_gap(gap)
    assert ledger.resolve_gap(gap.signature_key) is True
    assert ledger.resolve_gap(gap.signature_key) is False
    assert ledger.list_gaps() == ()
