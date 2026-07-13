from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from threading import Event

from select_fuzz.validation.ledger import ValidationLedger
from select_fuzz.validation.loop import ContinuousValidationLoop, HookContext
from select_fuzz.validation.models import (
    FeatureSignature,
    GapRecord,
    Reachability,
    ReachabilityResult,
    SourceCandidate,
)


NOW = datetime(2026, 7, 13, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class RecordingHooks:
    def __init__(
        self,
        *,
        unique: bool = False,
        fail_first: bool = False,
        fail_first_regression: bool = False,
    ) -> None:
        self.unique = unique
        self.fail_first = fail_first
        self.fail_first_regression = fail_first_regression
        self.search_calls = 0
        self.regressions: list[tuple[GapRecord, bool]] = []
        self.deadlines: list[float] = []
        self.completed_sources: list[str] = []
        self.failed_sources: list[str] = []

    def search(self, epoch: int, context: HookContext) -> SourceCandidate | None:
        self.deadlines.append(context.deadline_monotonic)
        self.search_calls += 1
        if self.fail_first and self.search_calls == 1:
            raise ConnectionError("offline")
        return SourceCandidate(
            f"https://dev.mysql.com/doc/{epoch}", "a" * 64, NOW, "text/html"
        )

    def analyze(
        self, source: SourceCandidate, context: HookContext
    ) -> tuple[FeatureSignature, ...]:
        suffix = source.url.rsplit("/", maxsplit=1)[1] if self.unique else "stable"
        return (FeatureSignature("8.0.41", ("select", f"shape_{suffix}"), ("table",)),)

    def audit(
        self, signature: FeatureSignature, context: HookContext
    ) -> ReachabilityResult:
        return ReachabilityResult(signature.key, Reachability.GAP, ("unreachable",))

    def regression(
        self, gap: GapRecord, *, allow_code_change: bool, context: HookContext
    ) -> ReachabilityResult:
        self.regressions.append((gap, allow_code_change))
        if self.fail_first_regression and len(self.regressions) == 1:
            raise RuntimeError("worker crashed during regression")
        return ReachabilityResult(
            gap.signature_key,
            Reachability.SUPPORTED,
            witness_seed=0,
            witness_feature_id="fixed_feature",
        )

    def complete(self, source: SourceCandidate, context: HookContext) -> None:
        self.completed_sources.append(source.url)

    def fail(
        self, source: SourceCandidate, error: Exception, context: HookContext
    ) -> None:
        self.failed_sources.append(source.url)


def _loop(tmp_path: Path, hooks: RecordingHooks, clock: FakeClock, **kwargs: object):
    ledger = ValidationLedger(tmp_path / "state.db", tmp_path / "events.jsonl")
    loop = ContinuousValidationLoop(
        run_id="run-1",
        ledger=ledger,
        hooks=hooks,
        clock=clock,
        duration_s=float(kwargs.pop("duration_s", 100)),
        freeze_s=float(kwargs.pop("freeze_s", 0)),
        idle_sleep_s=float(kwargs.pop("idle_sleep_s", 1)),
        backoff_s=float(kwargs.pop("backoff_s", 5)),
        stop_event=kwargs.pop("stop_event", None),
        checkpoint_s=float(kwargs.pop("checkpoint_s", 30)),
        max_consecutive_errors=int(kwargs.pop("max_consecutive_errors", 5)),
    )
    assert not kwargs
    return loop, ledger


def test_each_epoch_persists_immediately_and_resume_does_not_recount(tmp_path: Path) -> None:
    clock = FakeClock()
    hooks = RecordingHooks()
    loop, ledger = _loop(tmp_path, hooks, clock)

    first = loop.run(max_epochs=2)
    resumed = ContinuousValidationLoop(
        run_id="run-1",
        ledger=ledger,
        hooks=hooks,
        clock=clock,
        duration_s=100,
        idle_sleep_s=1,
    ).run(max_epochs=1)

    assert first.epochs_completed == 2
    assert resumed.epochs_completed == 3
    assert ledger.signature_count() == 1
    assert len(ledger.list_gaps()) == 0
    assert len(hooks.regressions) == 3
    assert hooks.deadlines and all(deadline == 100 for deadline in hooks.deadlines[:2])
    assert len(hooks.completed_sources) == 3
    assert ledger.latest_checkpoint("run-1").epoch == 3  # type: ignore[union-attr]


def test_deadline_stop_event_backoff_and_freeze_are_clock_driven(tmp_path: Path) -> None:
    clock = FakeClock()
    hooks = RecordingHooks(unique=True, fail_first=True)
    loop, ledger = _loop(
        tmp_path,
        hooks,
        clock,
        duration_s=12,
        freeze_s=4,
        idle_sleep_s=3,
        backoff_s=2,
    )

    summary = loop.run()

    assert summary.elapsed_s == 12
    assert summary.errors == 1
    assert [allow for _, allow in hooks.regressions] == [True, True, False, False]
    checkpoint = ledger.latest_checkpoint("run-1")
    assert checkpoint is not None
    assert checkpoint.elapsed_s == 12

    stopped = Event()
    stopped.set()
    other, _ = _loop(tmp_path / "stopped", RecordingHooks(), FakeClock(), stop_event=stopped)
    assert other.run().stopped is True


def test_failed_regression_hook_is_retried_after_gap_was_persisted(tmp_path: Path) -> None:
    clock = FakeClock()
    hooks = RecordingHooks(fail_first_regression=True)
    loop, ledger = _loop(tmp_path, hooks, clock, idle_sleep_s=0.1, backoff_s=0.1)

    summary = loop.run(max_epochs=2)

    assert summary.errors == 1
    assert len(hooks.regressions) == 2
    assert ledger.needs_regression(hooks.regressions[0][0].signature_key) is False


def test_checkpoint_cadence_is_time_based_not_one_per_epoch(tmp_path: Path) -> None:
    clock = FakeClock()
    hooks = RecordingHooks(unique=True)
    loop, ledger = _loop(
        tmp_path, hooks, clock, idle_sleep_s=3, checkpoint_s=5, duration_s=20
    )
    loop.run(max_epochs=4)
    checkpoint_events = [
        event for event in ledger.iter_events() if event["type"] == "checkpoint"
    ]
    assert [event["epoch"] for event in checkpoint_events] == [2, 4]
    assert [item.epoch for item in ledger.list_checkpoints("run-1")] == [2, 4]


def test_continuous_failure_threshold_records_sanitized_error(tmp_path: Path) -> None:
    class FailingHooks(RecordingHooks):
        def search(self, epoch: int, context: HookContext) -> SourceCandidate | None:
            raise ConnectionError("password=secret https://dev.mysql.com/x?token=secret")

    loop, ledger = _loop(
        tmp_path,
        FailingHooks(),
        FakeClock(),
        backoff_s=1,
        max_consecutive_errors=2,
    )
    summary = loop.run()
    assert summary.aborted_reason == "continuous_failure_threshold"
    assert summary.errors == 2
    assert "secret" not in ledger.list_errors()[0]["message"]
