from __future__ import annotations

from hashlib import sha256

import pytest
import yaml

from select_fuzz.generation.catalog_schema import (
    ALLOWED_AST_NODES,
    CatalogError,
    DuplicateKeySafeLoader,
    SourceLockError,
    _as_mapping,
    _as_record_list,
    _identifier,
    _identifier_list,
    _require_exact_keys,
    _validate_locator_manifest,
    canonicalize_source_content,
    inspect_catalog_source_locks,
    parse_version,
    verify_catalog_source_lock,
)


def source_catalog(
    *,
    lock_state: object = "verified",
    content_sha256: object | None = None,
    url: object = "https://example.com/source",
    kind: object = "exact_source",
    hash_scope: object = "response_bytes",
    locators: object | None = None,
) -> dict[str, object]:
    content = b"SELECT marker"
    return {
        "sources": [
            {
                "source_id": "source_one",
                "url": url,
                "kind": kind,
                "hash_scope": hash_scope,
                "lock_state": lock_state,
                "content_sha256": sha256(content).hexdigest()
                if content_sha256 is None
                else content_sha256,
                "locators": {
                    "marker": {"match_kind": "literal", "pattern": "marker"}
                }
                if locators is None
                else locators,
            }
        ]
    }


def test_official_docs_canonicalization_ignores_chrome_and_normalizes_visible_text() -> None:
    html = b"""
    <html><head><title>MySQL :: MySQL 8.0 Reference Manual</title></head>
    <body><div id="docs-body">Visible <div>nested</div><script>ignored</script> text</div></body>
    </html>
    """
    assert canonicalize_source_content(
        source_kind="manual_snapshot", hash_scope="docs_body_text_v1", content=html
    ) == b"Visible nested text"
    assert canonicalize_source_content(
        source_kind="exact_source", hash_scope="response_bytes", content=b"raw"
    ) == b"raw"


@pytest.mark.parametrize(
    ("source_kind", "hash_scope", "content", "message"),
    [
        ("exact_source", "response_bytes", "text", "must be bytes"),
        ("manual_snapshot", "response_bytes", b"x", "only for exact_source"),
        ("exact_source", "docs_body_text_v1", b"x", "invalid hash scope"),
        ("manual_snapshot", "other", b"x", "invalid hash scope"),
        ("manual_snapshot", "docs_body_text_v1", b"\xff", "not UTF-8"),
        (
            "manual_snapshot",
            "docs_body_text_v1",
            b"<title>MySQL :: MySQL 8.0 Reference Manual</title>",
            "exactly one docs-body",
        ),
        (
            "manual_snapshot",
            "docs_body_text_v1",
            b'<title>Error</title><div id="docs-body">text</div>',
            "unexpected official documentation title",
        ),
        (
            "release_note",
            "docs_body_text_v1",
            b'<title>MySQL :: MySQL 8.0 Reference Manual</title><div id="docs-body">text</div>',
            "does not match release_note",
        ),
        (
            "manual_snapshot",
            "docs_body_text_v1",
            b'<title>MySQL :: MySQL 8.0 Reference Manual</title><div id="docs-body"></div>',
            "docs-body is empty",
        ),
    ],
)
def test_source_canonicalization_rejects_untrusted_or_ambiguous_content(
    source_kind: str, hash_scope: str, content: object, message: str
) -> None:
    with pytest.raises(SourceLockError, match=message):
        canonicalize_source_content(
            source_kind=source_kind,
            hash_scope=hash_scope,
            content=content,
        )


def test_duplicate_key_loader_and_primitive_catalog_validators() -> None:
    with pytest.raises(yaml.constructor.ConstructorError, match="duplicate key"):
        yaml.load("key: 1\nkey: 2\n", Loader=DuplicateKeySafeLoader)
    with pytest.raises(CatalogError, match="keys"):
        _require_exact_keys({"a": 1}, {"a", "b"}, "record")
    with pytest.raises(CatalogError, match="string-keyed"):
        _as_mapping({1: "bad"}, "record")
    with pytest.raises(CatalogError, match="non-empty list"):
        _as_record_list([], "records")
    with pytest.raises(CatalogError, match="non-empty list"):
        _as_record_list({}, "records")
    with pytest.raises(CatalogError, match="must be a string"):
        parse_version(8041, "version")
    with pytest.raises(CatalogError, match="major.minor.patch"):
        parse_version("8.0", "version")
    assert parse_version("8.0.41", "version") == (8, 0, 41)

    with pytest.raises(CatalogError, match="snake_case"):
        _identifier("Bad-Id", "id")
    with pytest.raises(CatalogError, match="unknown"):
        _identifier("unknown", "node", ALLOWED_AST_NODES)
    with pytest.raises(CatalogError, match="executable SQL"):
        _identifier("select", "id")
    with pytest.raises(CatalogError, match="non-empty list"):
        _identifier_list([], "nodes", ALLOWED_AST_NODES)
    allowed = next(iter(ALLOWED_AST_NODES))
    with pytest.raises(CatalogError, match="duplicates"):
        _identifier_list([allowed, allowed], "nodes", ALLOWED_AST_NODES)


@pytest.mark.parametrize(
    "locators",
    [
        {},
        {"loc": {"match_kind": "unknown", "pattern": "x"}},
        {"loc": {"match_kind": "literal", "pattern": ""}},
        {"loc": {"match_kind": "regex", "pattern": "["}},
        {"loc": {"match_kind": "regex", "pattern": ".*"}},
    ],
)
def test_locator_manifests_reject_empty_invalid_and_unbounded_patterns(
    locators: dict[str, object]
) -> None:
    with pytest.raises((CatalogError, yaml.YAMLError)):
        _validate_locator_manifest(locators, "locators")


def test_source_lock_inspection_checks_fetch_types_locator_types_regex_and_matches() -> None:
    content = b"SELECT marker"
    report = inspect_catalog_source_locks(source_catalog(), fetch_bytes=lambda _: content)
    assert report[0].locators_checked == 1

    invalid_cases = [
        (source_catalog(url=1), lambda _: content, "lacks a URL"),
        (source_catalog(kind=1), lambda _: content, "lacks source kind"),
        (source_catalog(), lambda _: "text", "fetcher must return bytes"),
        (
            source_catalog(locators={"marker": {"match_kind": "bad", "pattern": "x"}}),
            lambda _: content,
            "invalid locator manifest",
        ),
        (
            source_catalog(locators={"marker": {"match_kind": "regex", "pattern": "["}}),
            lambda _: content,
            "invalid regex",
        ),
        (
            source_catalog(locators={"marker": {"match_kind": "literal", "pattern": "absent"}}),
            lambda _: content,
            "did not match",
        ),
    ]
    for catalog, fetcher, message in invalid_cases:
        with pytest.raises(SourceLockError, match=message):
            inspect_catalog_source_locks(catalog, fetch_bytes=fetcher)


def test_source_lock_verification_rejects_pending_missing_and_mismatched_hashes() -> None:
    content = b"SELECT marker"
    pending = source_catalog(lock_state="refresh_required")
    with pytest.raises(SourceLockError, match="requires refresh"):
        verify_catalog_source_lock(pending, fetch_bytes=lambda _: content)

    missing = source_catalog(content_sha256=1)
    with pytest.raises(SourceLockError, match="lacks a SHA-256"):
        verify_catalog_source_lock(missing, fetch_bytes=lambda _: content)

    mismatch = source_catalog(content_sha256="0" * 64)
    with pytest.raises(SourceLockError, match="SHA-256 mismatch"):
        verify_catalog_source_lock(mismatch, fetch_bytes=lambda _: content)

    report = verify_catalog_source_lock(source_catalog(), fetch_bytes=lambda _: content)
    assert (report.sources_checked, report.locators_checked) == (1, 1)
