"""Production wiring for the concurrent read/write fuzz mode."""

from __future__ import annotations

from pathlib import Path

import mysql.connector

from select_fuzz.artifacts import JsonlWriter
from select_fuzz.config import AppConfig
from select_fuzz.execution import MySQLConnectorFactory
from select_fuzz.generation.query import WeightedQueryGenerator
from select_fuzz.generation.query.grammar import RandomGrammarQueryGenerator
from select_fuzz.generation.query.load_shaped import LoadShapedQueryGenerator
from select_fuzz.generation.query_grammar import GrammarQueryConfig, GrammarQueryGenerator
from select_fuzz.modes.fuzz.materialization import FuzzMaterializer
from select_fuzz.modes.fuzz.models import FuzzConnectionLayout
from select_fuzz.modes.fuzz.query_pipeline import (
    ProcessQueryPipeline,
    resolve_query_generator_processes,
)
from select_fuzz.modes.fuzz.service import FuzzModeService
from select_fuzz.modes.fuzz.sql_log import FuzzSqlRecorder


def build_fuzz_runner(config: AppConfig, artifact_root: Path) -> FuzzModeService:
    if config.mode.value != "fuzz":
        raise ValueError("fuzz runner requires fuzz config mode")
    FuzzConnectionLayout.from_config(config.fuzz)
    primary = config.node_for(config.fuzz.target_role)
    replica = config.replica_for(config.fuzz.target_role)
    have_cext = bool(getattr(mysql.connector, "HAVE_CEXT", False))
    requested_connector = config.fuzz.connector_implementation
    if requested_connector == "c" and not have_cext:
        raise RuntimeError("fuzz connector_implementation=c requires MySQL Connector C extension")
    use_pure = requested_connector == "python" or (
        requested_connector == "auto" and not have_cext
    )
    actual_connector = "python" if use_pure else "c"
    worker_factory = MySQLConnectorFactory(
        use_pure=use_pure,
        control_use_pure=True,
        control_connection_limit=config.fuzz.control_connection_reserve,
    )
    setup_factory = MySQLConnectorFactory(
        use_pure=True,
        control_use_pure=True,
        control_connection_limit=config.fuzz.control_connection_reserve,
    )
    generation_processes = resolve_query_generator_processes(
        config.fuzz.query_generator_processes,
        reader_workers=(
            config.fuzz.databases * config.fuzz.reader_threads_per_database
        ),
    )
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
        factory=worker_factory,
        records=JsonlWriter(artifact_root / "events.jsonl"),
        query_generator=query_generator,
        query_pipeline_factory=lambda: ProcessQueryPipeline(
            process_count=generation_processes,
            max_tables_per_query_block=config.fuzz.initial_tables,
            reader_keys=tuple(
                (database_ordinal, reader_id)
                for database_ordinal in range(config.fuzz.databases)
                for reader_id in range(config.fuzz.reader_threads_per_database)
            ),
        ),
        sql_recorder=sql_recorder,
        connector_implementation=actual_connector,
        materializer_factory=lambda: FuzzMaterializer(
            setup_factory,
            primary,
            replica,
            config.fuzz,
            replica_sync_timeout_seconds=config.replica_sync_timeout_seconds,
            sql_recorder=sql_recorder,
        ),
    )


__all__ = ["build_fuzz_runner"]
