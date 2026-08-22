"""Bounded error evidence and aggregation for fuzz diagnostics."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import hashlib
import json
import re
from threading import Lock
import time
from typing import Any

from select_fuzz.execution.evidence import (
    _bounded_text,
    capture_exception_evidence,
    render_traceback_text,
)


_DEFAULT_MAX_FINGERPRINTS = 64
_REPRESENTATIVE_INTERVAL_NS = 30_000_000_000
_MEMBER_LIMIT = 64

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_CONNECTION_RE = re.compile(r"(?i)\b(connection(?:_id)?[ =:#-]*)\d+\b")
_DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(milliseconds?|ms|seconds?|secs?|s)\b", re.I)
_LARGE_INTEGER_RE = re.compile(r"\b\d{3,}\b")


def watchdog_diagnostic_snapshot(handle: Any | None) -> dict[str, object]:
    """Read a watchdog snapshot while supporting lightweight test doubles."""

    if handle is None:
        return {
            "timed_out": False,
            "fired": False,
            "completed": True,
        }
    snapshot = getattr(handle, "diagnostic_snapshot", None)
    if callable(snapshot):
        value = snapshot()
        if isinstance(value, Mapping):
            return dict(value)
    return {
        "timed_out": bool(getattr(handle, "timed_out", False)),
        "fired": bool(getattr(handle, "fired", False)),
        "completed": not bool(getattr(handle, "thread_alive", False)),
    }


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _normalized_message(message: object) -> str:
    value = _bounded_text(message).lower()
    value = _IPV4_RE.sub("<ip>", value)
    value = _CONNECTION_RE.sub(r"\1<n>", value)
    value = _DURATION_RE.sub(r"<duration> \1", value)
    value = _LARGE_INTEGER_RE.sub("<n>", value)
    return " ".join(value.split())


def _exception_identity(value: object) -> dict[str, object]:
    item = _mapping(value)
    return {
        "module": item.get("module"),
        "type": item.get("type"),
        "errno": item.get("errno"),
        "sqlstate": item.get("sqlstate"),
        "message": _normalized_message(item.get("message", "")),
    }


def error_fingerprint(evidence: Mapping[str, object]) -> str:
    """Build a stable root-cause fingerprint without workload identifiers."""

    cursor_close = _mapping(evidence.get("cursor_close_error"))
    watchdog = _mapping(evidence.get("watchdog"))
    payload = {
        "failure_stage": evidence.get("failure_stage"),
        "exception": _exception_identity(evidence.get("exception")),
        "cursor_close_exception": _exception_identity(cursor_close.get("exception")),
        "watchdog": {
            "timed_out": watchdog.get("timed_out"),
            "kill_query_succeeded": watchdog.get("kill_query_succeeded"),
            "kill_query_error_type": watchdog.get("kill_query_error_type"),
            "abort_succeeded": watchdog.get("abort_succeeded"),
            "abort_error_type": watchdog.get("abort_error_type"),
        },
    }
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class FuzzErrorRecordDecision:
    fingerprint: str
    is_new: bool
    write_operation_event: bool
    suppressed_repeats: int = 0


@dataclass(slots=True)
class _ErrorState:
    fingerprint: str
    first_seen_ns: int
    last_seen_ns: int
    first_evidence: dict[str, object]
    first_sql: str | None
    total_count: int = 0
    interval_count: int = 0
    timed_out_count: int = 0
    connection_lost_count: int = 0
    mysql_not_visible_count: int = 0
    workers: set[str] = field(default_factory=set)
    databases: set[str] = field(default_factory=set)
    endpoints: set[str] = field(default_factory=set)
    workers_truncated: int = 0
    databases_truncated: int = 0
    endpoints_truncated: int = 0
    recent_samples: deque[dict[str, str]] = field(
        default_factory=lambda: deque(maxlen=3)
    )
    last_operation_event_ns: int = 0
    suppressed_since_event: int = 0


def _add_bounded_member(values: set[str], value: str, state: _ErrorState, name: str) -> None:
    if value in values:
        return
    if len(values) < _MEMBER_LIMIT:
        values.add(value)
        return
    attribute = f"{name}_truncated"
    setattr(state, attribute, getattr(state, attribute) + 1)


class FuzzErrorAggregator:
    """Thread-safe bounded root-cause aggregation for high-rate failures."""

    def __init__(
        self,
        *,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        max_fingerprints: int = _DEFAULT_MAX_FINGERPRINTS,
    ) -> None:
        if max_fingerprints <= 0:
            raise ValueError("max_fingerprints must be positive")
        self._clock_ns = clock_ns
        self._max_fingerprints = max_fingerprints
        self._states: dict[str, _ErrorState] = {}
        self._total_count = 0
        self._interval_count = 0
        self._other_count = 0
        self._other_interval_count = 0
        self._lock = Lock()

    def record(
        self,
        *,
        evidence: Mapping[str, object],
        worker: str,
        database: str,
        endpoint: str,
        sql: str | None,
        timed_out: bool,
        connection_lost: bool,
        mysql_visible: bool | None = None,
    ) -> FuzzErrorRecordDecision:
        fingerprint = error_fingerprint(evidence)
        now_ns = self._clock_ns()
        with self._lock:
            self._total_count += 1
            self._interval_count += 1
            state = self._states.get(fingerprint)
            if state is None and len(self._states) >= self._max_fingerprints:
                self._other_count += 1
                self._other_interval_count += 1
                return FuzzErrorRecordDecision(
                    fingerprint=fingerprint,
                    is_new=False,
                    write_operation_event=False,
                )
            is_new = state is None
            if state is None:
                state = _ErrorState(
                    fingerprint=fingerprint,
                    first_seen_ns=now_ns,
                    last_seen_ns=now_ns,
                    first_evidence=dict(evidence),
                    first_sql=sql,
                    last_operation_event_ns=now_ns,
                )
                self._states[fingerprint] = state
            state.last_seen_ns = now_ns
            state.total_count += 1
            state.interval_count += 1
            state.timed_out_count += int(timed_out)
            state.connection_lost_count += int(connection_lost)
            state.mysql_not_visible_count += int(mysql_visible is False)
            _add_bounded_member(state.workers, worker, state, "workers")
            _add_bounded_member(state.databases, database, state, "databases")
            _add_bounded_member(state.endpoints, endpoint, state, "endpoints")
            state.recent_samples.append(
                {
                    "worker": _bounded_text(worker, 300),
                    "database": _bounded_text(database, 300),
                    "endpoint": _bounded_text(endpoint, 300),
                    "sql": _bounded_text(sql or "", 4096),
                }
            )
            if is_new:
                return FuzzErrorRecordDecision(fingerprint, True, True, 0)
            if now_ns - state.last_operation_event_ns >= _REPRESENTATIVE_INTERVAL_NS:
                suppressed = state.suppressed_since_event
                state.suppressed_since_event = 0
                state.last_operation_event_ns = now_ns
                return FuzzErrorRecordDecision(
                    fingerprint,
                    False,
                    True,
                    suppressed,
                )
            state.suppressed_since_event += 1
            return FuzzErrorRecordDecision(fingerprint, False, False, 0)

    def snapshot(self, *, interval_seconds: float) -> dict[str, object]:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        with self._lock:
            interval_count = self._interval_count
            states = tuple(
                self._state_snapshot(state, interval_seconds)
                for state in self._states.values()
            )
            ordered = tuple(
                sorted(
                    states,
                    key=lambda item: (
                        -self._integer(item.get("interval_count")),
                        -self._integer(item.get("total_count")),
                        str(item["fingerprint"]),
                    ),
                )
            )
            result = {
                "total_count": self._total_count,
                "interval_count": interval_count,
                "rate_per_second": interval_count / interval_seconds,
                "fingerprint_count": len(self._states),
                "other_count": self._other_count,
                "other_interval_count": self._other_interval_count,
                "top": ordered[:8],
                "fingerprints": ordered,
            }
            self._interval_count = 0
            self._other_interval_count = 0
            for state in self._states.values():
                state.interval_count = 0
            return result

    @staticmethod
    def _integer(value: object) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    @staticmethod
    def _state_snapshot(
        state: _ErrorState,
        interval_seconds: float,
    ) -> dict[str, object]:
        return {
            "fingerprint": state.fingerprint,
            "first_seen_ns": state.first_seen_ns,
            "last_seen_ns": state.last_seen_ns,
            "total_count": state.total_count,
            "interval_count": state.interval_count,
            "rate_per_second": state.interval_count / interval_seconds,
            "worker_count": len(state.workers),
            "database_count": len(state.databases),
            "endpoints": tuple(sorted(state.endpoints)),
            "workers_truncated": state.workers_truncated,
            "databases_truncated": state.databases_truncated,
            "endpoints_truncated": state.endpoints_truncated,
            "timed_out_count": state.timed_out_count,
            "connection_lost_count": state.connection_lost_count,
            "mysql_not_visible_count": state.mysql_not_visible_count,
            "first_evidence": dict(state.first_evidence),
            "sample_sql": state.first_sql,
            "recent_samples": tuple(dict(sample) for sample in state.recent_samples),
        }


__all__ = [
    "FuzzErrorAggregator",
    "FuzzErrorRecordDecision",
    "capture_exception_evidence",
    "error_fingerprint",
    "render_traceback_text",
    "watchdog_diagnostic_snapshot",
]
