from __future__ import annotations

import os
import time
from typing import Any, cast

import mysql.connector
import pytest

from select_fuzz.generation.data import (
    DataGenerator,
    DataScenario,
    DistributionKind,
)
from select_fuzz.generation.schema import (
    BoundaryDeclarationId,
    ColumnDef,
    IndexDef,
    IndexPart,
    SchemaGenerator,
    SchemaLimits,
    SchemaManifest,
    SchemaProfile,
    TableDef,
)
from select_fuzz.generation.schema_rules import SchemaRules
from select_fuzz.generation.setup import SetupBundleBuilder


_SCENARIOS = (
    DataScenario.SEEDED_RANDOM,
    DataScenario.BOUNDARY,
    DataScenario.ALL_NULL,
    DataScenario.MIXED_NULL,
    DataScenario.DUPLICATE,
    DataScenario.HOTSPOT,
)
_ROW_COUNTS = (0, 1, 8)
_SINGLE_COLUMN_BOUNDARIES = frozenset(
    {
        BoundaryDeclarationId.VARCHAR_LENGTH_MAX,
        BoundaryDeclarationId.VARBINARY_LENGTH_MAX,
    }
)


def _sockets() -> tuple[str, ...]:
    if os.environ.get("SELECT_FUZZ_MYSQL_SOCKET_INTEGRATION") != "1":
        pytest.skip("set SELECT_FUZZ_MYSQL_SOCKET_INTEGRATION=1 and socket list")
    sockets_value = os.environ.get("SELECT_FUZZ_MYSQL_SOCKETS")
    if sockets_value is None:
        pytest.skip("SELECT_FUZZ_MYSQL_SOCKETS is unset")
    sockets = tuple(item for item in sockets_value.split(",") if item)
    if len(sockets) != 3:
        pytest.skip("SELECT_FUZZ_MYSQL_SOCKETS must contain exactly three paths")
    return sockets


def _scenario_schema() -> tuple[
    SchemaManifest,
    dict[BoundaryDeclarationId, tuple[str, str]],
]:
    # Executable maxima are safe beside mandatory columns, but combining both
    # maxima with fifty more boundaries would exceed one InnoDB logical row.
    # Keep each contextual maximum in its own table and execute every typed ID.
    limits = SchemaLimits(
        min_tables=3,
        max_tables=3,
        min_columns=2,
        max_columns=64,
    )
    generator = SchemaGenerator()
    boundaries = generator.executable_boundary_declarations(limits)
    assert tuple(boundary.boundary_id for boundary in boundaries) == tuple(
        BoundaryDeclarationId
    )

    def table(name: str, value_columns: tuple[ColumnDef, ...]) -> TableDef:
        return TableDef(
            name,
            False,
            (ColumnDef("id", "BIGINT UNSIGNED", False), *value_columns),
            (
                IndexDef(
                    "PRIMARY",
                    (IndexPart(column_name="id"),),
                    unique=True,
                    primary=True,
                ),
            ),
        )

    locations: dict[BoundaryDeclarationId, tuple[str, str]] = {}
    ordinary_columns: list[ColumnDef] = []
    for boundary in boundaries:
        if boundary.boundary_id in _SINGLE_COLUMN_BOUNDARIES:
            continue
        column_name = f"v_{boundary.boundary_id.value}"
        ordinary_columns.append(
            generator.typed_boundary_column(
                name=column_name,
                boundary_id=boundary.boundary_id,
                limits=limits,
            )
        )
        locations[boundary.boundary_id] = ("scenario_values", column_name)

    tables = [table("scenario_values", tuple(ordinary_columns))]
    for boundary_id, table_name in (
        (BoundaryDeclarationId.VARCHAR_LENGTH_MAX, "varchar_max_values"),
        (BoundaryDeclarationId.VARBINARY_LENGTH_MAX, "varbinary_max_values"),
    ):
        column = generator.typed_boundary_column(
            name="boundary_value",
            boundary_id=boundary_id,
            limits=limits,
        )
        tables.append(table(table_name, (column,)))
        locations[boundary_id] = (table_name, column.name)

    assert set(locations) == set(BoundaryDeclarationId)
    schema = SchemaManifest(
        SchemaProfile.REGULAR_INNODB,
        "mysql8041_data_scenarios",
        8_041,
        tuple(tables),
    )
    SchemaRules.mysql_8041().validate(schema, limits=limits)
    return schema, locations


def _execute_case(
    connection: Any,
    *,
    database: str,
    statements: tuple[str, ...],
    scenario: DataScenario,
    row_count: int,
    node_index: int,
    table_names: tuple[str, ...],
) -> tuple[tuple[str, int, tuple[tuple[object, ...], ...]], ...]:
    cursor = connection.cursor()
    try:
        cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
        cursor.execute(f"CREATE DATABASE `{database}`")
        cursor.execute(f"USE `{database}`")
        for statement_index, statement in enumerate(statements):
            try:
                cursor.execute(statement)
            except mysql.connector.Error as error:
                pytest.fail(
                    f"setup failed scenario={scenario.value} rows={row_count} "
                    f"node={node_index} statement={statement_index} "
                    f"errno={error.errno} sqlstate={error.sqlstate} "
                    f"message={error.msg} sql={statement[:240]}"
                )
        outcomes: list[tuple[str, int, tuple[tuple[object, ...], ...]]] = []
        for table_name in table_names:
            cursor.execute(f"SELECT COUNT(*) FROM `{table_name}`")
            count_rows = cursor.fetchall()
            assert count_rows == [(row_count,)], (
                scenario,
                row_count,
                node_index,
                table_name,
            )
            cursor.execute(f"SELECT * FROM `{table_name}` ORDER BY `id`")
            rows = tuple(tuple(row) for row in cursor.fetchall())
            outcomes.append((table_name, row_count, rows))
        return tuple(outcomes)
    finally:
        cursor.close()


@pytest.mark.mysql
@pytest.mark.timeout(900)
def test_all_explicit_data_scenarios_on_three_exact_8041_sockets() -> None:
    sockets = _sockets()
    connections = [
        mysql.connector.connect(
            unix_socket=socket,
            user="root",
            autocommit=True,
        )
        for socket in sockets
    ]
    databases: list[str] = []
    try:
        for node_index, connection in enumerate(connections):
            cursor = connection.cursor()
            cursor.execute("SELECT VERSION()")
            version_row = cast(tuple[object, ...] | None, cursor.fetchone())
            cursor.close()
            assert version_row is not None and len(version_row) == 1
            assert str(version_row[0]).startswith("8.0.41"), (
                node_index,
                version_row,
            )

        schema, boundary_locations = _scenario_schema()
        table_names = tuple(table.name for table in schema.tables)
        builder = SetupBundleBuilder(DataGenerator())
        for scenario_index, scenario in enumerate(_SCENARIOS):
            for row_count in _ROW_COUNTS:
                bundle = builder.build(
                    schema,
                    seed=8_041_500 + scenario_index * 100 + row_count,
                    rows_per_table=row_count,
                    scenario=scenario,
                )
                assert bundle.data.scenario is scenario
                assert all(
                    len(bundle.data.rows_by_table[table_name]) == row_count
                    for table_name in table_names
                )
                if scenario is DataScenario.BOUNDARY:
                    for boundary_id, (
                        table_name,
                        column_name,
                    ) in boundary_locations.items():
                        plan = next(
                            plan
                            for plan in bundle.data.distributions[table_name]
                            if plan.column_name == column_name
                        )
                        assert plan.kind is DistributionKind.BOUNDARY, boundary_id
                database = (
                    f"sf_data_scenario_{scenario_index}_{row_count}_{time.time_ns():x}"
                )
                databases.append(database)
                outcomes = tuple(
                    _execute_case(
                        connection,
                        database=database,
                        statements=bundle.statements,
                        scenario=scenario,
                        row_count=row_count,
                        node_index=node_index,
                        table_names=table_names,
                    )
                    for node_index, connection in enumerate(connections)
                )
                assert outcomes[0] == outcomes[1] == outcomes[2], (
                    scenario,
                    row_count,
                )
    finally:
        for connection in connections:
            cursor = connection.cursor()
            try:
                for database in databases:
                    cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
            finally:
                cursor.close()
                connection.close()
