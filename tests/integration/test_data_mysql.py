from __future__ import annotations

import os

import mysql.connector
import pytest

from select_fuzz.generation.data import DataGenerator
from select_fuzz.generation.schema import (
    ColumnDef,
    IndexDef,
    IndexPart,
    SchemaManifest,
    SchemaProfile,
    TableDef,
)
from select_fuzz.generation.setup import SetupBundleBuilder


@pytest.mark.mysql
def test_generated_setup_executes_on_an_opt_in_local_mysql() -> None:
    if os.environ.get("SELECT_FUZZ_MYSQL_INTEGRATION") != "1":
        pytest.skip(
            "set SELECT_FUZZ_MYSQL_INTEGRATION=1 with environment-only credentials; "
            "the exact 8.0.41 daemon cannot start inside the current sysctl sandbox"
        )
    username = os.environ.get("SELECT_FUZZ_MYSQL_USER")
    password = os.environ.get("SELECT_FUZZ_MYSQL_PASSWORD")
    if not username or password is None:
        pytest.skip("environment-only MySQL credentials are not configured")

    table = TableDef(
        "t0",
        False,
        (
            ColumnDef("id", "BIGINT UNSIGNED", False),
            ColumnDef("payload", "VARCHAR(80)", True, "utf8mb4", "utf8mb4_0900_ai_ci"),
            ColumnDef("amount", "DECIMAL(20,4)", False),
            ColumnDef("document", "JSON", False),
            ColumnDef("location", "POINT", False, srid=4326),
        ),
        (
            IndexDef(
                "PRIMARY", (IndexPart(column_name="id"),), unique=True, primary=True
            ),
        ),
    )
    schema = SchemaManifest(
        SchemaProfile.REGULAR_INNODB,
        "mysql_data_smoke",
        1,
        (table,),
    )
    setup = SetupBundleBuilder(DataGenerator()).build(
        schema, seed=41, rows_per_table=20
    )
    database = "select_fuzz_data_generator_it"
    connection = mysql.connector.connect(
        host=os.environ.get("SELECT_FUZZ_MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("SELECT_FUZZ_MYSQL_PORT", "3306")),
        user=username,
        password=password,
        autocommit=True,
    )
    try:
        cursor = connection.cursor()
        cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
        cursor.execute(f"CREATE DATABASE `{database}`")
        cursor.execute(f"USE `{database}`")
        for statement in setup.statements:
            cursor.execute(statement)
        cursor.execute("SELECT COUNT(*) FROM `t0`")
        assert cursor.fetchone() == (20,)
    finally:
        cursor = connection.cursor()
        cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
        connection.close()
