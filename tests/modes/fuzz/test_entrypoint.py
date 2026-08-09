from __future__ import annotations

from dataclasses import dataclass

from select_fuzz.config import (
    AppConfig,
    FuzzConfig,
    NodeRole,
    NodeTopologyConfig,
    RunMode,
)
from select_fuzz.config.models import ServerEndpointConfig
from select_fuzz.modes.fuzz import entrypoint


@dataclass
class _Factory:
    use_pure: bool
    control_use_pure: bool | None
    control_connection_limit: int | None


def _topology(role: NodeRole, primary_port: int) -> NodeTopologyConfig:
    return NodeTopologyConfig(
        role=role,
        primary=ServerEndpointConfig(host="127.0.0.1", port=primary_port),
        replica=ServerEndpointConfig(host="127.0.0.1", port=primary_port + 1),
    )


def test_c_workers_use_a_separate_pure_python_setup_factory(
    monkeypatch,
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    factories: list[_Factory] = []

    def build_factory(
        *,
        use_pure: bool,
        control_use_pure: bool | None = None,
        control_connection_limit: int | None = None,
    ) -> _Factory:
        factory = _Factory(
            use_pure,
            control_use_pure,
            control_connection_limit,
        )
        factories.append(factory)
        return factory

    monkeypatch.setattr(entrypoint.mysql.connector, "HAVE_CEXT", True)
    monkeypatch.setattr(entrypoint, "MySQLConnectorFactory", build_factory)
    config = AppConfig(
        mode=RunMode.FUZZ,
        nodes=(
            _topology(NodeRole.BASELINE, 33061),
            _topology(NodeRole.CUSTOM_OFF, 33063),
            _topology(NodeRole.CUSTOM_ON, 33065),
        ),
        fuzz=FuzzConfig(
            connector_implementation="auto",
            initial_tables=1,
            initial_rows_per_table=100,
            max_rows_per_database=1000,
        ),
    )

    service = entrypoint.build_fuzz_runner(config, tmp_path)
    materializer = service._materializer_factory()  # type: ignore[attr-defined]

    assert [factory.use_pure for factory in factories] == [False, True]
    assert service._factory is factories[0]  # type: ignore[attr-defined]
    assert materializer._factory is factories[1]  # type: ignore[attr-defined]
    assert all(factory.control_use_pure is True for factory in factories)


def test_production_fuzz_progress_is_flushed_to_stderr_only(capsys) -> None:  # type: ignore[no-untyped-def]
    entrypoint._stderr_progress("[fuzz状态] 判断=负载正常推进")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "[fuzz状态] 判断=负载正常推进\n"
