"""Deadline-aware continuous validation coordinator with durable cadence checkpoints."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import threading
import time
from typing import Protocol

from select_fuzz.validation.ledger import ValidationLedger
from select_fuzz.validation.models import (
    EpochCheckpoint,
    FeatureSignature,
    GapRecord,
    Reachability,
    ReachabilityResult,
    SourceCandidate,
)


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


@dataclass(frozen=True, slots=True)
class HookContext:
    epoch: int
    deadline_monotonic: float
    stop_event: threading.Event
    monotonic: Callable[[], float]

    @property
    def remaining_s(self) -> float:
        return max(0.0, self.deadline_monotonic - self.monotonic())

    def active(self) -> bool:
        return not self.stop_event.is_set() and self.remaining_s > 0


class ValidationHooks(Protocol):
    def search(self, epoch: int, context: HookContext) -> SourceCandidate | None: ...

    def analyze(
        self, source: SourceCandidate, context: HookContext
    ) -> tuple[FeatureSignature, ...]: ...

    def audit(
        self, signature: FeatureSignature, context: HookContext
    ) -> ReachabilityResult: ...

    def regression(
        self,
        gap: GapRecord,
        *,
        allow_code_change: bool,
        context: HookContext,
    ) -> ReachabilityResult | None: ...

    def complete(self, source: SourceCandidate, context: HookContext) -> None: ...

    def fail(
        self, source: SourceCandidate, error: Exception, context: HookContext
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ValidationRunSummary:
    run_id: str
    epochs_completed: int
    unique_signatures: int
    gaps: int
    errors: int
    elapsed_s: float
    stopped: bool
    aborted_reason: str | None = None


class _SystemClock:
    @staticmethod
    def monotonic() -> float:
        return time.monotonic()

    @staticmethod
    def sleep(seconds: float) -> None:
        time.sleep(seconds)


class ContinuousValidationLoop:
    def __init__(
        self,
        *,
        run_id: str,
        ledger: ValidationLedger,
        hooks: ValidationHooks,
        duration_s: float = 12 * 3600,
        checkpoint_s: float = 30 * 60,
        freeze_s: float | None = None,
        idle_sleep_s: float = 1.0,
        backoff_s: float = 5.0,
        max_consecutive_errors: int = 5,
        clock: Clock | None = None,
        stop_event: threading.Event | None = None,
        priority: str = "P1",
        telemetry_hook: Callable[[str, int, float], None] | None = None,
    ) -> None:
        if duration_s <= 0 or checkpoint_s <= 0:
            raise ValueError("duration_s and checkpoint_s must be positive")
        resolved_freeze_s = min(30 * 60, duration_s) if freeze_s is None else freeze_s
        if not 0 <= resolved_freeze_s <= duration_s:
            raise ValueError("freeze_s must be between zero and duration")
        if idle_sleep_s < 0 or backoff_s <= 0 or max_consecutive_errors <= 0:
            raise ValueError("sleep values and max_consecutive_errors are invalid")
        if priority not in {"P0", "P1", "P2", "P3"}:
            raise ValueError("priority must be P0, P1, P2, or P3")
        self.run_id = run_id
        self.ledger = ledger
        self.hooks = hooks
        self.duration_s = duration_s
        self.checkpoint_s = checkpoint_s
        self.freeze_s = resolved_freeze_s
        self.idle_sleep_s = idle_sleep_s
        self.backoff_s = backoff_s
        self.max_consecutive_errors = max_consecutive_errors
        self.clock = clock or _SystemClock()
        self.stop_event = stop_event or threading.Event()
        self.priority = priority
        self.telemetry_hook = telemetry_hook

    def run(self, *, max_epochs: int | None = None) -> ValidationRunSummary:
        if max_epochs is not None and max_epochs <= 0:
            raise ValueError("max_epochs must be positive")
        previous = self.ledger.latest_checkpoint(self.run_id)
        epoch = 0 if previous is None else previous.epoch
        elapsed_before = 0.0 if previous is None else previous.elapsed_s
        started = self.clock.monotonic()
        deadline = started + max(0.0, self.duration_s - elapsed_before)
        next_checkpoint = (
            (int(elapsed_before // self.checkpoint_s) + 1) * self.checkpoint_s
        )
        errors = 0
        consecutive_errors = 0
        completed_this_call = 0
        cursor = ""
        aborted_reason: str | None = None

        while self.clock.monotonic() < deadline and not self.stop_event.is_set():
            if max_epochs is not None and completed_this_call >= max_epochs:
                break
            epoch += 1
            context = HookContext(epoch, deadline, self.stop_event, self.clock.monotonic)
            cursor = ""
            sleep_s = self.idle_sleep_s
            source: SourceCandidate | None = None
            try:
                source = self.hooks.search(epoch, context)
                if source is not None and context.active():
                    cursor = source.url
                    for signature in self.hooks.analyze(source, context):
                        self.ledger.record_signature(
                            signature,
                            run_id=self.run_id,
                            epoch=epoch,
                            source_sha256=source.content_sha256,
                        )
                        result = self.hooks.audit(signature, context)
                        self.ledger.record_audit(result, run_id=self.run_id, epoch=epoch)
                        if result.status is Reachability.SUPPORTED:
                            self.ledger.resolve_gap(signature.key)
                            continue
                        gap = GapRecord.from_result(
                            result,
                            priority=self.priority,
                            discovered_at=datetime.now(UTC),
                        )
                        self.ledger.record_gap(gap)
                        if (
                            result.status is Reachability.GAP
                            and self.ledger.needs_regression(gap.signature_key)
                            and context.active()
                        ):
                            elapsed_now = elapsed_before + self.clock.monotonic() - started
                            reaudit = self.hooks.regression(
                                gap,
                                allow_code_change=elapsed_now
                                < self.duration_s - self.freeze_s,
                                context=context,
                            )
                            if reaudit is not None:
                                self.ledger.record_audit(
                                    reaudit, run_id=self.run_id, epoch=epoch
                                )
                                if reaudit.status is Reachability.SUPPORTED:
                                    self.ledger.mark_regression_complete(gap.signature_key)
                                else:
                                    self.ledger.record_gap(
                                        GapRecord.from_result(
                                            reaudit,
                                            priority=self.priority,
                                            discovered_at=datetime.now(UTC),
                                        )
                                    )
                    self.hooks.complete(source, context)
                consecutive_errors = 0
            except Exception as exc:
                if source is not None:
                    self.hooks.fail(source, exc, context)
                errors += 1
                consecutive_errors += 1
                cursor = f"error:{type(exc).__name__}"
                self.ledger.record_error(
                    run_id=self.run_id,
                    epoch=epoch,
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
                sleep_s = self.backoff_s
                if consecutive_errors >= self.max_consecutive_errors:
                    aborted_reason = "continuous_failure_threshold"

            self.clock.sleep(min(sleep_s, max(0.0, deadline - self.clock.monotonic())))
            elapsed = self._elapsed(elapsed_before, started)
            if self.telemetry_hook is not None:
                self.telemetry_hook(self.run_id, epoch, elapsed)
            completed_this_call += 1
            if elapsed >= next_checkpoint:
                self._checkpoint(epoch, cursor, elapsed)
                while next_checkpoint <= elapsed:
                    next_checkpoint += self.checkpoint_s
            if aborted_reason is not None:
                break

        elapsed = self._elapsed(elapsed_before, started)
        latest = self.ledger.latest_checkpoint(self.run_id)
        if epoch > 0 and (latest is None or latest.epoch != epoch):
            self._checkpoint(epoch, cursor, elapsed)
        return ValidationRunSummary(
            run_id=self.run_id,
            epochs_completed=epoch,
            unique_signatures=self.ledger.signature_count(),
            gaps=len(self.ledger.list_gaps()),
            errors=errors,
            elapsed_s=elapsed,
            stopped=self.stop_event.is_set(),
            aborted_reason=aborted_reason,
        )

    def _elapsed(self, elapsed_before: float, started: float) -> float:
        return min(
            self.duration_s,
            elapsed_before + self.clock.monotonic() - started,
        )

    def _checkpoint(self, epoch: int, cursor: str, elapsed: float) -> None:
        self.ledger.checkpoint(
            EpochCheckpoint(
                run_id=self.run_id,
                epoch=epoch,
                source_cursor=cursor,
                unique_signatures=self.ledger.signature_count(),
                gaps=len(self.ledger.list_gaps()),
                updated_at=datetime.now(UTC),
                elapsed_s=elapsed,
            )
        )


__all__ = [
    "Clock",
    "ContinuousValidationLoop",
    "HookContext",
    "ValidationHooks",
    "ValidationRunSummary",
]
