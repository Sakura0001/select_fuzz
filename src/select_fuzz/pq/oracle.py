"""PQ result comparison, including the source spec's precision/order exceptions.

Raw values are compared without string rounding or numeric coercion. Complete
numeric type metadata must agree. FLOAT (4) uses its configured epsilon only
when both descriptions identify FLOAT; other float metadata uses DOUBLE bounds.
Decimal tolerance is opt-in per expression column, never inferred from numeric
metadata. Accepted numeric differences are PRECISION_VARIANCE, with unrounded
evidence; MATCH has no value differences under the selected contract
(count_only excludes row values).

Multisets retain duplicate counts. Exact rows seed a capacitated bipartite
matching, partitioned by exact cells, and augmenting paths may reassign even
those exact pairs: numerical closeness is not transitive. multiset_budget caps
candidate/cell checks and graph/path visits, including order diagnosis. Linear
row validation, hashing, grouping and exact cancellation are outside this
budget. Exhaustion yields INCONCLUSIVE, never a verdict about partial work.
"""

from __future__ import annotations

import math
from collections import Counter, deque
from dataclasses import dataclass, replace
from decimal import Decimal
from fractions import Fraction
from typing import Any

from mysql.connector.constants import FieldType

from select_fuzz.pq.config import Tolerance
from select_fuzz.pq.connection import QueryResult

FLOAT_CODE = 4
DOUBLE_CODE = 5
DECIMAL_CODES = frozenset({0, 246})
NUMERIC_CODES = frozenset(FieldType.get_number_types())

Row = tuple[Any, ...]
CellKey = tuple[type, Any]
RowKey = tuple[CellKey, ...]
_NAN = object()
_TOLERANT = object()


@dataclass(frozen=True, slots=True)
class Diff:
    """An actual pair of rows; index is the PQ row index, column is optional.

    In multiset mode the SEQ row may have another index (included in detail).
    kind is null, string, numeric, order or value for newly produced evidence.
    The original three-argument constructor and render format remain supported.
    """

    index: int
    pq: tuple[str, ...]
    seq: tuple[str, ...]
    kind: str = ""
    column: int | None = None

    def render(self) -> str:
        return f"  row[{self.index}] PQ ={self.pq}\n  row[{self.index}] SEQ={self.seq}"


@dataclass(frozen=True, slots=True)
class CompareOutcome:
    """MATCH, PRECISION_VARIANCE, INCONCLUSIVE, or a substantive mismatch.

    Substantive verdicts are MISMATCH, ERROR_PARITY and ROW_COUNT. An ORDER
    anomaly is MISMATCH with first_diff.kind == 'order'.
    """

    verdict: str
    detail: str
    first_diff: Diff | None = None


def _cell_key(value: Any) -> str:
    """Display only: retain every digit and distinguish text from numbers."""
    if value is None:
        return "\x00NULL"
    if isinstance(value, float):
        return f"f:{value!r}"
    if isinstance(value, Decimal):
        return f"d:{value}"
    if isinstance(value, str):
        return f"v:{value!r}"
    return f"v:{value}"


def _exact_key(value: Any) -> CellKey:
    # Only known byte buffers are converted. bytes(integer) allocates memory
    # and can turn a genuine type mismatch into an apparent equality.
    if isinstance(value, (bytes, bytearray)):
        return bytes, bytes(value)
    if isinstance(value, float) and math.isnan(value):
        return float, _NAN  # Retain the existing NaN/NaN policy.
    if isinstance(value, Decimal) and value.is_nan():
        return Decimal, object()  # Decimal NaNs (including sNaN) are not equal.
    return type(value), value


def _row_key(row: Row) -> RowKey:
    return tuple(_exact_key(value) for value in row)


def _make_diff(index: int, left: Row, right: Row, column: int | None = None) -> Diff:
    if column is None:
        column = next((i for i, (a, b) in enumerate(zip(left, right))
                       if _exact_key(a) != _exact_key(b)), None)
    kind = "value"
    if column is not None:
        a, b = left[column], right[column]
        if a is None or b is None:
            kind = "null"
        elif isinstance(a, (str, bytes, bytearray)) or isinstance(b, (str, bytes, bytearray)):
            kind = "string"
        elif isinstance(a, (int, float, Decimal)) or isinstance(b, (int, float, Decimal)):
            kind = "numeric"
    return Diff(index, tuple(map(_cell_key, left)), tuple(map(_cell_key, right)), kind, column)


@dataclass(frozen=True)
class _Comparison:
    type_codes: tuple[int, ...]
    tolerance: Tolerance
    decimal_columns: frozenset[int]

    def float_bounds(self, column: int) -> tuple[float, float]:
        tol = self.tolerance
        if self.type_codes[column] == FLOAT_CODE:
            return tol.float_absolute, tol.float_relative
        return tol.double_absolute, tol.double_relative

    def cells_equal(self, left: Any, right: Any, column: int) -> bool:
        if _exact_key(left) == _exact_key(right):
            return True
        # Neither metadata nor decimal_columns permits converting raw values.
        if type(left) is not type(right):
            return False
        if isinstance(left, float):
            absolute, relative = self.float_bounds(column)
            return math.isclose(left, right, abs_tol=absolute, rel_tol=relative)
        if isinstance(left, Decimal) and column in self.decimal_columns:
            if not left.is_finite() or not right.is_finite():
                return False
            # Fraction(Decimal) preserves the full coefficient and exponent.
            # Even subtraction/abs/multiplication on Decimal would otherwise
            # depend on the caller's precision, rounding mode and traps.
            a, b = Fraction(left), Fraction(right)
            return abs(a - b) <= max(
                Fraction(self.tolerance.decimal_absolute),
                Fraction(self.tolerance.decimal_relative) * max(abs(a), abs(b)),
            )
        return False

    def bucket_key(self, key: RowKey) -> RowKey:
        bucket = []
        for i, (kind, value) in enumerate(key):
            tolerant = False
            if kind is float and value is not _NAN and math.isfinite(value):
                tolerant = any(self.float_bounds(i))
            elif kind is Decimal and i in self.decimal_columns and isinstance(value, Decimal):
                tolerant = value.is_finite() and bool(
                    self.tolerance.decimal_absolute or self.tolerance.decimal_relative
                )
            bucket.append((kind, _TOLERANT if tolerant else value))
        return tuple(bucket)


class _BudgetExceeded(Exception):
    pass


@dataclass
class _Work:
    remaining: int

    def spend(self, amount: int = 1) -> None:
        if self.remaining < amount:
            raise _BudgetExceeded
        self.remaining -= amount


@dataclass
class _Group:
    row: Row
    key: RowKey
    positions: list[int]


def _group_rows(rows: tuple[Row, ...], comparison: _Comparison) -> dict[RowKey, list[_Group]]:
    buckets: dict[RowKey, dict[RowKey, _Group]] = {}
    for index, row in enumerate(rows):
        key = _row_key(row)
        bucket = buckets.setdefault(comparison.bucket_key(key), {})
        if key in bucket:
            bucket[key].positions.append(index)
        else:
            bucket[key] = _Group(row, key, [index])
    return {key: list(groups.values()) for key, groups in buckets.items()}


class _BucketMatching:
    """Integral flow over distinct rows; capacities are duplicate counts."""

    def __init__(self, left: list[_Group], right: list[_Group],
                 comparison: _Comparison, work: _Work) -> None:
        self.left, self.right = left, right
        self.comparison, self.work = comparison, work
        self.left_free = [len(group.positions) for group in left]
        self.right_free = [len(group.positions) for group in right]
        self.incoming: list[dict[int, int]] = [{} for _ in right]
        self.edges: dict[int, list[int]] = {}
        right_index = {group.key: j for j, group in enumerate(right)}
        for i, group in enumerate(left):
            j = right_index.get(group.key)
            if j is not None:
                count = min(self.left_free[i], self.right_free[j])
                self.incoming[j][i] = count
                self.left_free[i] -= count
                self.right_free[j] -= count

    def neighbors(self, i: int) -> list[int]:
        if i not in self.edges:
            edges = []
            for j, group in enumerate(self.right):
                self.work.spend()
                for column, (a, b) in enumerate(zip(self.left[i].row, group.row, strict=True)):
                    self.work.spend()
                    if not self.comparison.cells_equal(a, b, column):
                        break
                else:
                    edges.append(j)
            self.edges[i] = edges
        return self.edges[i]

    def augment(self, root: int) -> bool:
        # BFS is iterative: long alternating paths cannot exhaust Python's
        # recursion limit. Reverse edges allow reassignment of exact matches.
        queue = deque([root])
        left_parent = {root: -1}
        right_parent: dict[int, int] = {}
        end = None
        while queue and end is None:
            i = queue.popleft()
            for j in self.neighbors(i):
                self.work.spend()
                if j in right_parent:
                    continue
                right_parent[j] = i
                if self.right_free[j]:
                    end = j
                    break
                for previous, count in self.incoming[j].items():
                    self.work.spend()
                    if count and previous not in left_parent:
                        left_parent[previous] = j
                        queue.append(previous)
        if end is None:
            return False

        count = min(self.left_free[root], self.right_free[end])
        path = []
        j = end
        while True:
            self.work.spend(2)  # Charge both reconstruction and application.
            i = right_parent[j]
            path.append((i, j))
            if i == root:
                break
            j = left_parent[i]
            count = min(count, self.incoming[j][i])
        for i, j in path:
            self.incoming[j][i] = self.incoming[j].get(i, 0) + count
            if i != root:
                old = self.incoming[left_parent[i]]
                old[i] -= count
                if not old[i]:
                    del old[i]
        self.left_free[root] -= count
        self.right_free[end] -= count
        return True

    def run(self) -> None:
        for i in range(len(self.left)):
            while self.left_free[i] and self.augment(i):
                pass

    def precision_diff(self) -> tuple[Diff, int] | None:
        offsets = [0] * len(self.left)
        for j, incoming in enumerate(self.incoming):
            right_offset = 0
            for i, count in incoming.items():
                if self.left[i].key != self.right[j].key:
                    return (
                        _make_diff(self.left[i].positions[offsets[i]],
                                   self.left[i].row, self.right[j].row),
                        self.right[j].positions[right_offset],
                    )
                offsets[i] += count
                right_offset += count
        return None


def _multiset_compare(pq: QueryResult, seq: QueryResult, comparison: _Comparison) -> CompareOutcome:
    left_buckets = _group_rows(pq.rows, comparison)
    right_buckets = _group_rows(seq.rows, comparison)
    work = _Work(comparison.tolerance.multiset_budget)
    unmatched_left: list[tuple[int, Row]] = []
    unmatched_right: list[tuple[int, Row]] = []
    precision: tuple[Diff, int] | None = None
    buckets = dict.fromkeys(left_buckets)
    buckets.update(dict.fromkeys(right_buckets))
    try:
        for key in buckets:
            matching = _BucketMatching(left_buckets.get(key, []), right_buckets.get(key, []),
                                       comparison, work)
            matching.run()
            for groups, counts, unmatched in (
                (matching.left, matching.left_free, unmatched_left),
                (matching.right, matching.right_free, unmatched_right),
            ):
                for group, count in zip(groups, counts, strict=True):
                    if count:
                        unmatched.append((group.positions[len(group.positions) - count], group.row))
            if precision is None:
                precision = matching.precision_diff()
        if unmatched_left:
            i, left = min(unmatched_left, key=lambda entry: entry[0])
            j, right = min(unmatched_right, key=lambda entry: entry[0])
            column = None
            for position, (a, b) in enumerate(zip(left, right, strict=True)):
                work.spend()
                if not comparison.cells_equal(a, b, position):
                    column = position
                    break
            return CompareOutcome("MISMATCH", f"unmatched PQ row {i} vs SEQ row {j} (multiset)",
                                  _make_diff(i, left, right, column))
    except _BudgetExceeded:
        return CompareOutcome(
            "INCONCLUSIVE",
            f"multiset comparison budget exhausted ({comparison.tolerance.multiset_budget} "
            "work units); no conclusion from partial matching",
        )
    if precision is not None:
        diff, j = precision
        return CompareOutcome("PRECISION_VARIANCE",
                              f"numeric differences within tolerance: PQ row {diff.index} "
                              f"vs SEQ row {j} (multiset)", diff)
    return CompareOutcome("MATCH", "multisets exactly equal")


def _ordered_compare(pq: QueryResult, seq: QueryResult, comparison: _Comparison) -> CompareOutcome:
    precision = None
    mismatch = None
    for i, (left, right) in enumerate(zip(pq.rows, seq.rows, strict=True)):
        for column, (a, b) in enumerate(zip(left, right, strict=True)):
            if _exact_key(a) == _exact_key(b):
                continue
            if not comparison.cells_equal(a, b, column):
                mismatch = _make_diff(i, left, right, column)
                break
            if precision is None:
                precision = _make_diff(i, left, right, column)
        if mismatch is not None:
            break
    order_detail = "ORDER mismatch: same multiset, different row order"
    if mismatch is None:
        if precision is None:
            return CompareOutcome("MATCH", "ordered sets exactly equal")
        # A pure permutation is an order error even if neighboring numbers
        # happen to fall within epsilon when compared at the wrong positions.
        if Counter(map(_row_key, pq.rows)) != Counter(map(_row_key, seq.rows)):
            return CompareOutcome("PRECISION_VARIANCE", "ordered numeric differences within tolerance",
                                  precision)
        mismatch = precision
    else:
        unordered = _multiset_compare(pq, seq, comparison)
        if unordered.verdict == "INCONCLUSIVE":
            return unordered
        if unordered.verdict == "MISMATCH":
            return CompareOutcome("MISMATCH", f"ordered row {mismatch.index} differs", mismatch)
        if unordered.verdict == "PRECISION_VARIANCE" and unordered.first_diff is not None:
            order_detail += (f"; {unordered.detail}; PQ={unordered.first_diff.pq} "
                             f"SEQ={unordered.first_diff.seq}")
    return CompareOutcome("MISMATCH", order_detail, replace(mismatch, kind="order"))


def compare_result_sets(
    pq: QueryResult,
    seq: QueryResult,
    *,
    compare_mode: str,
    tolerance: Tolerance,
    decimal_columns: tuple[int, ...] = (),
) -> CompareOutcome:
    """Compare complete results under an explicit ordering contract.

    exact requires a total ORDER BY; multiset ignores order, preserving counts;
    count_only checks counts for implementation-defined LIMIT selection. Column
    names and complete numeric type metadata are checked in every mode.
    Invalid/missing modes are errors. Only declared Decimal result
    positions admit expression tolerance. The caller retains raw QueryResults;
    first_diff keeps representative, unrounded evidence of accepted differences.

    Error parity precedes completeness checks because execution/fetch errors
    carry incomplete rows and warnings. Successful partial results, fallback,
    unknown warning evidence, and exhausted matching budgets are INCONCLUSIVE.
    """
    if compare_mode not in ("exact", "multiset", "count_only"):
        raise ValueError("compare_mode must be 'exact', 'multiset', or 'count_only'")

    if pq.status != seq.status:
        return CompareOutcome("ERROR_PARITY",
                              f"PQ={pq.status}({pq.error}) vs SEQ={seq.status}({seq.error})")
    if pq.status == "ERROR":
        same = pq.errno > 0 and pq.errno == seq.errno
        return CompareOutcome("MATCH" if same else "ERROR_PARITY",
                              f"both ERROR: PQ[{pq.errno}]='{pq.error}' SEQ[{seq.errno}]='{seq.error}'")

    for label, result in (("PQ", pq), ("SEQ", seq)):
        if not result.complete or (result.total_rows is not None
                                   and result.total_rows != len(result.rows)):
            return CompareOutcome("INCONCLUSIVE", f"{label} result incomplete: "
                                  f"retained={len(result.rows)}, total_rows={result.total_rows}")
        if result.fallback:
            return CompareOutcome("INCONCLUSIVE", f"{label} execution fell back to serial")
        if not result.warnings_complete:
            return CompareOutcome("INCONCLUSIVE", f"{label} warning evidence incomplete")

    if pq.columns != seq.columns:
        return CompareOutcome("MISMATCH", f"columns differ: PQ={pq.columns} SEQ={seq.columns}")
    width = len(pq.columns)
    if any(type(column) is not int or not 0 <= column < width for column in decimal_columns):
        raise ValueError("decimal_columns must contain valid zero-based integer column indexes")
    for label, result in (("PQ", pq), ("SEQ", seq)):
        if any(len(row) != width for row in result.rows):
            return CompareOutcome("INCONCLUSIVE", f"{label} row width disagrees with column metadata")
    if len(pq.rows) != len(seq.rows):
        return CompareOutcome("ROW_COUNT", f"row count: PQ={len(pq.rows)} SEQ={len(seq.rows)}")
    if len(pq.type_codes) == len(seq.type_codes) == width:
        for column, (pq_code, seq_code) in enumerate(zip(pq.type_codes, seq.type_codes, strict=True)):
            if pq_code != seq_code and (pq_code in NUMERIC_CODES or seq_code in NUMERIC_CODES):
                diff = None
                if pq.rows:
                    diff = replace(_make_diff(0, pq.rows[0], seq.rows[0], column), kind="numeric")
                return CompareOutcome(
                    "MISMATCH", f"numeric column {column} type differs: PQ={pq_code} SEQ={seq_code}",
                    diff,
                )
    if compare_mode == "count_only":
        return CompareOutcome("MATCH", "count_only: row counts equal (values not compared)")

    type_codes = tuple(
        FLOAT_CODE if (i < len(pq.type_codes) and i < len(seq.type_codes)
                       and pq.type_codes[i] == seq.type_codes[i] == FLOAT_CODE) else DOUBLE_CODE
        for i in range(width)
    )
    comparison = _Comparison(type_codes, tolerance, frozenset(decimal_columns))
    if compare_mode == "exact":
        return _ordered_compare(pq, seq, comparison)
    return _multiset_compare(pq, seq, comparison)


__all__ = ["CompareOutcome", "Diff", "compare_result_sets"]
