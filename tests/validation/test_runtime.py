from __future__ import annotations

from pathlib import Path
import json
import sys

from select_fuzz.validation.runtime import (
    ProductionValidationConfig,
    run_production_validation,
)
from select_fuzz.validation.source import FetchResponse
from select_fuzz.validation.ledger import ValidationLedger
from select_fuzz.validation.models import FeatureSignature, GapRecord, Reachability
from select_fuzz.validation.telemetry import build_fault_schedule
from datetime import UTC, datetime


def test_production_runtime_wires_real_loop_and_persists_report(tmp_path: Path) -> None:
    source_url = "https://dev.mysql.com/doc/refman/8.0/en/group-by-modifiers.html"
    html = b"""
      <code>SELECT a, COUNT(*) FROM t GROUP BY a WITH ROLLUP HAVING COUNT(*) > 0 ORDER BY 1</code>
    """

    def fetch(url: str, max_bytes: int) -> FetchResponse:
        assert url == source_url
        return FetchResponse(url, 200, "text/html", html)

    config = ProductionValidationConfig(
        run_id="runtime-test",
        output_dir=tmp_path,
        duration_s=5,
        checkpoint_s=1,
        freeze_s=0,
        max_epochs=1,
        seed_urls=(source_url,),
        mysql_connection_probe_command=(sys.executable, "-c", "print(7)"),
    )
    result = run_production_validation(config, transport=fetch)

    assert result.summary.epochs_completed == 1
    assert result.ledger.list_sources()
    assert result.ledger.list_signatures()
    assert result.ledger.list_audits()
    assert (tmp_path / "report" / "coverage.json").is_file()
    assert (tmp_path / "report" / "index.html").is_file()
    coverage = json.loads((tmp_path / "report" / "coverage.json").read_text())
    assert coverage["telemetry"]["samples"] == 1
    assert coverage["telemetry"]["latest"]["mysql_connections"] == 7


def test_production_runtime_resumes_checkpoint_and_persistent_frontier(tmp_path: Path) -> None:
    html = b"<code>SELECT id FROM t ORDER BY 1</code>"

    def fetch(url: str, max_bytes: int) -> FetchResponse:
        return FetchResponse(url, 200, "text/html", html)

    config = ProductionValidationConfig(
        run_id="resume-test",
        output_dir=tmp_path,
        duration_s=10,
        checkpoint_s=1,
        freeze_s=0,
        max_epochs=1,
        seed_urls=("https://dev.mysql.com/doc/refman/8.0/en/select.html",),
    )
    first = run_production_validation(config, transport=fetch)
    second = run_production_validation(config, transport=fetch)

    assert first.summary.epochs_completed == 1
    assert second.summary.epochs_completed == 2
    assert len(second.ledger.list_sources()) == 2
    assert second.ledger.signature_count() == 1
    assert len(second.ledger.list_audits()) == 1


def test_runtime_completes_original_queue_url_after_official_redirect(
    tmp_path: Path,
) -> None:
    requested = "https://dev.mysql.com/doc/refman/8.0/en/select.html"
    redirected = "https://dev.mysql.com/doc/refman/8.0/en/select-statement.html"
    config = ProductionValidationConfig(
        run_id="redirect-test",
        output_dir=tmp_path,
        duration_s=2,
        checkpoint_s=1,
        freeze_s=0,
        max_epochs=1,
        seed_urls=(requested,),
    )

    result = run_production_validation(
        config,
        transport=lambda url, limit: FetchResponse(
            redirected,
            200,
            "text/html",
            b"<code>SELECT 1</code>",
        ),
    )

    assert result.ledger.recover_claimed_sources() == 0


def test_startup_reaudits_persisted_signatures_and_closes_old_gap(tmp_path: Path) -> None:
    ledger = ValidationLedger(tmp_path / "state.db", tmp_path / "events.jsonl")
    signature = FeatureSignature("8.0.41", ("order_by", "select"), ("table", "unique_tiebreaker"))
    ledger.record_signature(signature, run_id="upgrade", epoch=0)
    ledger.record_gap(
        GapRecord(signature.key, "P1", Reachability.GAP, ("old generator",), datetime.now(UTC))
    )
    html = b"<code>SELECT id FROM t ORDER BY 1</code>"
    config = ProductionValidationConfig(
        run_id="upgrade",
        output_dir=tmp_path,
        duration_s=2,
        checkpoint_s=1,
        freeze_s=0,
        max_epochs=1,
        seed_urls=("https://dev.mysql.com/doc/refman/8.0/en/select.html",),
    )
    result = run_production_validation(
        config, transport=lambda url, limit: FetchResponse(url, 200, "text/html", html)
    )
    assert result.ledger.list_gaps() == ()


def test_runtime_executes_configured_scheduled_fault_and_records_recovery(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "fault-marker"
    html = b"<code>SELECT id FROM t ORDER BY 1</code>"
    config = ProductionValidationConfig(
        run_id="fault-test",
        output_dir=tmp_path,
        duration_s=2,
        checkpoint_s=1,
        freeze_s=0,
        max_epochs=2,
        seed_urls=("https://dev.mysql.com/doc/refman/8.0/en/select.html",),
        fault_seed=1,
        fault_commands=(
            (
                "connection_reset",
                (
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).touch()",
                ),
            ),
        ),
        fault_probe_commands=(
            (
                "connection_reset",
                (
                    sys.executable,
                    "-c",
                    "from pathlib import Path; "
                    f"raise SystemExit(0 if Path({str(marker)!r}).exists() else 1)",
                ),
            ),
        ),
    )
    result = run_production_validation(
        config, transport=lambda url, limit: FetchResponse(url, 200, "text/html", html)
    )
    assert marker.exists()
    fault_rows = [json.loads(line) for line in (tmp_path / "faults.jsonl").read_text().splitlines()]
    assert [row["status"] for row in fault_rows] == ["started", "recovered"]
    assert result.summary.aborted_reason is None


def test_runtime_fails_acceptance_when_scheduled_fault_is_not_configured(
    tmp_path: Path,
) -> None:
    url = "https://dev.mysql.com/doc/refman/8.0/en/select.html"
    config = ProductionValidationConfig(
        run_id="missing-fault-test",
        output_dir=tmp_path,
        duration_s=2,
        checkpoint_s=1,
        freeze_s=0,
        max_epochs=2,
        seed_urls=(url,),
        fault_seed=1,
    )

    result = run_production_validation(
        config,
        transport=lambda source_url, limit: FetchResponse(
            source_url, 200, "text/html", b"<code>SELECT 1</code>"
        ),
    )

    rows = [json.loads(line) for line in (tmp_path / "faults.jsonl").read_text().splitlines()]
    assert rows[-1]["status"] == "not_configured"
    assert result.summary.aborted_reason == "fault_recovery_failed"


def test_persisted_fault_cursor_prevents_replay_before_next_checkpoint(
    tmp_path: Path,
) -> None:
    event = build_fault_schedule(seed=1, duration_s=2)[0]
    event_id = f"fault-cursor-test:1:2:{event.kind.value}:{event.at_s:.6f}"
    (tmp_path / "faults.jsonl").write_text(
        json.dumps(
            {
                "event_id": event_id,
                "at_s": event.at_s,
                "kind": event.kind.value,
                "status": "recovered",
                "recovery_s": 0.1,
            }
        )
        + "\n"
    )
    marker = tmp_path / "replayed"
    url = "https://dev.mysql.com/doc/refman/8.0/en/select.html"
    config = ProductionValidationConfig(
        run_id="fault-cursor-test",
        output_dir=tmp_path,
        duration_s=2,
        checkpoint_s=2,
        freeze_s=0,
        max_epochs=2,
        seed_urls=(url,),
        fault_seed=1,
        fault_commands=(
            (
                "connection_reset",
                (
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).touch()",
                ),
            ),
        ),
        fault_probe_commands=(("connection_reset", (sys.executable, "-c", "pass")),),
    )

    result = run_production_validation(
        config,
        transport=lambda source_url, limit: FetchResponse(
            source_url, 200, "text/html", b"<code>SELECT 1</code>"
        ),
    )

    assert marker.exists() is False
    assert result.summary.aborted_reason is None
    rows = [json.loads(line) for line in (tmp_path / "faults.jsonl").read_text().splitlines()]
    assert [row["event_id"] for row in rows] == [event_id]
