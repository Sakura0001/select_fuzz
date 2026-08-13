from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from select_fuzz.api.schemas import TaskCreateRequest
from select_fuzz.base_tables import generate_core_base_sql_bundle
from select_fuzz.sqlgen import registry as generator_registry
from select_fuzz.sqlgen.dml import DMLGenerator
from select_fuzz.sqlgen.generator import GenerationOptions, SQLGenerator
from select_fuzz.sqlgen.registry import (
    available_crud_generator_versions,
    available_query_generator_versions,
    create_crud_generator,
    create_query_generator,
)
from select_fuzz.sqlgen.rng import FrozenRandomV1
from select_fuzz.sqlgen.seeds import derive_worker_seed


def _request(**overrides: object) -> TaskCreateRequest:
    payload: dict[str, object] = {
        "node_name": "测试节点",
        "host": "127.0.0.1",
        "port": 3306,
        "username": "tester",
        "password": "secret",
        "database": "test",
    }
    payload.update(overrides)
    return TaskCreateRequest.model_validate(payload)


def test_query_crud_v1_永久登记且工厂注入冻结_rng() -> None:
    assert available_query_generator_versions() == ("v1",)
    assert available_crud_generator_versions() == ("v1",)

    query = create_query_generator("v1", 123, max_sql_length=321)
    crud = create_crud_generator("v1", 456, base_table_seed="789")

    assert isinstance(query, SQLGenerator)
    assert isinstance(query.random, FrozenRandomV1)
    assert query.max_sql_length == 321
    assert query.generator_version == "v1"
    assert isinstance(crud, DMLGenerator)
    assert isinstance(crud.random, FrozenRandomV1)
    assert crud.generator_version == "v1"
    assert crud.base_table_seed == "789"


def test_registry_模块级类型别名兼容_python39运行时求值() -> None:
    # Python 3.9 即使启用 future annotations，也会立即求值模块级类型别名；
    # 因此 factory alias 不能在 Callable 参数中使用 PEP 604 ``int | None``。
    source = Path(generator_registry.__file__).read_text(encoding="utf-8")

    assert "Callable[[int | None" not in source


@pytest.mark.parametrize(
    ("factory", "args", "message"),
    (
        (create_query_generator, ("v999", 1), "未知查询生成器版本"),
        (create_crud_generator, ("v999", 1), "未知 CRUD 生成器版本"),
    ),
)
def test_生成器工厂拒绝未知版本(factory, args: tuple[object, ...], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory(*args)


def test_api_schema_动态接受所有已登记版本(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        generator_registry._QUERY_GENERATORS,
        "v-test-query",
        generator_registry._QUERY_GENERATORS["v1"],
    )
    monkeypatch.setitem(
        generator_registry._CRUD_GENERATORS,
        "v-test-crud",
        generator_registry._CRUD_GENERATORS["v1"],
    )

    request = _request(
        enable_crud=True,
        query_generator_version="v-test-query",
        crud_generator_version="v-test-crud",
    )

    assert request.query_generator_version == "v-test-query"
    assert request.crud_generator_version == "v-test-crud"


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"query_generator_version": "v999"}, "未知查询生成器版本"),
        (
            {"enable_crud": True, "crud_generator_version": "v999"},
            "未知 CRUD 生成器版本",
        ),
    ),
)
def test_api_schema_未知query_crud版本返回校验错误(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        _request(**overrides)


def test_sql与dml生成器仍支持直接random_seed构造并允许显式rng注入() -> None:
    query_rng = FrozenRandomV1(11)
    crud_rng = FrozenRandomV1(22)

    assert isinstance(SQLGenerator(random_seed=1).random, FrozenRandomV1)
    assert isinstance(DMLGenerator(random_seed=2).random, FrozenRandomV1)
    assert SQLGenerator(rng=query_rng).random is query_rng
    assert DMLGenerator(rng=crud_rng).random is crud_rng
    with pytest.raises(ValueError, match="不能同时"):
        SQLGenerator(random_seed=1, rng=query_rng)
    with pytest.raises(ValueError, match="不能同时"):
        DMLGenerator(random_seed=1, rng=crud_rng)


def test_v1_多worker_query与dml完整多步sql金标() -> None:
    query_tables = generate_core_base_sql_bundle().tables
    query_options = (
        GenerationOptions(
            allow_locking=False,
            allow_temporary_tables=False,
            invalid_sql_ratio=0,
            null_compare_ratio=0,
            risky_expr_ratio=0,
        ),
        GenerationOptions(
            require_join=True,
            require_subquery=True,
            allow_locking=False,
            allow_temporary_tables=False,
            invalid_sql_ratio=0,
            null_compare_ratio=0,
            risky_expr_ratio=0,
        ),
        GenerationOptions(
            require_cte=True,
            require_set_operation=True,
            require_window=True,
            allow_locking=False,
            allow_temporary_tables=False,
            invalid_sql_ratio=0,
            null_compare_ratio=0,
            risky_expr_ratio=0,
        ),
    )
    query_sequences: list[list[str]] = []
    for worker_key in ("query:0", "query:1"):
        worker_seed = derive_worker_seed("123456789", "query", worker_key)
        generator = create_query_generator("v1", worker_seed, max_sql_length=8000)
        query_sequences.append(
            [generator.generate(query_tables, options) for options in query_options]
        )

    core_tables = {table.name: table for table in generate_core_base_sql_bundle().tables}
    dml_sequences: list[list[str | None]] = []
    for worker_key in ("dml:t0", "dml:t7"):
        worker_seed = derive_worker_seed("987654321", "dml", worker_key)
        generator = create_crud_generator("v1", worker_seed, base_table_seed="0")
        table = core_tables[worker_key.removeprefix("dml:")]
        dml_sequences.append(
            [
                generator.generate(table, estimated_rows=estimated_rows).sql
                for estimated_rows in (10, 50, 200)
            ]
        )

    assert query_sequences == [
        [
            "(SELECT DISTINCT 6 AS c0, (((87 * t1.`metric_parent_tenant_id`) * LOG(ABS(((t2.`tenant_id` DIV NULLIF(t0.`decimal_col`, 0)) >> 3)) + 1)) >> 2) AS c1 FROM `t29` AS t0 JOIN `t59` AS t1 ON t0.`id_col` <=> t1.`id_col` INNER JOIN `t19` AS t2 ON t1.`id_col` <=> t2.`id_col` RIGHT JOIN `t52` AS t3 ON t1.`id_col` <=> t3.`id_col` WHERE t0.`text_col` <> '_za9za9' LIMIT 2, 13) UNION ALL (SELECT ALL t0.`mediumint_col` AS c0, (~(POW(ABS(((t0.`bigint_col` - -27.841) - 77.838)) + 1, 2) << 3)) AS c1 FROM `t32` AS t0 WHERE ((((t0.`unsigned_int_col` * t0.`tinyint_col`) << 0) >= (SELECT COALESCE(MAX(sq.`id_col`), 0) FROM `t75` AS sq)) XOR (EXISTS (SELECT 1 FROM `t73` AS sq WHERE sq.`id_col` <=> t0.`id_col` LIMIT 5))) OR (((t0.`varchar_col` NOT REGEXP '^[0-9]+$') OR (((t0.`mediumtext_col` <> '9b39yy1') XOR (t0.`metric_parent_subpart_id` <= -89)) XOR (t0.`metric_parent_tenant_id` IN (SELECT sq.`id_col` FROM `t16` AS sq)))) AND (t0.`mediumtext_col` NOT LIKE '%z%')) ORDER BY 1 DESC LIMIT 4, 44)",
            "SELECT -5.252 AS c0, TAN(((-19.770 - t1.`unsigned_decimal_col`) ^ (t1.`bigint_col` | t1.`tenant_id`))) AS c1, (~(12 / NULLIF(t0.`bit_col`, 0))) AS c2, JSON_CONTAINS_PATH(COALESCE(t1.`json_col`, JSON_OBJECT()), 'one', '$.k') AS c3, (t0.`parent_subpart_id` MOD NULLIF(t0.`int_col`, 0)) AS c4 FROM `t35` AS t0 INNER JOIN `t69` AS t1 ON t0.`id_col` <=> t1.`id_col` WHERE EXISTS (SELECT 1 FROM `t12` AS sq WHERE sq.`id_col` IS NOT NULL LIMIT 5) LIMIT 25",
            "WITH RECURSIVE cte_num(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM cte_num WHERE n < 5) (SELECT ALL LTRIM(LOCATE('a', REGEXP_SUBSTR(t1.`longtext_col`, '[a-z0-9_]+'))) AS c0, JSON_OBJECT('k', 'c_yc', 'n', 6) AS c1, RANK() OVER w AS rn FROM `t8` AS t0 STRAIGHT_JOIN `t25` AS t1 ON t0.`id_col` <=> t1.`id_col` WHERE NOT (t1.`unsigned_decimal_col` >= (-0.234 ^ (53.482 >> 1))) WINDOW w AS (PARTITION BY t0.`parent_bigint_col` ORDER BY t1.`bigint_col`) ORDER BY t1.`datetime_col` DESC LIMIT 2, 10) UNION (VALUES ROW(1, '3', 3))",
        ],
        [
            "SELECT ALL t1.`metric_parent_tenant_id` AS c0 FROM `t74` AS t0 NATURAL JOIN `t16` AS t1 JOIN `t48` AS t2 ON t0.`id_col` <=> t2.`id_col` WHERE t2.`longblob_col` IS NOT NULL ORDER BY t2.`time_col` ASC LIMIT 28",
            "SELECT DISTINCT (TAN(t1.`parent_tenant_id`) << 3) AS c0, RIGHT('_4za2330', 1) AS c1, HEX(t0.`binary_col`) AS c2, t0.`mediumtext_col` AS c3 FROM `t58` AS t0 JOIN `t41` AS t1 ON t0.`id_col` <=> t1.`id_col` WHERE (t1.`subpart_id` | t1.`parent_bigint_col`) >= (SELECT COALESCE(MAX(sq.`id_col`), 0) FROM `t12` AS sq) ORDER BY c2 DESC LIMIT 4 OFFSET 3",
            "WITH RECURSIVE cte_num(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM cte_num WHERE n < 5) (SELECT ALL t1.`bigint_col` AS c0, -66.688 AS c1, LAG(t1.`tenant_id`, 1, 0) OVER w AS wx, SUM(t1.`mediumint_col`) OVER (PARTITION BY t0.`parent_tenant_id` ORDER BY t1.`bigint_col` ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS wf FROM `t51` AS t0 FORCE INDEX (`idx_t51_extra_tinytext_enum`) LEFT JOIN `t62` AS t1 ON t0.`id_col` <=> t1.`id_col` WHERE t1.`unsigned_decimal_col` BETWEEN -3 AND 64 WINDOW w AS (PARTITION BY t1.`parent_tenant_id` ORDER BY t0.`tinyint_col`) ORDER BY t1.`subpart_id` ASC LIMIT 3 OFFSET 3) UNION ALL (VALUES ROW(1, '32xxa4x511', 3))",
        ],
    ]
    # 以下完整 SQL 是 v1 发布协议的一部分；修改现有生成逻辑应让金标失败，
    # 需要改变算法时必须新增 v2 实现和 registry 登记，不能替换 v1 factory。
    assert [
        [hashlib.sha256(sql.encode("utf-8")).hexdigest() for sql in sequence if sql is not None]
        for sequence in dml_sequences
    ] == [
        [
            "4fdfc73a1f66d26fffe8ccce39d0f15a15a14c1b291b339fe04bad2ca66927ca",
            "ac391787c08be4135338dc39324633b8f1f7fe8f5f78f3bbd86efaad86c5afef",
            "996943f16789a6bfade4c8efa2e6665453a84d77a8bb88b9755bf605ea9b4f52",
        ],
        [
            "ba2845ccd514ca6736f1fcafabf5951fb2b84269d5226e196631a932b6a67512",
            "9a968855153195d884922b43684d502fa5e763a60299a5c8cebc3ae7dde27faf",
            "a90d8f08a459be46ac151ef58225d8ddbbeec9e057a9174f1116188d1edbc7ae",
        ],
    ]
    assert "SELECT `n` + 11544 AS `n`" in (dml_sequences[0][0] or "")
    assert "WHERE `n` BETWEEN 1 AND 6" in (dml_sequences[0][0] or "")
    assert "SELECT `n` + 13577 AS `n`" in (dml_sequences[1][0] or "")
    assert "WHERE `n` BETWEEN 1 AND 3" in (dml_sequences[1][0] or "")
    assert dml_sequences[0][1:] == [
        "UPDATE `t0` SET `varbinary_col` = IF(`varbinary_col` IS NULL, X'', NULL) ORDER BY RAND(323820310) LIMIT 1",
        "DELETE FROM `t0` ORDER BY RAND(500322743) LIMIT 7",
    ]


def test_v1_完整sql序列跨进程且不受_pythonhashseed_影响() -> None:
    project_root = Path(__file__).parents[1]
    script = r'''
import json
from select_fuzz.base_tables import generate_core_base_sql_bundle
from select_fuzz.sqlgen.generator import GenerationOptions
from select_fuzz.sqlgen.registry import create_crud_generator, create_query_generator
from select_fuzz.sqlgen.seeds import derive_worker_seed

tables = generate_core_base_sql_bundle().tables
query = create_query_generator(
    "v1", derive_worker_seed("123456789", "query", "query:0")
)
options = (
    GenerationOptions(
        allow_locking=False, allow_temporary_tables=False,
        invalid_sql_ratio=0, null_compare_ratio=0, risky_expr_ratio=0,
    ),
    GenerationOptions(
        require_join=True, require_subquery=True,
        allow_locking=False, allow_temporary_tables=False,
        invalid_sql_ratio=0, null_compare_ratio=0, risky_expr_ratio=0,
    ),
    GenerationOptions(
        require_cte=True, require_set_operation=True, require_window=True,
        allow_locking=False, allow_temporary_tables=False,
        invalid_sql_ratio=0, null_compare_ratio=0, risky_expr_ratio=0,
    ),
)
query_sql = [
    query.generate(tables, option) for option in options
]
table = tables[0]
crud = create_crud_generator(
    "v1", derive_worker_seed("987654321", "dml", "dml:t0")
)
dml_sql = [
    crud.generate(table, estimated_rows=estimated_rows).sql
    for estimated_rows in (10, 50, 200)
]
print(json.dumps({"query": query_sql, "dml": dml_sql}, ensure_ascii=False, sort_keys=True))
'''

    outputs = []
    for hash_seed in ("0", "1", "123456789", "random"):
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = hash_seed
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=project_root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        outputs.append(json.loads(result.stdout))

    assert outputs[1:] == [outputs[0], outputs[0], outputs[0]]
    canonical_output = json.dumps(
        outputs[0],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert hashlib.sha256(canonical_output).hexdigest() == (
        "b8ac405637dcb6d75b1d081d4e5b3b055850857329d2f7ff18c6d050a9455635"
    )
    assert [sql.split(None, 1)[0] for sql in outputs[0]["dml"]] == [
        "INSERT",
        "UPDATE",
        "DELETE",
    ]
