from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

import pytest

from select_fuzz.validation.models import (
    EpochCheckpoint,
    FeatureSignature,
    GapRecord,
    Reachability,
    ReachabilityResult,
    SourceCandidate,
    TelemetrySample,
    is_official_source_url,
)


DIGEST = "0" * 64
NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("factory", "error"),
    [
        (
            lambda: SourceCandidate(
                "https://dev.mysql.com:bad/doc/", DIGEST, NOW, "text/html"
            ),
            ValueError,
        ),
        (
            lambda: SourceCandidate(
                "http://dev.mysql.com/doc/", DIGEST, NOW, "text/html"
            ),
            ValueError,
        ),
        (
            lambda: SourceCandidate(
                "https://dev.mysql.com/doc/", "bad", NOW, "text/html"
            ),
            ValueError,
        ),
        (
            lambda: SourceCandidate(
                "https://dev.mysql.com/doc/", DIGEST, datetime(2026, 1, 1), "text/html"
            ),
            ValueError,
        ),
        (
            lambda: SourceCandidate(
                "https://dev.mysql.com/doc/", DIGEST, NOW, "text /html"
            ),
            ValueError,
        ),
        (lambda: FeatureSignature("8.1", ("select",), ("guard",)), ValueError),
        (lambda: FeatureSignature("8.0.41", (), ("guard",)), ValueError),
        (lambda: FeatureSignature("8.0.41", ("bad token",), ("guard",)), ValueError),
        (lambda: ReachabilityResult("bad", Reachability.SUPPORTED), ValueError),
        (lambda: ReachabilityResult(DIGEST, "supported"), TypeError),
        (
            lambda: ReachabilityResult(DIGEST, Reachability.SUPPORTED, ("failure",)),
            ValueError,
        ),
        (lambda: ReachabilityResult(DIGEST, Reachability.GAP), ValueError),
        (
            lambda: ReachabilityResult(
                DIGEST, Reachability.GAP, ("missing",), witness_seed=-1
            ),
            ValueError,
        ),
        (
            lambda: GapRecord(DIGEST, "P4", Reachability.GAP, ("missing",), NOW),
            ValueError,
        ),
        (
            lambda: GapRecord(
                DIGEST, "P1", Reachability.SUPPORTED, ("missing",), NOW
            ),
            ValueError,
        ),
        (
            lambda: GapRecord(DIGEST, "P1", Reachability.GAP, (), NOW),
            ValueError,
        ),
        (
            lambda: GapRecord(
                DIGEST,
                "P1",
                Reachability.GAP,
                ("missing",),
                datetime(2026, 1, 1),
            ),
            ValueError,
        ),
        (
            lambda: EpochCheckpoint("", 0, "cursor", 0, 0, NOW),
            ValueError,
        ),
        (
            lambda: EpochCheckpoint("run", -1, "cursor", 0, 0, NOW),
            ValueError,
        ),
        (
            lambda: EpochCheckpoint(
                "run", 0, "cursor", 0, 0, datetime(2026, 1, 1)
            ),
            ValueError,
        ),
        (
            lambda: TelemetrySample("", 0, 0, 0, 0, 0, 0),
            ValueError,
        ),
        (
            lambda: TelemetrySample("run", 0, -1, 0, 0, 0, 0),
            ValueError,
        ),
    ],
)
def test_validation_models_reject_invalid_boundaries(
    factory: Callable[[], object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        factory()


def test_official_url_and_normalized_signature_success_paths() -> None:
    assert is_official_source_url("https://dev.mysql.com/doc/refman/8.0/en/select.html")
    assert is_official_source_url(
        "https://raw.githubusercontent.com/mysql/mysql-server/mysql-8.0.41/sql/sql_yacc.yy"
    )
    assert not is_official_source_url(
        "https://raw.githubusercontent.com/mysql/mysql-server/main/sql/sql_yacc.yy"
    )
    source = SourceCandidate(
        "https://dev.mysql.com/doc/refman/8.0/en/select.html",
        DIGEST,
        NOW,
        "text/html",
    )
    assert source.official
    signature = FeatureSignature("8.0.41", (" Select ", "select"), ("Guard",))
    assert signature.nodes == ("select",)
    assert len(signature.key) == 64
