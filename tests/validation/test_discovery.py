from __future__ import annotations

from pathlib import Path
from threading import Event
import subprocess
import sys

from select_fuzz.validation.discovery import (
    PersistentSourceDiscovery,
    extract_official_links,
)
from select_fuzz.validation.ledger import ValidationLedger


def test_catalog_and_official_directories_seed_persistent_frontier(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        """
sources:
  - url: https://dev.mysql.com/doc/refman/8.0/en/select.html
  - url: https://raw.githubusercontent.com/mysql/mysql-server/mysql-8.0.41/sql/sql_yacc.yy
"""
    )
    ledger = ValidationLedger(tmp_path / "state.db", tmp_path / "events.jsonl")
    discovery = PersistentSourceDiscovery(ledger, catalog_path=catalog)

    assert discovery.seed() >= 4
    first = discovery.next(deadline_monotonic=100, monotonic=lambda: 0, stop_event=Event())
    assert first is not None
    discovery.complete(first.url)

    resumed = PersistentSourceDiscovery(
        ValidationLedger(tmp_path / "state.db", tmp_path / "events.jsonl"),
        catalog_path=catalog,
    )
    second = resumed.next(deadline_monotonic=100, monotonic=lambda: 0, stop_event=Event())
    assert second is not None
    assert second.url != first.url


def test_html_link_expansion_keeps_only_exact_official_allowlist() -> None:
    links = extract_official_links(
        b"""
        <a href="window-functions.html">window</a>
        <a href="https://dev.mysql.com/doc/refman/8.0/en/with.html">cte</a>
        <a href="window-function-frames.html#frame-clause">fragment</a>
        <a href="https://example.com/evil">evil</a>
        <a href="https://raw.githubusercontent.com/mysql/mysql-server/trunk/sql/x">trunk</a>
        """,
        base_url="https://dev.mysql.com/doc/refman/8.0/en/sql-statements.html",
    )
    assert links == (
        "https://dev.mysql.com/doc/refman/8.0/en/window-function-frames.html",
        "https://dev.mysql.com/doc/refman/8.0/en/window-functions.html",
        "https://dev.mysql.com/doc/refman/8.0/en/with.html",
    )


def test_deadline_or_stop_prevents_new_source_claim(tmp_path: Path) -> None:
    ledger = ValidationLedger(tmp_path / "state.db", tmp_path / "events.jsonl")
    discovery = PersistentSourceDiscovery(ledger)
    discovery.seed()
    stopped = Event()
    stopped.set()
    assert discovery.next(deadline_monotonic=10, monotonic=lambda: 0, stop_event=stopped) is None
    assert discovery.next(deadline_monotonic=10, monotonic=lambda: 10, stop_event=Event()) is None


def test_default_source_checkout_seeds_exact_8041_catalog_sources(tmp_path: Path) -> None:
    ledger = ValidationLedger(tmp_path / "state.db", tmp_path / "events.jsonl")
    discovery = PersistentSourceDiscovery(ledger)
    discovery.seed()
    assert any(
        url.startswith(
            "https://raw.githubusercontent.com/mysql/mysql-server/mysql-8.0.41/"
        )
        for url in ledger.queued_source_urls()
    )


def test_claimed_source_is_retried_after_process_restart(tmp_path: Path) -> None:
    ledger = ValidationLedger(tmp_path / "state.db", tmp_path / "events.jsonl")
    url = "https://dev.mysql.com/doc/refman/8.0/en/select.html"
    ledger.enqueue_source(url, discovered_from="test")
    first = PersistentSourceDiscovery(ledger).next(
        deadline_monotonic=10, monotonic=lambda: 0, stop_event=Event()
    )
    assert first is not None
    resumed = PersistentSourceDiscovery(
        ValidationLedger(tmp_path / "state.db", tmp_path / "events.jsonl")
    ).next(deadline_monotonic=10, monotonic=lambda: 0, stop_event=Event())
    assert resumed is not None
    assert resumed.url == url
    assert resumed.attempt == 2


def test_hard_process_exit_after_claim_is_recoverable(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    events = tmp_path / "events.jsonl"
    url = "https://dev.mysql.com/doc/refman/8.0/en/select.html"
    code = (
        "from pathlib import Path;"
        "from select_fuzz.validation.ledger import ValidationLedger;"
        f"x=ValidationLedger(Path({str(db)!r}),Path({str(events)!r}));"
        f"x.enqueue_source({url!r},discovered_from='crash-e2e');"
        "assert x.claim_source() is not None;"
        "raise SystemExit(17)"
    )
    crashed = subprocess.run([sys.executable, "-c", code], check=False)
    assert crashed.returncode == 17
    resumed = PersistentSourceDiscovery(ValidationLedger(db, events)).next(
        deadline_monotonic=10, monotonic=lambda: 0, stop_event=Event()
    )
    assert resumed is not None and resumed.url == url and resumed.attempt == 2
