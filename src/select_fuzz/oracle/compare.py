"""Three-node typed differential oracle with deterministic multiset matching."""

from __future__ import annotations

from array import array
from collections import Counter, defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from itertools import combinations

from select_fuzz.config import NodeRole
from select_fuzz.domain.models import ColumnMeta, ExecutionStatus, NodeExecution
from select_fuzz.oracle.canonical import (
    CanonicalValue,
    FloatCell,
    FloatTolerance,
    canonical_float_cell,
    canonical_group_value,
    float_cells_equal,
    is_float_column,
    tolerance_for,
)
from select_fuzz.oracle.errors import (
    CanonicalizationError,
    OracleCapacityError,
    OracleInputError,
    normalize_error,
)


MAX_FUZZY_SCALAR_COMPARISONS = 4_000_000
ORDERED_FAST_PATH_MIN_ROWS = 256


class OracleVerdict(StrEnum):
    MATCH = "match"
    OVER_BUDGET = "over_budget"
    RESULT_MISMATCH = "result_mismatch"


@dataclass(frozen=True, slots=True)
class PairwiseComparison:
    left_role: NodeRole
    right_role: NodeRole
    matched: bool
    category: str
    detail: str


@dataclass(frozen=True, slots=True)
class OracleResult:
    verdict: OracleVerdict
    pairwise: tuple[PairwiseComparison, ...]

    @property
    def matched(self) -> bool:
        return self.verdict is OracleVerdict.MATCH


@dataclass(slots=True)
class _FuzzyComparisonBudget:
    remaining: int = MAX_FUZZY_SCALAR_COMPARISONS

    def consume(self, amount: int) -> None:
        if amount > self.remaining:
            raise OracleCapacityError(
                "fuzzy matching graph exceeds shared fuzzy comparison budget: "
                f"requested {amount}, remaining {self.remaining} scalar comparisons"
            )
        self.remaining -= amount


FloatVector = tuple[FloatCell, ...]
GroupMap = dict[tuple[CanonicalValue, ...], list[FloatVector]]


def _group_rows(
    columns: tuple[ColumnMeta, ...],
    rows: tuple[tuple[object, ...], ...],
) -> GroupMap:
    groups: GroupMap = defaultdict(list)
    for row in rows:
        group_key: list[CanonicalValue] = []
        float_vector: list[FloatCell] = []
        for column, value in zip(columns, row, strict=True):
            if is_float_column(column):
                float_vector.append(canonical_float_cell(value))
            else:
                group_key.append(canonical_group_value(value, column))
        groups[tuple(group_key)].append(tuple(float_vector))
    return dict(groups)


def _vector_sort_key(vector: FloatVector) -> tuple[tuple[int, float, str], ...]:
    return tuple(cell.sort_key() for cell in vector)


def _vectors_close(
    left: FloatVector,
    right: FloatVector,
    tolerances: tuple[FloatTolerance, ...],
) -> bool:
    return all(
        float_cells_equal(left_cell, right_cell, tolerance)
        for left_cell, right_cell, tolerance in zip(left, right, tolerances, strict=True)
    )


def _has_perfect_matching(
    left_vectors: list[FloatVector],
    right_vectors: list[FloatVector],
    tolerances: tuple[FloatTolerance, ...],
    budget: _FuzzyComparisonBudget,
) -> bool:
    if len(left_vectors) != len(right_vectors):
        return False
    left = sorted(left_vectors, key=_vector_sort_key)
    right = sorted(right_vectors, key=_vector_sort_key)
    if Counter(left) == Counter(right):
        return True
    dimensions = max(1, len(tolerances))
    if len(left) >= ORDERED_FAST_PATH_MIN_ROWS:
        budget.consume(len(left) * dimensions)
        if all(
            _vectors_close(left_vector, right_vector, tolerances)
            for left_vector, right_vector in zip(left, right, strict=True)
        ):
            return True
    scalar_comparisons = len(left) * len(right) * max(1, len(tolerances))
    budget.consume(scalar_comparisons)
    adjacency = [
        array(
            "I",
            (
                right_index
                for right_index, right_vector in enumerate(right)
                if _vectors_close(left_vector, right_vector, tolerances)
            ),
        )
        for left_vector in left
    ]
    if any(not neighbors for neighbors in adjacency):
        return False
    matched_left = [-1] * len(left)
    matched_right = [-1] * len(right)

    infinity = len(left) + 1
    distances = [infinity] * len(left)

    def build_layers() -> int:
        queue: deque[int] = deque()
        for left_index in range(len(left)):
            if matched_left[left_index] == -1:
                distances[left_index] = 0
                queue.append(left_index)
            else:
                distances[left_index] = infinity
        shortest = infinity
        while queue:
            left_index = queue.popleft()
            if distances[left_index] + 1 > shortest:
                continue
            for right_index in adjacency[left_index]:
                owner = matched_right[right_index]
                if owner == -1:
                    shortest = distances[left_index] + 1
                elif distances[owner] == infinity:
                    distances[owner] = distances[left_index] + 1
                    queue.append(owner)
        return shortest

    def augment(start_left: int, shortest: int) -> bool:
        """Follow one layered augmenting path without recursive depth risk."""

        queue = deque([start_left])
        visited_left = [False] * len(left)
        visited_left[start_left] = True
        parent_right = [-1] * len(right)
        while queue:
            left_index = queue.popleft()
            for right_index in adjacency[left_index]:
                if parent_right[right_index] != -1:
                    continue
                owner = matched_right[right_index]
                if owner == -1 and distances[left_index] + 1 != shortest:
                    continue
                if owner != -1 and distances[owner] != distances[left_index] + 1:
                    continue
                parent_right[right_index] = left_index
                if owner == -1:
                    current_right = right_index
                    while current_right != -1:
                        current_left = parent_right[current_right]
                        previous_right = matched_left[current_left]
                        matched_left[current_left] = current_right
                        matched_right[current_right] = current_left
                        current_right = previous_right
                    return True
                if not visited_left[owner]:
                    visited_left[owner] = True
                    queue.append(owner)
        return False

    matched_count = 0
    while True:
        shortest = build_layers()
        if shortest == infinity:
            break
        phase_progress = 0
        for left_index in range(len(left)):
            if matched_left[left_index] == -1 and augment(left_index, shortest):
                matched_count += 1
                phase_progress += 1
        if phase_progress == 0:  # pragma: no cover - defensive invariant
            break
    return matched_count == len(left)


def _result_sets_equal(
    left: NodeExecution,
    right: NodeExecution,
    budget: _FuzzyComparisonBudget,
) -> tuple[bool, str, str]:
    if left.columns != right.columns:
        return False, "metadata", "complete column metadata differs"
    columns = left.columns
    tolerances = tuple(tolerance_for(column) for column in columns if is_float_column(column))
    try:
        left_groups = _group_rows(columns, left.rows)
        right_groups = _group_rows(columns, right.rows)
    except CanonicalizationError:
        raise
    if left_groups.keys() != right_groups.keys():
        return False, "rows", "non-floating typed row groups differ"
    for key in sorted(left_groups, key=repr):
        if not _has_perfect_matching(
            left_groups[key], right_groups[key], tolerances, budget
        ):
            return False, "rows", "typed multiset has no tolerance-valid perfect matching"
    return True, "rows", "typed unordered multisets match"


def _compare_pair(
    left: NodeExecution,
    right: NodeExecution,
    budget: _FuzzyComparisonBudget,
) -> PairwiseComparison:
    if left.status is not right.status:
        return PairwiseComparison(
            left.role,
            right.role,
            False,
            "status",
            f"status differs: {left.status.value} != {right.status.value}",
        )
    if left.status is ExecutionStatus.TIMEOUT:
        return PairwiseComparison(left.role, right.role, True, "timeout", "both timed out")
    if left.status is ExecutionStatus.ERROR:
        if left.error is None or right.error is None:  # pragma: no cover - domain invariant
            raise OracleInputError("error execution lacks ErrorInfo")
        matched = normalize_error(left.error) == normalize_error(right.error)
        return PairwiseComparison(
            left.role,
            right.role,
            matched,
            "error",
            "normalized errors match" if matched else "normalized errors differ",
        )
    if left.status is not ExecutionStatus.SUCCESS:  # pragma: no cover - infra preflight
        raise OracleInputError(f"unsupported execution status: {left.status.value}")
    try:
        matched, category, detail = _result_sets_equal(left, right, budget)
    except CanonicalizationError as error:
        raise OracleInputError(f"cannot compare typed result: {error}") from error
    return PairwiseComparison(left.role, right.role, matched, category, detail)


def compare_three_nodes(executions: Iterable[NodeExecution]) -> OracleResult:
    """Compare baseline/off/on pairwise; never infer correctness by majority vote."""

    execution_tuple = tuple(executions)
    by_role = {execution.role: execution for execution in execution_tuple}
    if len(execution_tuple) != 3 or len(by_role) != 3 or set(by_role) != set(NodeRole):
        raise OracleInputError("oracle requires exactly one execution per three-node role")
    ordered = tuple(by_role[role] for role in NodeRole)
    infra_roles = [
        execution.role.value
        for execution in ordered
        if execution.status is ExecutionStatus.INFRA_ERROR
    ]
    if infra_roles:
        raise OracleInputError(
            f"infra_error executions must not enter oracle: {', '.join(infra_roles)}"
        )
    budget = _FuzzyComparisonBudget()
    pairwise = tuple(
        _compare_pair(left, right, budget) for left, right in combinations(ordered, 2)
    )
    if all(execution.status is ExecutionStatus.TIMEOUT for execution in ordered):
        verdict = OracleVerdict.OVER_BUDGET
    elif all(pair.matched for pair in pairwise):
        verdict = OracleVerdict.MATCH
    else:
        verdict = OracleVerdict.RESULT_MISMATCH
    return OracleResult(verdict=verdict, pairwise=pairwise)
