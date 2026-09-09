from __future__ import annotations

import math
import random
from collections import Counter
from decimal import Decimal, localcontext
from itertools import permutations

import pytest

from select_fuzz.pq.config import Tolerance
from select_fuzz.pq.connection import QueryResult
from select_fuzz.pq.oracle import Diff, compare_result_sets


def result(rows=(), *, type_codes=None, columns=None, **kwargs):
    rows = tuple(tuple(row) for row in rows)
    width = len(rows[0]) if rows else 1
    return QueryResult(
        status=kwargs.pop("status", "OK"),
        rows=rows,
        columns=tuple(f"c{i}" for i in range(width)) if columns is None else columns,
        type_codes=(5,) * width if type_codes is None else type_codes,
        **kwargs,
    )


def compare(left, right, *, mode="multiset", tolerance=None, type_codes=None, **kwargs):
    return compare_result_sets(
        result(left, type_codes=type_codes),
        result(right, type_codes=type_codes),
        compare_mode=mode,
        tolerance=tolerance or Tolerance(),
        **kwargs,
    )


@pytest.mark.parametrize("mode", ["exact", "multiset"])
def test_decimal_exactness_does_not_round_past_context_precision(mode):
    a = Decimal("123456789012345678901234567890.123456789")
    b = Decimal("123456789012345678901234567890.123456790")
    with localcontext() as ctx:
        ctx.prec = 6
        outcome = compare([(a,)], [(b,)], mode=mode, type_codes=(246,))
    assert outcome.verdict == "MISMATCH"
    assert outcome.first_diff.pq == (f"d:{a}",)
    assert outcome.first_diff.seq == (f"d:{b}",)


@pytest.mark.parametrize("mode", ["exact", "multiset"])
def test_equal_decimals_with_different_scales_are_exact_matches(mode):
    with localcontext() as ctx:
        ctx.prec = 3
        outcome = compare(
            [(Decimal("123456789012345678901234567890.123400"),)],
            [(Decimal("123456789012345678901234567890.1234"),)],
            mode=mode,
        )
    assert outcome.verdict == "MATCH"
    assert outcome.first_diff is None


@pytest.mark.parametrize("mode", ["exact", "multiset"])
@pytest.mark.parametrize(
    "code,left,right",
    [(4, 0.0, 9e-7), (4, 1000.0, 1000.009), (5, 0.0, 9e-13), (5, 1.0, 1.0000000009)],
)
def test_in_tolerance_float_differences_are_preserved_as_precision_variance(
    mode, code, left, right
):
    outcome = compare([(left,)], [(right,)], mode=mode, type_codes=(code,))
    assert outcome.verdict == "PRECISION_VARIANCE"
    assert outcome.first_diff.pq == (f"f:{left!r}",)
    assert outcome.first_diff.seq == (f"f:{right!r}",)
    assert outcome.first_diff.kind == "numeric"
    assert outcome.first_diff.column == 0


@pytest.mark.parametrize("mode", ["exact", "multiset"])
@pytest.mark.parametrize(
    "code,left,right",
    [(4, 0.0, 1.1e-6), (4, 1000.0, 1000.02), (5, 0.0, 1.1e-12), (5, 1.0, 1.000000002)],
)
def test_outside_tolerance_is_a_mismatch(mode, code, left, right):
    assert compare([(left,)], [(right,)], mode=mode, type_codes=(code,)).verdict == "MISMATCH"


@pytest.mark.parametrize("mode", ["exact", "multiset"])
def test_rounded_float_string_collision_cannot_hide_a_difference(mode):
    tolerance = Tolerance(double_absolute=0.0, double_relative=0.0)
    outcome = compare(
        [(1.00000000001,)], [(1.00000000002,)], mode=mode, tolerance=tolerance
    )
    assert outcome.verdict == "MISMATCH"
    assert outcome.first_diff.pq != outcome.first_diff.seq


@pytest.mark.parametrize("mode", ["exact", "multiset"])
@pytest.mark.parametrize(
    "left,right",
    [
        (None, 0), (None, "\x00NULL"), ("abc", "ab"), (1, "1"),
        (1.0, "1.0"), (1.0, b"1.0"), (Decimal("2"), "2"),
        (Decimal("0.1"), 0.1), (2**53 + 1, float(2**53)),
        (b"\x00\x00", 2), (b"abc", "abc"), (2**60, 2**60 + 1),
    ],
)
def test_raw_types_and_exact_values_cannot_be_coerced_by_numeric_metadata(mode, left, right):
    assert compare([(left,)], [(right,)], mode=mode, type_codes=(4,)).verdict == "MISMATCH"


@pytest.mark.parametrize("mode", ["exact", "multiset"])
def test_bytes_and_bytearray_compare_without_converting_other_types(mode):
    assert compare([(b"\x00a",)], [(bytearray(b"\x00a"),)], mode=mode).verdict == "MATCH"


def test_diff_index_is_the_row_index_and_column_is_separate():
    outcome = compare([(0, 0), (1, 0), (2, 3)], [(0, 0), (1, 0), (2, 4)], mode="exact")
    assert outcome.verdict == "MISMATCH"
    assert outcome.first_diff.index == 2
    assert outcome.first_diff.column == 1
    assert outcome.first_diff.kind == "numeric"


@pytest.mark.parametrize("left,right,kind", [(None, "x", "null"), ("abc", "ab", "string")])
def test_diff_kind_identifies_null_and_string_changes(left, right, kind):
    outcome = compare([(left,)], [(right,)])
    assert outcome.first_diff.kind == kind


def test_unordered_diff_reports_unmatched_rows_after_exact_cancellation():
    outcome = compare([(1,), (2,), (3,)], [(3,), (1,), (4,)])
    assert outcome.verdict == "MISMATCH"
    assert outcome.first_diff.index == 1
    assert outcome.first_diff.pq == ("v:2",)
    assert outcome.first_diff.seq == ("v:4",)


@pytest.mark.parametrize(
    "left,right,absolute",
    [
        ([(0.5,), (0.5,), (0.0,)], [(0.4,), (0.6,), (1.0,)], 0.5),
        ([(0.0,), (1.0,)], [(1.0,), (2.0,)], 1.0),
        ([(0.0, 1.0), (0.1, 0.0)] * 2, [(0.0, 0.0), (0.1, 1.0)] * 2, 0.1),
    ],
)
def test_multiset_matching_can_reassign_ambiguous_and_exact_pairs(left, right, absolute):
    tolerance = Tolerance(double_absolute=absolute, double_relative=0.0)
    for pq_rows, seq_rows in [(left, right), (right, left), (left[::-1], right[::-1])]:
        outcome = compare(pq_rows, seq_rows, tolerance=tolerance)
        assert outcome.verdict == "PRECISION_VARIANCE"
        assert outcome.first_diff is not None
        assert outcome.first_diff.pq != outcome.first_diff.seq


def test_multiset_matching_preserves_duplicate_counts():
    tolerance = Tolerance(double_absolute=0.1, double_relative=0.0)
    outcome = compare([(0.0,), (0.0,), (1.0,)], [(0.0,), (1.0,), (1.0,)], tolerance=tolerance)
    assert outcome.verdict == "MISMATCH"
    assert outcome.first_diff.pq == ("f:0.0",)
    assert outcome.first_diff.seq == ("f:1.0",)


@pytest.mark.parametrize("mode", ["exact", "multiset", "count_only"])
def test_row_count_mismatch(mode):
    assert compare([(1,)], [(1,), (1,)], mode=mode).verdict == "ROW_COUNT"


@pytest.mark.parametrize("mode", ["exact", "multiset", "count_only"])
def test_empty_results_match(mode):
    assert compare([], [], mode=mode).verdict == "MATCH"


@pytest.mark.parametrize("values", [[1, 2], [1.0, 1.0000000001]])
def test_order_anomaly_is_identified_even_when_swapped_numbers_are_within_tolerance(values):
    rows = [(value,) for value in values]
    assert compare(rows, rows[::-1]).verdict == "MATCH"
    outcome = compare(rows, rows[::-1], mode="exact")
    assert outcome.verdict == "MISMATCH"
    assert outcome.first_diff.kind == "order"
    assert "order" in outcome.detail.lower()


@pytest.mark.parametrize("mode", [None, "", "ordered", "MULTISET", "bogus"])
def test_invalid_comparison_mode_is_rejected_even_for_empty_results(mode):
    with pytest.raises(ValueError, match="compare_mode"):
        compare([], [], mode=mode)


def test_missing_comparison_mode_has_a_clear_error():
    with pytest.raises(TypeError, match="compare_mode"):
        compare_result_sets(result(), result(), tolerance=Tolerance())


@pytest.mark.parametrize(
    "pq_status,pq_errno,seq_status,seq_errno,expected",
    [
        ("OK", 0, "ERROR", 123, "ERROR_PARITY"),
        ("ERROR", 123, "OK", 0, "ERROR_PARITY"),
        ("ERROR", 123, "ERROR", 124, "ERROR_PARITY"),
        ("ERROR", 0, "ERROR", 0, "ERROR_PARITY"),
        ("ERROR", 123, "ERROR", 123, "MATCH"),
    ],
)
def test_error_parity_uses_known_errno_and_allows_worker_message_variations(
    pq_status, pq_errno, seq_status, seq_errno, expected
):
    outcome = compare_result_sets(
        result(status=pq_status, errno=pq_errno, error="PQ worker error",
               complete=pq_status == "OK", warnings_complete=pq_status == "OK"),
        result(status=seq_status, errno=seq_errno, error="serial error",
               complete=seq_status == "OK", warnings_complete=seq_status == "OK"),
        compare_mode="exact",
        tolerance=Tolerance(),
    )
    assert outcome.verdict == expected


def test_diff_existing_constructor_and_render_remain_compatible():
    diff = Diff(2, ("a",), ("b",))
    assert diff.render() == "  row[2] PQ =('a',)\n  row[2] SEQ=('b',)"


@pytest.mark.parametrize("mode", ["exact", "multiset"])
@pytest.mark.parametrize("delta,expected", [("0.000000001", "PRECISION_VARIANCE"),
                                           ("0.00000000100000001", "MISMATCH")])
def test_decimal_expression_absolute_boundary_is_context_independent(mode, delta, expected):
    # Construct both exact operands before reducing the ambient context.
    left = Decimal("123456789012345678901234567890.00000000000000000")
    right = Decimal("123456789012345678901234567890." + delta.split(".")[1])
    with localcontext() as ctx:
        ctx.prec = 3
        outcome = compare([(left,)], [(right,)], mode=mode, decimal_columns=(0,))
    assert outcome.verdict == expected
    assert outcome.first_diff.pq == (f"d:{left}",)
    assert outcome.first_diff.seq == (f"d:{right}",)


@pytest.mark.parametrize("mode", ["exact", "multiset"])
@pytest.mark.parametrize("right,expected", [("1001", "PRECISION_VARIANCE"), ("1001.002", "MISMATCH")])
def test_decimal_expression_relative_boundary(mode, right, expected):
    tolerance = Tolerance(decimal_absolute=Decimal(0), decimal_relative=Decimal("0.001"))
    with localcontext() as ctx:
        ctx.prec = 2
        outcome = compare(
            [(Decimal("1000"),)], [(Decimal(right),)], mode=mode,
            tolerance=tolerance, decimal_columns=(0,),
        )
    # Relative tolerance uses max(abs(left), abs(right)), as math.isclose does.
    assert outcome.verdict == expected


@pytest.mark.parametrize("mode", ["exact", "multiset"])
def test_only_declared_decimal_columns_allow_tolerance(mode):
    left = Decimal("1")
    right = Decimal("1.0000000001")
    assert compare([(left,)], [(right,)], mode=mode).verdict == "MISMATCH"
    outcome = compare([(left, left)], [(right, right)], mode=mode, decimal_columns=(1,))
    assert outcome.verdict == "MISMATCH"
    assert outcome.first_diff.column == 0
    for a, b in [(1, 2), (Decimal("0.1"), 0.1), ("1.0", "1.0000000001")]:
        assert compare([(a,)], [(b,)], mode=mode, decimal_columns=(0,)).verdict == "MISMATCH"


@pytest.mark.parametrize("columns", [(-1,), (1,), (True,), (0.5,)])
def test_invalid_decimal_column_declarations_have_a_clear_error(columns):
    with pytest.raises(ValueError, match="decimal_columns"):
        compare([(Decimal(1),)], [(Decimal(1),)], decimal_columns=columns)


@pytest.mark.parametrize("mode", ["exact", "multiset", "count_only"])
@pytest.mark.parametrize(
    "pq_metadata,seq_metadata",
    [
        ({"complete": False}, {}), ({}, {"complete": False}),
        ({"complete": False, "total_rows": 20}, {"complete": False, "total_rows": 20}),
        ({"total_rows": 2}, {}), ({}, {"total_rows": 2}),
    ],
)
def test_incomplete_results_never_compare_partial_prefixes(mode, pq_metadata, seq_metadata):
    for right in [[(1,)], [(9,), (10,), (11,)]]:
        outcome = compare_result_sets(
            result([(1,)], **pq_metadata), result(right, **seq_metadata),
            compare_mode=mode, tolerance=Tolerance(),
        )
        assert outcome.verdict == "INCONCLUSIVE"
        assert outcome.first_diff is None
        assert "incomplete" in outcome.detail.lower()


@pytest.mark.parametrize("metadata", [{"fallback": True}, {"warnings_complete": False}])
def test_fallback_or_missing_warning_evidence_is_inconclusive(metadata):
    for pq, seq in [(result([(1,)], **metadata), result([(1,)])),
                    (result([(1,)]), result([(1,)], **metadata))]:
        outcome = compare_result_sets(pq, seq, compare_mode="exact", tolerance=Tolerance())
        assert outcome.verdict == "INCONCLUSIVE"


def test_complete_counts_and_warning_multiplicity_do_not_hide_a_match():
    warning = ("Warning", 1265, "Data truncated")
    outcome = compare_result_sets(
        result([(1,)], total_rows=1, warnings=(warning,) * 4),
        result([(1,)], total_rows=1, warnings=(warning,)),
        compare_mode="exact", tolerance=Tolerance(),
    )
    assert outcome.verdict == "MATCH"


def test_count_only_is_an_explicit_count_contract():
    assert compare([(None,)], [("different",)], mode="count_only").verdict == "MATCH"


@pytest.mark.parametrize("mode", ["exact", "multiset"])
def test_column_names_and_row_widths_are_checked(mode):
    outcome = compare_result_sets(
        result([(1,)], columns=("a",)), result([(1,)], columns=("b",)),
        compare_mode=mode, tolerance=Tolerance(),
    )
    assert outcome.verdict == "MISMATCH"
    outcome = compare([(1, 2)], [(1,)], mode=mode)
    assert outcome.verdict == "MISMATCH"
    # Even if both sides have the same malformed row, it is not a match.
    outcome = compare_result_sets(
        result([(1,)], columns=("a", "b")), result([(1,)], columns=("a", "b")),
        compare_mode=mode, tolerance=Tolerance(),
    )
    assert outcome.verdict == "INCONCLUSIVE"


@pytest.mark.parametrize("mode", ["exact", "multiset"])
def test_numeric_metadata_selection_is_symmetric_and_defaults_to_double(mode):
    for pq_codes, seq_codes in [((4,), (5,)), ((5,), (4,)), ((), ()), ((4,), ())]:
        outcome = compare_result_sets(
            result([(1.0,)], type_codes=pq_codes), result([(1.000001,)], type_codes=seq_codes),
            compare_mode=mode, tolerance=Tolerance(),
        )
        assert outcome.verdict == "MISMATCH"


@pytest.mark.parametrize("mode", ["exact", "multiset"])
@pytest.mark.parametrize("value", [float("inf"), -float("inf"), float("nan"), -0.0])
def test_equal_special_floats_match(mode, value):
    assert compare([(value,)], [(value,)], mode=mode).verdict == "MATCH"


@pytest.mark.parametrize("mode", ["exact", "multiset"])
@pytest.mark.parametrize("left,right", [(math.inf, 1.0), (math.inf, -math.inf), (math.nan, 0.0)])
def test_special_floats_do_not_match_finite_or_other_special_values(mode, left, right):
    assert compare([(left,)], [(right,)], mode=mode).verdict == "MISMATCH"


def test_multiset_verdict_agrees_with_exhaustive_small_bipartite_matching():
    rng = random.Random(20260908)
    tolerance = Tolerance(double_absolute=0.25, double_relative=0.0)
    for _ in range(120):
        size = rng.randrange(1, 6)
        left = [(rng.choice([0.0, 0.25, 0.5]), rng.choice([0.0, 0.25, 0.5]))
                for _ in range(size)]
        right = [(rng.choice([0.0, 0.25, 0.5]), rng.choice([0.0, 0.25, 0.5]))
                 for _ in range(size)]
        feasible = any(
            all(all(math.isclose(a, b, abs_tol=0.25, rel_tol=0.0) for a, b in zip(lr, rr))
                for lr, rr in zip(left, ordering))
            for ordering in permutations(right)
        )
        expected = "MATCH" if Counter(left) == Counter(right) else (
            "PRECISION_VARIANCE" if feasible else "MISMATCH"
        )
        outcome = compare(left, right, tolerance=tolerance)
        assert outcome.verdict == expected, (left, right, outcome)


@pytest.mark.timeout(3)
def test_large_exact_multiset_uses_cancellation_without_tolerance_search():
    rows = [(float(i),) for i in range(20_000)]
    outcome = compare(rows, rows[::-1], tolerance=Tolerance(multiset_budget=1))
    assert outcome.verdict == "MATCH"


@pytest.mark.timeout(3)
def test_large_duplicate_counts_do_not_expand_into_a_dense_graph():
    outcome = compare([(0.0,)] * 20_000, [(9e-13,)] * 20_000,
                      tolerance=Tolerance(multiset_budget=50))
    assert outcome.verdict == "PRECISION_VARIANCE"


@pytest.mark.timeout(3)
def test_exact_columns_partition_the_numeric_matching_work():
    left = [(i, f"key{i}", None, 0.0) for i in range(2_000)]
    right = [(i, f"key{i}", None, 9e-13) for i in reversed(range(2_000))]
    outcome = compare(left, right, tolerance=Tolerance(multiset_budget=20_000))
    assert outcome.verdict == "PRECISION_VARIANCE"


@pytest.mark.timeout(3)
@pytest.mark.parametrize("mode", ["exact", "multiset"])
def test_dense_numeric_matching_stops_at_work_budget(monkeypatch, mode):
    from select_fuzz.pq import oracle

    calls = 0
    original = math.isclose

    def measured(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(oracle.math, "isclose", measured)
    left = [(float(i),) for i in range(2_000)]
    right = [(float(i) + 0.1,) for i in reversed(range(2_000))]
    outcome = compare(left, right, mode=mode,
                      tolerance=Tolerance(double_absolute=0.2, multiset_budget=500))
    assert outcome.verdict == "INCONCLUSIVE"
    assert "budget" in outcome.detail.lower()
    assert outcome.first_diff is None
    assert calls <= 501  # At most one preliminary ordered comparison.


@pytest.mark.parametrize("mode", ["exact", "multiset"])
@pytest.mark.parametrize("left,right,kind", [(None, "x", "null"), ("abc", "ab", "string"),
                                           (1, 2, "numeric")])
def test_mismatch_evidence_skips_earlier_accepted_numeric_variance(mode, left, right, kind):
    outcome = compare([(0.0, left)], [(9e-13, right)], mode=mode)
    assert outcome.verdict == "MISMATCH"
    assert outcome.first_diff.column == 1
    assert outcome.first_diff.kind == kind


def test_multiset_mismatch_diagnosis_respects_the_cell_check_budget():
    outcome = compare([(0.0, None)], [(9e-13, "x")],
                      tolerance=Tolerance(multiset_budget=1))
    assert outcome.verdict == "INCONCLUSIVE"
    assert "budget" in outcome.detail
    assert outcome.first_diff is None


def test_order_diagnosis_retains_the_numeric_variance_used_to_match_rows():
    outcome = compare([(1, 0.0), (2, 0.0)], [(2, 9e-13), (1, 0.0)], mode="exact")
    assert outcome.verdict == "MISMATCH"
    assert outcome.first_diff.kind == "order"
    assert "tolerance" in outcome.detail
    assert "9e-13" in outcome.detail


def test_decimal_multiset_can_reassign_exact_pairs_and_preserves_duplicates():
    tolerance = Tolerance(decimal_absolute=Decimal(1), decimal_relative=Decimal(0))
    outcome = compare([(Decimal(0),), (Decimal(1),)] * 2,
                      [(Decimal(1),), (Decimal(2),)] * 2,
                      decimal_columns=(0,), tolerance=tolerance)
    assert outcome.verdict == "PRECISION_VARIANCE"
    assert outcome.first_diff.pq != outcome.first_diff.seq


@pytest.mark.parametrize("mode", ["exact", "multiset", "count_only"])
@pytest.mark.parametrize("pq_code,seq_code,value", [(4, 5, 1.0), (246, 5, 0.1),
                                                   (3, 8, 1), (5, 246, Decimal("0.1")),
                                                   (3, 8, None)])
def test_complete_numeric_metadata_divergence_is_visible_even_when_values_are_equal(
    mode, pq_code, seq_code, value
):
    outcome = compare_result_sets(
        result([(value,)], type_codes=(pq_code,)), result([(value,)], type_codes=(seq_code,)),
        compare_mode=mode, tolerance=Tolerance(), decimal_columns=(0,),
    )
    assert outcome.verdict == "MISMATCH"
    assert outcome.first_diff.kind == "numeric"
    assert outcome.first_diff.column == 0
    assert "type" in outcome.detail.lower()
    assert str(pq_code) in outcome.detail and str(seq_code) in outcome.detail


@pytest.mark.parametrize("mode", ["exact", "multiset", "count_only"])
def test_empty_results_do_not_conceal_complete_numeric_metadata_changes(mode):
    outcome = compare_result_sets(
        result(type_codes=(4,)), result(type_codes=(5,)),
        compare_mode=mode, tolerance=Tolerance(),
    )
    assert outcome.verdict == "MISMATCH"
    assert outcome.first_diff is None  # There is no actual row to report.


@pytest.mark.parametrize("mode", ["exact", "multiset"])
@pytest.mark.parametrize("pq_codes,seq_codes", [((4,), ()), ((), (4,)), ((), ())])
def test_missing_numeric_metadata_retains_double_tolerance(mode, pq_codes, seq_codes):
    outcome = compare_result_sets(
        result([(0.0,)], type_codes=pq_codes), result([(9e-13,)], type_codes=seq_codes),
        compare_mode=mode, tolerance=Tolerance(),
    )
    assert outcome.verdict == "PRECISION_VARIANCE"
