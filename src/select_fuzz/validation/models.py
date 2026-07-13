"""Immutable domain objects shared by the validation subsystems."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
import json
import re
from urllib.parse import urlsplit


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[a-z][a-z0-9_./:-]*$")
_MYSQL_8041_RAW_PATHS = frozenset(
    {
        "/mysql/mysql-server/mysql-8.0.41/sql/sql_yacc.yy",
        "/mysql/mysql-server/mysql-8.0.41/sql/parse_tree_nodes.h",
    }
)


def is_official_source_url(url: str) -> bool:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        return False
    common = (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
        and not parsed.fragment
    )
    return common and (
        parsed.hostname == "dev.mysql.com"
        or (
            parsed.hostname == "raw.githubusercontent.com"
            and parsed.path in _MYSQL_8041_RAW_PATHS
            and not parsed.query
        )
    )


def _require_nonnegative(**values: int | float) -> None:
    if any(value < 0 for value in values.values()):
        raise ValueError("counters and times must be nonnegative")


def _normalize_tokens(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    normalized = tuple(sorted({value.strip().lower() for value in values}))
    if not normalized or any(not _TOKEN.fullmatch(value) for value in normalized):
        raise ValueError(f"{label} must contain normalized identifiers")
    return normalized


class Reachability(StrEnum):
    """Fail-closed outcomes for a generator reachability audit."""

    SUPPORTED = "supported"
    GAP = "gap"
    BLOCKED_EVIDENCE = "blocked_evidence"


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    """An immutable, offline-only snapshot of an official documentation source."""

    url: str
    content_sha256: str
    fetched_at: datetime
    media_type: str

    def __post_init__(self) -> None:
        if not is_official_source_url(self.url):
            raise ValueError("source must use an approved official MySQL 8.0.41 source URL")
        if not _DIGEST.fullmatch(self.content_sha256):
            raise ValueError("content_sha256 must be a lowercase 64-character sha256")
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise ValueError("fetched_at must be timezone-aware")
        if not self.media_type or any(char.isspace() for char in self.media_type):
            raise ValueError("media_type must be a non-empty MIME token")

    @property
    def official(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class FeatureSignature:
    version: str
    nodes: tuple[str, ...]
    requirements: tuple[str, ...]

    def __post_init__(self) -> None:
        if not re.fullmatch(r"8\.0\.\d+", self.version):
            raise ValueError("version must identify a MySQL 8.0 patch release")
        object.__setattr__(self, "nodes", _normalize_tokens(self.nodes, "nodes"))
        object.__setattr__(
            self,
            "requirements",
            _normalize_tokens(self.requirements, "requirements"),
        )

    @property
    def key(self) -> str:
        payload = {
            "nodes": self.nodes,
            "requirements": self.requirements,
            "version": self.version,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ReachabilityResult:
    signature_key: str
    status: Reachability
    reasons: tuple[str, ...] = ()
    witness_seed: int | None = None
    witness_feature_id: str | None = None

    def __post_init__(self) -> None:
        if not _DIGEST.fullmatch(self.signature_key):
            raise ValueError("signature_key must be a sha256 digest")
        if not isinstance(self.status, Reachability):
            raise TypeError("status must be a Reachability")
        if self.status is Reachability.SUPPORTED and self.reasons:
            raise ValueError("supported results cannot contain failure reasons")
        if self.status is not Reachability.SUPPORTED and not self.reasons:
            raise ValueError("non-supported results require reasons")
        if self.witness_seed is not None and self.witness_seed < 0:
            raise ValueError("witness_seed must be nonnegative")


@dataclass(frozen=True, slots=True)
class GapRecord:
    signature_key: str
    priority: str
    status: Reachability
    reasons: tuple[str, ...]
    discovered_at: datetime

    def __post_init__(self) -> None:
        if not _DIGEST.fullmatch(self.signature_key):
            raise ValueError("signature_key must be a sha256 digest")
        if not re.fullmatch(r"P[0-3]", self.priority):
            raise ValueError("priority must be P0, P1, P2, or P3")
        if self.status is Reachability.SUPPORTED:
            raise ValueError("a supported result is not a gap")
        if not self.reasons:
            raise ValueError("gap reasons must not be empty")
        if self.discovered_at.tzinfo is None:
            raise ValueError("discovered_at must be timezone-aware")

    @classmethod
    def from_result(
        cls,
        result: ReachabilityResult,
        *,
        priority: str,
        discovered_at: datetime,
    ) -> GapRecord:
        return cls(
            signature_key=result.signature_key,
            priority=priority,
            status=result.status,
            reasons=result.reasons,
            discovered_at=discovered_at,
        )


@dataclass(frozen=True, slots=True)
class EpochCheckpoint:
    run_id: str
    epoch: int
    source_cursor: str
    unique_signatures: int
    gaps: int
    updated_at: datetime
    elapsed_s: float = 0.0

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty")
        _require_nonnegative(
            epoch=self.epoch,
            unique_signatures=self.unique_signatures,
            gaps=self.gaps,
            elapsed_s=self.elapsed_s,
        )
        if self.updated_at.tzinfo is None:
            raise ValueError("updated_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class TelemetrySample:
    run_id: str
    epoch: int
    monotonic_s: float
    rss_bytes: int
    threads: int
    open_fds: int
    mysql_connections: int

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty")
        _require_nonnegative(
            epoch=self.epoch,
            monotonic_s=self.monotonic_s,
            rss_bytes=self.rss_bytes,
            threads=self.threads,
            open_fds=self.open_fds,
            mysql_connections=self.mysql_connections,
        )
