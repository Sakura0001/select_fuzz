"""Production assembly for official discovery, analysis, reachability and reporting."""

from __future__ import annotations

from dataclasses import dataclass, replace
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
import json
import os
import resource
import subprocess
import sys
import time
from threading import Event, active_count

from select_fuzz.validation.candidate import CandidateExtractor
from select_fuzz.validation.discovery import PersistentSourceDiscovery, extract_official_links
from select_fuzz.validation.generator_adapter import ProductionGeneratorAdapter
from select_fuzz.validation.ledger import ValidationLedger
from select_fuzz.validation.loop import (
    ContinuousValidationLoop,
    HookContext,
    ValidationRunSummary,
)
from select_fuzz.validation.models import (
    FeatureSignature,
    GapRecord,
    Reachability,
    ReachabilityResult,
    SourceCandidate,
    TelemetrySample,
)
from select_fuzz.validation.reachability import CapabilityAuditor
from select_fuzz.validation.reaudit_worker import run_isolated_reaudit
from select_fuzz.validation.regression_hook import ExternalRegressionHook
from select_fuzz.validation.report import build_coverage_report, write_validation_report
from select_fuzz.validation.signature import SignatureExtractor
from select_fuzz.validation.source import FetchTransport, OfficialSourceAcquirer
from select_fuzz.validation.telemetry import (
    FaultEvent,
    ScheduledFaultController,
    TelemetryRecorder,
    ResourceTrendPolicy,
    build_fault_schedule,
    fault_event_id,
)


@dataclass(frozen=True, slots=True)
class ProductionValidationConfig:
    run_id: str
    output_dir: Path
    duration_s: float = 12 * 3600
    checkpoint_s: float = 30 * 60
    freeze_s: float = 30 * 60
    max_epochs: int | None = None
    max_consecutive_errors: int = 5
    seed_urls: tuple[str, ...] = ()
    catalog_path: Path | None = None
    regression_commands: tuple[tuple[str, ...], ...] = ()
    regression_timeout_s: float = 300
    fault_seed: int = 0
    fault_commands: tuple[tuple[str, tuple[str, ...]], ...] = ()
    fault_probe_commands: tuple[tuple[str, tuple[str, ...]], ...] = ()
    mysql_connection_probe_command: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProductionValidationResult:
    summary: ValidationRunSummary
    ledger: ValidationLedger
    report_paths: dict[str, Path]


class ProductionValidationHooks:
    def __init__(
        self,
        *,
        ledger: ValidationLedger,
        discovery: PersistentSourceDiscovery,
        acquirer: OfficialSourceAcquirer,
        adapter: ProductionGeneratorAdapter,
        transport: FetchTransport | None,
        regression_commands: tuple[tuple[str, ...], ...],
        regression_timeout_s: float,
        audit_path: Path,
    ) -> None:
        self.ledger = ledger
        self.discovery = discovery
        self.acquirer = acquirer
        self.adapter = adapter
        self.transport = transport
        self.extractor = CandidateExtractor()
        self.signature_extractor = SignatureExtractor("8.0.41")
        self.auditor = CapabilityAuditor(extractor=self.signature_extractor)
        self._cached: dict[str, Path] = {}
        self._claimed_urls: dict[SourceCandidate, str] = {}
        self._reaudit_timeout_s = regression_timeout_s
        self._regression = (
            None
            if not regression_commands
            else ExternalRegressionHook(
                commands=regression_commands,
                timeout_s=regression_timeout_s,
                audit_path=audit_path,
                reaudit=self._reaudit_gap,
            )
        )

    def search(self, epoch: int, context: HookContext) -> SourceCandidate | None:
        queued = self.discovery.next(
            deadline_monotonic=context.deadline_monotonic,
            monotonic=context.monotonic,
            stop_event=context.stop_event,
        )
        if queued is None:
            return None
        try:
            cached = self.acquirer.acquire(
                queued.url,
                self.transport,
                deadline_monotonic=context.deadline_monotonic,
                monotonic=context.monotonic,
            )
            self.ledger.record_source(cached.source)
            self._cached[cached.source.content_sha256] = cached.path
            if cached.source.media_type == "text/html":
                links = extract_official_links(cached.path.read_bytes(), base_url=queued.url)
                self.discovery.add_links(links, discovered_from=queued.url)
            self._claimed_urls[cached.source] = queued.url
            return cached.source
        except Exception as exc:
            self.discovery.retry(queued.url, error=type(exc).__name__)
            raise

    def complete(self, source: SourceCandidate, context: HookContext) -> None:
        queued_url = self._claimed_urls.get(source, source.url)
        self.discovery.complete(queued_url)
        self._claimed_urls.pop(source, None)

    def fail(self, source: SourceCandidate, error: Exception, context: HookContext) -> None:
        queued_url = self._claimed_urls.get(source, source.url)
        self.discovery.retry(queued_url, error=type(error).__name__)
        self._claimed_urls.pop(source, None)

    def analyze(
        self, source: SourceCandidate, context: HookContext
    ) -> tuple[FeatureSignature, ...]:
        if not context.active():
            return ()
        path = self._cached[source.content_sha256]
        if source.media_type == "text/html":
            candidates = self.extractor.from_html(path.read_bytes())
        else:
            candidates = ()
        signatures = {self.signature_extractor.extract(item.sql) for item in candidates}
        return tuple(sorted(signatures, key=lambda item: item.key))

    def audit(self, signature: FeatureSignature, context: HookContext) -> ReachabilityResult:
        if not context.active():
            return ReachabilityResult(
                signature.key, Reachability.GAP, ("validation deadline reached",)
            )
        capability = self.adapter.find_capability(signature)
        return self.auditor.audit(signature, capability, generator=self.adapter, budget=16)

    def regression(
        self,
        gap: GapRecord,
        *,
        allow_code_change: bool,
        context: HookContext,
    ) -> ReachabilityResult | None:
        if self._regression is None:
            return ReachabilityResult(
                gap.signature_key,
                Reachability.GAP,
                ("operator regression command is not configured",),
            )
        return self._regression.run(gap, allow_code_change=allow_code_change, context=context)

    def _reaudit_gap(self, gap: GapRecord) -> ReachabilityResult:
        signatures = {item.key: item for item in self.ledger.list_signatures()}
        signature = signatures[gap.signature_key]
        return run_isolated_reaudit(
            signature,
            budget=32,
            timeout_s=self._reaudit_timeout_s,
        )


def _resource_sample(
    run_id: str, epoch: int, elapsed: float, *, mysql_connections: int
) -> TelemetrySample:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    rss = int(usage.ru_maxrss * (1024 if sys.platform.startswith("linux") else 1))
    try:
        open_fds = len(os.listdir("/dev/fd"))
    except OSError:
        open_fds = 0
    return TelemetrySample(run_id, epoch, elapsed, rss, active_count(), open_fds, mysql_connections)


def run_production_validation(
    config: ProductionValidationConfig,
    *,
    transport: FetchTransport | None = None,
    stop_event: Event | None = None,
    fault_injector: Callable[[FaultEvent], None] | None = None,
    fault_recovery_probe: Callable[[FaultEvent], bool] | None = None,
    mysql_connection_probe: Callable[[], int] | None = None,
) -> ProductionValidationResult:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    ledger = ValidationLedger(config.output_dir / "state.db", config.output_dir / "events.jsonl")
    discovery = PersistentSourceDiscovery(ledger, catalog_path=config.catalog_path)
    for url in config.seed_urls:
        ledger.enqueue_source(url, discovered_from="operator_seed")
    discovery.seed()
    adapter = ProductionGeneratorAdapter()
    hooks = ProductionValidationHooks(
        ledger=ledger,
        discovery=discovery,
        acquirer=OfficialSourceAcquirer(config.output_dir / "source-cache"),
        adapter=adapter,
        transport=transport,
        regression_commands=config.regression_commands,
        regression_timeout_s=config.regression_timeout_s,
        audit_path=config.output_dir / "regression.jsonl",
    )
    previous = ledger.latest_checkpoint(config.run_id)
    elapsed_before = 0.0 if previous is None else previous.elapsed_s
    startup_epoch = 0 if previous is None else previous.epoch
    startup_deadline = time.monotonic() + max(0.0, config.duration_s - elapsed_before)
    startup_context = HookContext(
        startup_epoch,
        startup_deadline,
        stop_event or Event(),
        time.monotonic,
    )
    for signature in ledger.list_signatures():
        if not startup_context.active():
            break
        result = hooks.audit(signature, startup_context)
        ledger.record_audit(result, run_id=config.run_id, epoch=startup_epoch)
        if result.status is Reachability.SUPPORTED:
            ledger.resolve_gap(signature.key)
        else:
            ledger.record_gap(
                GapRecord.from_result(
                    result,
                    priority="P1",
                    discovered_at=datetime.now(UTC),
                )
            )
    telemetry = TelemetryRecorder(config.output_dir / "telemetry.jsonl")
    samples = list(telemetry.read())
    fault_path = config.output_dir / "faults.jsonl"
    schedule = build_fault_schedule(seed=config.fault_seed, duration_s=config.duration_s)
    fault_namespace = f"{config.run_id}:{config.fault_seed}:{config.duration_s:g}"

    def event_id_for(event: FaultEvent) -> str:
        return fault_event_id(event, namespace=fault_namespace)

    persisted_faults = _read_fault_statuses(fault_path)
    scheduled_ids = {event_id_for(event) for event in schedule}
    fault_failures = [
        f"{event_id}:{status}"
        for event_id, status in persisted_faults.items()
        if event_id in scheduled_ids and status != "recovered"
    ]
    fault_commands = dict(config.fault_commands)
    fault_probes = dict(config.fault_probe_commands)

    def inject_fault(event: FaultEvent) -> None:
        event_id = event_id_for(event)
        started_fault = time.monotonic()
        command = fault_commands.get(event.kind.value)
        probe_command = fault_probes.get(event.kind.value)
        _record_fault(fault_path, event, event_id=event_id, status="started", recovery_s=0.0)
        status = "not_configured"
        try:
            if fault_injector is not None:
                fault_injector(event)
                injected = True
            elif command is not None:
                completed = subprocess.run(
                    command,
                    shell=False,
                    check=False,
                    capture_output=True,
                    timeout=event.recovery_deadline_s,
                )
                injected = completed.returncode == 0
                if not injected:
                    status = "injection_failed"
            else:
                injected = False
            if injected:
                if fault_recovery_probe is None and probe_command is None:
                    status = "probe_not_configured"
                elif _wait_for_fault_recovery(
                    event,
                    started_monotonic=started_fault,
                    probe=fault_recovery_probe,
                    probe_command=probe_command,
                ):
                    status = "recovered"
                else:
                    status = "recovery_timeout"
        except subprocess.TimeoutExpired:
            status = "injection_timeout"
        except Exception:
            status = "injection_failed"
        recovery_s = time.monotonic() - started_fault
        _record_fault(fault_path, event, event_id=event_id, status=status, recovery_s=recovery_s)
        persisted_faults[event_id] = status
        if status != "recovered":
            fault_failures.append(f"{event.kind.value}:{status}")

    faults = ScheduledFaultController(
        schedule,
        inject=inject_fault,
        resume_elapsed_s=elapsed_before,
        completed_event_ids=frozenset(persisted_faults),
        event_id_factory=event_id_for,
    )

    def command_mysql_connection_probe() -> int:
        completed = subprocess.run(
            config.mysql_connection_probe_command,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if completed.returncode != 0:
            raise RuntimeError("mysql connection probe failed")
        try:
            count = int(completed.stdout.strip())
        except ValueError as exc:
            raise RuntimeError("mysql connection probe did not return an integer") from exc
        if count < 0:
            raise RuntimeError("mysql connection probe returned a negative count")
        return count

    effective_mysql_probe = mysql_connection_probe
    if effective_mysql_probe is None and config.mysql_connection_probe_command:
        effective_mysql_probe = command_mysql_connection_probe

    def record_telemetry(run_id: str, epoch: int, elapsed: float) -> None:
        sample = _resource_sample(
            run_id,
            epoch,
            elapsed,
            mysql_connections=(effective_mysql_probe or (lambda: 0))(),
        )
        telemetry.append(sample)
        samples.append(sample)
        faults.tick(elapsed)

    summary = ContinuousValidationLoop(
        run_id=config.run_id,
        ledger=ledger,
        hooks=hooks,
        duration_s=config.duration_s,
        checkpoint_s=config.checkpoint_s,
        freeze_s=min(config.freeze_s, config.duration_s),
        max_consecutive_errors=config.max_consecutive_errors,
        stop_event=stop_event,
        telemetry_hook=record_telemetry,
    ).run(max_epochs=config.max_epochs)
    if len(samples) >= 2:
        trend = ResourceTrendPolicy().evaluate(tuple(samples))
        if not trend.passed:
            ledger.record_error(
                run_id=config.run_id,
                epoch=summary.epochs_completed,
                error_type="ResourceTrendFailure",
                message=",".join(trend.reasons),
            )
            summary = replace(summary, aborted_reason="resource_trend_failed")
    if fault_failures:
        summary = replace(summary, aborted_reason="fault_recovery_failed")
    report = build_coverage_report(
        run_id=config.run_id,
        sources=ledger.list_sources(),
        signatures=ledger.list_signatures(),
        results=ledger.list_audits(),
        gaps=ledger.list_gaps(),
        checkpoints=ledger.list_checkpoints(config.run_id),
        telemetry=tuple(samples),
    )
    paths = write_validation_report(report, config.output_dir / "report")
    return ProductionValidationResult(summary, ledger, paths)


def _record_fault(
    path: Path,
    event: FaultEvent,
    *,
    event_id: str,
    status: str,
    recovery_s: float,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "event_id": event_id,
                    "at_s": event.at_s,
                    "kind": event.kind.value,
                    "status": status,
                    "recovery_s": recovery_s,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        stream.flush()
        os.fsync(stream.fileno())


def _read_fault_statuses(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    statuses: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        event_id = payload.get("event_id")
        status = payload.get("status")
        if isinstance(event_id, str) and isinstance(status, str):
            statuses[event_id] = status
    return statuses


def _wait_for_fault_recovery(
    event: FaultEvent,
    *,
    started_monotonic: float,
    probe: Callable[[FaultEvent], bool] | None,
    probe_command: tuple[str, ...] | None,
) -> bool:
    deadline = started_monotonic + event.recovery_deadline_s
    while time.monotonic() < deadline:
        if probe is not None:
            if probe(event):
                return True
        elif probe_command is not None:
            remaining = deadline - time.monotonic()
            try:
                completed = subprocess.run(
                    probe_command,
                    shell=False,
                    check=False,
                    capture_output=True,
                    timeout=max(0.001, remaining),
                )
            except subprocess.TimeoutExpired:
                return False
            if completed.returncode == 0:
                return True
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    return False


__all__ = [
    "ProductionValidationConfig",
    "ProductionValidationHooks",
    "ProductionValidationResult",
    "run_production_validation",
]
