from __future__ import annotations

from pathlib import Path

import pytest

from select_fuzz.validation.source import (
    FetchResponse,
    OfficialSourceAcquirer,
    SourcePolicyError,
)


def test_only_exact_official_https_origin_is_accepted(tmp_path: Path) -> None:
    acquirer = OfficialSourceAcquirer(tmp_path)
    acquirer.validate_url("https://dev.mysql.com/doc/refman/8.0/en/select.html")
    acquirer.validate_url(
        "https://raw.githubusercontent.com/mysql/mysql-server/mysql-8.0.41/sql/sql_yacc.yy"
    )
    for url in (
        "http://dev.mysql.com/x",
        "https://dev.mysql.com.evil.example/x",
        "https://user@dev.mysql.com/x",
        "https://example.com/x",
        "https://raw.githubusercontent.com/mysql/mysql-server/trunk/sql/sql_yacc.yy",
        "https://raw.githubusercontent.com/other/mysql-server/mysql-8.0.41/sql/x",
        "https://raw.githubusercontent.com/mysql/mysql-server/mysql-8.0.41/README",
        "https://raw.githubusercontent.com/mysql/mysql-server/mysql-8.0.41/mysql-test/t/x.test",
    ):
        with pytest.raises(SourcePolicyError):
            acquirer.validate_url(url)


def test_acquisition_revalidates_redirect_and_caches_by_content_hash(tmp_path: Path) -> None:
    acquirer = OfficialSourceAcquirer(tmp_path, max_bytes=100)
    body = b"<code>SELECT 1</code>"

    def fetch(url: str, max_bytes: int) -> FetchResponse:
        assert max_bytes == 100
        return FetchResponse(url=url, status=200, media_type="text/html", body=body)

    first = acquirer.acquire("https://dev.mysql.com/doc/refman/8.0/en/select.html", fetch)
    second = acquirer.acquire("https://dev.mysql.com/doc/refman/8.0/en/select.html", fetch)

    assert first.path == second.path
    assert first.path.name == first.source.content_sha256
    assert first.path.read_bytes() == body

    def evil_redirect(url: str, max_bytes: int) -> FetchResponse:
        return FetchResponse(
            url="https://example.com/stolen",
            status=200,
            media_type="text/html",
            body=body,
        )

    with pytest.raises(SourcePolicyError, match="allowlisted"):
        acquirer.acquire("https://dev.mysql.com/x", evil_redirect)


def test_cache_is_immutable_and_oversized_content_is_rejected(tmp_path: Path) -> None:
    acquirer = OfficialSourceAcquirer(tmp_path, max_bytes=4)
    cached = acquirer.cache_bytes(b"safe", media_type="text/plain")
    cached.path.chmod(0o600)
    cached.path.write_bytes(b"evil")

    with pytest.raises(SourcePolicyError, match="immutable"):
        acquirer.cache_bytes(b"safe", media_type="text/plain")
    with pytest.raises(SourcePolicyError, match="max_bytes"):
        acquirer.cache_bytes(b"12345", media_type="text/plain")
