from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path

from select_fuzz.validation.models import (
    EpochCheckpoint,
    FeatureSignature,
    GapRecord,
    Reachability,
    ReachabilityResult,
    SourceCandidate,
    TelemetrySample,
)
from select_fuzz.validation.report import build_coverage_report, write_validation_report


NOW = datetime(2026, 7, 13, tzinfo=UTC)


def test_report_exposes_coverage_saturation_gaps_and_runbook(tmp_path: Path) -> None:
    supported = FeatureSignature("8.0.41", ("select",), ("table",))
    missing = FeatureSignature("8.0.41", ("select", "window"), ("table",))
    gap = GapRecord(missing.key, "P1", Reachability.GAP, ("missing window",), NOW)
    report = build_coverage_report(
        run_id="run-1",
        sources=(
            SourceCandidate(
                "https://dev.mysql.com/doc/refman/8.0/en/select.html",
                "a" * 64,
                NOW,
                "text/html",
            ),
        ),
        signatures=(supported, missing),
        results=(
            ReachabilityResult(supported.key, Reachability.SUPPORTED, witness_seed=1),
            ReachabilityResult(missing.key, Reachability.GAP, ("missing window",)),
        ),
        gaps=(gap,),
        checkpoints=(
            EpochCheckpoint("run-1", 1, "u1", 1, 0, NOW, 10),
            EpochCheckpoint("run-1", 2, "u2", 2, 1, NOW, 20),
            EpochCheckpoint("run-1", 3, "u3", 2, 1, NOW, 30),
        ),
        telemetry=(TelemetrySample("run-1", 3, 30, 100, 2, 4, 3),),
        generated_at=NOW,
    )

    written = write_validation_report(report, tmp_path)

    assert report.saturation.new_signatures_last_checkpoint == 0
    assert report.unresolved_by_priority == {"P1": 1}
    assert set(written) == {
        "coverage.json",
        "gaps.json",
        "index.html",
        "operator-runbook.json",
        "source-manifest.json",
    }
    coverage = json.loads((tmp_path / "coverage.json").read_text())
    runbook = json.loads((tmp_path / "operator-runbook.json").read_text())
    assert coverage["status_counts"] == {"gap": 1, "supported": 1}
    assert runbook["raw_web_sql_policy"] == "never_execute"
    assert "scripts/validation_12h.py" in " ".join(runbook["commands"])
    assert "<!doctype html>" in (tmp_path / "index.html").read_text().lower()
