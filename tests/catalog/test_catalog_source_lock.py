from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
from types import ModuleType, SimpleNamespace
from typing import Any

import mysql.connector
import pytest

from select_fuzz.generation.catalog_schema import (
    SourceLockError,
    canonicalize_source_content,
    inspect_catalog_source_locks,
    verify_catalog_source_lock,
)


CATALOG_PATH = (
    Path(__file__).resolve().parents[2] / "catalog" / "mysql-8.0.41-query-shapes.yaml"
)
SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "verify_catalog_sources.py"
CANONICAL_SOURCE_URL = (
    "https://raw.githubusercontent.com/mysql/mysql-server/"
    "mysql-8.0.41/sql/sql_yacc.yy"
)
MATCHING_SOURCE_BYTES = b"query_expression:\r\n  SELECT_SYM table_ref\n"


def _load_cli_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_catalog_sources", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"unable to load CLI module from {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _catalog_document(
    source_bytes: bytes,
    *,
    match_kind: str = "literal",
    pattern: str = "query_expression:",
) -> dict[str, Any]:
    evidence = {"source_id": "grammar_8041", "locator": "query_expression_rule"}
    return {
        "schema_version": 2,
        "target_product": "MySQL Community Server",
        "target_version": "8.0.41",
        "raw_web_sql_policy": "signatures_only_never_execute",
        "checked_at": "2026-07-12",
        "guard_definitions": [
            "bounded_cardinality",
            "read_only_select",
            "stable_ordering",
        ],
        "profile_definitions": ["regular_innodb"],
        "sources": [
            {
                "source_id": "grammar_8041",
                "kind": "exact_source",
                "version": "8.0.41",
                "url": CANONICAL_SOURCE_URL,
                "hash_scope": "response_bytes",
                "lock_state": "verified",
                "content_sha256": hashlib.sha256(source_bytes).hexdigest(),
                "checked_at": "2026-07-12",
                "locators": {
                    "query_expression_rule": {
                        "match_kind": match_kind,
                        "pattern": pattern,
                    }
                },
            }
        ],
        "features": [
            {
                "feature_id": "select_family",
                "category": "select",
                "min_version": "5.7.0",
                "ast_nodes": ["query_expression", "query_specification"],
                "guards": [
                    "bounded_cardinality",
                    "read_only_select",
                    "stable_ordering",
                ],
                "profiles": ["regular_innodb"],
                "weight": 1,
                "evidence": [evidence],
                "variants": [
                    {
                        "variant_id": "select_query_specification",
                        "min_version": "5.7.0",
                        "ast_nodes": ["query_expression", "query_specification"],
                        "guards": [
                            "bounded_cardinality",
                            "read_only_select",
                            "stable_ordering",
                        ],
                        "profiles": ["regular_innodb"],
                        "weight": 1,
                        "evidence": [evidence],
                    }
                ],
            }
        ],
    }


def _docs_html(*, token: str, body: str, title: str | None = None) -> bytes:
    resolved_title = title or (
        "MySQL :: MySQL 8.0 Release Notes :: Changes in MySQL 8.0.41 (2025-01-21)"
    )
    return (
        "<!doctype html><html><head>"
        f"<title>{resolved_title}</title>"
        f"<script>window.BOOMR={{ak_rid: '{token}', ak_t: '{token}'}}</script>"
        "</head><body>"
        f'<div id="docs-body"><h2>Changes</h2><p>{body}</p></div>'
        "</body></html>"
    ).encode()


def _docs_catalog(source_bytes: bytes, *, pattern: str = "stable feature") -> dict[str, Any]:
    scoped = canonicalize_source_content(
        source_kind="release_note",
        hash_scope="docs_body_text_v1",
        content=source_bytes,
    )
    return {
        "sources": [
            {
                "source_id": "release_8041",
                "kind": "release_note",
                "version": "8.0.41",
                "url": "https://dev.mysql.com/doc/relnotes/mysql/8.0/en/news-8-0-41.html",
                "hash_scope": "docs_body_text_v1",
                "lock_state": "verified",
                "content_sha256": hashlib.sha256(scoped).hexdigest(),
                "checked_at": "2026-07-12",
                "locators": {
                    "stable_feature": {"match_kind": "literal", "pattern": pattern}
                },
            }
        ]
    }


def test_source_lock_hashes_exact_response_bytes_from_canonical_url() -> None:
    catalog = _catalog_document(MATCHING_SOURCE_BYTES)
    fetched_urls: list[str] = []

    def fetch_bytes(url: str) -> bytes:
        fetched_urls.append(url)
        return MATCHING_SOURCE_BYTES

    report = verify_catalog_source_lock(catalog, fetch_bytes=fetch_bytes)

    assert fetched_urls == [CANONICAL_SOURCE_URL]
    assert report.sources_checked == 1
    assert report.locators_checked == 1


def test_source_lock_rejects_response_byte_hash_drift() -> None:
    catalog = _catalog_document(MATCHING_SOURCE_BYTES)

    with pytest.raises(SourceLockError, match=r"grammar_8041.*SHA-256|SHA-256.*grammar_8041"):
        verify_catalog_source_lock(
            catalog,
            fetch_bytes=lambda _url: MATCHING_SOURCE_BYTES + b"\n",
        )


def test_source_lock_rejects_refresh_required_before_network_fetch() -> None:
    catalog = _catalog_document(MATCHING_SOURCE_BYTES)
    source = catalog["sources"][0]
    source["lock_state"] = "refresh_required"
    source["content_sha256"] = None

    def forbidden_fetch(_url: str) -> bytes:
        pytest.fail("pending source locks must fail before network fetch")

    with pytest.raises(SourceLockError, match="requires refresh"):
        verify_catalog_source_lock(catalog, fetch_bytes=forbidden_fetch)


def test_refresh_inspection_calculates_pending_stable_scope_without_weakening_verify() -> None:
    source_bytes = _docs_html(token="dynamic", body="stable feature documentation")
    catalog = _docs_catalog(source_bytes)
    source = catalog["sources"][0]
    source["lock_state"] = "refresh_required"
    source["content_sha256"] = None

    candidates = inspect_catalog_source_locks(
        catalog,
        fetch_bytes=lambda _url: source_bytes,
    )

    assert len(candidates) == 1
    assert candidates[0].source_id == "release_8041"
    assert candidates[0].content_sha256 == hashlib.sha256(
        canonicalize_source_content(
            source_kind="release_note",
            hash_scope="docs_body_text_v1",
            content=source_bytes,
        )
    ).hexdigest()
    with pytest.raises(SourceLockError, match="requires refresh"):
        verify_catalog_source_lock(catalog, fetch_bytes=lambda _url: source_bytes)


@pytest.mark.parametrize(
    ("match_kind", "pattern", "source_bytes"),
    [
        ("literal", "query_expression:", b"query_expression:\n"),
        ("regex", r"(?m)^query_expression\s*:", b"query_expression   :\n"),
    ],
)
def test_source_lock_accepts_literal_and_regex_locator_hits(
    match_kind: str,
    pattern: str,
    source_bytes: bytes,
) -> None:
    catalog = _catalog_document(
        source_bytes,
        match_kind=match_kind,
        pattern=pattern,
    )

    report = verify_catalog_source_lock(catalog, fetch_bytes=lambda _url: source_bytes)

    assert report.sources_checked == 1
    assert report.locators_checked == 1


@pytest.mark.parametrize(
    ("match_kind", "pattern"),
    [
        ("literal", "query_expression:"),
        ("regex", r"(?m)^query_expression\s*:"),
    ],
)
def test_source_lock_rejects_missing_literal_and_regex_locators(
    match_kind: str,
    pattern: str,
) -> None:
    source_bytes = b"unrelated_rule:\n"
    catalog = _catalog_document(
        source_bytes,
        match_kind=match_kind,
        pattern=pattern,
    )

    with pytest.raises(
        SourceLockError,
        match=r"grammar_8041.*query_expression_rule|query_expression_rule.*grammar_8041",
    ):
        verify_catalog_source_lock(catalog, fetch_bytes=lambda _url: source_bytes)


def test_source_lock_treats_downloaded_sql_as_inert_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_bytes = (
        b"query_expression:\n"
        b"SELECT secret FROM credentials; DROP TABLE production_data;\n"
    )
    catalog = _catalog_document(source_bytes)

    def forbidden_execution(*_args: object, **_kwargs: object) -> None:
        pytest.fail("source-lock verification must never execute downloaded content")

    monkeypatch.setattr(mysql.connector, "connect", forbidden_execution)
    monkeypatch.setattr(os, "system", forbidden_execution)
    monkeypatch.setattr(subprocess, "run", forbidden_execution)
    monkeypatch.setattr(subprocess, "Popen", forbidden_execution)

    report = verify_catalog_source_lock(catalog, fetch_bytes=lambda _url: source_bytes)

    assert report.sources_checked == 1
    assert report.locators_checked == 1


def test_docs_scope_ignores_dynamic_shell_and_hashes_visible_docs_body() -> None:
    first = _docs_html(token="first-request-token", body="stable feature documentation")
    second = _docs_html(token="different-token", body="stable feature documentation")
    catalog = _docs_catalog(first)

    assert canonicalize_source_content(
        source_kind="release_note",
        hash_scope="docs_body_text_v1",
        content=first,
    ) == canonicalize_source_content(
        source_kind="release_note",
        hash_scope="docs_body_text_v1",
        content=second,
    )
    report = verify_catalog_source_lock(catalog, fetch_bytes=lambda _url: second)
    assert report.sources_checked == 1
    assert report.locators_checked == 1


def test_docs_locator_cannot_match_outside_docs_body() -> None:
    source_bytes = _docs_html(token="outside-only-locator", body="unrelated body")
    catalog = _docs_catalog(source_bytes, pattern="outside-only-locator")

    with pytest.raises(SourceLockError, match="stable_feature"):
        verify_catalog_source_lock(catalog, fetch_bytes=lambda _url: source_bytes)


@pytest.mark.parametrize(
    "source_bytes",
    [
        b"<html><head><title>MySQL :: MySQL 8.0 Release Notes</title></head></html>",
        (
            b"<html><head><title>MySQL :: MySQL 8.0 Release Notes</title></head>"
            b'<body><div id="docs-body">one</div><div id="docs-body">two</div></body></html>'
        ),
        _docs_html(
            token="challenge",
            body="stable feature documentation",
            title="Access Denied - Robot Verification",
        ),
    ],
)
def test_docs_scope_rejects_missing_duplicate_body_or_error_title(
    source_bytes: bytes,
) -> None:
    with pytest.raises(SourceLockError):
        canonicalize_source_content(
            source_kind="release_note",
            hash_scope="docs_body_text_v1",
            content=source_bytes,
        )


def test_cli_exit_code_reflects_source_lock_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _load_cli_module()

    path = tmp_path / "catalog.yaml"
    path.write_text("schema_version: 2\n", encoding="utf-8")
    seen_paths: list[Path] = []

    def passing_verify(catalog_path: str | Path) -> SimpleNamespace:
        seen_paths.append(Path(catalog_path))
        return SimpleNamespace(sources_checked=2, locators_checked=3)

    monkeypatch.setattr(cli, "verify_catalog_source_lock", passing_verify)
    assert cli.main([str(path)]) == 0
    assert seen_paths == [path]
    assert "2 source(s)" in capsys.readouterr().out

    def failing_verify(_catalog_path: str | Path) -> None:
        raise SourceLockError("locked source drifted")

    monkeypatch.setattr(cli, "verify_catalog_source_lock", failing_verify)
    assert cli.main([str(path)]) == 1
    assert "locked source drifted" in capsys.readouterr().err


def test_cli_refresh_prints_reviewable_lock_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _load_cli_module()
    path = tmp_path / "catalog.yaml"
    path.write_text("schema_version: 2\n", encoding="utf-8")

    monkeypatch.setattr(
        cli,
        "inspect_catalog_source_locks",
        lambda _path: [
            SimpleNamespace(
                source_id="release_8041",
                content_sha256="a" * 64,
                locators_checked=9,
            )
        ],
    )

    assert cli.main(["--refresh", str(path)]) == 0
    output = capsys.readouterr().out
    assert "release_8041" in output
    assert "a" * 64 in output
    assert "9 locator(s)" in output


@pytest.mark.online
@pytest.mark.skipif(
    os.environ.get("SELECT_FUZZ_RUN_ONLINE") != "1",
    reason="set SELECT_FUZZ_RUN_ONLINE=1 to verify official catalog source locks",
)
def test_checked_in_catalog_sources_are_locked_online() -> None:
    report = verify_catalog_source_lock(CATALOG_PATH)

    assert report.sources_checked > 0
    assert report.locators_checked > 0
