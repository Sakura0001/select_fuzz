from __future__ import annotations

import re

import pytest

from select_fuzz.base_tables import generate_core_base_sql_bundle
from select_fuzz.base_tables import v1
from select_fuzz.metadata.ddl_parser import parse_create_table
from select_fuzz.metadata.models import (
    ColumnMetadata,
    ColumnTypeFamily,
    ForeignKeyMetadata,
    IndexMetadata,
    PartitionMetadata,
    TableMetadata,
)
from select_fuzz.sqlgen.dml import DMLGenerator, DMLOperation, eligible_v1_permanent_tables
from select_fuzz.sqlgen import seeds as seed_tools
from select_fuzz.sqlgen.seeds import (
    CURRENT_CRUD_GENERATOR_VERSION,
    CURRENT_QUERY_GENERATOR_VERSION,
    derive_worker_seed,
    normalize_uint64_seed,
)


@pytest.mark.parametrize(
    "seed",
    (None, True, False, 0, 1, "", "00", "01", "+1", "-1", " 1", "1 ", "1.0", "١", "１", str(2**64)),
)
def test_任务种子只接受规范_ascii_uint64_字符串(seed: object) -> None:
    with pytest.raises(ValueError, match="种子"):
        normalize_uint64_seed(seed)  # type: ignore[arg-type]


@pytest.mark.parametrize("seed", ("0", "1", "12345", str(2**64 - 1)))
def test_任务种子规范化保持原十进制字符串(seed: str) -> None:
    assert normalize_uint64_seed(seed) == seed


def test_超长任务种子在_python_int_转换前返回稳定中文错误(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_int_conversion(*args: object, **kwargs: object) -> int:
        del args, kwargs
        raise AssertionError("超长种子不应进入 int()")

    monkeypatch.setattr(seed_tools, "int", forbidden_int_conversion, raising=False)
    with pytest.raises(ValueError, match="^任务种子不能大于"):
        normalize_uint64_seed("9" * 10_000)


def test_查询和_crud_生成器版本固定为_v1() -> None:
    assert CURRENT_QUERY_GENERATOR_VERSION == "v1"
    assert CURRENT_CRUD_GENERATOR_VERSION == "v1"


@pytest.mark.parametrize(
    ("seed", "role", "identity", "expected"),
    (
        ("0", "query", "query:0", 15895540555596060051),
        (str(2**64 - 1), "dml", "dml:t78", 14239659546116458247),
        ("42", "查询", "worker/一", 14605790415882010908),
    ),
)
def test_worker_seed_sha256_固定向量(seed: str, role: str, identity: str, expected: int) -> None:
    assert derive_worker_seed(seed, role, identity) == expected


def test_worker_seed_角色与身份均参与派生() -> None:
    baseline = derive_worker_seed("7", "query", "query:0")

    assert derive_worker_seed("8", "query", "query:0") != baseline
    assert derive_worker_seed("7", "dml", "query:0") != baseline
    assert derive_worker_seed("7", "query", "query:1") != baseline


def test_v1_永久表过滤恰好排除五张临时表() -> None:
    tables = generate_core_base_sql_bundle().tables

    eligible = eligible_v1_permanent_tables(tables)

    assert len(tables) == 79
    assert len(eligible) == 74
    assert {table.name for table in tables if table.is_temporary} == {"t2", "t3", "t4", "t5", "t6"}
    assert not ({"t2", "t3", "t4", "t5", "t6"} & {table.name for table in eligible})


def test_相同_seed_产生相同_dml_序列_不同_seed_产生不同序列() -> None:
    table = generate_core_base_sql_bundle().tables[0]

    def sequence(seed: int) -> list[object]:
        generator = DMLGenerator(random_seed=seed, base_table_seed="0")
        return [generator.generate(table, estimated_rows=50) for _ in range(30)]

    assert sequence(101) == sequence(101)
    assert sequence(101) != sequence(102)


def test_crud_v1_seed_101_前六条计划金标() -> None:
    table = generate_core_base_sql_bundle().tables[0]
    generator = DMLGenerator(random_seed=101, base_table_seed="0")

    plans = [generator.generate(table, estimated_rows=50) for _ in range(6)]

    # v1 的随机调用顺序和 SQL 决策属于可复现契约；变更必须发布新版本。
    assert [
        (plan.operation.value, plan.requested_rows)
        for plan in plans
    ] == [
        ("DELETE", 4),
        ("UPDATE", 1),
        ("UPDATE", 8),
        ("UPDATE", 2),
        ("INSERT", 2),
        ("DELETE", 6),
    ]
    assert plans[0].sql == "DELETE FROM `t0` ORDER BY RAND(1540496234) LIMIT 4"
    assert plans[1].sql == (
        "UPDATE `t0` SET `binary_col` = IF(`binary_col` IS NULL, X'', NULL) "
        "ORDER BY RAND(952178134) LIMIT 1"
    )
    assert plans[2].sql == (
        "UPDATE `t0` SET `binary_col` = IF(`binary_col` IS NULL, X'', NULL) "
        "ORDER BY RAND(1413205252) LIMIT 8"
    )
    assert plans[3].sql == (
        "UPDATE `t0` SET `varbinary_col` = IF(`varbinary_col` IS NULL, X'', NULL) "
        "ORDER BY RAND(830159750) LIMIT 2"
    )
    assert "SELECT `n` + 15588 AS `n`" in (plans[4].sql or "")
    assert plans[5].sql == "DELETE FROM `t0` ORDER BY RAND(822964419) LIMIT 6"


def test_软边界强制_insert_delete_且每次最多十行() -> None:
    table = generate_core_base_sql_bundle().tables[0]
    generator = DMLGenerator(random_seed=201)

    low_plans = [generator.generate(table, estimated_rows=count) for count in (0, 1, 10)]
    high_plans = [generator.generate(table, estimated_rows=count) for count in (200, 201, 10_000)]

    assert {plan.operation for plan in low_plans} == {DMLOperation.INSERT}
    assert {plan.operation for plan in high_plans} == {DMLOperation.DELETE}
    assert all(1 <= plan.requested_rows <= 10 for plan in [*low_plans, *high_plans])


def test_软边界之间等权随机覆盖三类操作且批量上限为十() -> None:
    table = generate_core_base_sql_bundle().tables[0]
    generator = DMLGenerator(random_seed=301)

    plans = [generator.generate(table, estimated_rows=100) for _ in range(300)]

    assert {plan.operation for plan in plans} == set(DMLOperation)
    assert all(1 <= plan.requested_rows <= 10 for plan in plans)
    for plan in plans:
        assert plan.sql is not None
        assert re.search(r"\bLIMIT\s+([1-9]|10)\b", plan.sql, re.IGNORECASE) or plan.operation is DMLOperation.INSERT


@pytest.mark.parametrize("table_index", (0, 7, 78))
@pytest.mark.parametrize("expanded", (False, True))
def test_core_与_expanded_v1_多张永久表均可生成_insert(table_index: int, expanded: bool) -> None:
    seed = "987654321"
    table = parse_create_table(
        v1.create_table_sql(
            table_index,
            seed=seed,
            expand_base_table_columns=expanded,
        )
    )
    generator = DMLGenerator(random_seed=401 + table_index, base_table_seed=seed)

    plan = generator.generate(table, estimated_rows=10)

    assert plan.operation is DMLOperation.INSERT
    assert plan.sql is not None
    assert f"INSERT INTO `t{table_index}`" in plan.sql
    assert "FROM `_select_fuzz_seed_numbers`" in plan.sql
    assert re.search(r"WHERE `n` BETWEEN 1 AND ([1-9]|10)\b", plan.sql)
    insert_columns = plan.sql.split(")\nSELECT", 1)[0].split("(", 1)[1].split(", ")
    assert len(insert_columns) == len(table.columns)
    assert any("extra_t" in column for column in insert_columns) is expanded


def test_update_保护_primary_unique_fk_分区_generated_且允许普通索引列() -> None:
    columns = {
        name: ColumnMetadata(
            name=name,
            sql_type="INT",
            type_family=ColumnTypeFamily.INTEGER,
            generated=name == "generated_col",
        )
        for name in (
            "primary_col",
            "unique_col",
            "unique_prefix_col",
            "unique_function_col",
            "fk_col",
            "partition_col",
            "generated_col",
            "ordinary_index_col",
        )
    }
    table = TableMetadata(
        name="t0",
        columns=columns,
        indexes={
            "PRIMARY": IndexMetadata("PRIMARY", ["primary_col"], unique=True, primary=True),
            "uk": IndexMetadata("uk", ["unique_col"], unique=True),
            "uk_prefix": IndexMetadata("uk_prefix", ["unique_prefix_col`(8"], unique=True),
            "uk_function": IndexMetadata("uk_function", ["(lower(`unique_function_col"], unique=True),
            "idx": IndexMetadata("idx", ["ordinary_index_col"]),
        },
        foreign_keys=[ForeignKeyMetadata("fk", ["fk_col"], "t1", ["id_col"])],
        partition=PartitionMetadata("HASH", "`partition_col`"),
    )
    generator = DMLGenerator(random_seed=501)

    update_plans = []
    for _ in range(100):
        plan = generator.generate(table, estimated_rows=100)
        if plan.operation is DMLOperation.UPDATE:
            update_plans.append(plan)

    assert update_plans
    assert all(plan.sql is not None for plan in update_plans)
    assert all("SET `ordinary_index_col` =" in plan.sql for plan in update_plans if plan.sql is not None)
    assert not any(
        protected in plan.sql.split(" SET ", 1)[1].split(" ORDER BY ", 1)[0]
        for plan in update_plans
        if plan.sql is not None
        for protected in (
            "primary_col",
            "unique_col",
            "unique_prefix_col",
            "unique_function_col",
            "fk_col",
            "partition_col",
            "generated_col",
        )
    )


def test_update_没有安全候选列时返回跳过_plan() -> None:
    table = TableMetadata(
        name="t0",
        columns={
            "id": ColumnMetadata("id", "INT", ColumnTypeFamily.INTEGER, nullable=False),
        },
        indexes={"PRIMARY": IndexMetadata("PRIMARY", ["id"], unique=True, primary=True)},
    )
    generator = DMLGenerator(random_seed=601)

    skipped = None
    for _ in range(100):
        plan = generator.generate(table, estimated_rows=100)
        if plan.operation is DMLOperation.UPDATE:
            skipped = plan
            break

    assert skipped is not None
    assert skipped.skipped is True
    assert skipped.sql is None
    assert skipped.skip_reason == "没有可安全更新的列"


def test_t0_t1_元数据表达式不完整时使用保守的_v1_安全列集合() -> None:
    tables = generate_core_base_sql_bundle().tables
    generator = DMLGenerator(random_seed=650)
    safe_core_columns = {"bool_col", "binary_col", "varbinary_col", "enum_col"}

    for table in tables[:2]:
        candidates = {column.name for column in generator._update_candidates(table)}
        assert candidates == safe_core_columns


def test_临时表不能交给_dml_生成器() -> None:
    table = generate_core_base_sql_bundle().tables[2]

    with pytest.raises(ValueError, match="永久表"):
        DMLGenerator(random_seed=701).generate(table, estimated_rows=100)
