"""Allowlisted acquisition into an immutable, content-addressed offline cache."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from http.client import HTTPMessage
import os
from pathlib import Path
import subprocess
import tempfile
from typing import IO
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener
import time

from select_fuzz.validation.models import SourceCandidate, is_official_source_url


class SourcePolicyError(ValueError):
    """Acquisition was rejected before content can reach offline analysis."""


@dataclass(frozen=True, slots=True)
class FetchResponse:
    url: str
    status: int
    media_type: str
    body: bytes


@dataclass(frozen=True, slots=True)
class CachedOfficialSource:
    source: SourceCandidate
    path: Path


FetchTransport = Callable[[str, int], FetchResponse]


def _validate_official_url(url: str) -> None:
    if not is_official_source_url(url):
        raise SourcePolicyError("source URL is not on an allowlisted official MySQL source")


class _OfficialRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> Request | None:
        _validate_official_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class OfficialSourceAcquirer:
    """Fetch official docs only; returned bytes are never considered executable SQL."""

    def __init__(self, cache_dir: Path, *, max_bytes: int = 2_000_000, timeout_s: float = 15.0):
        if max_bytes <= 0 or timeout_s <= 0:
            raise ValueError("max_bytes and timeout_s must be positive")
        self.cache_dir = cache_dir
        self.max_bytes = max_bytes
        self.timeout_s = timeout_s

    def validate_url(self, url: str) -> None:
        _validate_official_url(url)

    def cache_bytes(self, content: bytes, *, media_type: str) -> CachedOfficialSource:
        if len(content) > self.max_bytes:
            raise SourcePolicyError("source exceeds max_bytes")
        digest = sha256(content).hexdigest()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / digest
        if path.exists():
            if path.read_bytes() != content:
                raise SourcePolicyError("immutable cache entry was modified")
        else:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            fd = os.open(path, flags, 0o400)
            try:
                with os.fdopen(fd, "wb", closefd=False) as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                os.close(fd)
            os.chmod(path, 0o400)
            directory_fd = os.open(self.cache_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        source = SourceCandidate(
            url="https://dev.mysql.com/offline-cache/" + digest,
            content_sha256=digest,
            fetched_at=datetime.now(UTC),
            media_type=media_type,
        )
        return CachedOfficialSource(source=source, path=path)

    def acquire(
        self,
        url: str,
        transport: FetchTransport | None = None,
        *,
        deadline_monotonic: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> CachedOfficialSource:
        self.validate_url(url)
        remaining = (
            self.timeout_s
            if deadline_monotonic is None
            else min(self.timeout_s, deadline_monotonic - monotonic())
        )
        if remaining <= 0:
            raise TimeoutError("source acquisition deadline reached")
        response = (
            transport(url, self.max_bytes)
            if transport is not None
            else self._fetch(url, self.max_bytes, timeout_s=remaining)
        )
        if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
            raise TimeoutError("source acquisition exceeded validation deadline")
        self.validate_url(response.url)
        if response.status != 200:
            raise SourcePolicyError(f"official source returned HTTP {response.status}")
        cached = self.cache_bytes(response.body, media_type=response.media_type)
        source = SourceCandidate(
            url=response.url,
            content_sha256=cached.source.content_sha256,
            fetched_at=cached.source.fetched_at,
            media_type=response.media_type,
        )
        return CachedOfficialSource(source=source, path=cached.path)

    def _fetch(self, url: str, max_bytes: int, *, timeout_s: float | None = None) -> FetchResponse:
        opener = build_opener(_OfficialRedirectHandler())
        request = Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
                ),
                "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1",
                "Accept-Language": "en-US,en;q=0.8",
            },
        )
        try:
            with opener.open(request, timeout=timeout_s or self.timeout_s) as response:
                final_url = response.geturl()
                self.validate_url(final_url)
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise SourcePolicyError("source exceeds max_bytes")
                media_type = response.headers.get_content_type()
                return FetchResponse(final_url, response.status, media_type, body)
        except HTTPError as exc:
            if exc.code == 403:
                return self._fetch_with_curl(
                    url, max_bytes, timeout_s=timeout_s or self.timeout_s
                )
            raise SourcePolicyError(f"official source returned HTTP {exc.code}") from exc

    def _fetch_with_curl(
        self, url: str, max_bytes: int, *, timeout_s: float
    ) -> FetchResponse:
        self.validate_url(url)
        with tempfile.NamedTemporaryFile() as body_file:
            completed = subprocess.run(
                (
                    "curl",
                    "--silent",
                    "--show-error",
                    "--proto",
                    "=https",
                    "--max-time",
                    str(timeout_s),
                    "--max-filesize",
                    str(max_bytes),
                    "--output",
                    body_file.name,
                    "--write-out",
                    "%{url_effective}\n%{http_code}\n%{content_type}",
                    url,
                ),
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_s + 1,
            )
            if completed.returncode != 0:
                raise SourcePolicyError("official source curl fallback failed")
            metadata = completed.stdout.splitlines()
            if len(metadata) < 3:
                raise SourcePolicyError("official source curl metadata is incomplete")
            final_url, status_raw, media_type = metadata[-3:]
            self.validate_url(final_url)
            if status_raw != "200":
                raise SourcePolicyError(f"official source returned HTTP {status_raw}")
            body_file.seek(0)
            body = body_file.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise SourcePolicyError("source exceeds max_bytes")
        return FetchResponse(final_url, 200, media_type.split(";", 1)[0], body)


__all__ = [
    "CachedOfficialSource",
    "FetchResponse",
    "FetchTransport",
    "OfficialSourceAcquirer",
    "SourcePolicyError",
]
