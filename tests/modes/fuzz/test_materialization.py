from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator

import pytest

from select_fuzz.config import FuzzConfig, NodeConfig, NodeRole
from select_fuzz.modes.fuzz.materialization import FuzzMaterializer


class _ProbeCursor:
    affected_rows = 0

    def __init__(self, rows: tuple[tuple[int, ...], ...]) -> None:
        self._rows = rows

    def fetchmany(self, size: int) -> tuple[tuple[int, ...], ...]:
        del size
        return self._rows

    def close(self) -> None:
        return None


class _ProbeSession:
    def __init__(self, rows: tuple[tuple[int, ...], ...]) -> None:
        self._rows = rows

    def execute(self, sql: str) -> _ProbeCursor:
        del sql
        return _ProbeCursor(self._rows)


class _ProbeFactory:
    def __init__(
        self,
        *,
        rows: tuple[tuple[int, ...], ...] = (),
        error: Exception | None = None,
    ) -> None:
        self._rows = rows
        self._error = error

    def query_session(self, node: NodeConfig, database: str) -> None:
        del node, database
        raise AssertionError("query session is not used by replica probes")

    @contextmanager
    def control_session(
        self,
        node: NodeConfig,
        database: str,
    ) -> Iterator[_ProbeSession]:
        del node, database
        if self._error is not None:
            raise self._error
        yield _ProbeSession(self._rows)


def _materializer(factory: _ProbeFactory) -> FuzzMaterializer:
    node = NodeConfig(role=NodeRole.CUSTOM_ON, host="127.0.0.1")
    return FuzzMaterializer(
        factory,  # type: ignore[arg-type]
        node,
        node,
        FuzzConfig(
            initial_tables=1,
            initial_rows_per_table=20,
            max_rows_per_database=100,
        ),
        replica_sync_timeout_seconds=0.001,
        sleeper=lambda seconds: None,
    )


def test_replica_timeout_reports_last_probe_exception() -> None:
    materializer = _materializer(
        _ProbeFactory(error=RuntimeError("replica route unavailable"))
    )

    with pytest.raises(TimeoutError) as captured:
        materializer._wait_for_replica("sf_f_timeout")

    assert str(captured.value) == (
        "replica synchronization timeout after 0.001 seconds; "
        "database=sf_f_timeout; last probe error=RuntimeError: "
        "replica route unavailable"
    )


def test_replica_timeout_reports_marker_not_visible() -> None:
    materializer = _materializer(_ProbeFactory(rows=()))

    with pytest.raises(TimeoutError) as captured:
        materializer._wait_for_replica("sf_f_timeout")

    assert str(captured.value) == (
        "replica synchronization timeout after 0.001 seconds; "
        "database=sf_f_timeout; replication marker not visible"
    )
