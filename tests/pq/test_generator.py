"""Offline SQL contracts; SQLite is used only for portable expression semantics."""

from __future__ import annotations

import math
import random
import re
import sqlite3
from collections import Counter
from dataclasses import replace

import pytest

from select_fuzz.pq import generator as gen
from select_fuzz.pq.materializer import (
    PQColumn,
    PQSchema,
    PQTable,
    STANDARD_COLUMNS,
    _row_values,
    build_schema_spec,
)


SHAPES = (
    "scan", "aggregate", "decimal_stress", "join", "join_multi", "self_join",
    "nested_agg", "subquery_in", "union", "derived", "distinct",
)
SCHEMA = build_schema_spec("pq_generator_test", table_count=3).schema
TOKEN = re.compile(r"'(?:''|[^'])*'|[A-Za-z_][A-Za-z_0-9]*|\d+(?:\.\d+)?|<>|<=|>=|\S")


def _tokens(sql: str) -> list[str]:
    return TOKEN.findall(sql)


def _depths(tokens: list[str]) -> list[int]:
    depth = 0
    out = []
    for token in tokens:
        if token == ")":
            depth -= 1
        out.append(depth)
        if token == "(":
            depth += 1
    return out


def _items(tokens: list[str]) -> list[list[str]]:
    cuts = [-1] + [i for i, (t, d) in enumerate(zip(tokens, _depths(tokens)))
                   if t == "," and d == 0] + [len(tokens)]
    return [tokens[a + 1:b] for a, b in zip(cuts, cuts[1:])]


def _selects(sql: str) -> list[dict[str, list[str]]]:
    """Separate SELECT scopes, including derived and scalar subqueries.

    This is deliberately a clause inspector, not a substitute MySQL parser.
    It checks MySQL grouping restrictions that SQLite does not enforce.
    """
    tokens = _tokens(sql.rstrip(";"))
    depths = _depths(tokens)
    result = []
    for start, token in enumerate(tokens):
        if token != "SELECT":
            continue
        level = depths[start]
        end = next((i for i in range(start + 1, len(tokens))
                    if depths[i] < level or
                    (depths[i] == level and tokens[i] == "UNION")), len(tokens))
        cuts = [(start, "SELECT", 1)]
        for i in range(start + 1, end):
            if depths[i] != level:
                continue
            if tokens[i] in {"FROM", "WHERE", "HAVING", "LIMIT"}:
                cuts.append((i, tokens[i], 1))
            elif tokens[i:i + 2] in (["GROUP", "BY"], ["ORDER", "BY"]):
                cuts.append((i, tokens[i], 2))
        block = {}
        for (i, name, width), (j, _, _) in zip(cuts, cuts[1:] + [(end, "", 0)]):
            block[name] = tokens[i + width:j]
        result.append(block)
    return result


def _unalias(tokens: list[str]) -> list[str]:
    depths = _depths(tokens)
    cut = next((i for i, t in enumerate(tokens) if t == "AS" and depths[i] == 0),
               len(tokens))
    return tokens[:cut]


def _assert_group_safe(sql: str) -> None:
    for block in _selects(sql):
        if "GROUP" not in block:
            continue
        groups = _items(block["GROUP"])
        for item in _items(block["SELECT"]):
            expr = _unalias(item)
            if any(t in gen.AGG_FUNCS for t in expr):
                continue
            assert expr in groups, f"ungrouped projection: {expr}, SQL: {sql}"


def _assert_total_order(q: gen.PQGeneratedQuery) -> None:
    blocks = _selects(q.sql)
    outer = blocks[-1] if q.shape == "union" else blocks[0]
    assert q.compare_mode in {gen.EXACT, gen.MULTISET}, q
    assert q.has_order_by == ("ORDER" in outer), q
    for block in blocks:
        if "LIMIT" in block:
            assert "ORDER" in block, q.sql
    if "ORDER" not in outer:
        return
    assert q.compare_mode == gen.EXACT, q
    keys = [p[:-1] if p[-1] in {"ASC", "DESC"} else p
            for p in _items(outer["ORDER"])]
    if "GROUP" in outer:
        assert all(g in keys for g in _items(outer["GROUP"])), q.sql
    elif q.shape in {"union", "distinct"}:
        projection = blocks[0]["SELECT"]
        if projection[0] == "DISTINCT":
            projection = projection[1:]
        assert keys == [[str(i)] for i in range(1, len(_items(projection)) + 1)], q.sql
    else:
        assert keys[-1][-1] == "id", q.sql


def _portable_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    functions = {
        "ABS": abs, "CEILING": math.ceil, "FLOOR": math.floor, "SQRT": math.sqrt,
        "EXP": math.exp, "LN": math.log, "LOG": math.log, "LOG10": math.log10,
        "SIN": math.sin, "COS": math.cos, "TAN": math.tan, "ASIN": math.asin,
        "ACOS": math.acos, "ATAN": math.atan, "DEGREES": math.degrees,
        "RADIANS": math.radians, "COT": lambda x: 1 / math.tan(x),
        "LEAST": min, "GREATEST": max,
        "TRUNCATE": lambda x, n: math.trunc(x * 10 ** n) / 10 ** n,
    }
    for name, fn in functions.items():
        db.create_function(name, -1, lambda *args, f=fn:
                           None if any(x is None for x in args) else f(*args))
    return db


def test_seed_1_has_no_ungrouped_select_columns() -> None:
    _assert_group_safe(gen.PQGenerator(SCHEMA).generate(seed=1).sql)


@pytest.mark.parametrize("seed", [3, 6, 20])
def test_fixed_order_and_limit_regressions(seed: int) -> None:
    _assert_total_order(gen.PQGenerator(SCHEMA).generate(seed=seed))


def test_seed_6_union_has_compatible_branch_types() -> None:
    q = gen.PQGenerator(SCHEMA).generate(seed=6, shape="union")
    blocks = _selects(q.sql)
    # Reusing the typed projection also avoids DATE/numeric and string/numeric coercions.
    assert blocks[0]["SELECT"] == blocks[1]["SELECT"], q.sql


@pytest.mark.parametrize("seed", [1, 7, 23, 31, 67, 101])
def test_fixed_scan_seeds_are_domain_safe(seed: int) -> None:
    db = _portable_db()
    try:
        for value in (-1e12, -99999, -1, 0, 1, 123456, 999999.999999):
            expr = gen._func_wrap(random.Random(seed), str(value))
            result = db.execute(f"SELECT {expr}").fetchone()[0]
            assert result is not None and math.isfinite(result), (seed, expr)
    finally:
        db.close()


def test_predicates_have_ordered_intervals_and_use_nullable_columns() -> None:
    table = SCHEMA.tables[0]
    nullable = {c.name for c in table.columns if c.nullable}
    for seed in range(300):
        sql = gen._predicate(random.Random(seed), table, "a")
        for name in re.findall(r"a\.(\w+) IS (?:NOT )?NULL", sql):
            assert name in nullable, (seed, sql)
        for lo, hi in re.findall(r"BETWEEN (-?[\d.]+) AND (-?[\d.]+)", sql):
            assert float(lo) < float(hi), (seed, sql)


def test_predicate_recursion_has_a_hard_depth_bound() -> None:
    class BranchingRandom(random.Random):
        def choice(self, seq):
            return "and_or" if "and_or" in seq else super().choice(seq)

    try:
        sql = gen._predicate(BranchingRandom(3), SCHEMA.tables[0], "a")
    except RecursionError:
        pytest.fail("predicate recursion is controlled only by chance")
    assert max(_depths(_tokens(sql))) <= 4, sql
    assert len(sql) < 2000


def test_predicates_usually_select_real_fixture_rows() -> None:
    db = _portable_db()
    try:
        names = ", ".join(c.name for c in STANDARD_COLUMNS)
        db.execute(f"CREATE TABLE t ({names})")
        rng = random.Random(140)
        rows = [tuple(None if v is None else float(v) if c.kind == "decimal" else
                      str(v) if c.kind in {"date", "datetime"} else v
                      for c, v in zip(STANDARD_COLUMNS, _row_values(rng, 0)))
                for _ in range(1000)]
        db.executemany(f"INSERT INTO t VALUES ({','.join('?' for _ in STANDARD_COLUMNS)})", rows)
        nonempty = 0
        for seed in range(300):
            pred = gen._predicate(random.Random(seed), SCHEMA.tables[0], "a")
            nonempty += db.execute(f"SELECT EXISTS(SELECT 1 FROM t a WHERE {pred})").fetchone()[0]
        assert nonempty >= 270, f"only {nonempty}/300 predicates select fixture data"
    finally:
        db.close()


@pytest.mark.parametrize("shape", ["join", "join_multi", "self_join"])
def test_every_join_path_bounds_driver_and_each_fanout(shape: str) -> None:
    for seed in range(100):
        q = getattr(gen._QueryBuilder(random.Random(seed), SCHEMA), f"shape_{shape}")()
        driver = "x" if shape == "self_join" else "a"
        assert re.search(rf"\({driver}\.id / 1\) BETWEEN 1 AND \d+", q.sql), (seed, q.sql)
        joined = ["y"] if shape == "self_join" else ["b", "c"] if shape == "join_multi" else ["b"]
        for alias in joined:
            assert re.search(rf"{alias}\.id BETWEEN {driver}\.id AND "
                             rf"\(?{driver}\.id \+ \d+\)?", q.sql), (seed, q.sql)


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("pq_friendly", [False, True])
def test_seeded_shape_contracts(shape: str, pq_friendly: bool) -> None:
    generator = gen.PQGenerator(SCHEMA)
    queries = []
    for seed in range(120):
        q = generator.generate(seed, shape=shape, pq_friendly=pq_friendly)
        assert q == generator.generate(seed, shape=shape, pq_friendly=pq_friendly)
        assert q.shape == shape and q.seed == seed
        assert q.sql.startswith("SELECT ") and q.sql.endswith(";")
        assert len(q.sql) < 12000
        assert not re.search(r"(?:COUNT|SUM|AVG|MIN|MAX)\(DISTINCT", q.sql)
        _assert_group_safe(q.sql)
        _assert_total_order(q)
        queries.append(q.sql)
    assert len(set(queries)) >= 25, f"{shape} lost seeded variation"


@pytest.mark.parametrize("count, requested, actual", [(1, "join", "scan"),
    (1, "join_multi", "scan"), (2, "join_multi", "join")])
def test_fallback_shape_metadata(count: int, requested: str, actual: str) -> None:
    schema = build_schema_spec("pq_test", table_count=count).schema
    for friendly in (False, True):
        q = gen.PQGenerator(schema).generate(25, shape=requested, pq_friendly=friendly)
        assert q.shape == actual
        _assert_total_order(q)


def test_first_generation_and_legacy_retry_use_one_pq_construction_path() -> None:
    markers = {"scan": " / ", "aggregate": "GROUP BY", "decimal_stress": "AVG(",
               "join": " JOIN ", "join_multi": " JOIN ", "self_join": " JOIN ",
               "nested_agg": "FROM (SELECT", "subquery_in": " IN (SELECT",
               "union": " UNION",
               "derived": "FROM (SELECT", "distinct": "SELECT DISTINCT"}
    for shape in SHAPES:
        generator = gen.PQGenerator(SCHEMA)
        for seed in range(30):
            q = generator.generate(seed, shape=shape)
            assert q == generator.generate(seed, shape=shape, pq_friendly=False)
            assert q == generator.generate(seed, shape=shape, pq_friendly=True)
            assert q.shape == shape and markers[shape] in q.sql, q
            assert " / " in q.sql, q


def test_conservative_subquery_intersection_excludes_scalar_shapes() -> None:
    assert "subquery_scalar" not in gen.SUPPORTED_SHAPES
    with pytest.raises(ValueError, match="shape"):
        gen.PQGenerator(SCHEMA).generate(1, shape="subquery_scalar")


def test_in_subqueries_keep_the_top_level_semijoin_conversion_form() -> None:
    for seed in range(100):
        q = gen.PQGenerator(SCHEMA).generate(seed, shape="subquery_in")
        outer, inner = _selects(q.sql)
        alias = outer["FROM"][1]
        assert outer["WHERE"][:3] in ([alias, ".", c.name]
                                      for c in SCHEMA.tables[0].columns)
        assert outer["WHERE"][3:6] == ["IN", "(", "SELECT"], q.sql
        assert len(_items(inner["SELECT"])) == 1, q.sql
        assert not any(c in inner for c in ("GROUP", "HAVING", "ORDER", "LIMIT")), q.sql
        assert not any(t in gen.AGG_FUNCS or t in {"DISTINCT", "UNION", "NOT"}
                       for t in inner["SELECT"]), q.sql


@pytest.mark.parametrize("kind", [
    "blob", "tinyblob", "mediumblob", "longblob", "text", "tinytext", "mediumtext",
    "longtext", "json", "geometry", "vector", "year", "unknown",
])
@pytest.mark.parametrize("name", ["k1", "s1", "payload"])
def test_incompatible_schema_is_rejected_before_any_shape_can_leak_a_column(kind, name) -> None:
    # Each previous blind path could reference these columns: grouping, ORDER BY,
    # nullable predicates, and the IN subquery's complete outer projection.
    table = PQTable("t0", (PQColumn(name, kind, True), PQColumn("measure", "int", False)))
    schema = PQSchema("pq_test", (table,))
    for shape in SHAPES:
        with pytest.raises(ValueError, match=rf"(?i)unsupported.*t0\.{name}.*{kind}"):
            gen.PQGenerator(schema).generate(7, shape=shape)


def test_all_supported_schemas_keep_predicates_order_limit_and_having_variation() -> None:
    generator = gen.PQGenerator(SCHEMA)
    sql = " ".join(generator.generate(seed, shape=shape).sql
                   for shape in ("scan", "aggregate", "union", "distinct")
                   for seed in range(80))
    for marker in (" WHERE ", " ORDER BY ", " LIMIT ", " OFFSET ", " HAVING "):
        assert marker in sql, marker


def test_table_selection_uses_valid_aliases_after_the_twenty_sixth_table() -> None:
    schema = build_schema_spec("pq_test", table_count=40).schema
    selected = set()
    for seed in range(150):
        q = gen.PQGenerator(schema).generate(seed, shape="scan")
        from_items = _selects(q.sql)[0]["FROM"]
        assert len(from_items) == 2 and re.fullmatch(r"[A-Za-z_]\w*", from_items[1]), q.sql
        selected.add(from_items[0])
    assert len(selected) > 26


def test_decimal_metadata_excludes_raw_values_counts_and_raw_extrema() -> None:
    generator = gen.PQGenerator(SCHEMA)
    assert gen.PQGeneratedQuery("SELECT 1;", False, gen.MULTISET, "scan", 1).decimal_columns == ()
    for seed in range(100):
        q = generator.generate(seed, shape="decimal_stress")
        items = _items(_selects(q.sql)[0]["SELECT"])
        allowed = tuple(i for i, item in enumerate(items) if item[0] in {"SUM", "AVG"})
        assert q.decimal_columns == allowed, q
        for shape in SHAPES:
            q = generator.generate(seed, shape=shape)
            projection = _selects(q.sql)[0]["SELECT"]
            if projection[0] == "DISTINCT":
                projection = projection[1:]
            items = _items(projection)
            assert tuple(sorted(set(q.decimal_columns))) == q.decimal_columns
            for index in q.decimal_columns:
                expr = _unalias(items[index])
                assert ("(" in expr or expr[0] == "CASE" or
                        any(op in expr for op in ("/", "*", "+", "-"))), q
                assert expr[0] != "COUNT", q
                assert not re.fullmatch(r"(?:MIN|MAX) \( \w+ \. val_dec \)", " ".join(expr)), q


def test_projection_covers_temporal_numeric_control_and_cast_whitelist() -> None:
    generator = gen.PQGenerator(SCHEMA)
    sql = " ".join(generator.generate(seed, shape="scan").sql for seed in range(1200))
    for pattern in (r"\bYEAR\(", r"\bEXTRACT\(", r"\bTIMESTAMPDIFF\(",
                    r"\bDATE_ADD\(", r"\bCAST\(", r"\bCASE WHEN\b", r"\bCOALESCE\(",
                    r"\bNULLIF\(", r"\bIF\(", r" / ", r" % "):
        assert re.search(pattern, sql), pattern
    forbidden = r"\b(?:CONCAT|LOWER|UPPER|LENGTH|RAND|NOW|GROUP_CONCAT|JSON_EXTRACT|SLEEP)\("
    assert not re.search(forbidden, sql)


def test_random_selection_keeps_high_risk_shapes_and_adds_select_distinct() -> None:
    generator = gen.PQGenerator(SCHEMA)
    shapes = Counter(generator.generate(seed).shape for seed in range(600))
    assert set(shapes) == set(SHAPES)


def test_small_and_heterogeneous_schemas_reference_existing_columns() -> None:
    schema = PQSchema("pq_test", (
        PQTable("first", (PQColumn("measure", "decimal", False),)),
        PQTable("second", (PQColumn("other", "double", True),)),
    ))
    for shape in SHAPES:
        q = gen.PQGenerator(schema).generate(9, shape=shape)
        assert not re.search(r"\b(?:k1|k2|opt|val_dec)\b", q.sql), q
        _assert_group_safe(q.sql)
        _assert_total_order(q)
    nonnull = replace(SCHEMA.tables[0], columns=tuple(replace(c, nullable=False)
                                                   for c in SCHEMA.tables[0].columns))
    for seed in range(100):
        assert not re.search(r"IS (?:NOT )?NULL", gen._predicate(random.Random(seed), nonnull, "a"))


def test_invalid_inputs_fail_clearly() -> None:
    with pytest.raises(ValueError, match="shape"):
        gen.PQGenerator(SCHEMA).generate(1, shape="arbitrary_method")
    with pytest.raises(ValueError, match="table"):
        gen.PQGenerator(PQSchema("empty", ())).generate(1)


def test_public_shapes_are_unique_and_complete_for_the_live_matrix() -> None:
    assert len(gen.SUPPORTED_SHAPES) == len(set(gen.SUPPORTED_SHAPES))
    assert set(gen.SUPPORTED_SHAPES) == set(SHAPES)
    assert "SUPPORTED_SHAPES" in gen.__all__


@pytest.mark.parametrize("fn", gen.NUMERIC_FUNCS)
def test_each_whitelisted_numeric_function_is_finite_on_boundary_values(fn: str) -> None:
    db = _portable_db()
    try:
        for value in (-(2**63), 2**63 - 1, -1e100, 1e100, -1, 0, 1, 0.5, 999999.999999):
            expr = gen._func_wrap(random.Random(0), str(value), fn)
            try:
                result = db.execute(f"SELECT {expr}").fetchone()[0]
            except sqlite3.Error as exc:
                pytest.fail(f"{fn} has an invalid domain for {value}: {expr}: {exc}")
            assert result is not None and math.isfinite(result), expr
        assert db.execute(f"SELECT {gen._func_wrap(random.Random(0), 'NULL', fn)}").fetchone() == (None,)
    finally:
        db.close()


@pytest.mark.parametrize("shape, legs", [("join", 1), ("join_multi", 2), ("self_join", 1)])
@pytest.mark.parametrize("friendly", [False, True])
def test_join_cardinality_is_bounded_even_when_all_keys_are_equal(shape, legs, friendly) -> None:
    # This is the adversarial distribution that made the old joins O(n**3).
    columns = (PQColumn("k1", "int", False), PQColumn("k2", "int", False))
    schema = PQSchema("skew", tuple(PQTable(f"t{i}", columns) for i in range(3)))
    db = _portable_db()
    try:
        for t in schema.tables:
            db.execute(f"CREATE TABLE {t.name} (id INTEGER PRIMARY KEY, k1 INTEGER, k2 INTEGER)")
            db.executemany(f"INSERT INTO {t.name} VALUES (?, 1, 1)", [(i,) for i in range(1, 601)])
        for seed in range(16):
            q = gen.PQGenerator(schema).generate(seed, shape, friendly)
            items = _items(_selects(q.sql)[0]["SELECT"])
            count_index = next(i for i, item in enumerate(items) if item[-1] == "cnt")
            driver = "x" if shape == "self_join" else "a"
            assert re.search(rf"\(?{driver}\.id(?: / 1\))? BETWEEN 1 AND \d+", q.sql)
            rows = db.execute(q.sql).fetchall()
            assert sum(row[count_index] for row in rows) <= 512 * 8**legs, q.sql
    finally:
        db.close()


@pytest.mark.parametrize("shape", ["join", "join_multi"])
def test_left_join_windows_keep_unmatched_driving_rows(shape: str) -> None:
    columns = (PQColumn("k1", "int", False),)
    schema = PQSchema("outer_join", tuple(PQTable(f"t{i}", columns) for i in range(3)))
    db = _portable_db()
    try:
        for t in schema.tables:
            db.execute(f"CREATE TABLE {t.name} (id INTEGER PRIMARY KEY, k1 INTEGER)")
        db.executemany("INSERT INTO t0 VALUES (?, 1)", [(i,) for i in range(1, 65)])
        candidates = (gen.PQGenerator(schema).generate(seed, shape, True) for seed in range(30))
        q = next(q for q in candidates if "INNER JOIN" not in q.sql and " AND (" not in q.sql)
        items = _items(_selects(q.sql)[0]["SELECT"])
        count_index = next(i for i, item in enumerate(items) if item[-1] == "cnt")
        assert sum(row[count_index] for row in db.execute(q.sql)) == 64, q.sql
    finally:
        db.close()


def test_limited_union_is_stable_when_branch_scan_order_changes() -> None:
    columns = (PQColumn("k1", "int", False), PQColumn("k2", "int", False))
    schema = PQSchema("union_test", (PQTable("t0", columns),))
    db = _portable_db()
    db.create_function("IF", 3, lambda condition, yes, no: yes if condition else no)
    try:
        db.execute("CREATE TABLE t0 (id INTEGER PRIMARY KEY, k1 INTEGER, k2 INTEGER)")
        db.executemany("INSERT INTO t0 VALUES (?, ?, ?)",
                       [(i, i % 7 + 1, i % 200) for i in range(1, 501)])
        limited = 0
        for seed in range(100):
            q = gen.PQGenerator(schema).generate(seed, "union")
            if "LIMIT" not in q.sql:
                continue
            limited += 1
            db.execute("PRAGMA reverse_unordered_selects = OFF")
            forward = db.execute(q.sql).fetchall()
            db.execute("PRAGMA reverse_unordered_selects = ON")
            backward = db.execute(q.sql).fetchall()
            assert forward == backward, q.sql
        assert limited >= 30
    finally:
        db.close()
