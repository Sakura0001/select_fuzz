"""Production mode wiring must accept MySQL sessions with no PQ support."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from select_fuzz.config import AppConfig, NodeConfig, NodeRole
from select_fuzz.correctness import build_correctness_runner
from select_fuzz.domain import ColumnMeta, ExecutionStatus, RunRequest
from select_fuzz.execution import PreparedRound, PrepareStatus, QueryLimits
from select_fuzz.performance.entrypoint import build_performance_runner
from select_fuzz.performance.models import FrozenCase, ScaleKnobs
from select_fuzz.performance.tree import Family, ShapeBoundary
from select_fuzz.replay import build_replay_service


SQL = "SELECT SUM(v) FROM t"
SERIAL_TREE = "-> Table scan on t (cost=1 rows=3) (actual time=0.01..0.02 rows=3 loops=1)"


class _Cursor:
    affected_rows = None

    def __init__(self, rows: tuple[tuple[object, ...], ...]) -> None:
        self._rows = rows
        self.columns = (ColumnMeta("value", 253, True, False, False),)

    def fetchmany(self, size: int) -> tuple[tuple[object, ...], ...]:
        batch, self._rows = self._rows[:size], self._rows[size:]
        return batch

    def warnings(self) -> tuple[str, ...]:
        return ()

    def close(self) -> None:
        pass


class _SerialSession:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def connection_id(self) -> int:
        return 7

    def is_alive(self) -> bool:
        return True

    def execute(self, sql: str) -> _Cursor:
        self.sql.append(sql)
        if sql.startswith("EXPLAIN "):
            return _Cursor(((SERIAL_TREE,),))
        return _Cursor(((6,),))

    def abort(self) -> None:
        pass


class _SerialFactory:
    def __init__(self) -> None:
        self.sessions = {role: _SerialSession() for role in NodeRole}

    @contextmanager
    def query_session(self, node: NodeConfig, database: str):
        del database
        yield self.sessions[node.role]

    control_session = query_session


def _config(mode: str) -> AppConfig:
    return AppConfig.model_validate({
        "mode": mode,
        "nodes": [
            {"role": role.value, "host": "127.0.0.1", "port": 3306 + index}
            for index, role in enumerate(NodeRole)
        ],
    })


@pytest.mark.parametrize("mode", ["correctness", "replay"])
def test_production_comparison_builders_execute_ordinary_mysql_queries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    factory = _SerialFactory()
    module = f"select_fuzz.{mode}"
    monkeypatch.setattr(f"{module}.MySQLConnectorFactory", lambda **kwargs: factory)
    if mode == "correctness":
        service = build_correctness_runner(_config("correctness"), tmp_path)
        coordinator = service._rounds._coordinator._triad
    else:
        service = build_replay_service(_config("correctness"), tmp_path)
        coordinator = service._coordinator._triad
    # Setup is already complete. Exercise the real production coordinator and
    # NodeQueryRunner against connector sessions that only return serial plans.
    prepared = PreparedRound(
        status=PrepareStatus.READY,
        database="sf_standard_mysql",
        bundle=SimpleNamespace(requires_same_session=False),
        nodes=(),
        generation=0,
    )

    batch = coordinator.execute(prepared, SQL, QueryLimits(2, 100, 10000))

    assert all(result.status is ExecutionStatus.SUCCESS for result in batch)
    assert all(result.rows == ((6,),) for result in batch)
    assert all(session.sql == [SQL] for session in factory.sessions.values())


def test_production_performance_accepts_serial_analyze_measurements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _SerialFactory()
    monkeypatch.setattr(
        "select_fuzz.performance.entrypoint.MySQLConnectorFactory", lambda **kwargs: factory,
    )
    monkeypatch.setattr(
        "select_fuzz.performance.entrypoint.MySQLDiagnosticsCollector", lambda factory: None,
    )

    class Preparation:
        def prepare(self, template, initial, *, database):
            return FrozenCase(
                case_id=template.case_id,
                template_id=template.template_id,
                seed=template.seed,
                database=database,
                scale=ScaleKnobs(),
                data_manifest={},
                sql=SQL,
                boundary=ShapeBoundary(frozenset({Family.SCAN})),
                medians_seconds={},
                attempts=(),
            )

    monkeypatch.setattr(
        "select_fuzz.performance.entrypoint.SharedRoundCasePreparer",
        lambda materializer: Preparation(),
    )

    summary = build_performance_runner(_config("performance"), tmp_path).run(
        RunRequest("run_serial_mysql", "performance", 7, 1, 1, 1), Event(),
    )

    assert summary.queries_completed == 1
    assert summary.rejected == summary.findings == 0
    for session in factory.sessions.values():
        explain = [sql for sql in session.sql if sql.startswith("EXPLAIN ")]
        assert explain == [f"EXPLAIN ANALYZE FORMAT=TREE {SQL}"]
        assert not any(sql.startswith("SET ") for sql in session.sql)
