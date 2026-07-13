from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from select_fuzz.validation.models import (
    EpochCheckpoint,
    FeatureSignature,
    GapRecord,
    Reachability,
    ReachabilityResult,
    SourceCandidate,
    TelemetrySample,
)


NOW = datetime(2026, 7, 13, tzinfo=UTC)
DIGEST = "a" * 64


def test_feature_signature_key_is_order_independent_and_normalized() -> None:
    left = FeatureSignature(
        version="8.0.41",
        nodes=("WINDOW", "select", "cte", "window"),
        requirements=("Table", "unique_tiebreaker"),
    )
    right = FeatureSignature(
        version="8.0.41",
        nodes=("cte", "select", "window"),
        requirements=("unique_tiebreaker", "table"),
    )

    assert left.nodes == ("cte", "select", "window")
    assert left.requirements == ("table", "unique_tiebreaker")
    assert left.key == right.key


def test_source_candidate_requires_official_https_url_and_digest() -> None:
    source = SourceCandidate(
        url="https://dev.mysql.com/doc/refman/8.0/en/select.html",
        content_sha256=DIGEST,
        fetched_at=NOW,
        media_type="text/html",
    )
    assert source.official is True
    with pytest.raises(FrozenInstanceError):
        source.url = "https://example.com"  # type: ignore[misc]
    with pytest.raises(ValueError, match="official"):
        SourceCandidate("https://example.com/x", DIGEST, NOW, "text/html")
    with pytest.raises(ValueError, match="sha256"):
        SourceCandidate("https://dev.mysql.com/x", "bad", NOW, "text/html")
    github = SourceCandidate(
        "https://raw.githubusercontent.com/mysql/mysql-server/mysql-8.0.41/sql/sql_yacc.yy",
        DIGEST,
        NOW,
        "text/plain",
    )
    assert github.official is True


def test_reachability_and_gap_models_are_fail_closed() -> None:
    signature = FeatureSignature("8.0.41", ("select", "window"), ("table",))
    result = ReachabilityResult(
        signature_key=signature.key,
        status=Reachability.GAP,
        reasons=("missing node: window",),
    )
    gap = GapRecord.from_result(result, priority="P1", discovered_at=NOW)

    assert gap.signature_key == signature.key
    assert gap.status is Reachability.GAP
    with pytest.raises(ValueError, match="reasons"):
        ReachabilityResult(signature.key, Reachability.SUPPORTED, ("unexpected",))


def test_checkpoint_and_telemetry_validate_monotonic_counters() -> None:
    checkpoint = EpochCheckpoint(
        run_id="run-1",
        epoch=2,
        source_cursor="page-3",
        unique_signatures=9,
        gaps=2,
        updated_at=NOW,
    )
    sample = TelemetrySample(
        run_id="run-1",
        epoch=2,
        monotonic_s=30.5,
        rss_bytes=100,
        threads=2,
        open_fds=4,
        mysql_connections=3,
    )
    assert checkpoint.epoch == sample.epoch
    with pytest.raises(ValueError, match="nonnegative"):
        EpochCheckpoint("r", -1, "", 0, 0, NOW)
    with pytest.raises(ValueError, match="nonnegative"):
        TelemetrySample("r", 0, -0.1, 0, 0, 0, 0)
