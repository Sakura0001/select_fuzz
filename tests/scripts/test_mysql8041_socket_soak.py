from __future__ import annotations

from collections.abc import Callable
import importlib.util
import json
from pathlib import Path
import sys
from threading import Event
from typing import Any

import pytest

from select_fuzz.artifacts import JsonlWriter, read_jsonl
from select_fuzz.config import NodeRole
from select_fuzz.correctness import JsonlEventSink
from select_fuzz.domain import RunEvent
from select_fuzz.generation.query_scope import QueryExclusionReason
from select_fuzz.service import RunSummary


_SPEC = importlib.util.spec_from_file_location(
    "mysql8041_socket_soak_script",
    Path("scripts/run_mysql8041_socket_soak.py"),
)
assert _SPEC is not None and _SPEC.loader is not None
soak_script = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = soak_script
_SPEC.loader.exec_module(soak_script)


class _RawVersionCursor:
    warning_count = 0

    def __init__(self, version: str) -> None:
        self.version = version
        self.description = (
            ("VERSION()", 253, None, None, None, None, False, 0, 45),
        )
        self.executed: list[str] = []
        self.closed = False
        self._sent = False

    def execute(self, sql: str) -> None:
        self.executed.append(sql)

    def fetchmany(self, size: int) -> tuple[tuple[object, ...], ...]:
        if self._sent:
            return ()
        self._sent = True
        return ((self.version,),)

    def close(self) -> None:
        self.closed = True


class _RawVersionConnection:
    connection_id = 41

    def __init__(self, version: str) -> None:
        self.version = version
        self.closed = False

    def cursor(self, **kwargs: object) -> _RawVersionCursor:
        return _RawVersionCursor(self.version)

    def close(self) -> None:
        self.closed = True


class _FakeConnect:
    def __init__(self, versions: dict[str, str]) -> None:
        self.versions = versions
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> _RawVersionConnection:
        self.calls.append(dict(kwargs))
        socket_path = kwargs["unix_socket"]
        assert isinstance(socket_path, str)
        return _RawVersionConnection(self.versions[socket_path])


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    return (
        tmp_path / "baseline.sock",
        tmp_path / "custom-off.sock",
        tmp_path / "custom-on.sock",
    )


def _config(tmp_path: Path, **overrides: object) -> Any:
    values: dict[str, object] = {
        "sockets": _paths(tmp_path),
        "duration_seconds": 1.0,
        "queries_per_round": 3,
        "workers": 1,
        "seed": 8041,
        "artifact_root": tmp_path / "artifacts",
        "run_id": "socket-soak-test",
        "max_rounds": 1,
    }
    values.update(overrides)
    return soak_script.SocketSoakConfig(**values)


def test_default_scope_excludes_json_fulltext_and_spatial_query_families() -> None:
    scope = soak_script.DEFAULT_QUERY_SCOPE

    assert set(scope.exclusion_reasons.values()) == {
        QueryExclusionReason.JSON,
        QueryExclusionReason.FULLTEXT,
        QueryExclusionReason.SPATIAL,
    }
    assert {
        "index_fulltext",
        "index_spatial",
        "json_create_extract",
        "json_table_columns",
    } <= scope.excluded_feature_ids
    assert set(scope.excluded_profile_reasons) == {
        "fulltext_innodb",
        "json_multivalue_innodb",
        "spatial_innodb",
    }


def test_jsonl_event_sink_thaws_nested_negative_error_payloads(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    sink = JsonlEventSink(JsonlWriter(path))
    event = RunEvent(
        run_id="socket-soak-test",
        sequence=1,
        kind="query_completed",
        payload={
            "expected_error": {
                "errno": 1054,
                "sqlstate": "42S22",
            },
            "observed_error_identities": (
                {"errno": 1054, "sqlstate": "42S22"},
            ),
        },
    )

    sink.publish(event)

    assert read_jsonl(path)[0]["payload"] == {
        "expected_error": {"errno": 1054, "sqlstate": "42S22"},
        "observed_error_identities": [
            {"errno": 1054, "sqlstate": "42S22"},
        ],
    }


@pytest.mark.parametrize(
    "values",
    (
        ("/tmp/a.sock", "/tmp/b.sock", "/tmp/c.sock"),
        ("/tmp/a.sock,/tmp/b.sock,/tmp/c.sock",),
    ),
)
def test_socket_cli_accepts_separate_or_comma_delimited_paths(
    values: tuple[str, ...],
) -> None:
    parsed = soak_script._socket_paths(values)

    assert tuple(path.name for path in parsed) == ("a.sock", "b.sock", "c.sock")


def test_mysql_connector_factory_injection_maps_ports_to_sockets_without_password(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    fake_connect = _FakeConnect({str(path): "8.0.41" for path in paths})
    factory = soak_script.build_connector(paths, connect=fake_connect)

    versions = soak_script.probe_mysql8041_versions(
        factory,
        soak_script.build_nodes(),
    )

    assert versions == {role: "8.0.41" for role in NodeRole}
    assert [call["unix_socket"] for call in fake_connect.calls] == [
        str(path) for path in paths
    ]
    assert all(call["user"] == "root" for call in fake_connect.calls)
    assert all("host" not in call and "port" not in call for call in fake_connect.calls)
    assert all("password" not in call for call in fake_connect.calls)


def test_version_probe_rejects_any_non_exact_mysql_version(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    versions = {str(path): "8.0.41" for path in paths}
    versions[str(paths[2])] = "8.0.42"
    factory = soak_script.build_connector(paths, connect=_FakeConnect(versions))

    with pytest.raises(RuntimeError, match="exact MySQL 8.0.41"):
        soak_script.probe_mysql8041_versions(factory, soak_script.build_nodes())


def test_runtime_assembles_real_production_service_after_fake_socket_preflight(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    fake_connect = _FakeConnect(
        {str(path): "8.0.41-community" for path in config.sockets}
    )

    runtime = soak_script.build_runtime(config, fake_connect)

    assert runtime.versions == {
        role: "8.0.41-community" for role in NodeRole
    }
    assert runtime.service.__class__.__name__ == "CorrectnessRunService"
    assert len(fake_connect.calls) == 3


class _FakeService:
    def __init__(self) -> None:
        self.request = None
        self.stop_seen = False

    def run(self, request: Any, stop_event: Event) -> RunSummary:
        self.request = request
        self.stop_seen = stop_event.is_set()
        return RunSummary(
            run_id=request.run_id,
            rounds_completed=1,
            queries_completed=3,
            findings=0,
            rejected=1,
            over_budget=0,
            stopped=stop_event.is_set(),
        )


class _ImmediateTimer:
    instances: list[_ImmediateTimer] = []

    def __init__(self, interval: float, callback: Callable[[], None]) -> None:
        self.interval = interval
        self.callback = callback
        self.cancelled = False
        type(self).instances.append(self)

    def start(self) -> None:
        self.callback()

    def cancel(self) -> None:
        self.cancelled = True


def test_duration_stop_and_finite_round_request_produce_machine_summary(
    tmp_path: Path,
) -> None:
    _ImmediateTimer.instances.clear()
    config = _config(tmp_path)
    service = _FakeService()
    clocks = iter((10.0, 10.25))

    def runtime_factory(config: Any, connect: Any) -> Any:
        return soak_script.SocketSoakRuntime(
            service,
            {role: "8.0.41" for role in NodeRole},
        )

    summary = soak_script.run_socket_soak(
        config,
        connect=lambda **kwargs: None,
        runtime_factory=runtime_factory,
        timer_factory=_ImmediateTimer,
        monotonic=lambda: next(clocks),
    )

    assert service.stop_seen is True
    assert service.request.rounds == 1
    assert service.request.queries_per_round == 3
    assert summary["status"] == "duration_elapsed"
    assert summary["elapsed_seconds"] == 0.25
    assert summary["queries_completed"] == 3
    assert summary["rejected"] == 1
    assert summary["sql_log_directory"] == str(config.artifact_root / "sql")
    assert summary["sql_log_paths"] == []
    assert _ImmediateTimer.instances[0].cancelled is True


def test_main_prints_one_strict_json_summary_without_connecting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: list[Any] = []

    def fake_run(config: Any, *, stop_event: Event) -> dict[str, object]:
        captured.append(config)
        return {
            "artifact_root": str(config.artifact_root),
            "findings": 0,
            "run_id": config.run_id,
            "status": "completed",
        }

    monkeypatch.setattr(soak_script, "_validate_socket_files", lambda paths: None)
    monkeypatch.setattr(soak_script, "run_socket_soak", fake_run)

    result = soak_script.main(
        [
            "--sockets",
            *(str(path) for path in _paths(tmp_path)),
            "--duration-seconds",
            "0.01",
            "--queries-per-round",
            "7",
            "--workers",
            "2",
            "--seed",
            "99",
            "--artifact-root",
            str(tmp_path / "output"),
            "--run-id",
            "machine-summary-test",
            "--max-rounds",
            "1",
        ]
    )

    output = capsys.readouterr()
    assert result == 0
    assert output.err == ""
    assert json.loads(output.out) == {
        "artifact_root": str((tmp_path / "output").resolve()),
        "findings": 0,
        "run_id": "machine-summary-test",
        "status": "completed",
    }
    assert captured[0].queries_per_round == 7
    assert captured[0].workers == 2
    assert captured[0].seed == 99
