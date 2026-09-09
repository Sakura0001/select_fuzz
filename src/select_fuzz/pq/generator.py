"""Seeded SELECTs for the conservative intersection of the PQ support lists.

All comparisons cover complete results: unordered queries use multisets, and
ordered queries have total keys (including every grouping/projected UNION key).
LIMIT is always coupled to such an order. The stricter appendix excludes DISTINCT
aggregates and subqueries that cannot become semijoins, so scalar subqueries are
conservatively omitted despite the broader earlier list. Ordinary SELECT DISTINCT,
semijoin-shaped IN and materialized derived tables remain available. EXPLAIN must
still gate every query.

Expressions carry their result type and precision eligibility into the query
metadata. Raw DECIMAL, counts and MIN/MAX of raw columns remain exact. Join
windows bound intermediate cardinality even if every data key has one value.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace

from select_fuzz.pq.materializer import PQColumn, PQSchema, PQTable

NUMERIC_FUNCS = (
    "ABS", "CEILING", "FLOOR", "ROUND", "TRUNCATE", "SQRT", "EXP", "LN", "LOG10",
    "SIN", "COS", "TAN", "ASIN", "ACOS", "ATAN", "DEGREES", "RADIANS", "LOG", "COT",
)
DATE_FUNCS = (
    "YEAR", "MONTH", "DAY", "DAYOFYEAR", "QUARTER", "WEEK", "WEEKDAY", "TO_DAYS",
    "MONTHNAME", "DAYNAME", "DATE", "EXTRACT", "DATE_ADD", "TIMESTAMPDIFF",
)
TIME_FUNCS = ("HOUR", "MINUTE", "SECOND", "MICROSECOND", "EXTRACT", "ADDTIME")
AGG_FUNCS = ("COUNT", "SUM", "AVG", "MIN", "MAX")
COMP_OPS = ("=", "<>", "<", "<=", ">", ">=")

EXACT = "exact"
MULTISET = "multiset"
COUNT_ONLY = "count_only"  # compatibility export only; never generated

_INTEGER_KINDS = {"tinyint", "smallint", "mediumint", "int", "integer", "bigint"}
_DECIMAL_KINDS = {"decimal", "numeric"}
_FLOAT_KINDS = {"float", "double", "real"}
_DATE_KINDS = {"date", "datetime", "timestamp", "time"}
_STRING_KINDS = {"char", "varchar", "string", "var_string", "enum", "set"}
_SUPPORTED_KINDS = {
    "int", "decimal", "double", "date", "datetime", "time", "string", "bit", "null",
}
_MAX_PREDICATE_DEPTH = 2
_JOIN_DRIVER_ROWS = 512
_JOIN_FANOUT = 8


@dataclass(frozen=True, slots=True)
class PQGeneratedQuery:
    sql: str
    has_order_by: bool
    compare_mode: str
    shape: str
    seed: int
    # Zero-based positions of Decimal expressions eligible for configured tolerance.
    decimal_columns: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class _Expr:
    sql: str
    kind: str
    decimal_tolerance: bool = False

    def named(self, name: str) -> _Expr:
        return replace(self, sql=f"{self.sql} AS {name}")


def _kind(c: PQColumn) -> str:
    kind = c.kind.lower()
    if kind in _INTEGER_KINDS:
        return "int"
    if kind in _DECIMAL_KINDS:
        return "decimal"
    if kind in _FLOAT_KINDS:
        return "double"
    if kind in _STRING_KINDS:
        return "string"
    return "datetime" if kind == "timestamp" else kind


def _is_numeric(c: PQColumn) -> bool:
    return _kind(c) in {"int", "decimal", "double"}


def _is_date(c: PQColumn) -> bool:
    return c.kind.lower() in _DATE_KINDS


def _numeric_cols(table: PQTable) -> list[PQColumn]:
    return [c for c in table.columns if _is_numeric(c)]


def _key_cols(table: PQTable) -> list[PQColumn]:
    return [c for c in table.columns if c.name in {"k1", "k2", "opt", "s1"}]


def _col_ref(c: PQColumn, alias: str | None) -> str:
    return f"{alias}.{c.name}" if alias else c.name


def _column(c: PQColumn, alias: str | None) -> _Expr:
    return _Expr(_col_ref(c, alias), _kind(c))


def _bounded(expr: str) -> str:
    # Clamp before ABS or arithmetic, including the signed BIGINT minimum.
    return f"LEAST(GREATEST({expr}, -1000000), 1000000)"


def _func_wrap(rng: random.Random, expr: str, fn: str | None = None) -> str:
    fn = fn or rng.choice(NUMERIC_FUNCS)
    x = _bounded(expr)
    if fn in {"ROUND", "TRUNCATE"}:
        return f"{fn}({x}, {rng.randint(0, 6)})"
    if fn in {"ASIN", "ACOS", "TAN"}:
        return f"{fn}(({x} % 1000) / 1000.0)"
    if fn == "COT":
        return f"COT((ABS({x}) % 1000) / 1000.0 + 0.5)"
    if fn == "EXP":
        return f"EXP({x} % 10)"
    if fn in {"LN", "LOG", "LOG10"}:
        return f"{fn}(ABS({x}) + 1)"
    if fn == "SQRT":
        return f"SQRT(ABS({x}))"
    return f"{fn}({x})"


def _func_expr(rng: random.Random, arg: _Expr) -> _Expr:
    fn = rng.choice(NUMERIC_FUNCS)
    sql = _func_wrap(rng, arg.sql, fn)
    if fn in {"CEILING", "FLOOR"} and arg.kind != "double":
        # Their exact return type can depend on precision. Make the contract explicit.
        return _Expr(f"CAST({sql} AS SIGNED)", "int")
    kind = arg.kind if fn in {"ABS", "ROUND", "TRUNCATE"} else "double"
    return _Expr(sql, kind, kind == "decimal")


def _numeric_expr(rng: random.Random, table: PQTable, alias: str | None) -> _Expr:
    columns = _numeric_cols(table)
    if not columns:
        return _Expr(f"{alias}.id" if alias else "id", "int")
    arg = _column(rng.choice(columns), alias)
    # Small literals and at most two operations avoid integer and Decimal overflow.
    kind = "double" if arg.kind == "double" else "decimal"
    sql = _bounded(arg.sql)
    if kind == "decimal":
        sql = f"CAST({sql} AS DECIMAL(30,6))"
    for _ in range(rng.randint(1, 2)):
        op = rng.choice(("+", "-", "*", "/", "%"))
        literal = rng.choice((2, 3, 7, 10))
        sql = f"({sql} {op} {literal})"
    return _Expr(sql, kind, kind == "decimal")


def _division(rng: random.Random, table: PQTable, alias: str | None) -> _Expr:
    columns = _numeric_cols(table)
    # Prefer a non-indexed exact measure to make a scan with computation attractive.
    candidates = [c for c in columns if _kind(c) == "decimal"] or [
        c for c in columns if not c.indexed
    ] or columns
    arg = _column(rng.choice(candidates), alias) if candidates else _Expr(
        f"{alias}.id" if alias else "id", "int"
    )
    kind = "double" if arg.kind == "double" else "decimal"
    return _Expr(f"({arg.sql} / {rng.choice((3, 7, 100, 1000))})", kind, kind == "decimal")


def _date_expr(rng: random.Random, c: PQColumn, alias: str | None) -> _Expr:
    ref = _col_ref(c, alias)
    functions: tuple[str, ...] = TIME_FUNCS if _kind(c) == "time" else DATE_FUNCS
    if _kind(c) == "datetime":
        functions += TIME_FUNCS
    fn = rng.choice(functions)
    if fn == "EXTRACT":
        unit = rng.choice(("HOUR", "MINUTE", "SECOND") if _kind(c) == "time" else
                          ("YEAR", "MONTH", "DAY"))
        return _Expr(f"EXTRACT({unit} FROM {ref})", "int")
    if fn == "DATE_ADD":
        return _Expr(f"DATE_ADD({ref}, INTERVAL {rng.choice((1, 7, 30))} DAY)", _kind(c))
    if fn == "TIMESTAMPDIFF":
        unit = rng.choice(("DAY", "MONTH", "YEAR"))
        return _Expr(f"TIMESTAMPDIFF({unit}, '2018-01-01', {ref})", "int")
    if fn == "ADDTIME":
        return _Expr(f"ADDTIME({ref}, '00:01:00')", _kind(c))
    kind = "string" if fn in {"MONTHNAME", "DAYNAME"} else "date" if fn == "DATE" else "int"
    return _Expr(f"{fn}({ref})", kind)


def _projection_expr(rng: random.Random, table: PQTable, c: PQColumn, alias: str) -> _Expr:
    arg = _column(c, alias)
    if _is_date(c):
        return _date_expr(rng, c, alias)
    if not _is_numeric(c):
        return arg
    choice = rng.choice(("function", "arithmetic", "cast", "case", "coalesce", "if", "nullif"))
    if choice == "function":
        return _func_expr(rng, arg)
    if choice == "arithmetic":
        return _numeric_expr(rng, table, alias)
    if choice == "cast":
        return _Expr(f"CAST({_bounded(arg.sql)} AS DECIMAL(30,6))", "decimal", True)
    if choice == "case":
        sql = f"CASE WHEN {arg.sql} > 0 THEN {arg.sql} ELSE 0 END"
    elif choice == "coalesce":
        sql = f"COALESCE({arg.sql}, 0)"
    elif choice == "if":
        sql = f"IF({arg.sql} < 0, 0, {arg.sql})"
    else:
        sql = f"NULLIF({arg.sql}, 0)"
    return _Expr(sql, arg.kind, arg.kind == "decimal")


def _select_list_proj(rng: random.Random, table: PQTable, alias: str) -> list[_Expr]:
    cols = [c for c in table.columns if _kind(c) in
            {"int", "decimal", "double", "date", "datetime", "time", "string", "bit", "null"}]
    if not cols:
        return [_Expr(f"{alias}.id", "int")]
    picked = rng.sample(cols, rng.randint(min(2, len(cols)), min(5, len(cols))))
    out = []
    for index, c in enumerate(picked):
        arg = _column(c, alias)
        if (_is_numeric(c) or _is_date(c)) and rng.random() < 0.45:
            arg = _projection_expr(rng, table, c, alias).named(f"f_{index}")
        out.append(arg)
    return out


def _range(c: PQColumn) -> tuple[int, int]:
    # These domains mirror the materializer; equality/IN use discrete keys only.
    return {"k1": (1, 7), "k2": (0, 200), "opt": (0, 3),
            "val_dec": (-99999, 999999), "val_dbl": (-100000, 100000)}.get(
                c.name, (-100, 100))


def _predicate(rng: random.Random, table: PQTable, alias: str | None, depth: int = 0) -> str:
    numeric = _numeric_cols(table)
    discrete = [c for c in numeric if _kind(c) == "int"]
    nullable = [c for c in table.columns if c.nullable]
    strings = [c for c in table.columns if _kind(c) == "string"]
    dates = [c for c in table.columns if _kind(c) in {"date", "datetime"}]
    kinds = []
    if numeric:
        kinds += ["cmp", "cmp", "between"]
    if discrete:
        kinds.append("in")
    if nullable:
        kinds.append("null")
    if strings:
        kinds.append("like")
    if dates:
        kinds.append("date")
    if not kinds:
        return "1 = 1"
    if depth < _MAX_PREDICATE_DEPTH:
        kinds.append("and_or")
    kind = rng.choice(kinds)
    if kind == "and_or":
        left = _predicate(rng, table, alias, depth + 1)
        right = _predicate(rng, table, alias, depth + 1)
        return f"({left} {rng.choice(('AND', 'OR'))} {right})"
    if kind == "null":
        c = rng.choice(nullable)
        return f"{_col_ref(c, alias)} IS {'NOT ' if rng.random() < 0.4 else ''}NULL"
    if kind == "like":
        c = rng.choice(strings)
        return f"{_col_ref(c, alias)} LIKE '{rng.choice(('a%', 'b%', '%a%', 'alpha', 'beta'))}'"
    if kind == "date":
        c = rng.choice(dates)
        year = rng.choice((2019, 2020, 2021))
        return f"{_col_ref(c, alias)} BETWEEN '{year}-01-01' AND '{year + 1}-12-31'"
    c = rng.choice(discrete if kind == "in" else numeric)
    ref = _col_ref(c, alias)
    low, high = _range(c)
    if kind == "between":
        midpoint = (low + high) // 2
        lo, hi = rng.randint(low, midpoint), rng.randint(midpoint + 1, high)
        return f"{ref} BETWEEN {lo} AND {hi}"
    if kind == "in":
        values = sorted(rng.sample(range(low, high + 1), min(rng.randint(2, 5), high - low + 1)))
        return f"{ref} IN ({', '.join(map(str, values))})"
    op = rng.choice(COMP_OPS if _kind(c) == "int" else ("<", "<=", ">", ">="))
    # Strict comparisons must leave values on both sides of the threshold.
    literal = rng.randint(low + 1, high - 1)
    return f"{ref} {op} {literal}"


def _aggregate(fn: str, arg: _Expr) -> _Expr:
    if fn == "COUNT":
        return _Expr(f"COUNT({arg.sql})", "int")
    if fn in {"SUM", "AVG"}:
        kind = "double" if arg.kind == "double" else "decimal"
        # Integer SUM and sums of counts do not need a precision allowance.
        eligible = kind == "decimal" and (arg.kind == "decimal" or fn == "AVG")
    else:
        kind, eligible = arg.kind, arg.decimal_tolerance
    return _Expr(f"{fn}({arg.sql})", kind, eligible)


def _agg_expr(rng: random.Random, table: PQTable, alias: str | None) -> _Expr:
    fn = rng.choice(AGG_FUNCS)
    numeric = _numeric_cols(table)
    if not numeric or (fn == "COUNT" and rng.random() < 0.5):
        return _Expr("COUNT(*)", "int")
    arg = _column(rng.choice(numeric), alias)
    if rng.random() < 0.35:
        arg = _func_expr(rng, arg)
    elif rng.random() < 0.25:
        arg = _Expr(f"CASE WHEN {arg.sql} > 0 THEN {arg.sql} ELSE 0 END",
                    arg.kind, arg.kind == "decimal")
    return _aggregate(fn, arg)


def _order_clause_total(rng: random.Random, table: PQTable, alias: str) -> str:
    cols = [c for c in table.columns if c.name != "id"]
    picked = rng.sample(cols, min(rng.randint(1, 2), len(cols)))
    parts = [f"{_col_ref(c, alias)} {'DESC' if rng.random() < 0.4 else 'ASC'}" for c in picked]
    parts.append(f"{alias}.id ASC")
    return ", ".join(parts)


def _join_bound(rng: random.Random, alias: str) -> str:
    width = rng.choice((64, 128, 256, _JOIN_DRIVER_ROWS))
    # A non-sargable equivalent encourages a scan without removing the hard bound.
    return f"({alias}.id / 1) BETWEEN 1 AND {width}"


@dataclass(slots=True)
class _QueryBuilder:
    rng: random.Random
    schema: PQSchema

    def _table(self) -> tuple[PQTable, str]:
        # This alias is local to its query block; its spelling must not depend
        # on table count (the 27th table previously produced punctuation).
        return self.rng.choice(self.schema.tables), "a"

    def _limit_clause(self) -> str:
        limit = self.rng.choice((5, 10, 20, 50, 100, 500))
        if self.rng.random() < 0.3:
            return f"LIMIT {limit} OFFSET {self.rng.choice((0, 5, 10))}"
        return f"LIMIT {limit}"

    def _where(self, table: PQTable, alias: str | None) -> str:
        if self.rng.random() < 0.4:
            return ""
        return f"WHERE {_predicate(self.rng, table, alias)}"

    def _groups(self, table: PQTable) -> list[PQColumn]:
        keys = _key_cols(table)
        return self.rng.sample(keys, self.rng.randint(1, min(2, len(keys)))) if keys else []

    def _group_clauses(self, refs: list[str], *, allow_limit: bool = False) -> str:
        if not refs:
            return ""
        sql = f"GROUP BY {', '.join(refs)}"
        if self.rng.random() < 0.5:
            sql += f" ORDER BY {', '.join(refs)}"
            if allow_limit and self.rng.random() < 0.4:
                sql += f" {self._limit_clause()}"
        return sql

    def _finish(self, sql: str, shape: str, projection: list[_Expr], *,
                ordered: bool = False) -> PQGeneratedQuery:
        return PQGeneratedQuery(
            sql=sql.strip() + ";", has_order_by=ordered,
            compare_mode=EXACT if ordered else MULTISET, shape=shape, seed=0,
            decimal_columns=tuple(i for i, e in enumerate(projection) if e.decimal_tolerance),
        )

    def shape_scan(self) -> PQGeneratedQuery:
        t, alias = self._table()
        proj = _select_list_proj(self.rng, t, alias)
        proj.append(_division(self.rng, t, alias).named("scan_value"))
        where = self._where(t, alias)
        limit = self._limit_clause() if self.rng.random() < 0.55 else ""
        want_order = bool(limit) or self.rng.random() < 0.5
        order = f"ORDER BY {_order_clause_total(self.rng, t, alias)}" if want_order else ""
        sql = f"SELECT {', '.join(e.sql for e in proj)} FROM {t.name} {alias} {where} {order} {limit}"
        return self._finish(sql, "scan", proj, ordered=want_order)

    def shape_aggregate(self) -> PQGeneratedQuery:
        t, alias = self._table()
        groups = self._groups(t)
        refs = [_col_ref(c, alias) for c in groups]
        proj = [_column(c, alias).named(f"g_{c.name}") for c in groups]
        # Only aggregate expressions may be added after the actual grouping keys.
        # A computed measure keeps even an ungrouped query from being solely
        # index MIN/MAX or a metadata-only COUNT. Keep other aggregate variation.
        scan = _aggregate(self.rng.choice(("SUM", "AVG", "MIN", "MAX")),
                          _division(self.rng, t, alias))
        proj.append(scan.named("agg_0"))
        proj += [_agg_expr(self.rng, t, alias).named(f"agg_{i}")
                 for i in range(1, self.rng.randint(2, 4))]
        clauses = self._group_clauses(refs, allow_limit=True)
        if groups and self.rng.random() < 0.4:
            # Unlike random SUM > a random threshold, this cannot eliminate every group.
            having = " HAVING COUNT(*) > 0"
            offset = clauses.find(" ORDER BY")
            clauses = clauses + having if offset < 0 else clauses[:offset] + having + clauses[offset:]
        sql = (f"SELECT {', '.join(e.sql for e in proj)} FROM {t.name} {alias} "
               f"{self._where(t, alias)} {clauses}")
        return self._finish(sql, "aggregate", proj, ordered="ORDER BY" in clauses)

    def _join(self, shape: str, tables: list[PQTable], aliases: list[str]) -> PQGeneratedQuery:
        t, driver = tables[0], aliases[0]
        groups = self._groups(t)
        proj = [_column(c, driver).named(f"g_{c.name}") for c in groups]
        proj.append(_Expr("COUNT(*) AS cnt", "int"))
        joins = []
        for other, alias in zip(tables[1:], aliases[1:]):
            common = [c.name for c in _key_cols(t) if _is_numeric(c) and any(
                d.name == c.name and _kind(d) == _kind(c) for d in other.columns)]
            # The id window caps fanout even with a maximally skewed k1. Prefer
            # k1 on multi-joins so bounded windows still have useful matches.
            key = "k1" if shape == "join_multi" and "k1" in common else (
                self.rng.choice(common) if common else None
            )
            on = f"{driver}.{key} = {alias}.{key} AND " if key else ""
            on += f"{alias}.id BETWEEN {driver}.id AND {driver}.id + {_JOIN_FANOUT - 1}"
            # LEFT JOIN is supported with a divisible outer table. Keep the
            # inner id bound in ON so unmatched outer rows survive; the driver
            # remains a scan candidate instead of choosing an inner div table.
            kind = self.rng.choice(("INNER JOIN", "LEFT JOIN"))
            joins.append(f"{kind} {other.name} {alias} ON {on}")
            proj.append(_agg_expr(self.rng, other, alias).named(f"agg_{alias}"))
        where = f"WHERE {_join_bound(self.rng, driver)}"
        if self.rng.random() < 0.25:
            where += f" AND ({_predicate(self.rng, t, driver)})"
        clauses = self._group_clauses([_col_ref(c, driver) for c in groups])
        sql = (f"SELECT {', '.join(e.sql for e in proj)} FROM {t.name} {driver} "
               f"{' '.join(joins)} {where} {clauses}")
        return self._finish(sql, shape, proj, ordered="ORDER BY" in clauses)

    def shape_join(self) -> PQGeneratedQuery:
        if len(self.schema.tables) < 2:
            return self.shape_scan()
        return self._join("join", list(self.schema.tables[:2]), ["a", "b"])

    def shape_join_multi(self) -> PQGeneratedQuery:
        if len(self.schema.tables) < 3:
            return self.shape_join()
        return self._join("join_multi", list(self.schema.tables[:3]), ["a", "b", "c"])

    def shape_self_join(self) -> PQGeneratedQuery:
        t, _ = self._table()
        return self._join("self_join", [t, t], ["x", "y"])

    def shape_subquery_in(self) -> PQGeneratedQuery:
        t, alias = self._table()
        other = self.schema.tables[min(1, len(self.schema.tables) - 1)]
        pairs = [(a, b) for a in _numeric_cols(t) for b in _numeric_cols(other)
                 if a.name == b.name and _kind(a) == _kind(b)]
        if not pairs:
            return self.shape_scan()
        discrete = [(a, b) for a, b in pairs if _kind(a) == "int"]
        outer_col, inner_col = self.rng.choice(discrete or pairs)
        # Positive IN is a top-level WHERE term with a single, non-aggregate
        # SELECT and no LIMIT/UNION/HAVING: retain the semijoin-convertible form.
        # Engine transformation and choice of the outer split table remain gated.
        sub = (f"SELECT sj.{inner_col.name} FROM {other.name} sj "
               f"{self._where(other, 'sj')}")
        proj = [_column(c, alias) for c in t.columns]
        proj.append(_division(self.rng, t, alias).named("scan_value"))
        limit = self._limit_clause() if self.rng.random() < 0.5 else ""
        order = f"ORDER BY {_order_clause_total(self.rng, t, alias)}" if (
            limit or self.rng.random() < 0.6
        ) else ""
        sql = (f"SELECT {', '.join(e.sql for e in proj)} FROM {t.name} {alias} "
               f"WHERE {_col_ref(outer_col, alias)} IN ({sub}) {order} {limit}")
        return self._finish(sql, "subquery_in", proj, ordered=bool(order))

    def shape_union(self) -> PQGeneratedQuery:
        t, alias = self.schema.tables[0], "a"
        proj = _select_list_proj(self.rng, t, alias)
        proj.append(_division(self.rng, t, alias).named("scan_value"))
        # Identical typed expressions in both branches avoid implicit coercion.
        select = f"SELECT {', '.join(e.sql for e in proj)} FROM {t.name} {alias}"
        first, second = self._where(t, alias), self._where(t, alias)
        op = self.rng.choice(("UNION", "UNION ALL"))
        limit = self._limit_clause() if self.rng.random() < 0.6 else ""
        ordered = bool(limit) or self.rng.random() < 0.5
        order = "ORDER BY " + ", ".join(str(i + 1) for i in range(len(proj))) if ordered else ""
        # No branch ORDER BY/LIMIT: only the complete projected tuple orders the union.
        sql = f"{select} {first} {op} {select} {second} {order} {limit}"
        return self._finish(sql, "union", proj, ordered=ordered)

    def shape_distinct(self) -> PQGeneratedQuery:
        t, alias = self._table()
        proj = _select_list_proj(self.rng, t, alias)
        proj.append(_division(self.rng, t, alias).named("scan_value"))
        limit = self._limit_clause() if self.rng.random() < 0.4 else ""
        ordered = bool(limit) or self.rng.random() < 0.5
        order = "ORDER BY " + ", ".join(str(i + 1) for i in range(len(proj))) if ordered else ""
        sql = (f"SELECT DISTINCT {', '.join(e.sql for e in proj)} FROM {t.name} {alias} "
               f"{self._where(t, alias)} {order} {limit}")
        return self._finish(sql, "distinct", proj, ordered=ordered)

    def shape_derived(self) -> PQGeneratedQuery:
        t, _ = self._table()
        keys = _key_cols(t)
        if not keys:
            return self.shape_scan()
        # Vary the inner keys while retaining both aggregation stages.
        inner_keys = self.rng.sample(keys, self.rng.randint(2, len(keys))) if len(keys) > 1 else keys
        inner_cols = ", ".join(c.name for c in inner_keys)
        inner = f"SELECT {inner_cols}, COUNT(*) AS cnt"
        outer_key = self.rng.choice(inner_keys)
        proj = [_Expr(f"x.{outer_key.name} AS g_{outer_key.name}", _kind(outer_key)),
                _Expr("MAX(x.cnt) AS mx", "int"), _Expr("SUM(x.cnt) AS sm", "decimal")]
        agg = _aggregate("SUM", _division(self.rng, t, None))
        inner += f", {agg.sql} AS measure"
        proj.append(_Expr("SUM(x.measure) AS sm_measure", agg.kind, agg.decimal_tolerance))
        inner += f" FROM {t.name} GROUP BY {inner_cols}"
        clauses = self._group_clauses([f"x.{outer_key.name}"])
        sql = f"SELECT {', '.join(e.sql for e in proj)} FROM ({inner}) x {clauses}"
        return self._finish(sql, "derived", proj, ordered="ORDER BY" in clauses)

    def shape_decimal_stress(self) -> PQGeneratedQuery:
        t, alias = self._table()
        dec = next((c for c in t.columns if _kind(c) == "decimal"), None)
        if dec is None:
            return self.shape_aggregate()
        groups = self._groups(t)
        proj = [_column(c, alias).named(f"g_{c.name}") for c in groups]
        proj.append(_Expr("COUNT(*) AS cnt", "int"))
        arg = _division(self.rng, t, alias)
        proj += [_aggregate("SUM", arg).named("s_dec"), _aggregate("AVG", arg).named("a_dec"),
                 _aggregate("MIN", _column(dec, alias)).named("mn_dec"),
                 _aggregate("MAX", _column(dec, alias)).named("mx_dec")]
        clauses = self._group_clauses([_col_ref(c, alias) for c in groups])
        sql = (f"SELECT {', '.join(e.sql for e in proj)} FROM {t.name} {alias} "
               f"{self._where(t, alias)} {clauses}")
        return self._finish(sql, "decimal_stress", proj, ordered="ORDER BY" in clauses)

    def shape_nested_agg(self) -> PQGeneratedQuery:
        t, _ = self._table()
        keys = self._groups(t)
        if not keys:
            return self.shape_scan()
        inner_proj = [f"{c.name} AS g_{c.name}" for c in keys] + ["COUNT(*) AS cnt"]
        outer_key = self.rng.choice(keys)
        proj = [_Expr(f"x.g_{outer_key.name} AS g", _kind(outer_key)),
                _Expr("SUM(x.cnt) AS total_cnt", "decimal"), _Expr("MAX(x.cnt) AS mx_cnt", "int")]
        agg = _aggregate("SUM", _division(self.rng, t, None))
        inner_proj.append(f"{agg.sql} AS s_dec")
        proj.append(_Expr("SUM(x.s_dec) AS total_s_dec", agg.kind, agg.decimal_tolerance))
        inner = (f"SELECT {', '.join(inner_proj)} FROM {t.name} "
                 f"GROUP BY {', '.join(c.name for c in keys)}")
        clauses = self._group_clauses([f"x.g_{outer_key.name}"])
        sql = f"SELECT {', '.join(e.sql for e in proj)} FROM ({inner}) x {clauses}"
        return self._finish(sql, "nested_agg", proj, ordered="ORDER BY" in clauses)


_SHAPES = (
    "scan", "aggregate", "aggregate", "decimal_stress", "decimal_stress",
    "join", "join_multi", "self_join", "nested_agg", "nested_agg",
    "subquery_in", "union", "derived", "distinct",
)
SUPPORTED_SHAPES: tuple[str, ...] = tuple(dict.fromkeys(_SHAPES))


class PQGenerator:
    """Seed-reproducible PQ SELECTs with one construction path for every attempt.

    Each shape includes scan computation from its first attempt while retaining
    supported predicates, ordered LIMIT and bounded joins. The input describes
    materializer-style InnoDB tables, each with its implicit BIGINT primary id.
    Unsupported column types reject the schema before any clause can use them.
    Only the engine's EXPLAIN/runtime evidence can establish PQ engagement.
    """

    def __init__(self, schema: PQSchema, *, seed: int = 1) -> None:
        for table in schema.tables:
            for column in table.columns:
                if _kind(column) not in _SUPPORTED_KINDS:
                    raise ValueError(f"Unsupported PQ column {table.name}.{column.name}: "
                                     f"{column.kind}")
        self._schema = schema
        self._seed = seed  # Retain constructor compatibility; generate's seed is authoritative.

    def generate(self, seed: int, shape: str | None = None,
                 pq_friendly: bool = False) -> PQGeneratedQuery:
        # Compatibility argument only: False can no longer select ordinary SQL
        # and True does not drop supported operators or rewrite the same seed.
        if not self._schema.tables:
            raise ValueError("PQ generation requires at least one table")
        rng = random.Random(seed)
        if shape is None:
            shape = rng.choice(_SHAPES)
        if shape not in _SHAPES:
            raise ValueError(f"Unknown PQ query shape: {shape}")
        builder = _QueryBuilder(rng, self._schema)
        q: PQGeneratedQuery = getattr(builder, f"shape_{shape}")()
        # Preserve the actual fallback builder's shape and its expression metadata.
        return replace(q, sql=" ".join(q.sql.split()), seed=seed)


__all__ = ["COUNT_ONLY", "EXACT", "MULTISET", "SUPPORTED_SHAPES", "PQGeneratedQuery", "PQGenerator"]
