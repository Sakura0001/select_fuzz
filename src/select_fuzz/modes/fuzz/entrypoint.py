"""Production wiring for the concurrent read/write fuzz mode."""

from __future__ import annotations

from pathlib import Path

from select_fuzz.artifacts import JsonlWriter
from select_fuzz.config import AppConfig
from select_fuzz.execution import MySQLConnectorFactory
from select_fuzz.generation.query import WeightedQueryGenerator
from select_fuzz.generation.query.grammar import RandomGrammarQueryGenerator
from select_fuzz.generation.query.load_shaped import LoadShapedQueryGenerator
from select_fuzz.generation.query_grammar import GrammarQueryConfig, GrammarQueryGenerator
from select_fuzz.modes.fuzz.materialization import FuzzMaterializer
from select_fuzz.modes.fuzz.models import FuzzConnectionLayout
from select_fuzz.modes.fuzz.service import FuzzModeService
from select_fuzz.modes.fuzz.sql_log import FuzzSqlRecorder


def build_fuzz_runner(config: AppConfig, artifact_root: Path) -> FuzzModeService:
    if config.mode.value != "fuzz":
        raise ValueError("fuzz runner requires fuzz config mode")
    FuzzConnectionLayout.from_config(config.fuzz)
    primary = config.node_for(config.fuzz.target_role)
    replica = config.replica_for(config.fuzz.target_role)
    factory = MySQLConnectorFactory()
    sql_recorder = (
        FuzzSqlRecorder(artifact_root / "sql")
        if config.full_thread_sql_log
        else None
    )
    grammar = RandomGrammarQueryGenerator(
        GrammarQueryGenerator(
            config=GrammarQueryConfig(max_tables_per_query_block=config.fuzz.initial_tables)
        )
    )
    query_generator = WeightedQueryGenerator(
        (
            ("grammar", grammar, config.fuzz.grammar_query_weight),
            (
                "load_shaped",
                LoadShapedQueryGenerator(),
                config.fuzz.load_shaped_query_weight,
            ),
        )
    )
    return FuzzModeService(
        config=config.fuzz,
        primary=primary,
        replica=replica,
        factory=factory,
        records=JsonlWriter(artifact_root / "events.jsonl"),
        query_generator=query_generator,
        sql_recorder=sql_recorder,
        materializer_factory=lambda: FuzzMaterializer(
            factory,
            primary,
            replica,
            config.fuzz,
            replica_sync_timeout_seconds=config.replica_sync_timeout_seconds,
            sql_recorder=sql_recorder,
        ),
    )


__all__ = ["build_fuzz_runner"]
