from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pytest

from select_fuzz.base_tables import (
    CURRENT_BASE_TABLE_GENERATOR_VERSION,
    MAX_BASE_TABLE_SEED,
    available_base_table_generator_versions,
    generate_base_sql_bundle,
    generate_core_base_sql_bundle,
    normalize_base_table_seed,
    serialize_bundle,
)
from select_fuzz.base_tables.deterministic import derive_range, derive_uint64, pick
from select_fuzz.base_tables.models import BaseSqlBundle
from select_fuzz.base_tables import registry as base_table_registry
from select_fuzz.base_tables import v1
from select_fuzz.metadata.ddl_parser import parse_create_table
from select_fuzz.metadata.models import BaseSqlFile, TableMetadata
from tools import generate_sql_base_tables as generator
from tools import validate_sql_base_tables as validator


PARTITION_TYPES = {
    "RANGE",
    "RANGE COLUMNS",
    "LIST",
    "LIST COLUMNS",
    "HASH",
    "LINEAR HASH",
    "KEY",
    "LINEAR KEY",
}
GOLDEN_MANIFEST = Path(__file__).parent / "golden" / "base_table_v1_seed_12345.json"


def _replace_bundle_table(
    bundle: BaseSqlBundle,
    index: int,
    table: TableMetadata,
) -> BaseSqlBundle:
    tables = list(bundle.tables)
    tables[index] = table
    return replace(bundle, tables=tuple(tables))


def _replace_seed_sql(bundle: BaseSqlBundle, sql: str) -> BaseSqlBundle:
    files = list(bundle.files)
    files[-1] = replace(files[-1], sql=sql)
    return replace(bundle, files=tuple(files))


def _assert_v1_registry_rejects_bundle(
    monkeypatch: pytest.MonkeyPatch,
    bundle: BaseSqlBundle,
    message: str,
) -> None:
    monkeypatch.setitem(base_table_registry._GENERATORS, "v1", lambda seed: bundle)
    with pytest.raises(RuntimeError, match=message):
        generate_base_sql_bundle("v1", "12345")


def _partition_type(sql: str) -> str:
    for line in sql.splitlines():
        if line.startswith("PARTITION BY "):
            return line.removeprefix("PARTITION BY ").split(" (", 1)[0].split(" PARTITIONS ", 1)[0].strip()
    raise AssertionError("缺少 PARTITION BY")


def _subpartition_type(sql: str) -> str:
    for line in sql.splitlines():
        if line.startswith("SUBPARTITION BY "):
            return line.removeprefix("SUBPARTITION BY ").split(" (", 1)[0].split(" SUBPARTITIONS ", 1)[0].strip()
    raise AssertionError("缺少 SUBPARTITION BY")


def _split_top_level_csv(text: str) -> tuple[str, ...]:
    """拆分测试中的 INSERT 列和值，忽略括号或字符串内部的逗号。"""

    parts: list[str] = []
    depth = 0
    quote: str | None = None
    start = 0
    for index, char in enumerate(text):
        if quote is not None:
            if char == quote and (index == 0 or text[index - 1] != "\\"):
                quote = None
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    parts.append(text[start:].strip())
    return tuple(parts)


def _seed_insert_parts(bundle: BaseSqlBundle, index: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
    seed_sql = bundle.files[-1].sql
    match = re.search(
        rf"INSERT INTO `t{index}` \((?P<columns>.*?)\)\nSELECT\n  (?P<values>.*?)\nFROM `",
        seed_sql,
        flags=re.S,
    )
    assert match is not None
    columns = tuple(value.strip("`") for value in _split_top_level_csv(match.group("columns")))
    values = _split_top_level_csv(match.group("values"))
    return columns, values


def _core_projection(bundle: BaseSqlBundle) -> tuple[object, ...]:
    tables = []
    inserts = []
    for index, table in enumerate(bundle.tables):
        core_columns = tuple(table.columns.values())[:42]
        tables.append(
            (
                table.name,
                tuple((column.name, column.sql_type, column.nullable) for column in core_columns),
                tuple((item.name, tuple(item.columns), item.unique, item.primary) for item in table.indexes.values()),
                tuple(
                    (item.name, tuple(item.child_columns), item.parent_table, tuple(item.parent_columns))
                    for item in table.foreign_keys
                ),
                table.partition,
                table.is_temporary,
            )
        )
        columns, values = _seed_insert_parts(bundle, index)
        inserts.append((columns[:42], values[:42]))
    return tuple(tables), tuple(inserts)


def _evaluate_decimal_extra_expr(expression: str, n: int) -> Decimal:
    """按 v1 支持的 DECIMAL 扩展列表达式计算单个正数种子值。"""

    direct = re.fullmatch(r"ROUND\(\(`n` \+ (\d+)\) / (\d+), (\d+)\)", expression)
    bounded = re.fullmatch(r"ROUND\(MOD\(`n` \+ (\d+), (\d+)\) / (\d+), (\d+)\)", expression)
    if direct is not None:
        bias, divisor, scale = map(int, direct.groups())
        numerator = n + bias
    elif bounded is not None:
        bias, modulus, divisor, scale = map(int, bounded.groups())
        numerator = (n + bias) % modulus
    else:
        raise AssertionError(f"无法解析 DECIMAL 扩展列种子表达式：{expression}")
    quantum = Decimal(1).scaleb(-scale)
    return (Decimal(numerator) / Decimal(divisor)).quantize(quantum, rounding=ROUND_HALF_UP)


def test_确定性派生原语符合冻结向量() -> None:
    assert derive_uint64(seed="0", domain="vector/zero") == 15736695893721689427
    assert derive_uint64(seed="12345", domain="vector/basic") == 149763499408569138
    assert derive_uint64(seed=str(2**64 - 1), domain="vector/max") == 12404590578299919091
    assert derive_range(seed="12345", domain="vector/range", minimum=200, maximum=500) == 373
    assert pick(seed="12345", domain="vector/pick", candidates=("integer", "decimal", "json")) == "json"


def test_确定性范围使用拒绝采样而非直接取模() -> None:
    # counter 0、1、2 的 64 位派生值都落在拒绝区，counter 3 才被接受。
    assert derive_range(
        seed="12345",
        domain="vector/rejection/3",
        minimum=0,
        maximum=2**63,
    ) == 8602145318330365119


def test_版本注册表和默认核心包契约() -> None:
    assert CURRENT_BASE_TABLE_GENERATOR_VERSION == "v1"
    assert MAX_BASE_TABLE_SEED == 2**64 - 1
    assert available_base_table_generator_versions() == ("v1",)

    bundle = generate_core_base_sql_bundle()

    assert bundle.expand_base_table_columns is False
    assert bundle.generator_version is None
    assert bundle.seed is None
    assert [item.path.name for item in bundle.files] == [
        *[f"t{index}.sql" for index in range(79)],
        "zz_seed_fk_data.sql",
    ]
    assert [table.name for table in bundle.tables] == [f"t{index}" for index in range(79)]
    assert all(len(table.columns) == 42 for table in bundle.tables)
    assert all(not any(name.startswith("extra_t") for name in table.columns) for table in bundle.tables)
    assert all("\r" not in item.sql and item.sql.endswith("\n") for item in bundle.files)
    for index, table in enumerate(bundle.tables):
        insert_columns, insert_values = _seed_insert_parts(bundle, index)
        assert insert_columns == tuple(table.columns)
        assert len(insert_values) == 42


def test_v1_扩展包覆盖列数边界且_insert_列值等长同序() -> None:
    bundle = generate_base_sql_bundle("v1", "12345")

    assert bundle.expand_base_table_columns is True
    assert bundle.generator_version == "v1"
    assert bundle.seed == "12345"
    assert len(bundle.files) == 80
    assert len(bundle.tables[0].columns) == 200
    assert len(bundle.tables[1].columns) == 500
    assert all(200 <= len(table.columns) <= 500 for table in bundle.tables)
    for index, table in enumerate(bundle.tables):
        insert_columns, insert_values = _seed_insert_parts(bundle, index)
        assert insert_columns == tuple(table.columns)
        assert len(insert_values) == len(table.columns)
        assert f"extra_t{index}_000" in table.columns


def test_v1_注册入口独立拒绝生成器返回的各种非法完整结构(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = v1.generate_base_sql_bundle("12345", expand_base_table_columns=True)

    files = list(bundle.files)
    files[0], files[1] = files[1], files[0]
    _assert_v1_registry_rejects_bundle(
        monkeypatch,
        replace(bundle, files=tuple(files)),
        "文件名顺序",
    )

    for invalid_files in (bundle.files[:-1], (*bundle.files, bundle.files[-1])):
        _assert_v1_registry_rejects_bundle(
            monkeypatch,
            replace(bundle, files=tuple(invalid_files)),
            "文件名顺序",
        )

    for bundle_changes, message in (
        ({"expand_base_table_columns": False, "generator_version": None, "seed": None}, "扩展模式标记"),
        ({"generator_version": "v0"}, "生成器版本"),
        ({"seed": "67890"}, "基表种子"),
    ):
        _assert_v1_registry_rejects_bundle(
            monkeypatch,
            replace(bundle, **bundle_changes),
            message,
        )

    tables = list(bundle.tables)
    tables[0], tables[1] = tables[1], tables[0]
    _assert_v1_registry_rejects_bundle(
        monkeypatch,
        replace(bundle, tables=tuple(tables)),
        "表名顺序",
    )

    for table_index, target_count in ((0, 199), (1, 501)):
        table = bundle.tables[table_index]
        column_items = list(table.columns.items())[:target_count]
        if target_count > len(column_items):
            source_column = column_items[-1][1]
            for offset in range(len(column_items) - 42, target_count - 42):
                name = f"extra_t{table_index}_{offset:03d}"
                column_items.append((name, replace(source_column, name=name)))
        invalid_table = replace(table, columns=dict(column_items))
        _assert_v1_registry_rejects_bundle(
            monkeypatch,
            _replace_bundle_table(bundle, table_index, invalid_table),
            f"t{table_index} 列数",
        )

    table = bundle.tables[0]
    column_items = list(table.columns.items())
    column_items[0], column_items[1] = column_items[1], column_items[0]
    invalid_table = replace(table, columns=dict(column_items))
    _assert_v1_registry_rejects_bundle(
        monkeypatch,
        _replace_bundle_table(bundle, 0, invalid_table),
        "t0 核心列顺序",
    )

    table = bundle.tables[2]
    column_items = list(table.columns.items())
    _, first_extra = column_items[42]
    column_items[42] = ("extra_t2_999", replace(first_extra, name="extra_t2_999"))
    invalid_table = replace(table, columns=dict(column_items))
    _assert_v1_registry_rejects_bundle(
        monkeypatch,
        _replace_bundle_table(bundle, 2, invalid_table),
        "t2 扩展列顺序",
    )

    seed_sql = bundle.files[-1].sql
    invalid_seed_sql = seed_sql.replace(
        "INSERT INTO `t0` (`id_col`,`tenant_id`,",
        "INSERT INTO `t0` (`tenant_id`,`id_col`,",
        1,
    )
    assert invalid_seed_sql != seed_sql
    _assert_v1_registry_rejects_bundle(
        monkeypatch,
        _replace_seed_sql(bundle, invalid_seed_sql),
        "t0 种子 INSERT 列顺序",
    )

    match = re.search(
        r"INSERT INTO `t0` \(.*?\)\nSELECT\n  (?P<values>.*?)\nFROM `_select_fuzz_seed_numbers`",
        seed_sql,
        flags=re.S,
    )
    assert match is not None
    values = _split_top_level_csv(match.group("values"))
    invalid_values = ",\n  ".join(values[:-1])
    invalid_seed_sql = seed_sql[: match.start("values")] + invalid_values + seed_sql[match.end("values") :]

    _assert_v1_registry_rejects_bundle(
        monkeypatch,
        _replace_seed_sql(bundle, invalid_seed_sql),
        "t0 种子 INSERT 列和值数量",
    )


def test_v1_decimal_扩展列种子值不超出声明精度() -> None:
    seed = "12345"

    for table_index in range(v1.TOTAL_TABLE_COUNT):
        row_count = v1.seed_row_count(table_index)
        for spec in v1.extra_column_specs(table_index, seed=seed):
            type_match = re.fullmatch(r"decimal\((\d+),(\d+)\)", spec.sql_type)
            if type_match is None:
                continue
            precision, scale = map(int, type_match.groups())
            limit = Decimal(10) ** (precision - scale)
            maximum = max(
                _evaluate_decimal_extra_expr(spec.value_expr, n)
                for n in range(1, row_count + 1)
            )
            assert maximum < limit, (
                f"t{table_index}.{spec.name} 的最大种子值 {maximum} "
                f"超出 {spec.sql_type} 的整数位上限 {limit}"
            )


def test_v1_相同种子字节一致_不同种子只改变扩展投影() -> None:
    first = generate_base_sql_bundle("v1", "12345")
    repeated = generate_base_sql_bundle("v1", "12345")
    other = generate_base_sql_bundle("v1", "67890")

    assert serialize_bundle(first) == serialize_bundle(repeated)
    assert serialize_bundle(first) != serialize_bundle(other)
    assert _core_projection(first) == _core_projection(other)
    first_extra = tuple(tuple((column.name, column.sql_type) for column in tuple(table.columns.values())[42:]) for table in first.tables)
    other_extra = tuple(tuple((column.name, column.sql_type) for column in tuple(table.columns.values())[42:]) for table in other.tables)
    assert first_extra != other_extra


def test_v1_并发生成没有共享可变随机状态() -> None:
    seeds = ("12345", "67890", "12345", "0", "67890", str(MAX_BASE_TABLE_SEED))
    with ThreadPoolExecutor(max_workers=6) as executor:
        digests = tuple(executor.map(lambda seed: serialize_bundle(generate_base_sql_bundle("v1", seed)), seeds))

    assert digests[0] == digests[2]
    assert digests[1] == digests[4]
    assert len(set(digests)) == 4


def test_v1_运行时代码不依赖_python_random_或工具模块() -> None:
    source = Path(v1.__file__).read_text(encoding="utf-8")

    assert "import random" not in source
    assert "random." not in source
    assert "repr(" not in source
    assert "from tools" not in source
    assert "import tools" not in source


_TABLE_INDEX_HELPERS = (
    ("table_column_profile", lambda index: v1.table_column_profile(index)),
    ("table_column_count", lambda index: v1.table_column_count(index)),
    ("extra_column_specs", lambda index: v1.extra_column_specs(index, seed="0")),
    ("table_kind", lambda index: v1.table_kind(index)),
    ("subpartition_pair", lambda index: v1.subpartition_pair(index)),
    ("partition_clause", lambda index: v1.partition_clause(index)),
    ("can_use_unique_index", lambda index: v1.can_use_unique_index(index, "idx_t0_int_col")),
    ("key_line", lambda index: v1.key_line(index, "idx_t0_int_col", "(`int_col`)")),
    (
        "supplemental_index_lines",
        lambda index: v1.supplemental_index_lines(
            index,
            v1.TARGET_TOTAL_INDEX_COUNT,
            profile=v1.table_column_profile(0),
        ),
    ),
    ("create_table_sql", lambda index: v1.create_table_sql(index)),
    ("seed_row_count", lambda index: v1.seed_row_count(index)),
    ("tenant_expr", lambda index: v1.tenant_expr(index)),
    ("subpart_expr", lambda index: v1.subpart_expr(index)),
    ("parent_table_index", lambda index: v1.parent_table_index(index)),
    ("parent_row_expr", lambda index: v1.parent_row_expr(index)),
    ("parent_value_expr", lambda index: v1.parent_value_expr(index, "parent_id_col")),
    ("seed_columns", lambda index: v1.seed_columns(index)),
    ("unique_binary_expr", lambda index: v1.unique_binary_expr(index)),
    ("unique_text_expr", lambda index: v1.unique_text_expr(index, "text")),
    ("seed_value_exprs", lambda index: v1.seed_value_exprs(index)),
    ("insert_sql", lambda index: v1.insert_sql(index)),
)


@pytest.mark.parametrize("helper_name,helper", _TABLE_INDEX_HELPERS, ids=lambda value: value if isinstance(value, str) else None)
@pytest.mark.parametrize("index", (-1, 79, True, False, 1.0, "1"), ids=("negative", "overflow", "true", "false", "float", "string"))
def test_v1_所有按表_helper_严格拒绝非法基表编号(
    helper_name: str,
    helper: object,
    index: object,
) -> None:
    del helper_name
    with pytest.raises(ValueError, match=r"^基表编号必须"):
        helper(index)


@pytest.mark.parametrize(
    "seed",
    ("", "00", "01", "+1", "-1", " 1", "1 ", "1.0", "١", "１", str(2**64)),
)
def test_种子只接受规范_ascii_uint64(seed: str) -> None:
    with pytest.raises(ValueError, match="基表种子"):
        normalize_base_table_seed(seed)


@pytest.mark.parametrize("seed", ("0", "1", "12345", str(2**64 - 1)))
def test_规范种子保持原十进制字符串(seed: str) -> None:
    assert normalize_base_table_seed(seed) == seed


def test_未知生成器版本返回中文错误() -> None:
    with pytest.raises(ValueError, match="^未知基表生成器版本：v2$"):
        generate_base_sql_bundle("v2", "0")


def test_bundle_序列化使用大端长度前缀避免拼接歧义() -> None:
    bundle = BaseSqlBundle(
        files=(BaseSqlFile(Path("t0.sql"), "SELECT '甲';\n"),),
        tables=(),
    )
    name = b"t0.sql"
    sql = "SELECT '甲';\n".encode("utf-8")

    assert serialize_bundle(bundle) == struct.pack(">I", len(name)) + name + struct.pack(">I", len(sql)) + sql


def test_v1_seed_12345_匹配固定八十文件和完整_bundle_金标() -> None:
    expected = json.loads(GOLDEN_MANIFEST.read_text(encoding="utf-8"))
    bundle = generate_base_sql_bundle("v1", "12345")
    actual_files = {
        item.path.name: hashlib.sha256(item.sql.encode("utf-8")).hexdigest()
        for item in bundle.files
    }

    assert expected["generator_version"] == "v1"
    assert expected["seed"] == "12345"
    assert list(expected["files"]) == [*[f"t{index}.sql" for index in range(79)], "zz_seed_fk_data.sql"]
    assert actual_files == expected["files"]
    assert hashlib.sha256(serialize_bundle(bundle)).hexdigest() == expected["bundle_sha256"]


def test_v1_跨独立进程和_pythonhashseed_保持摘要一致() -> None:
    code = (
        "import hashlib; "
        "from select_fuzz.base_tables import generate_base_sql_bundle, serialize_bundle; "
        "print(hashlib.sha256(serialize_bundle(generate_base_sql_bundle('v1', '12345'))).hexdigest())"
    )
    digests = []
    for hash_seed in ("1", "8675309"):
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = hash_seed
        digests.append(
            subprocess.check_output(
                [sys.executable, "-c", code],
                cwd=Path(__file__).parents[1],
                env=environment,
                text=True,
            ).strip()
        )

    assert digests[0] == digests[1]
    assert digests[0] == json.loads(GOLDEN_MANIFEST.read_text(encoding="utf-8"))["bundle_sha256"]


def test_运行时生成入口不写磁盘(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    def reject_write(*args: object, **kwargs: object) -> int:
        raise AssertionError("运行时生成入口不应写磁盘")

    monkeypatch.setattr(Path, "write_text", reject_write)
    monkeypatch.setattr(Path, "open", reject_write)
    generate_core_base_sql_bundle()
    generate_base_sql_bundle("v1", "12345")

    assert list(tmp_path.iterdir()) == []


def test_工具公共_helper_默认生成四十二核心列() -> None:
    assert len(generator.base_seed_columns()) == 42
    assert generator.table_column_count(0) == 42
    assert generator.table_column_count(1) == 42

    table = parse_create_table(generator.create_table_sql(0))

    assert len(table.columns) == 42
    assert not any(name.startswith("extra_t0_") for name in table.columns)
    columns, values = _seed_insert_parts(generator.core_bundle(), 0)
    assert columns == tuple(table.columns)
    assert len(values) == len(columns)


def test_种子数据每张表生成可复现十到一百行() -> None:
    first_counts = [generator.seed_row_count(index) for index in range(79)]
    second_counts = [generator.seed_row_count(index) for index in range(79)]

    assert first_counts == second_counts
    assert all(10 <= count <= 100 for count in first_counts)
    assert len(set(first_counts)) > 1

    seed_sql = generator.seed_sql()
    for index, count in enumerate(first_counts):
        assert f"/* t{index}:rows={count} */" in seed_sql
        assert f"WHERE `n` <= {count}" in seed_sql
    assert seed_sql.upper().count("INSERT INTO") == 80


def test_核心列类型长度按表冻结并保留覆盖范围() -> None:
    profiles = [generator.table_column_profile(index) for index in range(79)]
    assert [generator.table_column_profile(index) for index in range(79)] == profiles

    char_lengths = {profile.char_length for profile in profiles}
    varchar_lengths = {profile.varchar_length for profile in profiles}
    assert min(char_lengths) == 1
    assert max(char_lengths) == 255
    assert min(varchar_lengths) == 1
    assert max(varchar_lengths) == 255

    short_varchar_sql = generator.create_table_sql(1)
    assert "`varchar_col` varchar(1)" in short_varchar_sql
    assert "`idx_t1_varchar_prefix` (`varchar_col`(1))" in short_varchar_sql


def test_默认生成八种一级分区和六十四种二级分区组合(tmp_path: Path) -> None:
    generator.generate_files(tmp_path)

    table_files = sorted(tmp_path.glob("t*.sql"), key=lambda path: int(path.stem[1:]))
    assert [path.name for path in table_files] == [f"t{index}.sql" for index in range(79)]
    assert all(len(parse_create_table(path.read_text(encoding="utf-8")).columns) == 42 for path in table_files)

    first_level_types = {
        _partition_type((tmp_path / f"t{index}.sql").read_text(encoding="utf-8"))
        for index in range(7, 15)
    }
    subpartition_pairs = {
        (
            _partition_type((tmp_path / f"t{index}.sql").read_text(encoding="utf-8")),
            _subpartition_type((tmp_path / f"t{index}.sql").read_text(encoding="utf-8")),
        )
        for index in range(15, 79)
    }

    assert first_level_types == PARTITION_TYPES
    assert subpartition_pairs == {(outer, inner) for outer in PARTITION_TYPES for inner in PARTITION_TYPES}


def test_离线生成器在_windows_换行环境仍逐字写入_lf_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write_bytes = Path.write_bytes

    def windows_write_text(
        path: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        del errors, newline
        return original_write_bytes(path, data.replace("\n", "\r\n").encode(encoding or "utf-8"))

    monkeypatch.setattr(Path, "write_text", windows_write_text)
    generator.generate_files(tmp_path)

    bundle = generate_core_base_sql_bundle()
    for sql_file in bundle.files:
        generated = (tmp_path / sql_file.path.name).read_bytes()
        assert generated == sql_file.sql.encode("utf-8")
        assert b"\r" not in generated
    execution_doc = (tmp_path / "执行顺序说明.md").read_bytes()
    assert execution_doc == generator.execution_doc().encode("utf-8")
    assert b"\r" not in execution_doc


def test_默认输出不包含向量并可关闭二级分区() -> None:
    normal_sql = generator.create_table_sql(0, include_subpartition=False)
    subpartition_sql = generator.create_table_sql(15, include_subpartition=False)
    seed_sql = generator.seed_sql()

    assert "VECTOR(" not in normal_sql.upper()
    assert "imci_vector_index=" not in normal_sql
    assert "VEC_FROMTEXT(" not in seed_sql
    assert "SUBPARTITION BY" not in subpartition_sql.upper()
    assert "PARTITION BY" in subpartition_sql.upper()


def test_唯一索引转换遵守分区键限制() -> None:
    normal_sql = generator.create_table_sql(0)
    partition_sql = generator.create_table_sql(7)
    subpartition_sql = generator.create_table_sql(15)

    assert "UNIQUE KEY `idx_t0_int_col`" in normal_sql
    assert "KEY `idx_t0_varchar_prefix`" in normal_sql
    assert "UNIQUE KEY `idx_t7_extra_tenant_int`" in partition_sql
    assert "UNIQUE KEY `idx_t7_int_col`" not in partition_sql
    assert "UNIQUE KEY `idx_t15_extra_tenant_int`" not in subpartition_sql


def test_离线生成器拒绝清理未知_sql(tmp_path: Path) -> None:
    user_sql = tmp_path / "用户.sql"
    user_sql.write_text("SELECT 1;\n", encoding="utf-8")

    with pytest.raises(ValueError, match="未知 SQL 文件"):
        generator.generate_files(tmp_path)

    assert user_sql.read_text(encoding="utf-8") == "SELECT 1;\n"


def test_校验器默认接受四十二核心列目录(tmp_path: Path) -> None:
    generator.generate_files(tmp_path)

    assert validator.main(sql_dir=tmp_path) == 0


def test_校验器扩展模式接受指定_v1_和种子(tmp_path: Path) -> None:
    generator.generate_files(
        tmp_path,
        expand_columns=True,
        generator_version="v1",
        seed="12345",
    )

    assert validator.main(
        sql_dir=tmp_path,
        expanded_columns=True,
        generator_version="v1",
        seed="12345",
    ) == 0


def test_校验器发现分区种子覆盖退化(tmp_path: Path) -> None:
    generator.generate_files(tmp_path)
    seed_path = tmp_path / "zz_seed_fk_data.sql"
    seed_sql = seed_path.read_text(encoding="utf-8")
    seed_path.write_text(seed_sql.replace(validator.TENANT_COVERAGE_EXPR, "1", 1), encoding="utf-8")

    assert validator.main(sql_dir=tmp_path) == 1


def test_校验器发现子分区种子覆盖退化(tmp_path: Path) -> None:
    generator.generate_files(tmp_path)
    seed_path = tmp_path / "zz_seed_fk_data.sql"
    seed_sql = seed_path.read_text(encoding="utf-8")
    seed_path.write_text(seed_sql.replace(validator.SUBPARTITION_COVERAGE_EXPR, "1", 1), encoding="utf-8")

    assert validator.main(sql_dir=tmp_path) == 1
