from __future__ import annotations

from itertools import permutations
import math

from hypothesis import given, settings, strategies as st

from select_fuzz.config import NodeRole
from select_fuzz.domain.models import ColumnMeta, NodeExecution
from select_fuzz.oracle import OracleVerdict, compare_three_nodes


DOUBLE = ColumnMeta("v", 5, True, False, False)


def _execution(role: NodeRole, values: list[float]) -> NodeExecution:
    return NodeExecution.success(
        role=role,
        connection_id=10 + list(NodeRole).index(role),
        started_ns=1,
        ended_ns=2,
        columns=(DOUBLE,),
        rows=tuple((value,) for value in values),
    )


@settings(max_examples=10_000, deadline=None)
@given(
    values=st.lists(
        st.floats(width=64, allow_nan=True, allow_infinity=True),
        max_size=6,
    ),
    shift=st.integers(min_value=0, max_value=100),
)
def test_float_multiset_is_permutation_invariant(
    values: list[float],
    shift: int,
) -> None:
    if values:
        offset = shift % len(values)
        rotated = values[offset:] + values[:offset]
    else:
        rotated = []
    reversed_values = list(reversed(values))
    executions = (
        _execution(NodeRole.BASELINE, values),
        _execution(NodeRole.CUSTOM_OFF, rotated),
        _execution(NodeRole.CUSTOM_ON, reversed_values),
    )

    result = compare_three_nodes(executions)

    assert result.verdict is OracleVerdict.MATCH
    assert result.matched


@st.composite
def _small_nontransitive_multisets(
    draw: st.DrawFn,
) -> tuple[list[float], list[float]]:
    size = draw(st.integers(min_value=0, max_value=6))
    point = st.integers(min_value=-6, max_value=6).map(lambda value: value * 0.75e-12)
    return (
        draw(st.lists(point, min_size=size, max_size=size)),
        draw(st.lists(point, min_size=size, max_size=size)),
    )


@settings(max_examples=2_000, deadline=None)
@given(multisets=_small_nontransitive_multisets())
def test_matching_agrees_with_exhaustive_bipartite_oracle(
    multisets: tuple[list[float], list[float]],
) -> None:
    left, right = multisets
    expected = any(
        all(
            math.isclose(left[index], right[right_index], abs_tol=1e-12, rel_tol=1e-9)
            for index, right_index in enumerate(order)
        )
        for order in permutations(range(len(right)))
    )
    executions = (
        _execution(NodeRole.BASELINE, left),
        _execution(NodeRole.CUSTOM_OFF, right),
        _execution(NodeRole.CUSTOM_ON, right),
    )

    assert compare_three_nodes(executions).matched is expected
