from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from itertools import permutations
import math

import pytest

from select_fuzz.config import NodeRole
from select_fuzz.domain.models import ColumnMeta, ErrorInfo, ExecutionStatus, NodeExecution
from select_fuzz.oracle import OracleVerdict, compare_three_nodes, compare_two_nodes
from select_fuzz.oracle.errors import OracleCapacityError, OracleInputError, normalize_error


INT = ColumnMeta("k", 3, False, False, False)
TEXT = ColumnMeta("v", 253, True, False, False)
DECIMAL = ColumnMeta("v", 246, True, False, False)
FLOAT = ColumnMeta("v", 4, True, False, False)
DOUBLE = ColumnMeta("v", 5, True, False, False)
BIT = ColumnMeta("v", 16, True, True, True)
JSON = ColumnMeta("v", 245, True, False, True)
GEOMETRY = ColumnMeta("v", 255, True, False, True)
SET = ColumnMeta("v", 254, True, False, False, flags=2048)


def _success(
    role: NodeRole,
    columns: tuple[ColumnMeta, ...],
    rows: tuple[tuple[object, ...], ...],
    *,
    warnings: tuple[str, ...] = (),
) -> NodeExecution:
    return NodeExecution.success(
        role=role,
        connection_id=10 + list(NodeRole).index(role),
        started_ns=1,
        ended_ns=2,
        columns=columns,
        rows=rows,
        warnings=warnings,
    )


def _failure(
    role: NodeRole,
    status: ExecutionStatus,
    message: str,
    *,
    errno: int = 1064,
    sqlstate: str = "42000",
    warnings: tuple[str, ...] = (),
) -> NodeExecution:
    return NodeExecution.failure(
        role=role,
        status=status,
        connection_id=10 + list(NodeRole).index(role),
        started_ns=1,
        ended_ns=2,
        error=ErrorInfo(errno=errno, sqlstate=sqlstate, message=message),
        warnings=warnings,
        watchdog_fired=status is ExecutionStatus.TIMEOUT,
    )


def _three(
    columns: tuple[ColumnMeta, ...],
    baseline: tuple[tuple[object, ...], ...],
    custom_off: tuple[tuple[object, ...], ...] | None = None,
    custom_on: tuple[tuple[object, ...], ...] | None = None,
) -> tuple[NodeExecution, NodeExecution, NodeExecution]:
    return (
        _success(NodeRole.BASELINE, columns, baseline),
        _success(NodeRole.CUSTOM_OFF, columns, baseline if custom_off is None else custom_off),
        _success(NodeRole.CUSTOM_ON, columns, baseline if custom_on is None else custom_on),
    )


def test_compare_two_nodes_matches_equal_results_and_rejects_baseline() -> None:
    pair = (
        _success(NodeRole.CUSTOM_OFF, (INT,), ((1,),)),
        _success(NodeRole.CUSTOM_ON, (INT,), ((1,),)),
    )

    result = compare_two_nodes(reversed(pair))

    assert result.verdict is OracleVerdict.MATCH
    assert len(result.pairwise) == 1
    with pytest.raises(OracleInputError, match="custom_off and custom_on"):
        compare_two_nodes((_success(NodeRole.BASELINE, (INT,), ((1,),)),))


@pytest.mark.parametrize(
    "changed",
    [
        ColumnMeta("k", 8, False, False, False),
        ColumnMeta("k", 3, False, True, False),
        ColumnMeta("k", 3, False, False, True),
        ColumnMeta("k", 3, False, False, False, character_set_id=45),
        ColumnMeta("k", 3, False, False, False, flags=32),
    ],
)
def test_complete_column_metadata_is_compared(changed: ColumnMeta) -> None:
    baseline = _success(NodeRole.BASELINE, (INT,), ((1,),))
    custom_off = _success(NodeRole.CUSTOM_OFF, (changed,), ((1,),))
    custom_on = _success(NodeRole.CUSTOM_ON, (INT,), ((1,),))

    result = compare_three_nodes((custom_on, baseline, custom_off))

    assert result.verdict is OracleVerdict.RESULT_MISMATCH
    assert len(result.pairwise) == 3
    assert any(pair.category == "metadata" for pair in result.pairwise if not pair.matched)


@pytest.mark.parametrize(
    "changed",
    [
        ColumnMeta("other", 3, False, False, False),
        ColumnMeta("k", 3, True, False, False),
        ColumnMeta("k", 3, False, False, False, column_length=11),
        ColumnMeta("k", 3, False, False, False, decimals=2),
    ],
)
def test_connector_only_metadata_is_advisory(changed: ColumnMeta) -> None:
    result = compare_two_nodes(
        (
            _success(NodeRole.CUSTOM_OFF, (INT,), ((1,),)),
            _success(NodeRole.CUSTOM_ON, (changed,), ((1,),)),
        )
    )

    assert result.verdict is OracleVerdict.MATCH
    assert result.advisories
    assert result.advisories[0].category == "metadata"


def test_plan_dependent_field_origin_flags_are_advisory() -> None:
    without_origin = ColumnMeta(
        "id",
        8,
        False,
        True,
        False,
        character_set_id=63,
        column_length=20,
        decimals=0,
        flags=4129,
    )
    with_origin = ColumnMeta(
        "id",
        8,
        False,
        True,
        False,
        character_set_id=63,
        column_length=20,
        decimals=0,
        flags=20515,
    )

    result = compare_three_nodes(
        (
            _success(NodeRole.BASELINE, (without_origin,), ((1,),)),
            _success(NodeRole.CUSTOM_OFF, (without_origin,), ((1,),)),
            _success(NodeRole.CUSTOM_ON, (with_origin,), ((1,),)),
        )
    )

    assert result.verdict is OracleVerdict.MATCH


def test_binary_character_set_id_is_advisory_when_binary_semantics_match() -> None:
    baseline = ColumnMeta("v", 11, True, False, True, character_set_id=255, flags=128)
    custom = ColumnMeta("v", 11, True, False, True, character_set_id=63, flags=128)

    result = compare_three_nodes(
        (
            _success(NodeRole.BASELINE, (baseline,), ()),
            _success(NodeRole.CUSTOM_OFF, (baseline,), ()),
            _success(NodeRole.CUSTOM_ON, (custom,), ()),
        )
    )

    assert result.verdict is OracleVerdict.MATCH


def test_value_semantic_field_flags_remain_strict() -> None:
    plain = ColumnMeta("v", 254, True, False, False, flags=0)
    mysql_set = ColumnMeta("v", 254, True, False, False, flags=2048)

    result = compare_three_nodes(
        (
            _success(NodeRole.BASELINE, (plain,), (("a",),)),
            _success(NodeRole.CUSTOM_OFF, (plain,), (("a",),)),
            _success(NodeRole.CUSTOM_ON, (mysql_set,), (("a",),)),
        )
    )

    assert result.verdict is OracleVerdict.RESULT_MISMATCH
    assert any(pair.category == "metadata" for pair in result.pairwise if not pair.matched)


@pytest.mark.parametrize(
    ("column", "left", "right"),
    [
        (TEXT, "same", b"same"),
        (DECIMAL, Decimal("1.25"), 1.25),
        (TEXT, date(2026, 7, 12), datetime(2026, 7, 12)),
        (TEXT, time(1, 2, 3), timedelta(hours=1, minutes=2, seconds=3)),
    ],
)
def test_python_value_types_are_not_coerced(
    column: ColumnMeta,
    left: object,
    right: object,
) -> None:
    result = compare_three_nodes(_three((column,), ((left,),), ((right,),)))
    assert result.verdict is OracleVerdict.RESULT_MISMATCH


def test_decimal_and_temporal_values_preserve_exact_typed_semantics() -> None:
    columns = (
        DECIMAL,
        ColumnMeta("d", 10, False, False, False),
        ColumnMeta("dt", 12, False, False, False),
        ColumnMeta("tm", 11, False, False, False),
    )
    row = (
        Decimal("1.2500"),
        date(2026, 7, 12),
        datetime(2026, 7, 12, 23, 59, 58, 123456),
        time(23, 59, 58, 123456, tzinfo=timezone.utc),
    )
    equivalent = (
        Decimal("1.25"),
        date(2026, 7, 12),
        datetime(2026, 7, 12, 23, 59, 58, 123456),
        time(23, 59, 58, 123456, tzinfo=timezone.utc),
    )

    assert compare_three_nodes(_three(columns, (row,), (equivalent,))).matched


def test_decimal_canonicalization_never_rounds_through_decimal_context() -> None:
    left = Decimal("1.0000000000000000000000000001")
    right = Decimal("1.0000000000000000000000000002")

    assert not compare_three_nodes(_three((DECIMAL,), ((left,),), ((right,),))).matched


def test_bit_json_and_spatial_values_use_type_specific_canonical_forms() -> None:
    wkb = b"\x01\x01\x00\x00\x00" + b"\x00" * 16
    spatial = (4326).to_bytes(4, "little") + wkb
    columns = (BIT, JSON, GEOMETRY)
    baseline = ((b"\x01", '{"b":2,"a":[1,true,null]}', spatial),)
    semantically_equal = ((1, '{ "a" : [1, true, null], "b" : 2 }', spatial),)

    assert compare_three_nodes(_three(columns, baseline, semantically_equal)).matched

    different_srid = ((1, '{"a":[1,true,null],"b":2}', (0).to_bytes(4, "little") + wkb),)
    assert (
        compare_three_nodes(_three(columns, baseline, different_srid)).verdict
        is OracleVerdict.RESULT_MISMATCH
    )
    different_wkb = ((1, '{"a":[1,true,null],"b":2}', spatial[:-1] + b"\x01"),)
    assert (
        compare_three_nodes(_three(columns, baseline, different_wkb)).verdict
        is OracleVerdict.RESULT_MISMATCH
    )


def test_mysql_set_values_are_unordered_typed_members_and_support_empty_set() -> None:
    baseline = (({"a", "b"},), (set(),))
    permuted = ((set(),), ({"b", "a"},))

    assert compare_three_nodes(_three((SET,), baseline, permuted)).matched
    assert not compare_three_nodes(
        _three((SET,), baseline, ((set(),), ({"a"},)))
    ).matched


@pytest.mark.parametrize("column", (BIT, JSON, GEOMETRY, SET))
def test_nullable_type_specific_columns_preserve_sql_null(column: ColumnMeta) -> None:
    assert compare_three_nodes(_three((column,), ((None,),))).matched


@pytest.mark.parametrize(
    ("column", "value"),
    [
        (BIT, -1),
        (BIT, b""),
        (BIT, b"\x00" * 9),
        (BIT, 1 << 64),
        (JSON, "not valid json"),
        (JSON, {"bad": Decimal("NaN")}),
        (JSON, {"bad": Decimal("Infinity")}),
        (GEOMETRY, b"\x00\x00\x00\x00"),
        (GEOMETRY, b"\x00\x00\x00\x00\x01"),
        (GEOMETRY, b"\x00\x00\x00\x00\x02\x01\x00\x00\x00"),
        (TEXT, object()),
    ],
)
def test_unsupported_or_malformed_wire_values_fail_closed(
    column: ColumnMeta,
    value: object,
) -> None:
    with pytest.raises(OracleInputError, match="cannot compare typed result"):
        compare_three_nodes(_three((column,), ((value,),)))


def test_unordered_multiset_preserves_duplicate_counts() -> None:
    baseline = ((1,), (1,), (2,))
    permuted = ((2,), (1,), (1,))
    missing_duplicate = ((2,), (1,))

    assert compare_three_nodes(_three((INT,), baseline, permuted)).matched
    assert (
        compare_three_nodes(_three((INT,), baseline, missing_duplicate)).verdict
        is OracleVerdict.RESULT_MISMATCH
    )


@pytest.mark.parametrize(
    ("column", "left", "right", "matches"),
    [
        (FLOAT, 0.0, 9e-7, True),
        (FLOAT, 1000.0, 1000.009, True),
        (FLOAT, 0.0, 1.1e-6, False),
        (DOUBLE, 0.0, 9e-13, True),
        (DOUBLE, 1000.0, 1000.0000009, True),
        (DOUBLE, 0.0, 1.1e-12, False),
    ],
)
def test_float_and_double_use_declared_abs_rel_tolerances(
    column: ColumnMeta,
    left: float,
    right: float,
    matches: bool,
) -> None:
    result = compare_three_nodes(_three((column,), ((left,),), ((right,),)))
    assert result.matched is matches


def test_float_matching_uses_perfect_matching_not_greedy_rounding() -> None:
    a = 0.0
    b = 0.75e-12
    c = 1.5e-12
    baseline = ((b,), (a,))
    custom = ((b,), (c,))

    result = compare_three_nodes(_three((DOUBLE,), baseline, custom, tuple(reversed(custom))))

    assert result.matched


def test_float_vector_matching_reassigns_an_earlier_nontransitive_choice() -> None:
    a = 0.0
    b = 0.75e-12
    c = 1.5e-12
    columns = (
        ColumnMeta("x", 5, True, False, False),
        ColumnMeta("y", 5, True, False, False),
    )
    baseline = ((a, a), (a, c))
    custom = ((a, b), (b, a))

    # The first baseline vector can match either custom row, while the second
    # can match only the first. A one-pass greedy choice therefore fails.
    assert compare_three_nodes(_three(columns, baseline, custom)).matched


def test_float_multiset_preserves_duplicate_multiplicity() -> None:
    baseline = ((0.0,), (0.0,), (1.0,))
    changed = ((0.0,), (1.0,), (1.0,))

    assert not compare_three_nodes(_three((DOUBLE,), baseline, changed)).matched


@pytest.mark.timeout(2)
def test_ten_thousand_near_equal_float_rows_use_bounded_fast_path() -> None:
    baseline = tuple((1.0 + index * 0.001,) for index in range(10_000))
    near = tuple((value[0] + 5e-10,) for value in baseline)

    assert compare_three_nodes(_three((DOUBLE,), baseline, near, tuple(reversed(near)))).matched


@pytest.mark.timeout(2)
def test_oversized_hard_fuzzy_graph_fails_with_typed_capacity_error() -> None:
    baseline = tuple((float(index * 10),) for index in range(2_001))
    changed = baseline[:-1] + ((1e100,),)

    with pytest.raises(OracleCapacityError, match="fuzzy matching graph"):
        compare_three_nodes(_three((DOUBLE,), baseline, changed))


@pytest.mark.timeout(2)
def test_fuzzy_capacity_budget_is_shared_across_non_float_groups_and_pairs() -> None:
    baseline = tuple(
        row
        for group in range(3)
        for row in (
            *((group, 0.0, 0.0) for _ in range(500)),
            *((group, 0.0, 1.5e-12) for _ in range(500)),
        )
    )
    changed = tuple(
        row
        for group in range(3)
        for row in (
            *((group, 0.0, 0.75e-12) for _ in range(500)),
            *((group, 0.75e-12, 0.0) for _ in range(500)),
        )
    )

    with pytest.raises(OracleCapacityError, match="shared fuzzy comparison budget"):
        compare_three_nodes(_three((INT, DOUBLE, DOUBLE), baseline, changed, changed))


@pytest.mark.timeout(6)
def test_group_and_execution_permutations_have_one_stable_capacity_outcome() -> None:
    baseline_groups = {
        0: ((0, 0.0, 0.0),),
        1: ((1, 0.0, 0.0),) * 500 + ((1, 0.0, 1.5e-12),) * 500,
        2: ((2, 0.0, 0.0),) * 500 + ((2, 0.0, 1.5e-12),) * 500,
    }
    changed_groups = {
        0: ((0, 100.0, 100.0),),
        1: ((1, 0.0, 0.75e-12),) * 500 + ((1, 0.75e-12, 0.0),) * 500,
        2: ((2, 0.0, 0.75e-12),) * 500 + ((2, 0.75e-12, 0.0),) * 500,
    }
    outcomes: set[tuple[str, str]] = set()

    for group_order in ((0, 1, 2), (1, 2, 0)):
        baseline = tuple(row for group in group_order for row in baseline_groups[group])
        changed = tuple(row for group in group_order for row in changed_groups[group])
        executions = _three(
            (INT, DOUBLE, DOUBLE),
            baseline,
            changed,
            baseline,
        )
        for execution_order in permutations(executions):
            try:
                outcome = ("verdict", compare_three_nodes(execution_order).verdict.value)
            except OracleCapacityError:
                outcome = ("exception", "capacity")
            outcomes.add(outcome)

    assert outcomes == {("verdict", OracleVerdict.RESULT_MISMATCH.value)}


def test_float_rows_cannot_cross_non_float_groups() -> None:
    columns = (INT, DOUBLE)
    baseline = ((1, 0.0), (2, 1.0))
    crossed = ((1, 1.0), (2, 0.0))

    assert (
        compare_three_nodes(_three(columns, baseline, crossed)).verdict
        is OracleVerdict.RESULT_MISMATCH
    )


def test_nan_and_infinity_have_explicit_semantics() -> None:
    values = ((math.nan,), (math.inf,), (-math.inf,))
    assert compare_three_nodes(_three((DOUBLE,), values, tuple(reversed(values)))).matched
    assert not compare_three_nodes(_three((DOUBLE,), ((math.nan,),), ((0.0,),))).matched
    assert not compare_three_nodes(_three((DOUBLE,), ((math.inf,),), ((-math.inf,),))).matched


def test_all_three_pairs_are_compared_without_majority_vote() -> None:
    result = compare_three_nodes(_three((INT,), ((1,),), ((1,),), ((2,),)))

    assert result.verdict is OracleVerdict.RESULT_MISMATCH
    assert len(result.pairwise) == 3
    assert [(pair.left_role, pair.right_role) for pair in result.pairwise] == [
        (NodeRole.BASELINE, NodeRole.CUSTOM_OFF),
        (NodeRole.BASELINE, NodeRole.CUSTOM_ON),
        (NodeRole.CUSTOM_OFF, NodeRole.CUSTOM_ON),
    ]
    assert [pair.matched for pair in result.pairwise].count(True) == 1
    assert [pair.matched for pair in result.pairwise].count(False) == 2


def test_warnings_do_not_affect_success_or_error_verdicts() -> None:
    successes = (
        _success(NodeRole.BASELINE, (INT,), ((1,),), warnings=("one",)),
        _success(NodeRole.CUSTOM_OFF, (INT,), ((1,),), warnings=("two",)),
        _success(NodeRole.CUSTOM_ON, (INT,), ((1,),), warnings=()),
    )
    assert compare_three_nodes(successes).matched

    errors = (
        _failure(NodeRole.BASELINE, ExecutionStatus.ERROR, "syntax error", warnings=("one",)),
        _failure(NodeRole.CUSTOM_OFF, ExecutionStatus.ERROR, "syntax error"),
        _failure(NodeRole.CUSTOM_ON, ExecutionStatus.ERROR, "syntax error", warnings=("two",)),
    )
    assert compare_three_nodes(errors).matched


def test_error_identity_normalizes_only_connection_and_host_fragments() -> None:
    messages = (
        "Connection id: 101 denied for user 'u'@'baseline.internal'; table t_17 failed",
        "Connection id: 202 denied for user 'u'@'off.internal'; table t_17 failed",
        "Connection id: 303 denied for user 'u'@'on.internal'; table t_17 failed",
    )
    executions = tuple(
        _failure(role, ExecutionStatus.ERROR, message)
        for role, message in zip(NodeRole, messages, strict=True)
    )

    result = compare_three_nodes(executions)

    assert result.matched
    normalized = [normalize_error(execution.error) for execution in executions]
    assert len({item.message for item in normalized}) == 1
    assert [item.raw_message for item in normalized] == list(messages)
    assert "t_17" in normalized[0].message

    changed = list(executions)
    changed[2] = _failure(
        NodeRole.CUSTOM_ON,
        ExecutionStatus.ERROR,
        messages[2].replace("t_17", "t_18"),
    )
    assert compare_three_nodes(tuple(changed)).verdict is OracleVerdict.RESULT_MISMATCH


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (
            "Unknown column x on host-a connection 12",
            "Unknown column x on host-b connection 99",
        ),
        (
            "Lost connection to host localhost connection 7",
            "Lost connection to host replica-2 connection 8",
        ),
        (
            "Lost connection on [2001:db8::1] thread 17",
            "Lost connection on [2001:db8::2] thread 18",
        ),
        (
            "Can't connect on host-a connection 12",
            "Can't connect on host-b connection 99",
        ),
    ],
)
def test_error_normalization_covers_controlled_mysql_host_connection_templates(
    left: str,
    right: str,
) -> None:
    first = normalize_error(ErrorInfo(2013, "HY000", left))
    second = normalize_error(ErrorInfo(2013, "HY000", right))

    assert first == second
    assert first.raw_message == left
    assert second.raw_message == right


def test_error_normalization_does_not_strip_object_names_or_unlabelled_numbers() -> None:
    messages = (
        "Unknown column host-a in table connection_12 at row 99",
        "syntax error near host 'left_token'",
        "Unknown column 'connection id 123'",
        "syntax near 'foo on host-a connection 12'",
        'syntax near "for user \'u\'@\'left-host\'"',
    )

    assert [
        normalize_error(ErrorInfo(1054, "42S22", message)).message
        for message in messages
    ] == list(messages)

    left = normalize_error(
        ErrorInfo(1064, "42000", "syntax error near host 'left_token'")
    )
    right = normalize_error(
        ErrorInfo(1064, "42000", "syntax error near host 'right_token'")
    )
    assert left != right

    quoted_left = normalize_error(
        ErrorInfo(1064, "42000", "syntax near 'foo on host-a connection 12'")
    )
    quoted_right = normalize_error(
        ErrorInfo(1064, "42000", "syntax near 'foo on host-b connection 99'")
    )
    assert quoted_left != quoted_right

    account_left = normalize_error(
        ErrorInfo(1064, "42000", 'syntax near "for user \'u\'@\'left-host\'"')
    )
    account_right = normalize_error(
        ErrorInfo(1064, "42000", 'syntax near "for user \'u\'@\'right-host\'"')
    )
    assert account_left != account_right

    quoted_fragments = (
        (
            "Can't parse 'foo on host-a connection 12'",
            "Can't parse 'foo on host-b connection 99'",
        ),
        (
            r"syntax near 'can\'t parse foo on host-a connection 12'",
            r"syntax near 'can\'t parse foo on host-b connection 99'",
        ),
        (
            "syntax near 'can''t parse foo on host-a connection 12'",
            "syntax near 'can''t parse foo on host-b connection 99'",
        ),
    )
    for quoted_host_left, quoted_host_right in quoted_fragments:
        assert normalize_error(
            ErrorInfo(1064, "42000", quoted_host_left)
        ) != normalize_error(ErrorInfo(1064, "42000", quoted_host_right))


def test_error_identity_requires_errno_sqlstate_and_normalized_message() -> None:
    baseline = _failure(NodeRole.BASELINE, ExecutionStatus.ERROR, "same")
    custom_off = _failure(NodeRole.CUSTOM_OFF, ExecutionStatus.ERROR, "same", errno=1054)
    custom_on = _failure(
        NodeRole.CUSTOM_ON,
        ExecutionStatus.ERROR,
        "same",
        sqlstate="42S22",
    )

    result = compare_three_nodes((baseline, custom_off, custom_on))
    assert result.verdict is OracleVerdict.RESULT_MISMATCH
    assert len(result.pairwise) == 3
    assert not any(pair.matched for pair in result.pairwise)


def test_timeout_vs_success_with_identical_results_is_over_budget() -> None:
    all_timeout = tuple(
        _failure(role, ExecutionStatus.TIMEOUT, f"query timeout connection id {index}")
        for index, role in enumerate(NodeRole, start=1)
    )
    result = compare_three_nodes(all_timeout)
    assert result.verdict is OracleVerdict.OVER_BUDGET
    assert not result.matched

    partial = (
        all_timeout[0],
        all_timeout[1],
        _success(NodeRole.CUSTOM_ON, (INT,), ((1,),)),
    )
    result = compare_three_nodes(partial)
    assert result.verdict is OracleVerdict.OVER_BUDGET
    assert len(result.pairwise) == 3

    different_success = (
        all_timeout[0],
        _success(NodeRole.CUSTOM_OFF, (INT,), ((1,),)),
        _success(NodeRole.CUSTOM_ON, (INT,), ((2,),)),
    )
    assert compare_three_nodes(different_success).verdict is OracleVerdict.RESULT_MISMATCH

    mixed_resource = (
        all_timeout[0],
        _failure(NodeRole.CUSTOM_OFF, ExecutionStatus.ERROR, "result row limit exceeded", errno=65001),
        _success(NodeRole.CUSTOM_ON, (INT,), ((1,),)),
    )
    assert compare_three_nodes(mixed_resource).verdict is OracleVerdict.OVER_BUDGET


def test_timeout_vs_database_error_is_over_budget_without_complete_results() -> None:
    executions = (
        _failure(
            NodeRole.CUSTOM_OFF,
            ExecutionStatus.TIMEOUT,
            "Query execution was interrupted",
            errno=1317,
            sqlstate="70100",
        ),
        _failure(
            NodeRole.CUSTOM_ON,
            ExecutionStatus.ERROR,
            "Can't write; duplicate key in temporary table",
            errno=1022,
            sqlstate="23000",
        ),
    )

    result = compare_two_nodes(executions)

    assert result.verdict is OracleVerdict.OVER_BUDGET
    assert result.pairwise[0].matched
    assert result.pairwise[0].category == "resource_limit"


def test_success_error_mix_is_result_mismatch() -> None:
    executions = (
        _success(NodeRole.BASELINE, (INT,), ((1,),)),
        _failure(NodeRole.CUSTOM_OFF, ExecutionStatus.ERROR, "syntax error"),
        _failure(NodeRole.CUSTOM_ON, ExecutionStatus.ERROR, "syntax error"),
    )
    result = compare_three_nodes(executions)
    assert result.verdict is OracleVerdict.RESULT_MISMATCH
    assert [pair.matched for pair in result.pairwise].count(True) == 1


def test_infra_error_never_enters_oracle() -> None:
    executions = (
        _success(NodeRole.BASELINE, (INT,), ((1,),)),
        _failure(NodeRole.CUSTOM_OFF, ExecutionStatus.INFRA_ERROR, "server restarted"),
        _success(NodeRole.CUSTOM_ON, (INT,), ((1,),)),
    )

    with pytest.raises(OracleInputError, match="infra_error"):
        compare_three_nodes(executions)


@pytest.mark.parametrize(
    "roles",
    [
        (NodeRole.BASELINE, NodeRole.CUSTOM_OFF),
        (NodeRole.BASELINE, NodeRole.BASELINE, NodeRole.CUSTOM_ON),
    ],
)
def test_oracle_requires_exactly_one_execution_per_role(roles: tuple[NodeRole, ...]) -> None:
    executions = tuple(_success(role, (INT,), ((1,),)) for role in roles)
    with pytest.raises(OracleInputError, match="exactly one"):
        compare_three_nodes(executions)
