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
from select_fuzz.generation.query import GeneratedQuery, QueryGenerationContext
from select_fuzz.generation.query.load_shaped import LoadShapedQueryGenerator
from select_fuzz.generation.query_grammar import (
    GrammarColumn,
    GrammarSchema,
    GrammarTable,
)
from select_fuzz.modes.fuzz import entrypoint
from select_fuzz.modes.fuzz import query_generation


_EXPECTED_FUZZ_EXCLUDED_FAMILIES = frozenset({"json", "fulltext", "spatial"})
# random.Random(0).randrange(100) == 49; grammar at the 50/50 boundary.
_GRAMMAR_SEED = 0
# random.Random(88).randrange(100) == 50; load-shaped at 50/50, grammar at 60/40.
_LOAD_BOUNDARY_SEED = 88


@dataclass
class _Factory:
    use_pure: bool
    control_use_pure: bool | None
    control_connection_limit: int | None


@dataclass
class _GrammarCandidate:
    sql: str


class _RecordingGrammarGenerator:
    def __init__(self) -> None:
        self.calls: list[tuple[object, int, frozenset[str]]] = []

    def generate(
        self,
        schema: object,
        *,
        seed: int,
        excluded_families: frozenset[str] = frozenset(),
    ) -> _GrammarCandidate:
        self.calls.append((schema, seed, excluded_families))
        return _GrammarCandidate("SELECT 1")


def _topology(role: NodeRole, primary_port: int) -> NodeTopologyConfig:
    return NodeTopologyConfig(
        role=role,
        primary=ServerEndpointConfig(host="127.0.0.1", port=primary_port),
        replica=ServerEndpointConfig(host="127.0.0.1", port=primary_port + 1),
    )


def _query_schema() -> GrammarSchema:
    return GrammarSchema(
        (
            GrammarTable(
                "fuzz_t0",
                (
                    GrammarColumn("id", "BIGINT"),
                    GrammarColumn("tenant_id", "BIGINT"),
                    GrammarColumn("amount", "BIGINT"),
                    GrammarColumn("payload", "VARCHAR(32)"),
                ),
                (),
            ),
        )
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


def test_entrypoint_uses_shared_fuzz_query_generator_factory(
    monkeypatch,
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    grammar = _RecordingGrammarGenerator()
    monkeypatch.setattr(
        query_generation,
        "GrammarQueryGenerator",
        lambda *, config: grammar,
    )
    config = AppConfig(
        mode=RunMode.FUZZ,
        nodes=(
            _topology(NodeRole.BASELINE, 33061),
            _topology(NodeRole.CUSTOM_OFF, 33063),
            _topology(NodeRole.CUSTOM_ON, 33065),
        ),
        fuzz=FuzzConfig(
            initial_tables=3,
            initial_rows_per_table=100,
            max_rows_per_database=1000,
        ),
    )

    service = entrypoint.build_fuzz_runner(config, tmp_path)
    schema = _query_schema()
    context = QueryGenerationContext("sf_f_case", schema)
    grammar_query = service._queries.generate(context, seed=_GRAMMAR_SEED)  # type: ignore[attr-defined]
    load_query = service._queries.generate(  # type: ignore[attr-defined]
        context,
        seed=_LOAD_BOUNDARY_SEED,
    )

    assert grammar.calls == [
        (schema, _GRAMMAR_SEED, _EXPECTED_FUZZ_EXCLUDED_FAMILIES)
    ]
    assert grammar_query == GeneratedQuery(
        "SELECT 1",
        _GRAMMAR_SEED,
        "grammar",
        frozenset({"grammar_random"}),
    )
    assert load_query == LoadShapedQueryGenerator().generate(
        context,
        seed=_LOAD_BOUNDARY_SEED,
    )
