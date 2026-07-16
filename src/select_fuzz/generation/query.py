"""Coverage-directed deterministic MySQL 8.0.41 SELECT generation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import json
import random

from select_fuzz.domain import SeedTree
from select_fuzz.generation.catalog import CapabilityStatus, FeatureCatalog, FeatureSpec
from select_fuzz.generation.catalog_schema import REVIEWED_VARIANT_IDS, Version
from select_fuzz.generation.coverage import CoverageScheduler
from select_fuzz.generation.function_registry import (
    DETERMINISTIC_FUNCTION_SIGNATURES,
    DeterministicFunctionSignature,
    FunctionArgument,
    FunctionResult,
)
from select_fuzz.generation.query_ast import (
    BetweenExpression,
    BinaryExpression,
    BinaryOperator,
    CaseExpression,
    CastExpression,
    ColumnRef,
    Cte,
    DerivedRelation,
    ExpectedError,
    ExpectedErrorKind,
    Expression,
    FunctionCall,
    FunctionName,
    FunctionalLowerExpression,
    IndexHint,
    IndexHintAction,
    IndexHintScope,
    InListExpression,
    InvalidFunctionArity,
    JsonMemberOf,
    JsonTableRelation,
    JoinKind,
    JoinRelation,
    LikeExpression,
    Literal,
    MatchAgainst,
    MixedSetQuery,
    NamedRelation,
    OrderBy,
    ParenthesizedQuery,
    Projection,
    QueryAst,
    QueryBody,
    QueryScope,
    RegisteredFunctionCall,
    Relation,
    RowExpression,
    SelectModifier,
    SelectQuery,
    SetOperator,
    SetOperation,
    SetQuery,
    SqlType,
    Star,
    SubqueryExpression,
    SubqueryOperator,
    TableQuery,
    TableRelation,
    UnaryExpression,
    UnaryOperator,
    ValuesQuery,
    WindowFrame,
    WindowFrameBound,
    WindowFrameBoundKind,
    WindowFunction,
    WindowFrameUnit,
    WindowOrder,
    WindowSpec,
)
from select_fuzz.generation.query_render import render_query_ast
from select_fuzz.generation.query_safety import ReadOnlyValidator
from select_fuzz.generation.schema import (
    ColumnDef,
    IndexKind,
    SchemaManifest,
    SchemaProfile,
    TableDef,
)


SUPPORTED_VARIANT_IDS: frozenset[str] = REVIEWED_VARIANT_IDS

_INDEX_HINT_VARIANTS: dict[
    str,
    tuple[IndexHintAction, IndexHintScope | None],
] = {
    f"index_hint_{action.name.lower()}_{scope_name}": (action, scope)
    for action in IndexHintAction
    for scope_name, scope in {
        "default": None,
        "join": IndexHintScope.JOIN,
        "order_by": IndexHintScope.ORDER_BY,
        "group_by": IndexHintScope.GROUP_BY,
    }.items()
}

_PREDICATE_VARIANTS = frozenset(
    {
        "null_safe_eq",
        "divide",
        "integer_divide",
        "modulo",
        "bit_and",
        "bit_or",
        "bit_xor",
        "shift_left",
        "shift_right",
        "logical_xor",
        "unary_plus",
        "unary_minus",
        "between",
        "not_between",
        "in_list_null",
        "not_in_list_null",
        "like_escape",
        "not_like_escape",
        "regexp_like",
        "not_regexp_like",
        "is_true",
        "is_false",
        "is_unknown",
        "is_not_true",
        "is_not_false",
        "is_not_unknown",
    }
)

_NULL_PREDICATE_VARIANTS = frozenset(
    {
        *(f"comparison_null_{position}" for position in ("left", "right", "both")),
        *(f"arithmetic_null_{position}" for position in ("left", "right", "both")),
        *(f"bitwise_null_{position}" for position in ("left", "right", "both")),
        *(f"logical_null_{position}" for position in ("left", "right", "both")),
        *(f"like_regexp_null_{position}" for position in ("left", "right", "both")),
        "between_null_value",
        "between_null_lower",
        "between_null_upper",
        "between_null_bounds",
        "between_null_all",
        "in_null_left",
        "in_null_right",
        "in_null_both",
    }
)

_AGGREGATE_VARIANTS = frozenset(
    {
        "sum",
        "avg",
        "min",
        "max",
        "count_distinct",
        "bit_and",
        "bit_or",
        "bit_xor",
        "stddev_pop",
        "stddev_samp",
        "var_pop",
        "var_samp",
        "group_null_having",
        "aggregate_all_null",
    }
)

_WINDOW_FUNCTION_VARIANTS = frozenset(
    {
        "row_number",
        "rank",
        "dense_rank",
        "cume_dist",
        "percent_rank",
        "ntile",
        "first_value",
        "last_value",
        "nth_value",
        "lag",
        "lag_offset",
        "lag_default",
        "lead",
        "lead_offset",
        "lead_default",
    }
)
_WINDOW_FRAME_VARIANTS = frozenset(
    {
        "rows_frame",
        "range_frame",
        "rows_unbounded_current",
        "range_current_unbounded",
    }
)

_SELECT_LEAF_VARIANTS = frozenset(
    {
        "star",
        "qualified_star",
        "order_by_alias",
        "order_by_expression",
        "modifier_all",
        "modifier_distinctrow",
        "modifier_high_priority",
        "modifier_straight_join",
        "modifier_sql_calc_found_rows",
        "modifier_sql_no_cache",
        "modifier_sql_small_result",
        "modifier_sql_big_result",
        "modifier_sql_buffer_result",
    }
)

_SET_PRECEDENCE_VARIANTS: dict[SetOperator, tuple[str, ...]] = {
    SetOperator.UNION: (
        "precedence_union_intersect",
        "parenthesized_union_intersect",
        "precedence_union_except",
        "parenthesized_union_except",
    ),
    SetOperator.INTERSECT: (),
    SetOperator.EXCEPT: (
        "precedence_except_intersect",
        "parenthesized_except_intersect",
    ),
}
_SET_TYPE_DOMAINS = ("numeric", "text", "binary", "temporal")

_FUNCTION_DIRECTED_VARIANTS = tuple(
    sorted(
        {signature.signature_id for signature in DETERMINISTIC_FUNCTION_SIGNATURES}
        | {
            f"{signature.signature_id}_null_{position}"
            for signature in DETERMINISTIC_FUNCTION_SIGNATURES
            for position in signature.null_argument_positions
        }
    )
)

_DIRECTED_LEAF_VARIANTS: dict[str, tuple[str, ...]] = {
    "select_query_specification": (
        "scalar_literal",
        "scalar_aggregate",
        "limit_zero",
        "limit_zero_offset",
        "table_limit_zero",
        "table_limit_zero_offset",
        "scalar_offset_limit",
        "offset_limit",
        "table_offset_limit",
        *sorted(_SELECT_LEAF_VARIANTS),
    ),
    "join_inner_cross_straight": (
        "comma",
        "inner",
        "inner_conditionless",
        "inner_using",
        "cross",
        "natural_inner",
        "straight",
        "nested_three",
        "inner_subquery",
        "inner_cast",
        "nullable_key_left",
        "nullable_key_right",
        "nullable_key_both",
        *sorted(_INDEX_HINT_VARIANTS),
    ),
    "join_outer_natural": (
        "left",
        "left_using",
        "right",
        "right_using",
        "natural_left",
        "natural_right",
        "left_subquery",
    ),
    "subquery_result_kinds": (
        "table_limit",
        "scalar_limit",
        "scalar",
        "row",
        "exists",
        "not_exists",
        "not_in",
        "not_in_null",
        "not_exists_empty",
    ),
    "subquery_quantified": ("any", "all"),
    "derived_regular": ("implicit_columns", "explicit_columns"),
    "cte_nonrecursive": ("single", "multiple", "dependency", "reuse"),
    "cte_recursive": ("recursive_union_all", "recursive_union_distinct"),
    "set_union": (
        "base",
        "scalar_intersect_except",
        *_SET_PRECEDENCE_VARIANTS[SetOperator.UNION],
        *(f"union_{domain}" for domain in _SET_TYPE_DOMAINS),
    ),
    "set_intersect": (
        "base",
        "intersect_all",
        *(f"intersect_{domain}" for domain in _SET_TYPE_DOMAINS),
    ),
    "set_except": (
        "base",
        "except_all",
        *_SET_PRECEDENCE_VARIANTS[SetOperator.EXCEPT],
        *(f"except_{domain}" for domain in _SET_TYPE_DOMAINS),
    ),
    "set_branch_local_top_n": ("table_branch_local_top_n", "scalar_branch_local_top_n"),
    "set_table_values": ("select_values", "values_only", "values_limit"),
    "table_values_union": ("table_values_union_all", "table_values_union_distinct"),
    "grouping_with_rollup": (
        "table_rollup",
        "scalar_rollup",
        "table_grouping_function",
    ),
    "function_aggregate": ("grouping", *sorted(_AGGREGATE_VARIANTS)),
    "window_inline_named": tuple(sorted(_WINDOW_FUNCTION_VARIANTS)),
    "window_frames": tuple(sorted(_WINDOW_FRAME_VARIANTS)),
    "function_deterministic_scalar": (
        *sorted(_PREDICATE_VARIANTS | _NULL_PREDICATE_VARIANTS),
        *_FUNCTION_DIRECTED_VARIANTS,
    ),
}


class EvidenceGateError(ValueError):
    """A renderer exists, but its official evidence lock is not ready."""


class UnsupportedQueryFeature(ValueError):
    """The requested catalog row has no registered renderer."""


class TargetNotReachable(ValueError):
    """The schema lacks a precondition required by a directed query shape."""


class QueryBudgetExceeded(ValueError):
    """A generated shape would exceed a configured hard complexity budget."""


class QueryLane(StrEnum):
    VALID = "valid"
    FREE_RANDOM = "free_random"
    NEGATIVE = "negative"


@dataclass(frozen=True, slots=True)
class QueryMix:
    valid_percent: int = 80
    free_random_percent: int = 20
    negative_percent: int = 0

    def __post_init__(self) -> None:
        values = (self.valid_percent, self.free_random_percent, self.negative_percent)
        if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
            raise TypeError("query lane percentages must be integers")
        if any(value < 0 for value in values) or sum(values) != 100:
            raise ValueError("query lane percentages must be nonnegative and sum to 100")

    def choose(self, *, seed: int, case_ordinal: int) -> QueryLane:
        if not isinstance(case_ordinal, int) or isinstance(case_ordinal, bool) or case_ordinal < 0:
            raise ValueError("case_ordinal must be a nonnegative integer")
        offset = SeedTree(seed).derive("query", "lane_offset") % 100
        ticket = (offset + case_ordinal) % 100
        if ticket < self.valid_percent:
            return QueryLane.VALID
        if ticket < self.valid_percent + self.free_random_percent:
            return QueryLane.FREE_RANDOM
        return QueryLane.NEGATIVE

    def identity(self) -> str:
        return f"{self.valid_percent}:{self.free_random_percent}:{self.negative_percent}"


@dataclass(frozen=True, slots=True)
class QueryBudget:
    max_tables: int = 4
    max_depth: int = 3
    max_ctes: int = 2
    max_set_branches: int = 3
    max_projection: int = 12
    max_predicates: int = 12
    max_intermediate_rows: int = 100_000
    max_output_rows: int = 10_000
    max_json_elements_per_row: int = 16
    default_rows_per_table: int = 100

    def __post_init__(self) -> None:
        for field_name in self.__dataclass_fields__:
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class QueryComplexity:
    tables: int
    depth: int
    ctes: int
    set_branches: int
    projection: int
    predicates: int
    estimated_scanned_rows: int
    estimated_intermediate_rows: int
    estimated_output_rows: int

    def __post_init__(self) -> None:
        for field_name in self.__dataclass_fields__:
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field_name} must be a nonnegative integer")
        for field_name in ("depth", "set_branches", "projection"):
            if getattr(self, field_name) == 0:
                raise ValueError(f"{field_name} must be positive")

    def within(self, budget: QueryBudget) -> bool:
        return not self.violations(budget)

    def violations(self, budget: QueryBudget) -> tuple[str, ...]:
        violations: list[str] = []
        for field_name, budget_name in (
            ("tables", "max_tables"),
            ("depth", "max_depth"),
            ("ctes", "max_ctes"),
            ("set_branches", "max_set_branches"),
            ("projection", "max_projection"),
            ("predicates", "max_predicates"),
            ("estimated_intermediate_rows", "max_intermediate_rows"),
            ("estimated_output_rows", "max_output_rows"),
        ):
            if getattr(self, field_name) > getattr(budget, budget_name):
                violations.append(field_name)
        return tuple(violations)


@dataclass(frozen=True, slots=True)
class DirectedQueryLeaf:
    feature_id: str
    variant_id: str

    def __post_init__(self) -> None:
        if not self.feature_id or not self.variant_id:
            raise ValueError("directed query leaf identifiers must be nonempty")

    @property
    def coverage_tag(self) -> str:
        return f"query_leaf:{self.feature_id}:{self.variant_id}"


@dataclass(frozen=True, slots=True)
class GeneratedQuery:
    ast: QueryAst
    sql: str
    target_feature_id: str
    feature_tags: frozenset[str]
    lane: QueryLane
    case_ordinal: int
    seed: int
    complexity: QueryComplexity
    expected_error: ExpectedError | None = None
    coverage_eligible: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.lane, QueryLane):
            raise TypeError("lane must be a QueryLane")
        if not isinstance(self.coverage_eligible, bool):
            raise TypeError("coverage_eligible must be a boolean")
        if (self.lane is QueryLane.NEGATIVE) != (self.expected_error is not None):
            raise ValueError("negative lane must have exactly one expected error contract")
        if self.lane is not QueryLane.VALID and self.coverage_eligible:
            raise ValueError("only valid-lane queries may be coverage eligible")

    def canonical_bytes(self) -> bytes:
        payload = {
            "case_ordinal": self.case_ordinal,
            "complexity": {
                name: getattr(self.complexity, name)
                for name in self.complexity.__dataclass_fields__
            },
            "coverage_eligible": self.coverage_eligible,
            "expected_error": (
                None
                if self.expected_error is None
                else {
                    "expected_errno": self.expected_error.expected_errno,
                    "expected_sqlstate": self.expected_error.expected_sqlstate,
                    "kind": self.expected_error.kind.value,
                }
            ),
            "feature_tags": sorted(self.feature_tags),
            "lane": self.lane.value,
            "seed": self.seed,
            "sql": self.sql,
            "target_feature_id": self.target_feature_id,
        }
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class _BuiltQuery:
    ast: QueryAst
    complexity: QueryComplexity
    extra_tags: frozenset[str] = frozenset()
    expected_error: ExpectedError | None = None


class QueryGenerator:
    """Render every catalogued shape through typed, version-gated AST nodes."""

    def __init__(
        self,
        *,
        version: Version = (8, 0, 41),
        mix: QueryMix | None = None,
        validator: ReadOnlyValidator | None = None,
    ) -> None:
        self.version = version
        self.mix = mix or QueryMix()
        self.validator = validator or ReadOnlyValidator()

    @staticmethod
    def feature_catalog() -> FeatureCatalog:
        """Load the official catalog with exactly the implemented renderer registry."""

        return FeatureCatalog.default(generator_supported_ids=SUPPORTED_VARIANT_IDS)

    @staticmethod
    def directed_leaf_variants(feature_id: str) -> tuple[DirectedQueryLeaf, ...]:
        if not isinstance(feature_id, str) or not feature_id:
            raise TypeError("feature_id must be a nonempty string")
        return tuple(
            DirectedQueryLeaf(feature_id, variant_id)
            for variant_id in _DIRECTED_LEAF_VARIANTS.get(feature_id, ())
        )

    def generate(
        self,
        manifest: SchemaManifest,
        *,
        target: FeatureSpec,
        seed: int,
        case_ordinal: int = 0,
        lane: QueryLane | None = None,
        budget: QueryBudget | None = None,
        estimated_rows_by_table: Mapping[str, int] | None = None,
        require_top_n: bool = False,
        directed_variant: str | None = None,
    ) -> GeneratedQuery:
        self._validate_request(manifest, target=target, seed=seed, case_ordinal=case_ordinal)
        limits = budget or QueryBudget()
        rows = self._row_estimates(manifest, estimated_rows_by_table, limits)
        chosen_lane = lane or self.mix.choose(seed=seed, case_ordinal=case_ordinal)
        if not isinstance(chosen_lane, QueryLane):
            raise TypeError("lane must be a QueryLane")
        rng = random.Random(
            SeedTree(seed).derive("query", target.feature_id, case_ordinal, chosen_lane.value)
        )
        if chosen_lane is QueryLane.NEGATIVE:
            built = self._build_negative(manifest, rng=rng, rows=rows)
        elif chosen_lane is QueryLane.FREE_RANDOM:
            built = self._build_free_random(
                manifest,
                rng=rng,
                rows=rows,
                require_top_n=require_top_n,
                budget=limits,
            )
        else:
            built = self._build_feature(
                manifest,
                feature_id=target.feature_id,
                rng=rng,
                rows=rows,
                budget=limits,
                require_top_n=require_top_n,
                directed_variant=directed_variant,
                free_random=False,
            )
        violations = built.complexity.violations(limits)
        if violations:
            raise QueryBudgetExceeded("query exceeds hard " + ", ".join(violations) + " budget")
        sql = render_query_ast(built.ast)
        self.validator.validate_text(sql)
        tags = {target.feature_id, f"lane_{chosen_lane.value}", *built.extra_tags}
        registered_leaves = _DIRECTED_LEAF_VARIANTS.get(target.feature_id, ())
        if (
            directed_variant is not None
            and directed_variant in registered_leaves
            and chosen_lane is QueryLane.VALID
        ):
            tags.add(DirectedQueryLeaf(target.feature_id, directed_variant).coverage_tag)
        return GeneratedQuery(
            ast=built.ast,
            sql=sql,
            target_feature_id=target.feature_id,
            feature_tags=frozenset(tags),
            lane=chosen_lane,
            case_ordinal=case_ordinal,
            seed=seed,
            complexity=built.complexity,
            expected_error=built.expected_error,
            coverage_eligible=chosen_lane is QueryLane.VALID,
        )

    def _build_free_random(
        self,
        manifest: SchemaManifest,
        *,
        rng: random.Random,
        rows: Mapping[str, int],
        require_top_n: bool,
        budget: QueryBudget,
    ) -> _BuiltQuery:
        """Choose an undirected safe shape that cannot satisfy coverage debt."""

        if require_top_n:
            built = self._simple(manifest, rows, top_n=True, free_random=True)
            shape = "top_n"
        else:
            shapes: tuple[str, ...] = (
                "simple",
                "scalar_literal",
                "parenthesized",
                "case",
                "function",
                "grouping",
                "join",
                "subquery",
                "set",
                "window",
                "predicate",
                "aggregate",
                "cte",
            )
            if manifest.requires_same_session:
                # MySQL cannot reference the same TEMPORARY table twice in one
                # statement (ER_CANT_REOPEN_TABLE).  Directed temporary-table
                # coverage uses single-reference shapes, so keep the
                # undirected lane within the same executable contract.
                shapes = tuple(
                    candidate
                    for candidate in shapes
                    if candidate not in {"join", "subquery", "set"}
                )
            shape = rng.choice(shapes)
            if shape == "simple":
                built = self._simple(manifest, rows, top_n=False, free_random=True)
            elif shape == "scalar_literal":
                built = self._scalar_literal()
            elif shape == "parenthesized":
                built = self._parenthesized(manifest, rows)
            elif shape == "case":
                built = self._case(manifest, rows, searched=bool(rng.randrange(2)))
            elif shape == "function":
                built = self._deterministic_function(
                    manifest,
                    rows,
                    rng=rng,
                    directed=None,
                )
            elif shape == "grouping":
                built = self._grouping(
                    manifest,
                    rows,
                    rollup=bool(rng.randrange(2)),
                    having=bool(rng.randrange(2)),
                )
            elif shape == "join":
                built = self._join(
                    manifest,
                    rows,
                    rng,
                    outer=bool(rng.randrange(2)),
                    directed=None,
                )
            elif shape == "subquery":
                built = self._subquery(
                    manifest,
                    rows,
                    materialized=False,
                    rng=rng,
                    directed=None,
                )
            elif shape == "set":
                built = self._set_operation(
                    manifest,
                    rows,
                    rng.choice(tuple(SetOperator)),
                    chain=bool(rng.randrange(2)),
                    all_rows=bool(rng.randrange(2)),
                )
            elif shape == "window":
                built = self._window(
                    manifest,
                    rows,
                    frame=bool(rng.randrange(2)),
                    rng=rng,
                    directed=None,
                )
            elif shape == "predicate":
                variant = rng.choice(sorted(_PREDICATE_VARIANTS | _NULL_PREDICATE_VARIANTS))
                if variant in _NULL_PREDICATE_VARIANTS:
                    built = self._null_predicate_semantics(variant=variant)
                else:
                    built = self._predicate_semantics(manifest, rows, variant=variant)
            elif shape == "aggregate":
                variant = rng.choice(sorted(_AGGREGATE_VARIANTS - {"aggregate_all_null"}))
                built = self._aggregate_semantics(manifest, rows, variant=variant)
            else:
                built = self._cte(
                    manifest,
                    rows,
                    recursive=bool(rng.randrange(2)),
                    rng=rng,
                    directed=None,
                )
        if built.complexity.violations(budget):
            built = self._scalar_literal()
            shape = "scalar_literal"
        return _BuiltQuery(
            built.ast,
            built.complexity,
            built.extra_tags | frozenset({f"free_shape_{shape}"}),
            built.expected_error,
        )

    def _validate_request(
        self,
        manifest: SchemaManifest,
        *,
        target: FeatureSpec,
        seed: int,
        case_ordinal: int,
    ) -> None:
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise TypeError("seed must be an integer")
        if not isinstance(case_ordinal, int) or isinstance(case_ordinal, bool) or case_ordinal < 0:
            raise ValueError("case_ordinal must be a nonnegative integer")
        if target.feature_id not in SUPPORTED_VARIANT_IDS:
            raise UnsupportedQueryFeature(target.feature_id)
        if target.capability_status is not CapabilityStatus.GENERATOR_SUPPORTED:
            raise UnsupportedQueryFeature(target.feature_id)
        if not target.evidence_lock_ready:
            raise EvidenceGateError(f"official evidence lock is not ready for {target.feature_id}")
        if target.min_version > self.version:
            raise UnsupportedQueryFeature(
                f"{target.feature_id} requires MySQL {target.min_version}"
            )
        if manifest.profile.value not in target.compatible_profiles:
            raise TargetNotReachable(
                f"{target.feature_id} is incompatible with {manifest.profile.value}"
            )

    @staticmethod
    def _row_estimates(
        manifest: SchemaManifest,
        supplied: Mapping[str, int] | None,
        budget: QueryBudget,
    ) -> dict[str, int]:
        known = {table.name for table in manifest.tables}
        supplied = supplied or {}
        unknown = set(supplied) - known
        if unknown:
            raise ValueError(f"row estimates contain unknown tables: {sorted(unknown)}")
        rows: dict[str, int] = {}
        for table in manifest.tables:
            value = supplied.get(table.name, budget.default_rows_per_table)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("row estimates must be nonnegative integers")
            rows[table.name] = value
        return rows

    def _build_feature(
        self,
        manifest: SchemaManifest,
        *,
        feature_id: str,
        rng: random.Random,
        rows: Mapping[str, int],
        budget: QueryBudget,
        require_top_n: bool,
        directed_variant: str | None,
        free_random: bool,
    ) -> _BuiltQuery:
        if require_top_n and feature_id != "select_query_specification":
            raise TargetNotReachable("directed top-N is a SELECT query-specification shape")
        if feature_id == "select_query_specification":
            if directed_variant == "scalar_literal":
                return self._scalar_literal()
            if directed_variant == "scalar_aggregate":
                return self._scalar_aggregate()
            if directed_variant == "limit_zero":
                return self._limit_zero()
            if directed_variant == "limit_zero_offset":
                return self._limit_zero(offset=1)
            if directed_variant == "table_limit_zero":
                return self._table_limit_zero(manifest, rows)
            if directed_variant == "table_limit_zero_offset":
                return self._table_limit_zero(manifest, rows, offset=1)
            if directed_variant == "scalar_offset_limit":
                return self._scalar_offset_limit()
            if directed_variant in {"offset_limit", "table_offset_limit"}:
                return self._offset_limit(manifest, rows)
            if directed_variant in _SELECT_LEAF_VARIANTS:
                return self._select_leaf(manifest, rows, variant=directed_variant)
            if directed_variant is None and rng.randrange(8) == 7:
                return self._select_leaf(
                    manifest,
                    rows,
                    variant=rng.choice(sorted(_SELECT_LEAF_VARIANTS)),
                )
            return self._simple(manifest, rows, top_n=require_top_n, free_random=free_random)
        if feature_id == "select_parenthesized":
            return self._parenthesized(manifest, rows)
        if feature_id == "select_nested_parenthesized_top_n":
            return self._nested_parenthesized_top_n(manifest, rows)
        if feature_id == "join_inner_cross_straight":
            return self._join(manifest, rows, rng, outer=False, directed=directed_variant)
        if feature_id == "join_outer_natural":
            return self._join(manifest, rows, rng, outer=True, directed=directed_variant)
        if feature_id in {"subquery_result_kinds", "regression_8041_subquery_materialization"}:
            if directed_variant in {"table_limit", "scalar_limit"}:
                return self._subquery_limit(
                    manifest,
                    rows,
                    scalar=directed_variant == "scalar_limit",
                )
            return self._subquery(
                manifest,
                rows,
                materialized=feature_id.startswith("regression"),
                rng=rng,
                directed=directed_variant,
            )
        if feature_id == "subquery_quantified":
            return self._quantified_subquery(
                manifest,
                rows,
                rng,
                directed=directed_variant,
            )
        if feature_id in {"derived_regular", "derived_explicit_columns"}:
            if directed_variant not in {None, "implicit_columns", "explicit_columns"}:
                raise ValueError(f"unknown directed derived variant: {directed_variant}")
            explicit_columns = (
                feature_id == "derived_explicit_columns"
                or directed_variant == "explicit_columns"
                or (directed_variant is None and bool(rng.getrandbits(1)))
            )
            return self._derived(
                manifest,
                rows,
                lateral=False,
                explicit_columns=explicit_columns,
            )
        if feature_id == "lateral_correlated":
            return self._derived(manifest, rows, lateral=True, explicit_columns=False)
        if feature_id == "cte_nonrecursive":
            return self._cte(
                manifest,
                rows,
                recursive=False,
                rng=rng,
                directed=directed_variant,
            )
        if feature_id == "cte_recursive":
            return self._cte(
                manifest,
                rows,
                recursive=True,
                rng=rng,
                directed=directed_variant,
            )
        if feature_id in {"set_union", "set_intersect", "set_except"}:
            if directed_variant == "scalar_intersect_except":
                return self._scalar_intersect_except()
            operation = {
                "set_union": SetOperator.UNION,
                "set_intersect": SetOperator.INTERSECT,
                "set_except": SetOperator.EXCEPT,
            }[feature_id]
            expected_all_variant = (
                f"{operation.value.lower()}_all"
                if operation in {SetOperator.INTERSECT, SetOperator.EXCEPT}
                else None
            )
            type_variants = tuple(
                f"{operation.value.lower()}_{domain}" for domain in _SET_TYPE_DOMAINS
            )
            choices = (
                "base",
                *_SET_PRECEDENCE_VARIANTS[operation],
                *type_variants,
                *((expected_all_variant,) if expected_all_variant is not None else ()),
            )
            variant = directed_variant or rng.choice(choices)
            if variant in _SET_PRECEDENCE_VARIANTS[operation]:
                return self._set_precedence(manifest, rows, variant=variant)
            if variant in type_variants:
                return self._typed_set_operation(
                    manifest,
                    rows,
                    operation=operation,
                    domain=variant.removeprefix(f"{operation.value.lower()}_"),
                )
            if variant not in {"base", expected_all_variant}:
                raise ValueError(f"unknown directed set variant: {directed_variant}")
            return self._set_operation(
                manifest,
                rows,
                operation,
                chain=False,
                all_rows=variant == expected_all_variant,
            )
        if feature_id == "set_branch_local_top_n":
            if directed_variant == "scalar_branch_local_top_n":
                return self._scalar_branch_local_top_n()
            if directed_variant not in {None, "table_branch_local_top_n"}:
                raise ValueError(f"unknown directed branch-local variant: {directed_variant}")
            return self._set_branch_local_top_n(manifest, rows)
        if feature_id == "set_table_values":
            if directed_variant == "values_only":
                return self._values_only(limit=False)
            if directed_variant == "values_limit":
                return self._values_only(limit=True)
            if directed_variant not in {None, "select_values"}:
                raise ValueError(f"unknown directed TABLE/VALUES variant: {directed_variant}")
            return self._set_values(manifest, rows)
        if feature_id == "table_explicit":
            return self._explicit_table(
                manifest,
                rows,
                max_projection=budget.max_projection,
            )
        if feature_id == "table_values_union":
            if directed_variant not in {
                None,
                "table_values_union",
                "table_values_union_all",
                "table_values_union_distinct",
            }:
                raise ValueError(f"unknown directed TABLE/VALUES UNION variant: {directed_variant}")
            return self._explicit_table_values(
                manifest,
                rows,
                max_projection=budget.max_projection,
                all_rows=directed_variant != "table_values_union_distinct",
            )
        if feature_id == "table_subquery_exists":
            return self._explicit_table_subquery(manifest, rows)
        if feature_id in {
            "grouping_aggregate_having",
            "grouping_with_rollup",
            "function_aggregate",
        }:
            if directed_variant == "scalar_rollup":
                return self._scalar_rollup()
            if directed_variant == "table_grouping_function":
                return self._grouping_function(manifest, rows)
            if feature_id == "grouping_with_rollup" and directed_variant not in {
                None,
                "table_rollup",
                "table_grouping_function",
            }:
                raise ValueError(f"unknown directed rollup variant: {directed_variant}")
            if feature_id == "function_aggregate" and (
                directed_variant in _AGGREGATE_VARIANTS
                or (directed_variant is None and rng.randrange(2) == 0)
            ):
                variant = directed_variant or rng.choice(sorted(_AGGREGATE_VARIANTS))
                return self._aggregate_semantics(manifest, rows, variant=variant)
            if feature_id == "function_aggregate" and directed_variant not in {
                None,
                "grouping",
            }:
                raise ValueError(f"unknown directed aggregate variant: {directed_variant}")
            return self._grouping(
                manifest,
                rows,
                rollup=feature_id == "grouping_with_rollup",
                having=feature_id == "grouping_aggregate_having",
            )
        if feature_id in {"window_inline_named", "window_frames"}:
            return self._window(
                manifest,
                rows,
                frame=feature_id == "window_frames",
                rng=rng,
                directed=directed_variant,
            )
        if feature_id in {"json_table_columns", "json_table_implicit_lateral"}:
            return self._json_table(
                manifest,
                rows,
                implicit=feature_id == "json_table_implicit_lateral",
                max_elements_per_row=budget.max_json_elements_per_row,
            )
        if feature_id in {
            "json_create_extract",
            "json_member_overlap",
            "json_value_scalar",
            "json_schema_validation",
        }:
            return self._json_function(manifest, rows, feature_id)
        if feature_id in {"case_simple", "case_searched"}:
            return self._case(manifest, rows, searched=feature_id == "case_searched")
        if feature_id.startswith("optimizer_hint_"):
            return self._optimizer_hint(manifest, rows, feature_id)
        if feature_id in {"partition_explicit_selection", "scene_partitioned"}:
            return self._partition(manifest, rows)
        if feature_id in {
            "function_deterministic_scalar",
            "function_version_import",
        }:
            if feature_id == "function_deterministic_scalar" and (
                directed_variant in (_PREDICATE_VARIANTS | _NULL_PREDICATE_VARIANTS)
                or (directed_variant is None and rng.randrange(2) == 0)
            ):
                variant = directed_variant or rng.choice(
                    sorted(_PREDICATE_VARIANTS | _NULL_PREDICATE_VARIANTS)
                )
                if variant in _NULL_PREDICATE_VARIANTS:
                    return self._null_predicate_semantics(variant=variant)
                return self._predicate_semantics(manifest, rows, variant=variant)
            return self._deterministic_function(
                manifest,
                rows,
                rng=rng,
                directed=directed_variant,
            )
        if feature_id == "function_fulltext_spatial":
            return self._profile_function(manifest, rows)
        if feature_id == "regression_8041_union_view_charset":
            return self._union_charset(manifest, rows)
        if feature_id == "regression_8041_desc_pk_index_merge":
            return self._index_merge(manifest, rows)
        if feature_id == "regression_8041_union_chain_flatten":
            return self._set_operation(
                manifest,
                rows,
                SetOperator.UNION,
                chain=True,
                all_rows=True,
            )
        if feature_id == "regression_8041_rollup_row_comparator":
            return self._rollup_row_comparator(manifest, rows)
        if feature_id == "regression_8041_antijoin_spill_null_key":
            return self._anti_join(manifest, rows)
        if feature_id == "regression_8041_distinct_not_in":
            return self._distinct_not_in(manifest, rows)
        if feature_id == "regression_8041_hint_lexer":
            return self._hint_lexer(manifest, rows)
        if feature_id in {
            "scene_regular",
            "scene_temporary",
            "scene_foreign_key",
            "scene_fulltext",
            "scene_spatial",
            "scene_json_multivalue",
        }:
            return self._scene(manifest, rows)
        if feature_id in {
            "index_prefix",
            "index_descending",
            "index_functional",
            "index_multivalue",
            "index_fulltext",
            "index_spatial",
        }:
            return self._index_shape(manifest, rows, feature_id)
        if feature_id in {
            "type_numeric_boundaries",
            "type_string_lob_boundaries",
            "type_temporal_json_spatial",
        }:
            return self._type_domain(manifest, rows, feature_id)
        raise UnsupportedQueryFeature(feature_id)  # pragma: no cover - registry equality test

    @staticmethod
    def _type(column: ColumnDef) -> SqlType:
        if column.base_type in {
            "TINYINT",
            "SMALLINT",
            "MEDIUMINT",
            "INT",
            "BIGINT",
            "BIT",
            "DECIMAL",
            "FLOAT",
            "DOUBLE",
            "YEAR",
        }:
            return SqlType.NUMERIC
        if column.base_type in {
            "CHAR",
            "VARCHAR",
            "TINYTEXT",
            "TEXT",
            "MEDIUMTEXT",
            "LONGTEXT",
            "ENUM",
            "SET",
        }:
            return SqlType.TEXT
        if column.base_type in {"DATE", "TIME", "DATETIME", "TIMESTAMP"}:
            return SqlType.TEMPORAL
        if column.base_type == "JSON":
            return SqlType.JSON
        if column.srid is not None:
            return SqlType.SPATIAL
        return SqlType.BINARY

    @classmethod
    def _column(cls, alias: str, column: ColumnDef) -> ColumnRef:
        return ColumnRef(alias, column.name, cls._type(column))

    @staticmethod
    def _id(table: TableDef, alias: str) -> ColumnRef:
        return ColumnRef(alias, table.column("id").name, SqlType.NUMERIC)

    @staticmethod
    def _unique_key(table: TableDef) -> tuple[str, ...] | None:
        by_name = {column.name: column for column in table.columns}
        for index in table.indexes:
            if not (index.primary or index.unique) or index.kind is not IndexKind.BTREE:
                continue
            names: list[str] = []
            for part in index.parts:
                if part.column_name is None or part.prefix_length is not None:
                    break
                column = by_name[part.column_name]
                if column.nullable:
                    break
                names.append(column.name)
            else:
                if names:
                    return tuple(names)
        return None

    @staticmethod
    def _index_hint_name(table: TableDef) -> str:
        for index in table.indexes:
            if index.kind is IndexKind.BTREE and index.visible and not index.primary:
                return index.name
        for index in table.indexes:
            if index.kind is IndexKind.BTREE and index.visible and index.primary:
                return "PRIMARY"
        raise TargetNotReachable("table index hints require a visible BTREE index")

    @classmethod
    def _base_projection(
        cls, table: TableDef, alias: str
    ) -> tuple[tuple[Projection, ...], frozenset[frozenset[int]]]:
        key = cls._unique_key(table)
        selected_names: list[str] = []
        if key is not None:
            selected_names.extend(key)
        if "id" not in selected_names:
            selected_names.insert(0, "id")
        preferred = next(
            (
                column.name
                for column in table.columns
                if column.name not in selected_names
                and cls._type(column) in {SqlType.NUMERIC, SqlType.TEXT, SqlType.TEMPORAL}
            ),
            None,
        )
        if preferred is not None:
            selected_names.append(preferred)
        projection = tuple(
            Projection(cls._column(alias, table.column(name)), alias=f"q{ordinal}")
            for ordinal, name in enumerate(selected_names, start=1)
        )
        unique_sets: frozenset[frozenset[int]] = frozenset()
        if key is not None:
            ordinals = frozenset(selected_names.index(name) + 1 for name in key)
            unique_sets = frozenset({ordinals})
        return projection, unique_sets

    @staticmethod
    def _ast(
        body: QueryBody,
        *,
        projection_count: int,
        max_rows: int,
        unique_sets: frozenset[frozenset[int]] = frozenset(),
        ctes: tuple[Cte, ...] = (),
        recursive: bool = False,
        limit: int | None = None,
        windows: tuple[WindowOrder, ...] = (),
        descending: frozenset[int] = frozenset(),
        offset: int | None = None,
        order_by: tuple[int, ...] | None = None,
    ) -> QueryAst:
        scope = QueryScope(projection_count, unique_sets, max_rows)
        if order_by is None:
            if unique_sets:
                smallest_unique = min(
                    unique_sets,
                    key=lambda ordinals: (len(ordinals), tuple(sorted(ordinals))),
                )
                order_by = tuple(sorted(smallest_unique | descending))
            else:
                order_by = tuple(range(1, projection_count + 1))
        return QueryAst(
            body,
            OrderBy(order_by, descending),
            scope,
            ctes,
            recursive,
            limit,
            windows,
            offset,
        )

    @staticmethod
    def _complexity(
        *,
        tables: int,
        depth: int,
        ctes: int,
        branches: int,
        projection: int,
        predicates: int,
        scanned: int,
        intermediate: int,
        output: int,
    ) -> QueryComplexity:
        return QueryComplexity(
            tables,
            depth,
            ctes,
            branches,
            projection,
            predicates,
            scanned,
            intermediate,
            output,
        )

    def _scalar_literal(self) -> _BuiltQuery:
        body = SelectQuery(
            (Projection(Literal(1, SqlType.NUMERIC), alias="q1"),),
        )
        ast = self._ast(
            body,
            projection_count=1,
            max_rows=1,
            unique_sets=frozenset({frozenset({1})}),
        )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=0,
                depth=1,
                ctes=0,
                branches=1,
                projection=1,
                predicates=0,
                scanned=0,
                intermediate=1,
                output=1,
            ),
            frozenset({"scalar_literal"}),
        )

    def _scalar_aggregate(self) -> _BuiltQuery:
        count = FunctionCall(FunctionName.COUNT, (Star(),), SqlType.NUMERIC)
        body = SelectQuery((Projection(count, alias="row_count"),))
        ast = self._ast(
            body,
            projection_count=1,
            max_rows=1,
            unique_sets=frozenset({frozenset({1})}),
        )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=0,
                depth=1,
                ctes=0,
                branches=1,
                projection=1,
                predicates=0,
                scanned=0,
                intermediate=1,
                output=1,
            ),
            frozenset({"scalar_literal", "aggregate"}),
        )

    def _limit_zero(self, *, offset: int | None = None) -> _BuiltQuery:
        body = SelectQuery((Projection(Literal(1, SqlType.NUMERIC), alias="q1"),))
        ast = self._ast(
            body,
            projection_count=1,
            max_rows=0,
            limit=0,
            offset=offset,
        )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=0,
                depth=1,
                ctes=0,
                branches=1,
                projection=1,
                predicates=0,
                scanned=0,
                intermediate=1,
                output=0,
            ),
            frozenset(
                {"scalar_literal", "limit_zero"} | ({"offset"} if offset is not None else set())
            ),
        )

    def _offset_limit(self, manifest: SchemaManifest, rows: Mapping[str, int]) -> _BuiltQuery:
        table = manifest.tables[0]
        projection, unique_sets = self._base_projection(table, "t")
        if not unique_sets:
            count = FunctionCall(FunctionName.COUNT, (Star(),), SqlType.NUMERIC)
            body = SelectQuery(
                (Projection(count, alias="row_count"),),
                TableRelation(table.name, "t"),
            )
            ast = self._ast(
                body,
                projection_count=1,
                max_rows=1,
                unique_sets=frozenset({frozenset({1})}),
                limit=1,
                offset=1,
            )
            return _BuiltQuery(
                ast,
                self._complexity(
                    tables=1,
                    depth=1,
                    ctes=0,
                    branches=1,
                    projection=1,
                    predicates=0,
                    scanned=rows[table.name],
                    intermediate=1,
                    output=0,
                ),
                frozenset({"aggregate", "offset"}),
            )
        maximum = min(1, max(0, rows[table.name] - 1))
        ast = self._ast(
            SelectQuery(projection, TableRelation(table.name, "t")),
            projection_count=len(projection),
            max_rows=maximum,
            unique_sets=unique_sets,
            limit=1,
            offset=1,
        )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=1,
                depth=1,
                ctes=0,
                branches=1,
                projection=len(projection),
                predicates=0,
                scanned=rows[table.name],
                intermediate=rows[table.name],
                output=maximum,
            ),
            frozenset({"offset", "top_n"}),
        )

    def _scalar_offset_limit(self) -> _BuiltQuery:
        body = SelectQuery((Projection(Literal(1, SqlType.NUMERIC), alias="q1"),))
        ast = self._ast(
            body,
            projection_count=1,
            max_rows=0,
            unique_sets=frozenset({frozenset({1})}),
            limit=1,
            offset=1,
        )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=0,
                depth=1,
                ctes=0,
                branches=1,
                projection=1,
                predicates=0,
                scanned=0,
                intermediate=1,
                output=0,
            ),
            frozenset({"scalar_literal", "offset"}),
        )

    def _table_limit_zero(
        self,
        manifest: SchemaManifest,
        rows: Mapping[str, int],
        *,
        offset: int | None = None,
    ) -> _BuiltQuery:
        table = manifest.tables[0]
        count = FunctionCall(FunctionName.COUNT, (Star(),), SqlType.NUMERIC)
        body = SelectQuery(
            (Projection(count, alias="row_count"),),
            TableRelation(table.name, "t"),
        )
        ast = self._ast(
            body,
            projection_count=1,
            max_rows=0,
            unique_sets=frozenset({frozenset({1})}),
            limit=0,
            offset=offset,
        )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=1,
                depth=1,
                ctes=0,
                branches=1,
                projection=1,
                predicates=0,
                scanned=rows[table.name],
                intermediate=1,
                output=0,
            ),
            frozenset({"aggregate", "limit_zero"} | ({"offset"} if offset is not None else set())),
        )

    def _simple(
        self,
        manifest: SchemaManifest,
        rows: Mapping[str, int],
        *,
        top_n: bool,
        free_random: bool,
    ) -> _BuiltQuery:
        table = manifest.tables[0]
        count = rows[table.name]
        projection, unique_sets = self._base_projection(table, "t")
        if top_n and not unique_sets:
            count_call = FunctionCall(FunctionName.COUNT, (Star(),), SqlType.NUMERIC)
            body = SelectQuery(
                (Projection(count_call, "row_count"),), TableRelation(table.name, "t")
            )
            ast = self._ast(body, projection_count=1, max_rows=1, limit=1)
            complexity = self._complexity(
                tables=1,
                depth=1,
                ctes=0,
                branches=1,
                projection=1,
                predicates=0,
                scanned=count,
                intermediate=count,
                output=1,
            )
            return _BuiltQuery(ast, complexity, frozenset({"top_n", "singleton_tiebreaker"}))
        predicate: Expression = BinaryExpression(
            self._id(table, "t"),
            BinaryOperator.GE,
            Literal(0, SqlType.NUMERIC),
            SqlType.BOOLEAN,
        )
        if free_random and len(projection) > 1:
            predicate = BinaryExpression(
                predicate,
                BinaryOperator.AND,
                UnaryExpression(UnaryOperator.IS_NOT_NULL, projection[0].expression),
                SqlType.BOOLEAN,
            )
        limit = min(10, max(1, count)) if top_n else None
        body = SelectQuery(projection, TableRelation(table.name, "t"), predicate)
        ast = self._ast(
            body,
            projection_count=len(projection),
            max_rows=min(count, limit) if limit else count,
            unique_sets=unique_sets,
            limit=limit,
        )
        complexity = self._complexity(
            tables=1,
            depth=1,
            ctes=0,
            branches=1,
            projection=len(projection),
            predicates=2 if free_random else 1,
            scanned=count,
            intermediate=count,
            output=min(count, limit) if limit else count,
        )
        tags = {"predicate"}
        if top_n:
            tags.add("top_n")
        return _BuiltQuery(ast, complexity, frozenset(tags))

    def _select_leaf(
        self,
        manifest: SchemaManifest,
        rows: Mapping[str, int],
        *,
        variant: str,
    ) -> _BuiltQuery:
        if variant not in _SELECT_LEAF_VARIANTS:
            raise ValueError(f"unknown directed SELECT leaf: {variant}")
        table = manifest.tables[0]
        maximum = rows[table.name]
        key = self._unique_key(table)
        if key is None:
            raise TargetNotReachable("SELECT leaf ordering requires a unique key")
        key_positions = frozenset(table.columns.index(table.column(name)) + 1 for name in key)
        projection: tuple[Projection, ...]

        if variant in {"star", "qualified_star"}:
            projection_count = len(table.columns)
            projection = (Projection(Star("t" if variant == "qualified_star" else None)),)
            body = SelectQuery(projection, TableRelation(table.name, "t"))
            scope = QueryScope(projection_count, frozenset({key_positions}), maximum)
            ast = QueryAst(body, OrderBy(tuple(sorted(key_positions))), scope)
        else:
            projection, unique_sets = self._base_projection(table, "t")
            projection_count = len(projection)
            if variant == "order_by_alias":
                proof = tuple(sorted(next(iter(unique_sets))))
                aliases = tuple(projection[ordinal - 1].alias or "" for ordinal in proof)
                body = SelectQuery(projection, TableRelation(table.name, "t"))
                ast = QueryAst(
                    body,
                    OrderBy((), aliases=aliases, projection_ordinals=proof),
                    QueryScope(projection_count, unique_sets, maximum),
                )
            elif variant == "order_by_expression":
                proof = tuple(sorted(next(iter(unique_sets))))
                expressions = tuple(projection[ordinal - 1].expression for ordinal in proof)
                body = SelectQuery(projection, TableRelation(table.name, "t"))
                ast = QueryAst(
                    body,
                    OrderBy(
                        (),
                        expressions=expressions,
                        projection_ordinals=proof,
                    ),
                    QueryScope(projection_count, unique_sets, maximum),
                )
            else:
                modifier = {
                    "modifier_all": SelectModifier.ALL,
                    "modifier_distinctrow": SelectModifier.DISTINCTROW,
                    "modifier_high_priority": SelectModifier.HIGH_PRIORITY,
                    "modifier_straight_join": SelectModifier.STRAIGHT_JOIN,
                    "modifier_sql_calc_found_rows": SelectModifier.SQL_CALC_FOUND_ROWS,
                    "modifier_sql_no_cache": SelectModifier.SQL_NO_CACHE,
                    "modifier_sql_small_result": SelectModifier.SQL_SMALL_RESULT,
                    "modifier_sql_big_result": SelectModifier.SQL_BIG_RESULT,
                    "modifier_sql_buffer_result": SelectModifier.SQL_BUFFER_RESULT,
                }[variant]
                if modifier is SelectModifier.DISTINCTROW:
                    unique_sets = unique_sets | frozenset(
                        {frozenset(range(1, projection_count + 1))}
                    )
                body = SelectQuery(
                    projection,
                    TableRelation(table.name, "t"),
                    modifiers=(modifier,),
                )
                ast = self._ast(
                    body,
                    projection_count=projection_count,
                    max_rows=maximum,
                    unique_sets=unique_sets,
                )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=1,
                depth=1,
                ctes=0,
                branches=1,
                projection=projection_count,
                predicates=0,
                scanned=maximum,
                intermediate=maximum,
                output=maximum,
            ),
            frozenset({f"select_{variant}"}),
        )

    def _parenthesized(self, manifest: SchemaManifest, rows: Mapping[str, int]) -> _BuiltQuery:
        base = self._simple(manifest, rows, top_n=False, free_random=False)
        ast = QueryAst(
            ParenthesizedQuery(base.ast.body),
            base.ast.order_by,
            base.ast.scope,
        )
        return _BuiltQuery(ast, base.complexity, frozenset({"parenthesized"}))

    def _nested_parenthesized_top_n(
        self,
        manifest: SchemaManifest,
        rows: Mapping[str, int],
    ) -> _BuiltQuery:
        table = manifest.tables[0]
        unique = frozenset({frozenset({1})})
        select = SelectQuery(
            (Projection(self._id(table, "t"), "id"),),
            TableRelation(table.name, "t"),
        )
        inner = ParenthesizedQuery(
            select,
            order_by=(1,),
            limit=5,
            unique_projection_sets=unique,
            max_rows=rows[table.name],
        )
        middle = ParenthesizedQuery(
            inner,
            order_by=(1,),
            limit=3,
            unique_projection_sets=unique,
            max_rows=min(rows[table.name], 5),
        )
        ast = self._ast(
            middle,
            projection_count=1,
            max_rows=min(rows[table.name], 3),
            unique_sets=unique,
            limit=2,
        )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=1,
                depth=3,
                ctes=0,
                branches=1,
                projection=1,
                predicates=0,
                scanned=rows[table.name],
                intermediate=min(rows[table.name], 5),
                output=min(rows[table.name], 2),
            ),
            frozenset({"nested_parenthesized_order_limit", "parenthesized_query"}),
        )

    def _join(
        self,
        manifest: SchemaManifest,
        rows: Mapping[str, int],
        rng: random.Random,
        *,
        outer: bool,
        directed: str | None,
    ) -> _BuiltQuery:
        left = manifest.tables[0]
        right = manifest.tables[1] if len(manifest.tables) > 1 else manifest.tables[0]
        left_nullable = next(
            (
                column
                for column in left.columns
                if column.nullable and self._type(column) is SqlType.NUMERIC
            ),
            None,
        )
        right_nullable = next(
            (
                column
                for column in right.columns
                if column.nullable and self._type(column) is SqlType.NUMERIC
            ),
            None,
        )
        if outer:
            choices = {
                "left": JoinKind.LEFT,
                "left_using": JoinKind.LEFT,
                "right": JoinKind.RIGHT,
                "right_using": JoinKind.RIGHT,
                "natural_left": JoinKind.NATURAL_LEFT,
                "natural_right": JoinKind.NATURAL_RIGHT,
            }
        else:
            choices = {
                "comma": JoinKind.COMMA,
                "inner": JoinKind.INNER,
                "inner_conditionless": JoinKind.INNER,
                "inner_using": JoinKind.INNER,
                "cross": JoinKind.CROSS,
                "natural_inner": JoinKind.NATURAL_INNER,
                "straight": JoinKind.STRAIGHT,
            }
            nested_tables = (
                left,
                right,
                manifest.tables[2] if len(manifest.tables) > 2 else left,
            )
            if directed == "nested_three" or all(
                self._unique_key(table) == ("id",) for table in nested_tables
            ):
                choices["nested_three"] = JoinKind.INNER
            has_btree = any(
                index.kind is IndexKind.BTREE and index.visible for index in left.indexes
            )
            if directed in _INDEX_HINT_VARIANTS and not has_btree:
                raise TargetNotReachable("table index hints require a visible BTREE index")
            if has_btree:
                choices.update({variant: JoinKind.INNER for variant in _INDEX_HINT_VARIANTS})
            if left_nullable is not None:
                choices["nullable_key_left"] = JoinKind.INNER
            if right_nullable is not None:
                choices["nullable_key_right"] = JoinKind.INNER
            if left_nullable is not None and right_nullable is not None:
                choices["nullable_key_both"] = JoinKind.INNER
        subquery = directed in {"left_subquery", "inner_subquery"}
        cast_projection = directed == "inner_cast"
        directed_kind = (
            "left"
            if directed == "left_subquery"
            else "inner"
            if directed in {"inner_subquery", "inner_cast"}
            else directed
        )
        if directed_kind is not None and directed_kind not in choices:
            if directed_kind.startswith("nullable_key_"):
                raise TargetNotReachable(
                    "nullable-key JOIN requires compatible nullable numeric columns"
                )
            raise ValueError(f"unknown directed join variant: {directed}")
        variant = directed_kind or rng.choice(sorted(choices))
        if variant == "nested_three":
            return self._nested_three_join(manifest, rows)
        kind = choices[variant]
        left_join_key = self._id(left, "t")
        right_join_key = self._id(right, "u")
        if variant in {"nullable_key_left", "nullable_key_both"}:
            assert left_nullable is not None
            left_join_key = self._column("t", left_nullable)
        if variant in {"nullable_key_right", "nullable_key_both"}:
            assert right_nullable is not None
            right_join_key = self._column("u", right_nullable)
        equality = BinaryExpression(
            left_join_key,
            BinaryOperator.EQ,
            right_join_key,
            SqlType.BOOLEAN,
        )
        natural = kind in {
            JoinKind.NATURAL_INNER,
            JoinKind.NATURAL_LEFT,
            JoinKind.NATURAL_RIGHT,
        }
        using_columns = (
            ("id",)
            if variant
            in {
                "inner_using",
                "left_using",
                "right_using",
            }
            else ()
        )
        index_hint = _INDEX_HINT_VARIANTS.get(variant)
        hints: tuple[IndexHint, ...] = ()
        if index_hint is not None:
            action, scope = index_hint
            hints = (IndexHint(action, (self._index_hint_name(left),), scope),)
        conditionless = (
            natural or kind in {JoinKind.COMMA, JoinKind.CROSS} or variant == "inner_conditionless"
        )
        relation = JoinRelation(
            TableRelation(left.name, "t", index_hints=hints),
            TableRelation(right.name, "u"),
            kind,
            None if conditionless or using_columns else equality,
            using_columns,
        )
        predicate: Expression | None = (
            equality if kind in {JoinKind.COMMA, JoinKind.CROSS} else None
        )
        if subquery:
            correlated = SelectQuery(
                (Projection(Literal(1, SqlType.NUMERIC), "one"),),
                TableRelation(right.name, "v"),
                BinaryExpression(
                    self._id(right, "v"),
                    BinaryOperator.EQ,
                    self._id(left, "t"),
                    SqlType.BOOLEAN,
                ),
            )
            predicate = SubqueryExpression(SubqueryOperator.EXISTS, correlated)
        right_projection: Expression = self._id(right, "u")
        if cast_projection:
            right_projection = CastExpression(right_projection, "SIGNED", SqlType.NUMERIC)
        projection = (
            Projection(self._id(left, "t"), "left_id"),
            Projection(right_projection, "right_id"),
        )
        product = rows[left.name] * rows[right.name]
        non_key_join = variant.startswith("nullable_key_") or variant == "inner_conditionless"
        key_left = not non_key_join and self._unique_key(left) == ("id",)
        key_right = not non_key_join and self._unique_key(right) == ("id",)
        if key_left and key_right:
            if kind in {JoinKind.LEFT, JoinKind.NATURAL_LEFT}:
                estimate = rows[left.name]
            elif kind in {JoinKind.RIGHT, JoinKind.NATURAL_RIGHT}:
                estimate = rows[right.name]
            else:
                estimate = min(rows[left.name], rows[right.name])
        else:
            if kind in {JoinKind.LEFT, JoinKind.NATURAL_LEFT}:
                estimate = rows[left.name] * max(1, rows[right.name])
            elif kind in {JoinKind.RIGHT, JoinKind.NATURAL_RIGHT}:
                estimate = rows[right.name] * max(1, rows[left.name])
            else:
                estimate = product
        # CROSS/comma evaluate the full Cartesian intermediate even when WHERE bounds output.
        intermediate = (
            product
            if kind in {JoinKind.COMMA, JoinKind.CROSS} or variant == "inner_conditionless"
            else estimate
        )
        if key_left and key_right and kind in {JoinKind.RIGHT, JoinKind.NATURAL_RIGHT}:
            unique_sets = frozenset({frozenset({2})})
        elif key_left and key_right:
            unique_sets = frozenset({frozenset({1})})
        else:
            unique_sets = frozenset()
        body = SelectQuery(projection, relation, predicate)
        ast = self._ast(
            body,
            projection_count=2,
            max_rows=estimate,
            unique_sets=unique_sets,
        )
        correlated_work = rows[left.name] * rows[right.name] if subquery else 0
        complexity = self._complexity(
            tables=2,
            depth=2 if subquery else 1,
            ctes=0,
            branches=1,
            projection=2,
            predicates=2 if subquery else 1,
            scanned=rows[left.name] + rows[right.name] + correlated_work,
            intermediate=max(intermediate, correlated_work),
            output=estimate,
        )
        if variant in _INDEX_HINT_VARIANTS:
            tag = f"table_{variant}"
        elif directed in {"left_subquery", "inner_subquery", "inner_cast"}:
            tag = f"join_{directed}"
        else:
            tag = f"join_{variant}"
        return _BuiltQuery(ast, complexity, frozenset({tag}))

    def _nested_three_join(
        self,
        manifest: SchemaManifest,
        rows: Mapping[str, int],
    ) -> _BuiltQuery:
        left = manifest.tables[0]
        middle = manifest.tables[1] if len(manifest.tables) > 1 else left
        right = manifest.tables[2] if len(manifest.tables) > 2 else left
        left_middle = BinaryExpression(
            self._id(left, "t"),
            BinaryOperator.EQ,
            self._id(middle, "u"),
            SqlType.BOOLEAN,
        )
        left_right = BinaryExpression(
            self._id(left, "t"),
            BinaryOperator.EQ,
            self._id(right, "v"),
            SqlType.BOOLEAN,
        )
        relation = JoinRelation(
            JoinRelation(
                TableRelation(left.name, "t"),
                TableRelation(middle.name, "u"),
                JoinKind.INNER,
                left_middle,
            ),
            TableRelation(right.name, "v"),
            JoinKind.LEFT,
            left_right,
        )
        projection = (
            Projection(self._id(left, "t"), "left_id"),
            Projection(self._id(middle, "u"), "middle_id"),
            Projection(self._id(right, "v"), "right_id"),
        )
        all_unique = all(self._unique_key(table) == ("id",) for table in (left, middle, right))
        product = rows[left.name] * rows[middle.name] * rows[right.name]
        estimate = min(rows[left.name], rows[middle.name]) if all_unique else product
        unique_sets = frozenset({frozenset({1})}) if all_unique else frozenset()
        ast = self._ast(
            SelectQuery(projection, relation),
            projection_count=3,
            max_rows=estimate,
            unique_sets=unique_sets,
        )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=3,
                depth=2,
                ctes=0,
                branches=1,
                projection=3,
                predicates=2,
                scanned=rows[left.name] + rows[middle.name] + rows[right.name],
                intermediate=estimate if all_unique else product,
                output=estimate,
            ),
            frozenset({"join_nested_three"}),
        )

    def _subquery_limit(
        self,
        manifest: SchemaManifest,
        rows: Mapping[str, int],
        *,
        scalar: bool,
    ) -> _BuiltQuery:
        inner = manifest.tables[1] if len(manifest.tables) > 1 else manifest.tables[0]
        if scalar:
            inner_query = SelectQuery((Projection(Literal(1, SqlType.NUMERIC), "one"),))
            expression = SubqueryExpression(
                SubqueryOperator.SCALAR,
                inner_query,
                sql_type=SqlType.NUMERIC,
            )
            body = SelectQuery((Projection(expression, "q1"),))
            tables = 0
            scanned = 0
            intermediate = 1
            tags = frozenset({"scalar_subquery", "top_n"})
        else:
            outer = manifest.tables[0]
            inner_query = SelectQuery(
                (Projection(Literal(1, SqlType.NUMERIC), "one"),),
                TableRelation(inner.name, "u"),
                BinaryExpression(
                    self._id(inner, "u"),
                    BinaryOperator.EQ,
                    self._id(outer, "t"),
                    SqlType.BOOLEAN,
                ),
            )
            predicate = SubqueryExpression(SubqueryOperator.EXISTS, inner_query)
            count = FunctionCall(FunctionName.COUNT, (Star(),), SqlType.NUMERIC)
            body = SelectQuery(
                (Projection(count, "row_count"),),
                TableRelation(outer.name, "t"),
                predicate,
            )
            tables = 2
            scanned = rows[outer.name] + rows[outer.name] * rows[inner.name]
            intermediate = rows[outer.name] * rows[inner.name]
            tags = frozenset({"table_subquery", "top_n"})
        ast = self._ast(
            body,
            projection_count=1,
            max_rows=1,
            unique_sets=frozenset({frozenset({1})}),
            limit=1,
        )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=tables,
                depth=2,
                ctes=0,
                branches=1,
                projection=1,
                predicates=0 if scalar else 1,
                scanned=scanned,
                intermediate=intermediate,
                output=1,
            ),
            tags,
        )

    def _subquery(
        self,
        manifest: SchemaManifest,
        rows: Mapping[str, int],
        *,
        materialized: bool,
        rng: random.Random,
        directed: str | None,
    ) -> _BuiltQuery:
        outer = manifest.tables[0]
        inner = manifest.tables[1] if len(manifest.tables) > 1 else outer
        minimum = FunctionCall(FunctionName.MIN, (self._id(inner, "u"),), SqlType.NUMERIC)
        inner_query = SelectQuery((Projection(minimum, "min_id"),), TableRelation(inner.name, "u"))
        variant = (
            "materialized"
            if materialized
            else directed
            or rng.choice(
                (
                    "scalar",
                    "row",
                    "exists",
                    "not_exists",
                    "not_in",
                    "not_in_null",
                    "not_exists_empty",
                )
            )
        )
        if not materialized and variant not in {
            "scalar",
            "row",
            "exists",
            "not_exists",
            "not_in",
            "not_in_null",
            "not_exists_empty",
        }:
            raise ValueError(f"unknown directed subquery result kind: {variant}")
        if materialized:
            inner_query = SelectQuery(
                (Projection(self._id(inner, "u"), "inner_id"),),
                TableRelation(inner.name, "u"),
                distinct=True,
            )
            predicate: Expression = SubqueryExpression(
                SubqueryOperator.IN,
                inner_query,
                self._id(outer, "t"),
            )
        elif variant in {"not_in", "not_in_null"}:
            values_query: QueryBody = SelectQuery(
                (Projection(self._id(inner, "u"), "inner_id"),),
                TableRelation(inner.name, "u"),
            )
            if variant == "not_in_null":
                values_query = SetQuery(
                    (
                        values_query,
                        ValuesQuery(((Literal(None, SqlType.NUMERIC),),)),
                    ),
                    SetOperator.UNION,
                    all=True,
                )
            predicate = SubqueryExpression(
                SubqueryOperator.NOT_IN,
                values_query,
                self._id(outer, "t"),
            )
        elif variant == "not_exists_empty":
            inner_query = SelectQuery(
                (Projection(Literal(1, SqlType.NUMERIC), "one"),),
                TableRelation(inner.name, "u"),
                BinaryExpression(
                    Literal(1, SqlType.NUMERIC),
                    BinaryOperator.EQ,
                    Literal(0, SqlType.NUMERIC),
                    SqlType.BOOLEAN,
                ),
            )
            predicate = SubqueryExpression(SubqueryOperator.NOT_EXISTS, inner_query)
        elif variant == "scalar":
            scalar = SubqueryExpression(
                SubqueryOperator.SCALAR,
                inner_query,
                sql_type=SqlType.NUMERIC,
            )
            predicate = BinaryExpression(
                self._id(outer, "t"), BinaryOperator.EQ, scalar, SqlType.BOOLEAN
            )
        elif variant == "row":
            maximum_call = FunctionCall(FunctionName.MAX, (self._id(inner, "u"),), SqlType.NUMERIC)
            inner_query = SelectQuery(
                (
                    Projection(minimum, "min_id"),
                    Projection(maximum_call, "max_id"),
                ),
                TableRelation(inner.name, "u"),
            )
            row_subquery = SubqueryExpression(
                SubqueryOperator.SCALAR,
                inner_query,
                sql_type=SqlType.UNKNOWN,
            )
            predicate = BinaryExpression(
                RowExpression((self._id(outer, "t"), self._id(outer, "t"))),
                BinaryOperator.EQ,
                row_subquery,
                SqlType.BOOLEAN,
            )
        else:
            inner_query = SelectQuery(
                (Projection(Literal(1, SqlType.NUMERIC), "one"),),
                TableRelation(inner.name, "u"),
                BinaryExpression(
                    self._id(inner, "u"),
                    BinaryOperator.EQ,
                    self._id(outer, "t"),
                    SqlType.BOOLEAN,
                ),
            )
            predicate = SubqueryExpression(
                (
                    SubqueryOperator.NOT_EXISTS
                    if variant == "not_exists"
                    else SubqueryOperator.EXISTS
                ),
                inner_query,
            )
        projection, unique = self._base_projection(outer, "t")
        body = SelectQuery(projection, TableRelation(outer.name, "t"), predicate)
        maximum = rows[outer.name]
        ast = self._ast(
            body,
            projection_count=len(projection),
            max_rows=maximum,
            unique_sets=unique,
        )
        correlated = variant in {"exists", "not_exists"}
        correlated_work = rows[outer.name] * rows[inner.name]
        complexity = self._complexity(
            tables=2,
            depth=2,
            ctes=0,
            branches=2 if variant == "not_in_null" else 1,
            projection=len(projection),
            predicates=1,
            scanned=(
                rows[outer.name] + correlated_work
                if correlated
                else rows[outer.name] + rows[inner.name]
            ),
            intermediate=(
                correlated_work if correlated else max(rows[outer.name], rows[inner.name])
            ),
            output=maximum,
        )
        tag = (
            f"subquery_{variant}"
            if variant in {"not_exists", "not_in", "not_in_null", "not_exists_empty"}
            else f"{variant}_subquery"
        )
        return _BuiltQuery(ast, complexity, frozenset({tag}))

    def _quantified_subquery(
        self,
        manifest: SchemaManifest,
        rows: Mapping[str, int],
        rng: random.Random,
        *,
        directed: str | None,
    ) -> _BuiltQuery:
        outer = manifest.tables[0]
        inner = manifest.tables[1] if len(manifest.tables) > 1 else outer
        inner_query = SelectQuery(
            (Projection(self._id(inner, "u"), "inner_id"),),
            TableRelation(inner.name, "u"),
        )
        if directed not in {None, "any", "all"}:
            raise ValueError(f"unknown directed quantified subquery variant: {directed}")
        operator = (
            rng.choice((SubqueryOperator.ANY, SubqueryOperator.ALL))
            if directed is None
            else SubqueryOperator(directed)
        )
        predicate = SubqueryExpression(operator, inner_query, self._id(outer, "t"))
        projection, unique = self._base_projection(outer, "t")
        body = SelectQuery(projection, TableRelation(outer.name, "t"), predicate)
        maximum = rows[outer.name]
        ast = self._ast(
            body, projection_count=len(projection), max_rows=maximum, unique_sets=unique
        )
        complexity = self._complexity(
            tables=2,
            depth=2,
            ctes=0,
            branches=1,
            projection=len(projection),
            predicates=1,
            scanned=rows[outer.name] + rows[inner.name],
            intermediate=max(rows[outer.name], rows[inner.name]),
            output=maximum,
        )
        return _BuiltQuery(ast, complexity, frozenset({f"quantified_{operator.value}"}))

    def _derived(
        self,
        manifest: SchemaManifest,
        rows: Mapping[str, int],
        *,
        lateral: bool,
        explicit_columns: bool,
    ) -> _BuiltQuery:
        outer = manifest.tables[0]
        inner = manifest.tables[1] if len(manifest.tables) > 1 else outer
        relation: Relation
        projection: tuple[Projection, ...]
        if lateral:
            minimum = FunctionCall(FunctionName.MIN, (self._id(inner, "u"),), SqlType.NUMERIC)
            predicate = BinaryExpression(
                self._id(inner, "u"),
                BinaryOperator.LE,
                self._id(outer, "t"),
                SqlType.BOOLEAN,
            )
            derived_body = SelectQuery(
                (Projection(minimum, "min_id"),), TableRelation(inner.name, "u"), predicate
            )
            relation = JoinRelation(
                TableRelation(outer.name, "t"),
                DerivedRelation(derived_body, "d", lateral=True),
                JoinKind.INNER,
                BinaryExpression(
                    Literal(1, SqlType.NUMERIC),
                    BinaryOperator.EQ,
                    Literal(1, SqlType.NUMERIC),
                    SqlType.BOOLEAN,
                ),
            )
            projection = (
                Projection(self._id(outer, "t"), "outer_id"),
                Projection(ColumnRef("d", "min_id", SqlType.NUMERIC), "min_id"),
            )
            maximum = rows[outer.name]
            unique = (
                frozenset({frozenset({1})}) if self._unique_key(outer) == ("id",) else frozenset()
            )
            scanned = rows[outer.name] + rows[outer.name] * rows[inner.name]
            intermediate = rows[outer.name] * rows[inner.name]
        else:
            inner_projection, inner_unique = self._base_projection(inner, "u")
            derived_body = SelectQuery(inner_projection, TableRelation(inner.name, "u"))
            derived_columns = (
                tuple(f"dq{ordinal}" for ordinal in range(1, len(inner_projection) + 1))
                if explicit_columns
                else ()
            )
            relation = DerivedRelation(derived_body, "d", columns=derived_columns)
            projection = tuple(
                Projection(
                    ColumnRef(
                        "d",
                        derived_columns[ordinal] if derived_columns else item.alias or "",
                        item.expression.sql_type,
                    ),
                    item.alias,
                )
                for ordinal, item in enumerate(inner_projection)
            )
            maximum = rows[inner.name]
            unique = inner_unique
            scanned = rows[inner.name]
            intermediate = rows[inner.name]
        body = SelectQuery(projection, relation)
        ast = self._ast(
            body, projection_count=len(projection), max_rows=maximum, unique_sets=unique
        )
        complexity = self._complexity(
            tables=2 if lateral else 1,
            depth=2,
            ctes=0,
            branches=1,
            projection=len(projection),
            predicates=1 if lateral else 0,
            scanned=scanned,
            intermediate=intermediate,
            output=maximum,
        )
        tags = {"lateral" if lateral else "derived"}
        if explicit_columns:
            tags.add("derived_explicit_columns")
        return _BuiltQuery(ast, complexity, frozenset(tags))

    def _cte(
        self,
        manifest: SchemaManifest,
        rows: Mapping[str, int],
        *,
        recursive: bool,
        rng: random.Random,
        directed: str | None,
    ) -> _BuiltQuery:
        table = manifest.tables[0]
        if recursive:
            if directed not in {
                None,
                "recursive_union_all",
                "recursive_union_distinct",
            }:
                raise ValueError(f"unknown directed recursive CTE variant: {directed}")
            variant = directed or "recursive_union_all"
            anchor = SelectQuery((Projection(Literal(1, SqlType.NUMERIC), "n"),))
            current = ColumnRef("r", "n", SqlType.NUMERIC)
            recursive_step = SelectQuery(
                (
                    Projection(
                        BinaryExpression(
                            current,
                            BinaryOperator.ADD,
                            Literal(1, SqlType.NUMERIC),
                            SqlType.NUMERIC,
                        ),
                        "n",
                    ),
                ),
                NamedRelation("r", "r"),
                BinaryExpression(
                    current,
                    BinaryOperator.LT,
                    Literal(8, SqlType.NUMERIC),
                    SqlType.BOOLEAN,
                ),
            )
            cte = Cte(
                "r",
                ("n",),
                SetQuery(
                    (anchor, recursive_step),
                    SetOperator.UNION,
                    all=variant == "recursive_union_all",
                ),
            )
            body = SelectQuery(
                (Projection(ColumnRef("r0", "n", SqlType.NUMERIC), "n"),),
                NamedRelation("r", "r0"),
            )
            ast = self._ast(
                body,
                projection_count=1,
                max_rows=8,
                unique_sets=frozenset({frozenset({1})}),
                ctes=(cte,),
                recursive=True,
            )
            complexity = self._complexity(
                tables=1,
                depth=2,
                ctes=1,
                branches=2,
                projection=1,
                predicates=1,
                scanned=8,
                intermediate=8,
                output=8,
            )
            return _BuiltQuery(
                ast,
                complexity,
                frozenset({"bounded_recursion", f"cte_{variant}"}),
            )
        if directed is not None and directed not in {
            "single",
            "multiple",
            "dependency",
            "reuse",
        }:
            raise ValueError(f"unknown directed CTE variant: {directed}")
        variants: tuple[str, ...] = ("single", "multiple", "dependency")
        if not table.temporary:
            variants += ("reuse",)
        if directed == "reuse" and table.temporary:
            raise TargetNotReachable("CTE reuse cannot reopen a MySQL temporary base table")
        variant = directed or rng.choice(variants)
        projection, unique = self._base_projection(table, "t")
        cte_query = SelectQuery(projection, TableRelation(table.name, "t"))
        aliases = tuple(item.alias or "" for item in projection)
        cte = Cte("c0", aliases, cte_query)
        maximum = rows[table.name]
        ctes: tuple[Cte, ...] = (cte,)
        predicates = 0
        if variant == "single":
            outer_projection = tuple(
                Projection(ColumnRef("c", alias, item.expression.sql_type), alias)
                for alias, item in zip(aliases, projection, strict=True)
            )
            body = SelectQuery(outer_projection, NamedRelation("c0", "c"))
            output_unique = unique
        elif variant == "multiple":
            marker_cte = Cte(
                "c1",
                ("marker",),
                SelectQuery((Projection(Literal(1, SqlType.NUMERIC), "marker"),)),
            )
            ctes = (cte, marker_cte)
            relation = JoinRelation(
                NamedRelation("c0", "c"),
                NamedRelation("c1", "m"),
                JoinKind.INNER,
                BinaryExpression(
                    Literal(1, SqlType.NUMERIC),
                    BinaryOperator.EQ,
                    Literal(1, SqlType.NUMERIC),
                    SqlType.BOOLEAN,
                ),
            )
            outer_projection = (
                *(
                    Projection(ColumnRef("c", alias, item.expression.sql_type), alias)
                    for alias, item in zip(aliases, projection, strict=True)
                ),
                Projection(ColumnRef("m", "marker", SqlType.NUMERIC), "marker"),
            )
            body = SelectQuery(outer_projection, relation)
            output_unique = unique
            predicates = 1
        elif variant == "dependency":
            dependency_projection = tuple(
                Projection(ColumnRef("d0", alias, item.expression.sql_type), alias)
                for alias, item in zip(aliases, projection, strict=True)
            )
            dependency = Cte(
                "c1",
                aliases,
                SelectQuery(dependency_projection, NamedRelation("c0", "d0")),
            )
            ctes = (cte, dependency)
            outer_projection = tuple(
                Projection(ColumnRef("c", alias, item.expression.sql_type), alias)
                for alias, item in zip(aliases, projection, strict=True)
            )
            body = SelectQuery(outer_projection, NamedRelation("c1", "c"))
            output_unique = unique
        else:
            key_alias = aliases[0]
            relation = JoinRelation(
                NamedRelation("c0", "a"),
                NamedRelation("c0", "b"),
                JoinKind.INNER,
                BinaryExpression(
                    ColumnRef("a", key_alias, SqlType.NUMERIC),
                    BinaryOperator.EQ,
                    ColumnRef("b", key_alias, SqlType.NUMERIC),
                    SqlType.BOOLEAN,
                ),
            )
            outer_projection = (
                Projection(ColumnRef("a", key_alias, SqlType.NUMERIC), "left_id"),
                Projection(ColumnRef("b", key_alias, SqlType.NUMERIC), "right_id"),
            )
            body = SelectQuery(outer_projection, relation)
            output_unique = frozenset({frozenset({1})}) if unique else frozenset()
            predicates = 1
        ast = self._ast(
            body,
            projection_count=len(outer_projection),
            max_rows=maximum,
            unique_sets=output_unique,
            ctes=ctes,
        )
        complexity = self._complexity(
            tables=2 if variant in {"multiple", "reuse"} else 1,
            depth=3 if variant == "dependency" else 2,
            ctes=len(ctes),
            branches=1,
            projection=len(outer_projection),
            predicates=predicates,
            scanned=maximum,
            intermediate=maximum,
            output=maximum,
        )
        tag = "cte_nonrecursive" if variant == "single" else f"cte_{variant}"
        return _BuiltQuery(ast, complexity, frozenset({tag}))

    def _set_precedence(
        self,
        manifest: SchemaManifest,
        rows: Mapping[str, int],
        *,
        variant: str,
    ) -> _BuiltQuery:
        allowed = {item for variants in _SET_PRECEDENCE_VARIANTS.values() for item in variants}
        if variant not in allowed:
            raise ValueError(f"unknown directed set precedence variant: {variant}")
        tables = list(manifest.tables[:3])
        while len(tables) < 3:
            tables.append(manifest.tables[0])
        branches = tuple(
            SelectQuery(
                (Projection(Literal(value, SqlType.NUMERIC), "set_value"),),
                TableRelation(table.name, f"p{index}"),
            )
            for index, (table, value) in enumerate(zip(tables, (1, 2, 2), strict=True))
        )
        first, second, third = branches
        if variant == "precedence_union_intersect":
            body: QueryBody = MixedSetQuery(
                first,
                (
                    SetOperation(SetOperator.UNION, second),
                    SetOperation(SetOperator.INTERSECT, third),
                ),
            )
            maximum = 2
        elif variant == "parenthesized_union_intersect":
            body = SetQuery(
                (
                    ParenthesizedQuery(SetQuery((first, second), SetOperator.UNION)),
                    third,
                ),
                SetOperator.INTERSECT,
            )
            maximum = 1
        elif variant == "precedence_except_intersect":
            body = MixedSetQuery(
                first,
                (
                    SetOperation(SetOperator.EXCEPT, second),
                    SetOperation(SetOperator.INTERSECT, third),
                ),
            )
            maximum = 1
        elif variant == "parenthesized_except_intersect":
            body = SetQuery(
                (
                    ParenthesizedQuery(SetQuery((first, second), SetOperator.EXCEPT)),
                    third,
                ),
                SetOperator.INTERSECT,
            )
            maximum = 1
        elif variant == "precedence_union_except":
            body = MixedSetQuery(
                first,
                (
                    SetOperation(SetOperator.UNION, second),
                    SetOperation(SetOperator.EXCEPT, third),
                ),
            )
            maximum = 2
        else:
            body = SetQuery(
                (
                    first,
                    ParenthesizedQuery(SetQuery((second, third), SetOperator.EXCEPT)),
                ),
                SetOperator.UNION,
            )
            maximum = 2
        unique = frozenset({frozenset({1})})
        ast = self._ast(
            body,
            projection_count=1,
            max_rows=maximum,
            unique_sets=unique,
        )
        total_rows = sum(rows[table.name] for table in tables)
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=3,
                depth=2,
                ctes=0,
                branches=3,
                projection=1,
                predicates=0,
                scanned=total_rows,
                intermediate=total_rows,
                output=maximum,
            ),
            frozenset({f"set_{variant}"}),
        )

    @staticmethod
    def _typed_set_expression(domain: str, *, first: bool) -> Expression:
        if domain == "numeric":
            return CastExpression(
                Literal(1 if first else 2, SqlType.NUMERIC),
                "SIGNED",
                SqlType.NUMERIC,
            )
        if domain == "text":
            return CastExpression(
                Literal("alpha" if first else "beta", SqlType.TEXT),
                "CHAR(64)",
                SqlType.TEXT,
            )
        if domain == "binary":
            return Literal(b"\x01" if first else b"\x02", SqlType.BINARY)
        if domain == "temporal":
            return CastExpression(
                Literal(
                    ("2024-01-01 00:00:00.000000" if first else "2024-01-02 00:00:00.000000"),
                    SqlType.TEMPORAL,
                ),
                "DATETIME(6)",
                SqlType.TEMPORAL,
            )
        raise ValueError(f"unknown set type domain: {domain}")

    def _typed_set_operation(
        self,
        manifest: SchemaManifest,
        rows: Mapping[str, int],
        *,
        operation: SetOperator,
        domain: str,
    ) -> _BuiltQuery:
        if domain not in _SET_TYPE_DOMAINS:
            raise ValueError(f"unknown set type domain: {domain}")
        tables = list(manifest.tables[:2])
        while len(tables) < 2:
            tables.append(manifest.tables[0])
        branches = tuple(
            SelectQuery(
                (
                    Projection(
                        self._typed_set_expression(domain, first=index == 0),
                        "typed_value",
                    ),
                ),
                TableRelation(table.name, f"y{index}"),
            )
            for index, table in enumerate(tables)
        )
        maximum = 2 if operation is SetOperator.UNION else 1
        ast = self._ast(
            SetQuery(branches, operation),
            projection_count=1,
            max_rows=maximum,
            unique_sets=frozenset({frozenset({1})}),
        )
        total_rows = sum(rows[table.name] for table in tables)
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=2,
                depth=1,
                ctes=0,
                branches=2,
                projection=1,
                predicates=0,
                scanned=total_rows,
                intermediate=total_rows,
                output=maximum,
            ),
            frozenset({f"set_type_{operation.value.lower()}_{domain}"}),
        )

    def _set_operation(
        self,
        manifest: SchemaManifest,
        rows: Mapping[str, int],
        operator: SetOperator,
        *,
        chain: bool,
        all_rows: bool = False,
    ) -> _BuiltQuery:
        tables = list(manifest.tables[: 3 if chain else 2])
        while len(tables) < (3 if chain else 2):
            tables.append(manifest.tables[0])
        branches = tuple(
            SelectQuery(
                (Projection(self._id(table, f"s{index}"), "id"),),
                TableRelation(table.name, f"s{index}"),
            )
            for index, table in enumerate(tables)
        )
        total_rows = sum(rows[table.name] for table in tables)
        if operator is SetOperator.INTERSECT:
            maximum = min(rows[table.name] for table in tables)
        elif operator is SetOperator.EXCEPT:
            maximum = rows[tables[0].name]
        else:
            maximum = total_rows
        body = SetQuery(branches, operator, all=all_rows)
        unique = frozenset() if all_rows else frozenset({frozenset({1})})
        ast = self._ast(body, projection_count=1, max_rows=maximum, unique_sets=unique)
        complexity = self._complexity(
            tables=len(tables),
            depth=1,
            ctes=0,
            branches=len(branches),
            projection=1,
            predicates=0,
            scanned=total_rows,
            intermediate=total_rows,
            output=maximum,
        )
        suffix = "_all" if all_rows else ""
        return _BuiltQuery(
            ast,
            complexity,
            frozenset({f"set_{operator.value.lower()}{suffix}"}),
        )

    def _set_branch_local_top_n(
        self,
        manifest: SchemaManifest,
        rows: Mapping[str, int],
    ) -> _BuiltQuery:
        tables = list(manifest.tables[:2])
        while len(tables) < 2:
            tables.append(manifest.tables[0])
        branches = tuple(
            ParenthesizedQuery(
                SelectQuery(
                    (Projection(self._id(table, f"s{index}"), "id"),),
                    TableRelation(table.name, f"s{index}"),
                ),
                order_by=(1,),
                limit=2,
                unique_projection_sets=frozenset({frozenset({1})}),
                max_rows=rows[table.name],
            )
            for index, table in enumerate(tables)
        )
        local_rows = sum(min(rows[table.name], 2) for table in tables)
        ast = self._ast(
            SetQuery(branches, SetOperator.UNION),
            projection_count=1,
            max_rows=local_rows,
            unique_sets=frozenset({frozenset({1})}),
        )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=2,
                depth=2,
                ctes=0,
                branches=2,
                projection=1,
                predicates=0,
                scanned=sum(rows[table.name] for table in tables),
                intermediate=local_rows,
                output=local_rows,
            ),
            frozenset({"branch_local_order_limit", "parenthesized_query"}),
        )

    def _scalar_branch_local_top_n(self) -> _BuiltQuery:
        branches = tuple(
            ParenthesizedQuery(
                SelectQuery(
                    (Projection(Literal(value, SqlType.NUMERIC), "id"),),
                ),
                order_by=(1,),
                limit=1,
                max_rows=1,
            )
            for value in (1, 2)
        )
        ast = self._ast(
            SetQuery(branches, SetOperator.UNION),
            projection_count=1,
            max_rows=2,
            unique_sets=frozenset({frozenset({1})}),
        )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=0,
                depth=2,
                ctes=0,
                branches=2,
                projection=1,
                predicates=0,
                scanned=0,
                intermediate=2,
                output=2,
            ),
            frozenset({"branch_local_order_limit", "parenthesized_query"}),
        )

    def _set_values(self, manifest: SchemaManifest, rows: Mapping[str, int]) -> _BuiltQuery:
        table = manifest.tables[0]
        select = SelectQuery(
            (Projection(self._id(table, "t"), "id"),), TableRelation(table.name, "t")
        )
        values = ValuesQuery(
            ((CastExpression(Literal(0, SqlType.NUMERIC), "UNSIGNED", SqlType.NUMERIC),),)
        )
        maximum = rows[table.name] + 1
        ast = self._ast(
            SetQuery((select, values), SetOperator.UNION, True),
            projection_count=1,
            max_rows=maximum,
        )
        complexity = self._complexity(
            tables=1,
            depth=1,
            ctes=0,
            branches=2,
            projection=1,
            predicates=0,
            scanned=rows[table.name],
            intermediate=maximum,
            output=maximum,
        )
        return _BuiltQuery(ast, complexity, frozenset({"table_value_constructor"}))

    def _values_only(self, *, limit: bool) -> _BuiltQuery:
        values = ValuesQuery(((Literal(0, SqlType.NUMERIC),),))
        ast = self._ast(
            values,
            projection_count=1,
            max_rows=1,
            unique_sets=frozenset({frozenset({1})}),
            limit=1 if limit else None,
        )
        complexity = self._complexity(
            tables=0,
            depth=1,
            ctes=0,
            branches=1,
            projection=1,
            predicates=0,
            scanned=0,
            intermediate=1,
            output=1,
        )
        return _BuiltQuery(ast, complexity, frozenset({"table_value_constructor"}))

    def _explicit_table(
        self,
        manifest: SchemaManifest,
        rows: Mapping[str, int],
        *,
        max_projection: int,
    ) -> _BuiltQuery:
        table = self._explicit_table_candidate(manifest, max_projection=max_projection)
        maximum = rows[table.name]
        key = self._unique_key(table)
        assert key is not None
        key_ordinals = tuple(table.columns.index(table.column(name)) + 1 for name in key)
        unique_sets = frozenset({frozenset(key_ordinals)})
        ast = self._ast(
            TableQuery(table.name),
            projection_count=len(table.columns),
            max_rows=maximum,
            unique_sets=unique_sets,
            order_by=key_ordinals,
        )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=1,
                depth=1,
                ctes=0,
                branches=1,
                projection=len(table.columns),
                predicates=0,
                scanned=maximum,
                intermediate=maximum,
                output=maximum,
            ),
            frozenset({"explicit_table"}),
        )

    def _explicit_table_values(
        self,
        manifest: SchemaManifest,
        rows: Mapping[str, int],
        *,
        max_projection: int,
        all_rows: bool,
    ) -> _BuiltQuery:
        table = self._explicit_table_candidate(manifest, max_projection=max_projection)
        key = self._unique_key(table)
        assert key is not None
        key_ordinals = tuple(table.columns.index(table.column(name)) + 1 for name in key)
        values = ValuesQuery(
            (tuple(Literal(None, self._type(column)) for column in table.columns),)
        )
        maximum = rows[table.name] + 1
        body = SetQuery((TableQuery(table.name), values), SetOperator.UNION, all=all_rows)
        ast = self._ast(
            body,
            projection_count=len(table.columns),
            max_rows=maximum,
            unique_sets=frozenset({frozenset(key_ordinals)}),
            order_by=key_ordinals,
        )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=1,
                depth=1,
                ctes=0,
                branches=2,
                projection=len(table.columns),
                predicates=0,
                scanned=rows[table.name],
                intermediate=maximum,
                output=maximum,
            ),
            frozenset({"explicit_table", "table_value_constructor"}),
        )

    @classmethod
    def _explicit_table_candidate(
        cls,
        manifest: SchemaManifest,
        *,
        max_projection: int,
    ) -> TableDef:
        projection_candidates = tuple(
            table for table in manifest.tables if len(table.columns) <= max_projection
        )
        if not projection_candidates:
            raise TargetNotReachable("explicit TABLE projection budget exceeded")
        candidates = tuple(
            table for table in projection_candidates if cls._unique_key(table) is not None
        )
        if not candidates:
            raise TargetNotReachable("explicit TABLE requires a nonnullable unique key")
        return min(candidates, key=lambda table: (len(table.columns), table.name))

    def _explicit_table_subquery(
        self, manifest: SchemaManifest, rows: Mapping[str, int]
    ) -> _BuiltQuery:
        table = manifest.tables[0]
        body = SelectQuery(
            (Projection(Literal(1, SqlType.NUMERIC), "q1"),),
            predicate=SubqueryExpression(SubqueryOperator.EXISTS, TableQuery(table.name)),
        )
        ast = self._ast(
            body,
            projection_count=1,
            max_rows=1,
            unique_sets=frozenset({frozenset({1})}),
        )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=1,
                depth=2,
                ctes=0,
                branches=1,
                projection=1,
                predicates=1,
                scanned=rows[table.name],
                intermediate=rows[table.name],
                output=1,
            ),
            frozenset({"explicit_table", "subquery_exists"}),
        )

    def _scalar_intersect_except(self) -> _BuiltQuery:
        first = SelectQuery((Projection(Literal(1, SqlType.NUMERIC), "q1"),))
        second = SelectQuery((Projection(Literal(1, SqlType.NUMERIC), "q1"),))
        third = SelectQuery((Projection(Literal(2, SqlType.NUMERIC), "q1"),))
        intersect = SetQuery((first, second), SetOperator.INTERSECT)
        body = SetQuery((intersect, third), SetOperator.EXCEPT)
        ast = self._ast(
            body,
            projection_count=1,
            max_rows=1,
            unique_sets=frozenset({frozenset({1})}),
        )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=0,
                depth=2,
                ctes=0,
                branches=3,
                projection=1,
                predicates=0,
                scanned=0,
                intermediate=3,
                output=1,
            ),
            frozenset({"set_intersect", "set_except", "scalar_literal"}),
        )

    def _scalar_rollup(self) -> _BuiltQuery:
        group = Literal(1, SqlType.NUMERIC)
        count = FunctionCall(FunctionName.COUNT, (Star(),), SqlType.NUMERIC)
        body = SelectQuery(
            (Projection(group, "group_key"), Projection(count, "row_count")),
            grouping=(group,),
            with_rollup=True,
        )
        ast = self._ast(
            body,
            projection_count=2,
            max_rows=2,
            unique_sets=frozenset({frozenset({1, 2})}),
        )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=0,
                depth=1,
                ctes=0,
                branches=1,
                projection=2,
                predicates=0,
                scanned=0,
                intermediate=2,
                output=2,
            ),
            frozenset({"aggregate", "rollup", "scalar_literal"}),
        )

    def _grouping_function(
        self,
        manifest: SchemaManifest,
        rows: Mapping[str, int],
    ) -> _BuiltQuery:
        table = manifest.tables[0]
        group = self._id(table, "t")
        grouping = FunctionCall(FunctionName.GROUPING, (group,), SqlType.NUMERIC)
        count = FunctionCall(FunctionName.COUNT, (Star(),), SqlType.NUMERIC)
        body = SelectQuery(
            (
                Projection(group, "group_key"),
                Projection(grouping, "rollup_marker"),
                Projection(count, "row_count"),
            ),
            TableRelation(table.name, "t"),
            grouping=(group,),
            with_rollup=True,
        )
        maximum = rows[table.name] + 1
        ast = self._ast(
            body,
            projection_count=3,
            max_rows=maximum,
            unique_sets=frozenset({frozenset({1, 2})}),
        )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=1,
                depth=1,
                ctes=0,
                branches=1,
                projection=3,
                predicates=0,
                scanned=rows[table.name],
                intermediate=rows[table.name],
                output=maximum,
            ),
            frozenset({"aggregate", "rollup", "grouping_function"}),
        )

    def _aggregate_semantics(
        self,
        manifest: SchemaManifest,
        rows: Mapping[str, int],
        *,
        variant: str,
    ) -> _BuiltQuery:
        if variant not in _AGGREGATE_VARIANTS:
            raise ValueError(f"unknown directed aggregate variant: {variant}")
        table = manifest.tables[0]
        maximum = rows[table.name]
        numeric_column = next(
            (
                column
                for column in table.columns
                if column.name != "id" and self._type(column) is SqlType.NUMERIC
            ),
            table.column("id"),
        )
        if variant in {
            "sum",
            "avg",
            "bit_and",
            "bit_or",
            "bit_xor",
            "stddev_pop",
            "stddev_samp",
            "var_pop",
            "var_samp",
        }:
            numeric_column = table.column("id")
        numeric = self._column("t", numeric_column)
        if variant == "aggregate_all_null":
            nullable_column = next(
                (
                    column
                    for column in table.columns
                    if column.nullable and self._type(column) is SqlType.NUMERIC
                ),
                None,
            )
            if nullable_column is None:
                raise TargetNotReachable("all-NULL aggregate requires a nullable numeric column")
            nullable = self._column("t", nullable_column)
            aggregates = (
                FunctionCall(FunctionName.COUNT, (Star(),), SqlType.NUMERIC),
                FunctionCall(FunctionName.SUM, (nullable,), SqlType.NUMERIC),
                FunctionCall(FunctionName.AVG, (nullable,), SqlType.NUMERIC),
                FunctionCall(FunctionName.MIN, (nullable,), SqlType.NUMERIC),
                FunctionCall(FunctionName.MAX, (nullable,), SqlType.NUMERIC),
                FunctionCall(FunctionName.COUNT, (nullable,), SqlType.NUMERIC),
                FunctionCall(
                    FunctionName.COUNT,
                    (nullable,),
                    SqlType.NUMERIC,
                    distinct=True,
                ),
            )
            aliases = (
                "row_count",
                "sum_value",
                "avg_value",
                "min_value",
                "max_value",
                "nonnull_count",
                "distinct_nonnull_count",
            )
            body = SelectQuery(
                tuple(
                    Projection(aggregate, alias)
                    for aggregate, alias in zip(aggregates, aliases, strict=True)
                ),
                TableRelation(table.name, "t"),
                UnaryExpression(UnaryOperator.IS_NULL, nullable),
            )
            projection_count = len(aggregates)
            output = 1
            unique = frozenset({frozenset({1})})
            predicates = 1
        elif variant == "group_null_having":
            group_column = next(
                (
                    column
                    for column in table.columns
                    if column.nullable
                    and self._type(column) in {SqlType.NUMERIC, SqlType.TEXT, SqlType.TEMPORAL}
                ),
                numeric_column,
            )
            group = self._column("t", group_column)
            count = FunctionCall(FunctionName.COUNT, (Star(),), SqlType.NUMERIC)
            body = SelectQuery(
                (
                    Projection(group, "group_key"),
                    Projection(count, "row_count"),
                ),
                TableRelation(table.name, "t"),
                grouping=(group,),
                having=UnaryExpression(UnaryOperator.IS_NULL, group),
            )
            projection_count = 2
            output = maximum
            unique = frozenset({frozenset({1})})
            predicates = 1
        else:
            function = {
                "sum": FunctionName.SUM,
                "avg": FunctionName.AVG,
                "min": FunctionName.MIN,
                "max": FunctionName.MAX,
                "count_distinct": FunctionName.COUNT,
                "bit_and": FunctionName.BIT_AND,
                "bit_or": FunctionName.BIT_OR,
                "bit_xor": FunctionName.BIT_XOR,
                "stddev_pop": FunctionName.STDDEV_POP,
                "stddev_samp": FunctionName.STDDEV_SAMP,
                "var_pop": FunctionName.VAR_POP,
                "var_samp": FunctionName.VAR_SAMP,
            }[variant]
            aggregate = FunctionCall(
                function,
                (numeric,),
                SqlType.NUMERIC,
                distinct=variant == "count_distinct",
            )
            body = SelectQuery(
                (Projection(aggregate, "aggregate_value"),),
                TableRelation(table.name, "t"),
            )
            projection_count = 1
            output = 1
            unique = frozenset({frozenset({1})})
            predicates = 0
        ast = self._ast(
            body,
            projection_count=projection_count,
            max_rows=output,
            unique_sets=unique,
        )
        tags = {"aggregate", f"aggregate_{variant}"}
        if variant == "aggregate_all_null":
            tags.update(
                {
                    "aggregate_all_null",
                    "aggregate_sum_all_null",
                    "aggregate_avg_all_null",
                    "aggregate_min_all_null",
                    "aggregate_max_all_null",
                    "aggregate_count_all_null",
                    "aggregate_count_distinct_all_null",
                }
            )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=1,
                depth=1,
                ctes=0,
                branches=1,
                projection=projection_count,
                predicates=predicates,
                scanned=maximum,
                intermediate=maximum,
                output=output,
            ),
            frozenset(tags),
        )

    def _grouping(
        self,
        manifest: SchemaManifest,
        rows: Mapping[str, int],
        *,
        rollup: bool,
        having: bool,
    ) -> _BuiltQuery:
        table = manifest.tables[0]
        group_column = next(
            (
                column
                for column in table.columns
                if column.name != "id"
                if self._type(column) in {SqlType.NUMERIC, SqlType.TEXT, SqlType.TEMPORAL}
            ),
            table.column("id"),
        )
        group = self._column("t", group_column)
        count = FunctionCall(FunctionName.COUNT, (Star(),), SqlType.NUMERIC)
        having_expr = (
            BinaryExpression(count, BinaryOperator.GT, Literal(0, SqlType.NUMERIC), SqlType.BOOLEAN)
            if having
            else None
        )
        body = SelectQuery(
            (Projection(group, "group_key"), Projection(count, "row_count")),
            TableRelation(table.name, "t"),
            grouping=(group,),
            with_rollup=rollup,
            having=having_expr,
        )
        maximum = min(rows[table.name] + (1 if rollup else 0), rows[table.name] + 1)
        ast = self._ast(body, projection_count=2, max_rows=maximum)
        complexity = self._complexity(
            tables=1,
            depth=1,
            ctes=0,
            branches=1,
            projection=2,
            predicates=1 if having else 0,
            scanned=rows[table.name],
            intermediate=rows[table.name],
            output=maximum,
        )
        tags = {"aggregate"}
        if rollup:
            tags.add("rollup")
        if having:
            tags.add("having")
        return _BuiltQuery(ast, complexity, frozenset(tags))

    def _window(
        self,
        manifest: SchemaManifest,
        rows: Mapping[str, int],
        *,
        frame: bool,
        rng: random.Random | None = None,
        directed: str | None = None,
    ) -> _BuiltQuery:
        rng = rng or random.Random(0)
        if frame:
            if directed is not None and directed not in _WINDOW_FRAME_VARIANTS:
                raise ValueError(f"unknown directed window frame variant: {directed}")
            variant = directed or rng.choice(sorted(_WINDOW_FRAME_VARIANTS))
        else:
            if directed is not None and directed not in _WINDOW_FUNCTION_VARIANTS:
                raise ValueError(f"unknown directed window function variant: {directed}")
            variant = directed or rng.choice(sorted({"row_number", *_WINDOW_FUNCTION_VARIANTS}))
        function = {
            "row_number": "ROW_NUMBER",
            "rank": "RANK",
            "dense_rank": "DENSE_RANK",
            "cume_dist": "CUME_DIST",
            "percent_rank": "PERCENT_RANK",
            "ntile": "NTILE",
            "first_value": "FIRST_VALUE",
            "last_value": "LAST_VALUE",
            "nth_value": "NTH_VALUE",
            "lag": "LAG",
            "lag_offset": "LAG",
            "lag_default": "LAG",
            "lead": "LEAD",
            "lead_offset": "LEAD",
            "lead_default": "LEAD",
            "rows_frame": "SUM",
            "range_frame": "SUM",
            "rows_unbounded_current": "SUM",
            "range_current_unbounded": "SUM",
        }[variant]
        frame_unit = (
            WindowFrameUnit.RANGE
            if variant in {"range_frame", "range_current_unbounded"}
            else WindowFrameUnit.ROWS
        )
        frame_bounds: WindowFrame | tuple[int, int] | None = None
        if variant in {"rows_frame", "range_frame"}:
            frame_bounds = (1, 1)
        elif variant == "rows_unbounded_current":
            frame_bounds = WindowFrame(
                WindowFrameBound(WindowFrameBoundKind.UNBOUNDED_PRECEDING),
                WindowFrameBound(WindowFrameBoundKind.CURRENT_ROW),
            )
        elif variant == "range_current_unbounded":
            frame_bounds = WindowFrame(
                WindowFrameBound(WindowFrameBoundKind.CURRENT_ROW),
                WindowFrameBound(WindowFrameBoundKind.UNBOUNDED_FOLLOWING),
            )

        def arguments(value: Expression) -> tuple[Expression | None, tuple[Expression, ...]]:
            if function in {"ROW_NUMBER", "RANK", "DENSE_RANK", "CUME_DIST", "PERCENT_RANK"}:
                return None, ()
            if function == "NTILE":
                return Literal(2, SqlType.NUMERIC), ()
            if function == "NTH_VALUE":
                return value, (Literal(2, SqlType.NUMERIC),)
            if variant in {"lag_offset", "lead_offset"}:
                return value, (Literal(2, SqlType.NUMERIC),)
            if variant in {"lag_default", "lead_default"}:
                return value, (
                    Literal(2, SqlType.NUMERIC),
                    Literal(0, SqlType.NUMERIC),
                )
            return value, ()

        table = manifest.tables[0]
        maximum = rows[table.name]
        key = self._unique_key(table)
        if frame_unit is WindowFrameUnit.RANGE and key != ("id",):
            key = None
        if key is None:
            count = FunctionCall(FunctionName.COUNT, (Star(),), SqlType.NUMERIC)
            derived = SelectQuery((Projection(count, "row_count"),), TableRelation(table.name, "t"))
            value = ColumnRef("d", "row_count", SqlType.NUMERIC)
            order = WindowOrder((value,), proven_unique=False, max_rows=1)
            spec = WindowSpec(
                (),
                order,
                frame_bounds,
                "w0" if not frame else None,
                frame_unit,
            )
            window_ref: WindowSpec | str = "w0" if not frame else spec
            argument, extra_arguments = arguments(value)
            window = WindowFunction(
                function,
                argument,
                window_ref,
                SqlType.NUMERIC,
                extra_arguments,
            )
            body = SelectQuery(
                (Projection(value, "row_count"), Projection(window, "row_number")),
                DerivedRelation(derived, "d"),
                named_windows=(spec,) if not frame else (),
            )
            ast = self._ast(
                body,
                projection_count=2,
                max_rows=1,
                unique_sets=frozenset({frozenset({1})}),
                windows=(order,),
            )
            output = 1
        else:
            key_columns = tuple(self._column("t", table.column(name)) for name in key)
            order = WindowOrder(key_columns, proven_unique=True, max_rows=maximum)
            spec = WindowSpec(
                (),
                order,
                frame_bounds,
                "w0" if not frame else None,
                frame_unit,
            )
            window_ref = "w0" if not frame else spec
            argument, extra_arguments = arguments(self._id(table, "t"))
            window = WindowFunction(
                function,
                argument,
                window_ref,
                SqlType.NUMERIC,
                extra_arguments,
            )
            base, unique = self._base_projection(table, "t")
            projection = (*base, Projection(window, "row_number"))
            body = SelectQuery(
                projection,
                TableRelation(table.name, "t"),
                named_windows=(spec,) if not frame else (),
            )
            ast = self._ast(
                body,
                projection_count=len(projection),
                max_rows=maximum,
                unique_sets=unique,
                windows=(order,),
            )
            output = maximum
        complexity = self._complexity(
            tables=1,
            depth=2 if key is None else 1,
            ctes=0,
            branches=1,
            projection=ast.scope.projection_count,
            predicates=0,
            scanned=maximum,
            intermediate=maximum,
            output=output,
        )
        tag = {
            "row_number": "window_named",
            "rank": "window_rank",
            "dense_rank": "window_dense_rank",
            "cume_dist": "window_cume_dist",
            "percent_rank": "window_percent_rank",
            "ntile": "window_ntile",
            "first_value": "window_first_value",
            "last_value": "window_last_value",
            "nth_value": "window_nth_value",
            "lag": "window_lag",
            "lag_offset": "window_lag_offset",
            "lag_default": "window_lag_default",
            "lead": "window_lead",
            "lead_offset": "window_lead_offset",
            "lead_default": "window_lead_default",
            "rows_frame": "window_rows_frame",
            "range_frame": "window_range_frame",
            "rows_unbounded_current": "window_rows_unbounded_current",
            "range_current_unbounded": "window_range_current_unbounded",
        }[variant]
        return _BuiltQuery(ast, complexity, frozenset({tag}))

    def _json_table(
        self,
        manifest: SchemaManifest,
        rows: Mapping[str, int],
        *,
        implicit: bool,
        max_elements_per_row: int,
    ) -> _BuiltQuery:
        relation: Relation
        projection: tuple[Projection, ...]
        if implicit:
            table = manifest.tables[0]
            json_column = next((item for item in table.columns if item.base_type == "JSON"), None)
            if json_column is None:
                raise TargetNotReachable("implicit JSON_TABLE requires a JSON column")
            source = self._column("t", json_column)
            relation = JoinRelation(
                TableRelation(table.name, "t"),
                JsonTableRelation(source, "j"),
                JoinKind.CROSS,
            )
            projection = (
                Projection(self._id(table, "t"), "id"),
                Projection(ColumnRef("j", "json_ordinal", SqlType.NUMERIC), "json_ordinal"),
                Projection(ColumnRef("j", "json_value", SqlType.NUMERIC), "json_value"),
            )
            maximum = rows[table.name] * max_elements_per_row
            scanned = rows[table.name]
            unique = frozenset({frozenset({1, 2})}) if self._unique_key(table) else frozenset()
            tables = 2
        else:
            relation = JsonTableRelation(Literal("[1,2,3]", SqlType.JSON), "j")
            projection = (
                Projection(ColumnRef("j", "json_ordinal", SqlType.NUMERIC), "json_ordinal"),
                Projection(ColumnRef("j", "json_value", SqlType.NUMERIC), "json_value"),
            )
            maximum = 3
            scanned = 3
            unique = frozenset({frozenset({1})})
            tables = 1
        body = SelectQuery(projection, relation)
        ast = self._ast(
            body, projection_count=len(projection), max_rows=maximum, unique_sets=unique
        )
        complexity = self._complexity(
            tables=tables,
            depth=1,
            ctes=0,
            branches=1,
            projection=len(projection),
            predicates=0,
            scanned=scanned,
            intermediate=maximum,
            output=maximum,
        )
        tags = {"json_table"}
        if implicit:
            tags.add("implicit_lateral")
        return _BuiltQuery(ast, complexity, frozenset(tags))

    def _json_document(self, table: TableDef, alias: str) -> Expression:
        column = next((item for item in table.columns if item.base_type == "JSON"), None)
        if column is not None:
            return self._column(alias, column)
        return CastExpression(Literal('[1,2,{"k":3}]', SqlType.TEXT), "JSON", SqlType.JSON)

    def _json_function(
        self, manifest: SchemaManifest, rows: Mapping[str, int], feature_id: str
    ) -> _BuiltQuery:
        table = manifest.tables[0]
        document = self._json_document(table, "t")
        if feature_id == "json_create_extract":
            created = FunctionCall(
                FunctionName.JSON_OBJECT,
                (Literal("id", SqlType.TEXT), self._id(table, "t")),
                SqlType.JSON,
            )
            expression: Expression = FunctionCall(
                FunctionName.JSON_EXTRACT,
                (created, Literal("$.id", SqlType.TEXT)),
                SqlType.JSON,
            )
        elif feature_id == "json_member_overlap":
            expression = FunctionCall(
                FunctionName.JSON_OVERLAPS,
                (
                    document,
                    CastExpression(Literal("[1]", SqlType.TEXT), "JSON", SqlType.JSON),
                ),
                SqlType.BOOLEAN,
            )
        elif feature_id == "json_value_scalar":
            expression = FunctionCall(
                FunctionName.JSON_VALUE,
                (document, Literal("$[0]", SqlType.TEXT)),
                SqlType.TEXT,
            )
        else:
            expression = FunctionCall(
                FunctionName.JSON_SCHEMA_VALID,
                (
                    CastExpression(Literal('{"type":"array"}', SqlType.TEXT), "JSON", SqlType.JSON),
                    document,
                ),
                SqlType.BOOLEAN,
            )
        projection = (
            Projection(self._id(table, "t"), "id"),
            Projection(expression, "json_result"),
        )
        maximum = rows[table.name]
        unique = frozenset({frozenset({1})}) if self._unique_key(table) == ("id",) else frozenset()
        body = SelectQuery(projection, TableRelation(table.name, "t"))
        ast = self._ast(body, projection_count=2, max_rows=maximum, unique_sets=unique)
        complexity = self._complexity(
            tables=1,
            depth=1,
            ctes=0,
            branches=1,
            projection=2,
            predicates=0,
            scanned=maximum,
            intermediate=maximum,
            output=maximum,
        )
        return _BuiltQuery(ast, complexity, frozenset({"deterministic_json"}))

    def _case(
        self, manifest: SchemaManifest, rows: Mapping[str, int], *, searched: bool
    ) -> _BuiltQuery:
        table = manifest.tables[0]
        identity = self._id(table, "t")
        if searched:
            expression = CaseExpression(
                None,
                (
                    (
                        BinaryExpression(
                            identity,
                            BinaryOperator.EQ,
                            Literal(0, SqlType.NUMERIC),
                            SqlType.BOOLEAN,
                        ),
                        Literal("zero", SqlType.TEXT),
                    ),
                ),
                Literal("nonzero", SqlType.TEXT),
                SqlType.TEXT,
            )
        else:
            expression = CaseExpression(
                identity,
                ((Literal(0, SqlType.NUMERIC), Literal("zero", SqlType.TEXT)),),
                Literal("nonzero", SqlType.TEXT),
                SqlType.TEXT,
            )
        projection = (Projection(identity, "id"), Projection(expression, "case_value"))
        maximum = rows[table.name]
        unique = frozenset({frozenset({1})}) if self._unique_key(table) == ("id",) else frozenset()
        ast = self._ast(
            SelectQuery(projection, TableRelation(table.name, "t")),
            projection_count=2,
            max_rows=maximum,
            unique_sets=unique,
        )
        complexity = self._complexity(
            tables=1,
            depth=1,
            ctes=0,
            branches=1,
            projection=2,
            predicates=1,
            scanned=maximum,
            intermediate=maximum,
            output=maximum,
        )
        return _BuiltQuery(
            ast, complexity, frozenset({"case_searched" if searched else "case_simple"})
        )

    def _optimizer_hint(
        self, manifest: SchemaManifest, rows: Mapping[str, int], feature_id: str
    ) -> _BuiltQuery:
        table = manifest.tables[0]
        maximum = rows[table.name]
        if feature_id == "optimizer_hint_join_order":
            joined = self._join(manifest, rows, random.Random(0), outer=False, directed="inner")
            assert isinstance(joined.ast.body, SelectQuery)
            body = SelectQuery(
                joined.ast.body.projection,
                joined.ast.body.source,
                joined.ast.body.predicate,
                optimizer_hint="JOIN_ORDER(t, u)",
            )
            return _BuiltQuery(
                QueryAst(body, joined.ast.order_by, joined.ast.scope),
                joined.complexity,
                frozenset({"optimizer_hint"}),
            )
        if feature_id == "optimizer_hint_index_level":
            index_name = self._index_hint_name(table)
            projection, unique = self._base_projection(table, "t")
            body = SelectQuery(
                projection,
                TableRelation(table.name, "t"),
                optimizer_hint=f"INDEX(t {index_name})",
            )
            ast = self._ast(
                body, projection_count=len(projection), max_rows=maximum, unique_sets=unique
            )
            leaf_tag = (
                "optimizer_hint_index_primary"
                if index_name == "PRIMARY"
                else "optimizer_hint_index_secondary"
            )
        else:
            inner_projection, unique = self._base_projection(table, "t")
            derived = SelectQuery(inner_projection, TableRelation(table.name, "t"))
            aliases = tuple(item.alias or "" for item in inner_projection)
            projection = tuple(
                Projection(ColumnRef("d", alias, item.expression.sql_type), alias)
                for alias, item in zip(aliases, inner_projection, strict=True)
            )
            body = SelectQuery(
                projection,
                DerivedRelation(derived, "d"),
                optimizer_hint="DERIVED_CONDITION_PUSHDOWN(d)",
            )
            ast = self._ast(
                body, projection_count=len(projection), max_rows=maximum, unique_sets=unique
            )
            leaf_tag = "optimizer_hint_derived_pushdown"
        complexity = self._complexity(
            tables=1,
            depth=2 if feature_id.endswith("pushdown") else 1,
            ctes=0,
            branches=1,
            projection=ast.scope.projection_count,
            predicates=0,
            scanned=maximum,
            intermediate=maximum,
            output=maximum,
        )
        return _BuiltQuery(ast, complexity, frozenset({"optimizer_hint", leaf_tag}))

    def _partition(self, manifest: SchemaManifest, rows: Mapping[str, int]) -> _BuiltQuery:
        table = next((item for item in manifest.tables if item.partition is not None), None)
        if table is None:
            raise TargetNotReachable("explicit partition selection requires a partitioned table")
        projection, unique = self._base_projection(table, "t")
        body = SelectQuery(
            projection,
            TableRelation(table.name, "t", ("p0",)),
            BinaryExpression(
                self._id(table, "t"),
                BinaryOperator.GE,
                Literal(0, SqlType.NUMERIC),
                SqlType.BOOLEAN,
            ),
        )
        maximum = rows[table.name]
        ast = self._ast(
            body, projection_count=len(projection), max_rows=maximum, unique_sets=unique
        )
        complexity = self._complexity(
            tables=1,
            depth=1,
            ctes=0,
            branches=1,
            projection=len(projection),
            predicates=1,
            scanned=maximum,
            intermediate=maximum,
            output=maximum,
        )
        return _BuiltQuery(ast, complexity, frozenset({"explicit_partition"}))

    @staticmethod
    def _function_argument(argument: FunctionArgument) -> Literal:
        values: dict[
            FunctionArgument,
            tuple[int | float | str | bytes | None, SqlType],
        ] = {
            FunctionArgument.NUMBER: (-2.5, SqlType.NUMERIC),
            FunctionArgument.UNIT_NUMBER: (0.5, SqlType.NUMERIC),
            FunctionArgument.INTEGER: (7, SqlType.NUMERIC),
            FunctionArgument.INTEGER_TWO: (2, SqlType.NUMERIC),
            FunctionArgument.INTEGER_THREE: (3, SqlType.NUMERIC),
            FunctionArgument.BASE_SIXTEEN: (16, SqlType.NUMERIC),
            FunctionArgument.TEXT: ("Alpha beta", SqlType.TEXT),
            FunctionArgument.TEXT_ALT: ("beta", SqlType.TEXT),
            FunctionArgument.SQL_TEXT: ("SELECT 1", SqlType.TEXT),
            FunctionArgument.SEPARATOR: (",", SqlType.TEXT),
            FunctionArgument.DATE: ("2024-02-29", SqlType.TEMPORAL),
            FunctionArgument.DATETIME: (
                "2024-02-29 12:34:56.123456",
                SqlType.TEMPORAL,
            ),
            FunctionArgument.TIME: ("12:34:56.123456", SqlType.TEMPORAL),
            FunctionArgument.PERIOD: (202401, SqlType.NUMERIC),
            FunctionArgument.YEAR_NUMBER: (2024, SqlType.NUMERIC),
            FunctionArgument.DAY_NUMBER: (738945, SqlType.NUMERIC),
            FunctionArgument.SHA_BITS: (256, SqlType.NUMERIC),
            FunctionArgument.BASE64_TEXT: ("YWJj", SqlType.TEXT),
            FunctionArgument.HEX_TEXT: ("616263", SqlType.TEXT),
            FunctionArgument.IPV4_TEXT: ("192.0.2.1", SqlType.TEXT),
            FunctionArgument.IPV4_NUMBER: (3221225985, SqlType.NUMERIC),
            FunctionArgument.IPV6_TEXT: ("2001:db8::1", SqlType.TEXT),
            FunctionArgument.IPV6_BINARY: (
                bytes.fromhex("20010db8000000000000000000000001"),
                SqlType.BINARY,
            ),
        }
        value, sql_type = values[argument]
        return Literal(value, sql_type)

    def _predicate_semantics(
        self,
        manifest: SchemaManifest,
        rows: Mapping[str, int],
        *,
        variant: str,
    ) -> _BuiltQuery:
        if variant not in _PREDICATE_VARIANTS:
            raise ValueError(f"unknown directed predicate variant: {variant}")
        table = manifest.tables[0]
        identity = self._id(table, "t")
        one = Literal(1, SqlType.NUMERIC)
        two = Literal(2, SqlType.NUMERIC)
        ten = Literal(10, SqlType.NUMERIC)
        binary_variants = {
            "null_safe_eq": (BinaryOperator.NULL_SAFE_EQ, Literal(None, SqlType.NUMERIC)),
            "divide": (BinaryOperator.DIVIDE, two),
            "integer_divide": (BinaryOperator.INTEGER_DIVIDE, two),
            "modulo": (BinaryOperator.MODULO, two),
            "bit_and": (BinaryOperator.BIT_AND, one),
            "bit_or": (BinaryOperator.BIT_OR, one),
            "bit_xor": (BinaryOperator.BIT_XOR, one),
            "shift_left": (BinaryOperator.SHIFT_LEFT, one),
            "shift_right": (BinaryOperator.SHIFT_RIGHT, one),
        }
        expression: Expression
        if variant in binary_variants:
            operator, right = binary_variants[variant]
            expression = BinaryExpression(
                identity,
                operator,
                right,
                (SqlType.BOOLEAN if operator is BinaryOperator.NULL_SAFE_EQ else SqlType.NUMERIC),
            )
        elif variant == "logical_xor":
            expression = BinaryExpression(
                BinaryExpression(
                    identity,
                    BinaryOperator.GT,
                    Literal(0, SqlType.NUMERIC),
                    SqlType.BOOLEAN,
                ),
                BinaryOperator.XOR,
                BinaryExpression(identity, BinaryOperator.LT, ten, SqlType.BOOLEAN),
                SqlType.BOOLEAN,
            )
        elif variant in {"unary_plus", "unary_minus"}:
            expression = UnaryExpression(
                UnaryOperator.PLUS if variant == "unary_plus" else UnaryOperator.MINUS,
                identity,
                SqlType.NUMERIC,
            )
        elif variant in {"between", "not_between"}:
            expression = BetweenExpression(
                identity,
                one,
                ten,
                negated=variant == "not_between",
            )
        elif variant in {"in_list_null", "not_in_list_null"}:
            expression = InListExpression(
                identity,
                (one, Literal(None, SqlType.NUMERIC), two),
                negated=variant == "not_in_list_null",
            )
        elif variant in {"like_escape", "not_like_escape"}:
            expression = LikeExpression(
                CastExpression(identity, "CHAR(64)", SqlType.TEXT),
                Literal(r"1\_%", SqlType.TEXT),
                "\\",
                negated=variant == "not_like_escape",
            )
        elif variant in {"regexp_like", "not_regexp_like"}:
            regexp = FunctionCall(
                FunctionName.REGEXP_LIKE,
                (
                    CastExpression(identity, "CHAR(64)", SqlType.TEXT),
                    Literal("^[0-9]+$", SqlType.TEXT),
                ),
                SqlType.BOOLEAN,
            )
            expression = (
                UnaryExpression(UnaryOperator.NOT, regexp)
                if variant == "not_regexp_like"
                else regexp
            )
        else:
            truth = BinaryExpression(
                identity,
                BinaryOperator.GT,
                Literal(0, SqlType.NUMERIC),
                SqlType.BOOLEAN,
            )
            unary = {
                "is_true": UnaryOperator.IS_TRUE,
                "is_false": UnaryOperator.IS_FALSE,
                "is_unknown": UnaryOperator.IS_UNKNOWN,
                "is_not_true": UnaryOperator.IS_NOT_TRUE,
                "is_not_false": UnaryOperator.IS_NOT_FALSE,
                "is_not_unknown": UnaryOperator.IS_NOT_UNKNOWN,
            }[variant]
            expression = UnaryExpression(unary, truth, SqlType.BOOLEAN)
        projection = (
            Projection(identity, "id"),
            Projection(expression, "operator_value"),
        )
        maximum = rows[table.name]
        unique = frozenset({frozenset({1})}) if self._unique_key(table) == ("id",) else frozenset()
        ast = self._ast(
            SelectQuery(projection, TableRelation(table.name, "t")),
            projection_count=2,
            max_rows=maximum,
            unique_sets=unique,
        )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=1,
                depth=1,
                ctes=0,
                branches=1,
                projection=2,
                predicates=1,
                scanned=maximum,
                intermediate=maximum,
                output=maximum,
            ),
            frozenset({"operator_predicate", f"predicate_{variant}"}),
        )

    def _null_predicate_semantics(self, *, variant: str) -> _BuiltQuery:
        if variant not in _NULL_PREDICATE_VARIANTS:
            raise ValueError(f"unknown directed NULL predicate variant: {variant}")

        numeric_null = Literal(None, SqlType.NUMERIC)
        text_null = Literal(None, SqlType.TEXT)
        zero = Literal(0, SqlType.NUMERIC)
        one = Literal(1, SqlType.NUMERIC)
        two = Literal(2, SqlType.NUMERIC)
        three = Literal(3, SqlType.NUMERIC)
        five = Literal(5, SqlType.NUMERIC)
        ten = Literal(10, SqlType.NUMERIC)

        expressions: tuple[Expression, ...]
        tags: set[str]
        operators: tuple[BinaryOperator, ...]
        pairs: tuple[tuple[Expression, BinaryOperator, Expression], ...]
        if variant.startswith("comparison_null_"):
            position = variant.removeprefix("comparison_null_")
            operators = (
                BinaryOperator.EQ,
                BinaryOperator.NE,
                BinaryOperator.LT,
                BinaryOperator.LE,
                BinaryOperator.GT,
                BinaryOperator.GE,
                BinaryOperator.NULL_SAFE_EQ,
            )
            left = numeric_null if position in {"left", "both"} else one
            right = numeric_null if position in {"right", "both"} else one
            expressions = tuple(
                BinaryExpression(left, operator, right, SqlType.BOOLEAN) for operator in operators
            )
            tags = {
                f"predicate_comparison_null_{position}",
                f"predicate_null_safe_eq_{position}",
            }
        elif variant.startswith("arithmetic_null_"):
            position = variant.removeprefix("arithmetic_null_")
            operators = (
                BinaryOperator.ADD,
                BinaryOperator.SUBTRACT,
                BinaryOperator.MULTIPLY,
                BinaryOperator.DIVIDE,
                BinaryOperator.INTEGER_DIVIDE,
                BinaryOperator.MODULO,
            )
            left = numeric_null if position in {"left", "both"} else two
            right = numeric_null if position in {"right", "both"} else two
            expressions = tuple(
                BinaryExpression(left, operator, right, SqlType.NUMERIC) for operator in operators
            )
            tags = {f"predicate_arithmetic_null_{position}"}
        elif variant.startswith("bitwise_null_"):
            position = variant.removeprefix("bitwise_null_")
            operators = (
                BinaryOperator.BIT_AND,
                BinaryOperator.BIT_OR,
                BinaryOperator.BIT_XOR,
                BinaryOperator.SHIFT_LEFT,
                BinaryOperator.SHIFT_RIGHT,
            )
            left = numeric_null if position in {"left", "both"} else one
            right = numeric_null if position in {"right", "both"} else one
            expressions = tuple(
                BinaryExpression(left, operator, right, SqlType.NUMERIC) for operator in operators
            )
            tags = {f"predicate_bitwise_null_{position}"}
        elif variant.startswith("logical_null_"):
            position = variant.removeprefix("logical_null_")
            if position == "left":
                pairs = (
                    (numeric_null, BinaryOperator.AND, zero),
                    (numeric_null, BinaryOperator.AND, one),
                    (numeric_null, BinaryOperator.OR, zero),
                    (numeric_null, BinaryOperator.OR, one),
                    (numeric_null, BinaryOperator.XOR, one),
                )
            elif position == "right":
                pairs = (
                    (zero, BinaryOperator.AND, numeric_null),
                    (one, BinaryOperator.AND, numeric_null),
                    (zero, BinaryOperator.OR, numeric_null),
                    (one, BinaryOperator.OR, numeric_null),
                    (one, BinaryOperator.XOR, numeric_null),
                )
            else:
                pairs = tuple(
                    (numeric_null, operator, numeric_null)
                    for operator in (
                        BinaryOperator.AND,
                        BinaryOperator.OR,
                        BinaryOperator.XOR,
                    )
                )
            expressions = tuple(
                BinaryExpression(left, operator, right, SqlType.BOOLEAN)
                for left, operator, right in pairs
            )
            tags = {f"predicate_logical_null_{position}"}
        elif variant.startswith("like_regexp_null_"):
            position = variant.removeprefix("like_regexp_null_")
            value = text_null if position in {"left", "both"} else Literal("abc", SqlType.TEXT)
            pattern = text_null if position in {"right", "both"} else Literal("a%", SqlType.TEXT)
            regexp_pattern = (
                text_null if position in {"right", "both"} else Literal("^a", SqlType.TEXT)
            )
            expressions = (
                LikeExpression(value, pattern, "\\"),
                FunctionCall(
                    FunctionName.REGEXP_LIKE,
                    (value, regexp_pattern),
                    SqlType.BOOLEAN,
                ),
            )
            tags = {
                f"predicate_like_null_{position}",
                f"predicate_regexp_null_{position}",
            }
        elif variant.startswith("between_null_"):
            position = variant.removeprefix("between_null_")
            value = numeric_null if position in {"value", "all"} else five
            lower = numeric_null if position in {"lower", "bounds", "all"} else one
            upper = numeric_null if position in {"upper", "bounds", "all"} else ten
            expressions = (
                BetweenExpression(value, lower, upper),
                BetweenExpression(value, lower, upper, negated=True),
            )
            tags = {
                f"predicate_between_null_{position}",
                f"predicate_not_between_null_{position}",
            }
        else:
            position = variant.removeprefix("in_null_")
            if position == "left":
                expressions = (
                    InListExpression(numeric_null, (one, two)),
                    InListExpression(numeric_null, (one, two), negated=True),
                )
                tags = {"predicate_in_null_left", "predicate_not_in_null_left"}
            elif position == "right":
                options = (one, numeric_null)
                expressions = (
                    InListExpression(one, options),
                    InListExpression(three, options),
                    InListExpression(one, options, negated=True),
                    InListExpression(three, options, negated=True),
                )
                tags = {
                    "predicate_in_null_right_match",
                    "predicate_in_null_right_no_match",
                    "predicate_not_in_null_right_match",
                    "predicate_not_in_null_right_no_match",
                }
            else:
                options = (one, numeric_null)
                expressions = (
                    InListExpression(numeric_null, options),
                    InListExpression(numeric_null, options, negated=True),
                )
                tags = {"predicate_in_null_both", "predicate_not_in_null_both"}

        projection = tuple(
            Projection(expression, f"null_value_{ordinal}")
            for ordinal, expression in enumerate(expressions, start=1)
        )
        ast = self._ast(
            SelectQuery(projection),
            projection_count=len(projection),
            max_rows=1,
            unique_sets=frozenset({frozenset({1})}),
        )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=0,
                depth=1,
                ctes=0,
                branches=1,
                projection=len(projection),
                predicates=len(projection),
                scanned=0,
                intermediate=1,
                output=1,
            ),
            frozenset({"operator_predicate", "predicate_null_matrix", *tags}),
        )

    @staticmethod
    def _function_result_type(result: FunctionResult) -> SqlType:
        return {
            FunctionResult.NUMERIC: SqlType.NUMERIC,
            FunctionResult.TEXT: SqlType.TEXT,
            FunctionResult.BINARY: SqlType.BINARY,
            FunctionResult.TEMPORAL: SqlType.TEMPORAL,
            FunctionResult.BOOLEAN: SqlType.BOOLEAN,
        }[result]

    @staticmethod
    def _choose_function_signature(
        rng: random.Random,
        directed: str | None,
    ) -> tuple[DeterministicFunctionSignature, int | None]:
        null_position: int | None = None
        signature_id = directed
        if directed is not None:
            candidate_id, marker, candidate_position = directed.rpartition("_null_")
            if marker:
                signature_id = candidate_id
                try:
                    null_position = int(candidate_position)
                except ValueError as error:
                    raise ValueError("invalid directed function NULL position") from error
        by_id = {
            signature.signature_id: signature for signature in DETERMINISTIC_FUNCTION_SIGNATURES
        }
        if signature_id is None:
            signature = rng.choice(DETERMINISTIC_FUNCTION_SIGNATURES)
            if signature.null_argument_positions and rng.randrange(4) == 0:
                null_position = rng.choice(sorted(signature.null_argument_positions))
        else:
            try:
                signature = by_id[signature_id]
            except KeyError as error:
                raise ValueError(f"unknown directed function signature: {signature_id}") from error
        if null_position is not None and null_position not in signature.null_argument_positions:
            raise ValueError("directed function NULL position is outside the signature")
        return signature, null_position

    def _deterministic_function(
        self,
        manifest: SchemaManifest,
        rows: Mapping[str, int],
        *,
        rng: random.Random,
        directed: str | None,
    ) -> _BuiltQuery:
        del manifest, rows
        signature, null_position = self._choose_function_signature(rng, directed)
        arguments = [self._function_argument(argument) for argument in signature.arguments]
        if null_position is not None:
            arguments[null_position] = Literal(None, arguments[null_position].sql_type)
        expression = RegisteredFunctionCall(
            signature,
            tuple(arguments),
            self._function_result_type(signature.result),
        )
        projection = (Projection(expression, "function_value"),)
        ast = self._ast(
            SelectQuery(projection),
            projection_count=1,
            max_rows=1,
            unique_sets=frozenset({frozenset({1})}),
        )
        complexity = self._complexity(
            tables=0,
            depth=1,
            ctes=0,
            branches=1,
            projection=1,
            predicates=0,
            scanned=0,
            intermediate=1,
            output=1,
        )
        tags = {
            "deterministic_function",
            f"fn_{signature.signature_id}",
            f"function_family_{signature.family.value}",
        }
        if null_position is not None:
            tags.add(f"function_null_argument_{null_position}")
            tags.add(f"fn_{signature.signature_id}_null_{null_position}")
        return _BuiltQuery(ast, complexity, frozenset(tags))

    def _profile_function(self, manifest: SchemaManifest, rows: Mapping[str, int]) -> _BuiltQuery:
        if manifest.profile is SchemaProfile.FULLTEXT_INNODB:
            return self._fulltext(manifest, rows)
        if manifest.profile is SchemaProfile.SPATIAL_INNODB:
            return self._spatial(manifest, rows)
        raise TargetNotReachable("fulltext/spatial function requires its matching profile")

    def _fulltext(self, manifest: SchemaManifest, rows: Mapping[str, int]) -> _BuiltQuery:
        table = manifest.tables[0]
        index = next((item for item in table.indexes if item.kind is IndexKind.FULLTEXT), None)
        if index is None:
            raise TargetNotReachable("FULLTEXT query requires a FULLTEXT index")
        columns = tuple(self._column("t", table.column(name)) for name in index.column_names)
        score = MatchAgainst(columns, "+alpha", True)
        projection = (
            Projection(self._id(table, "t"), "id"),
            Projection(score, "match_score"),
        )
        body = SelectQuery(
            projection,
            TableRelation(table.name, "t"),
            BinaryExpression(
                score, BinaryOperator.GT, Literal(0, SqlType.NUMERIC), SqlType.BOOLEAN
            ),
        )
        maximum = rows[table.name]
        unique = frozenset({frozenset({1})}) if self._unique_key(table) == ("id",) else frozenset()
        ast = self._ast(body, projection_count=2, max_rows=maximum, unique_sets=unique)
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=1,
                depth=1,
                ctes=0,
                branches=1,
                projection=2,
                predicates=1,
                scanned=maximum,
                intermediate=maximum,
                output=maximum,
            ),
            frozenset({"fulltext"}),
        )

    def _spatial(self, manifest: SchemaManifest, rows: Mapping[str, int]) -> _BuiltQuery:
        table = manifest.tables[0]
        index = next((item for item in table.indexes if item.kind is IndexKind.SPATIAL), None)
        if index is None or not index.column_names:
            raise TargetNotReachable("spatial query requires a SPATIAL index")
        spatial = self._column("t", table.column(index.column_names[0]))
        valid = FunctionCall(FunctionName.ST_ISVALID, (spatial,), SqlType.BOOLEAN)
        as_binary = FunctionCall(FunctionName.ST_ASBINARY, (spatial,), SqlType.BINARY)
        projection = (
            Projection(self._id(table, "t"), "id"),
            Projection(as_binary, "geometry_wkb"),
        )
        body = SelectQuery(projection, TableRelation(table.name, "t"), valid)
        maximum = rows[table.name]
        unique = frozenset({frozenset({1})}) if self._unique_key(table) == ("id",) else frozenset()
        ast = self._ast(body, projection_count=2, max_rows=maximum, unique_sets=unique)
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=1,
                depth=1,
                ctes=0,
                branches=1,
                projection=2,
                predicates=1,
                scanned=maximum,
                intermediate=maximum,
                output=maximum,
            ),
            frozenset({"spatial"}),
        )

    def _union_charset(self, manifest: SchemaManifest, rows: Mapping[str, int]) -> _BuiltQuery:
        table = manifest.tables[0]
        payload = table.column("payload")
        branch_one = SelectQuery(
            (Projection(self._column("a", payload), "payload"),),
            TableRelation(table.name, "a"),
        )
        branch_two = SelectQuery(
            (Projection(self._column("b", payload), "payload"),),
            TableRelation(table.name, "b"),
        )
        union = SetQuery((branch_one, branch_two), SetOperator.UNION, True)
        body = SelectQuery(
            (Projection(ColumnRef("d", "payload", SqlType.TEXT), "payload"),),
            DerivedRelation(union, "d"),
            BinaryExpression(
                ColumnRef("d", "payload", SqlType.TEXT),
                BinaryOperator.LIKE,
                Literal("%a%", SqlType.TEXT),
                SqlType.BOOLEAN,
            ),
        )
        maximum = rows[table.name] * 2
        ast = self._ast(body, projection_count=1, max_rows=maximum)
        complexity = self._complexity(
            tables=2,
            depth=2,
            ctes=0,
            branches=2,
            projection=1,
            predicates=1,
            scanned=maximum,
            intermediate=maximum,
            output=maximum,
        )
        return _BuiltQuery(ast, complexity, frozenset({"union_charset_pushdown"}))

    def _index_merge(self, manifest: SchemaManifest, rows: Mapping[str, int]) -> _BuiltQuery:
        table = manifest.tables[0]
        has_primary = any(index.primary for index in table.indexes)
        has_descending = any(
            part.direction.value == "DESC" for index in table.indexes for part in index.parts
        )
        if not has_primary or not has_descending:
            raise TargetNotReachable(
                "DESC primary/index-merge regression requires PRIMARY and a DESC index"
            )
        identity = self._id(table, "t")
        predicate = BinaryExpression(
            BinaryExpression(
                identity, BinaryOperator.EQ, Literal(1, SqlType.NUMERIC), SqlType.BOOLEAN
            ),
            BinaryOperator.OR,
            BinaryExpression(
                identity, BinaryOperator.GT, Literal(5, SqlType.NUMERIC), SqlType.BOOLEAN
            ),
            SqlType.BOOLEAN,
        )
        projection, unique = self._base_projection(table, "t")
        maximum = rows[table.name]
        ast = self._ast(
            SelectQuery(projection, TableRelation(table.name, "t"), predicate),
            projection_count=len(projection),
            max_rows=maximum,
            unique_sets=unique,
        )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=1,
                depth=1,
                ctes=0,
                branches=1,
                projection=len(projection),
                predicates=2,
                scanned=maximum,
                intermediate=maximum,
                output=maximum,
            ),
            frozenset({"index_merge", "descending_index"}),
        )

    def _rollup_row_comparator(
        self, manifest: SchemaManifest, rows: Mapping[str, int]
    ) -> _BuiltQuery:
        table = manifest.tables[0]
        identity = self._id(table, "t")
        count = FunctionCall(FunctionName.COUNT, (Star(),), SqlType.NUMERIC)
        normalized_identity = FunctionCall(
            FunctionName.COALESCE,
            (identity, Literal(0, SqlType.NUMERIC)),
            SqlType.NUMERIC,
        )
        row_predicate = BinaryExpression(
            RowExpression((normalized_identity, count)),
            BinaryOperator.GE,
            RowExpression((Literal(0, SqlType.NUMERIC), Literal(1, SqlType.NUMERIC))),
            SqlType.BOOLEAN,
        )
        body = SelectQuery(
            (Projection(identity, "group_id"), Projection(count, "row_count")),
            TableRelation(table.name, "t"),
            grouping=(identity,),
            with_rollup=True,
            having=row_predicate,
        )
        maximum = rows[table.name] + 1
        ast = self._ast(body, projection_count=2, max_rows=maximum)
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=1,
                depth=2,
                ctes=0,
                branches=1,
                projection=2,
                predicates=1,
                scanned=rows[table.name],
                intermediate=rows[table.name],
                output=maximum,
            ),
            frozenset({"rollup", "row_comparator"}),
        )

    def _anti_join(self, manifest: SchemaManifest, rows: Mapping[str, int]) -> _BuiltQuery:
        outer = manifest.tables[0]
        inner = manifest.tables[1] if len(manifest.tables) > 1 else outer
        inner_identity = self._id(inner, "u")
        null_key = CaseExpression(
            None,
            (
                (
                    BinaryExpression(
                        BinaryExpression(
                            inner_identity,
                            BinaryOperator.MODULO,
                            Literal(2, SqlType.NUMERIC),
                            SqlType.NUMERIC,
                        ),
                        BinaryOperator.EQ,
                        Literal(0, SqlType.NUMERIC),
                        SqlType.BOOLEAN,
                    ),
                    Literal(None, SqlType.NUMERIC),
                ),
            ),
            inner_identity,
            SqlType.NUMERIC,
        )
        inner_query = SelectQuery(
            (Projection(Literal(1, SqlType.NUMERIC), "one"),),
            TableRelation(inner.name, "u"),
            BinaryExpression(
                null_key,
                BinaryOperator.EQ,
                self._id(outer, "t"),
                SqlType.BOOLEAN,
            ),
        )
        predicate = SubqueryExpression(SubqueryOperator.NOT_EXISTS, inner_query)
        projection, unique = self._base_projection(outer, "t")
        maximum = rows[outer.name]
        ast = self._ast(
            SelectQuery(projection, TableRelation(outer.name, "t"), predicate),
            projection_count=len(projection),
            max_rows=maximum,
            unique_sets=unique,
        )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=2,
                depth=2,
                ctes=0,
                branches=1,
                projection=len(projection),
                predicates=2,
                scanned=rows[outer.name] + rows[inner.name],
                intermediate=rows[outer.name] * rows[inner.name],
                output=maximum,
            ),
            frozenset({"antijoin", "nullable_key"}),
        )

    def _distinct_not_in(self, manifest: SchemaManifest, rows: Mapping[str, int]) -> _BuiltQuery:
        outer = manifest.tables[0]
        inner = manifest.tables[1] if len(manifest.tables) > 1 else outer
        subquery = SelectQuery(
            (Projection(self._id(inner, "u"), "id"),),
            TableRelation(inner.name, "u"),
            distinct=True,
        )
        predicate = SubqueryExpression(SubqueryOperator.NOT_IN, subquery, self._id(outer, "t"))
        # DISTINCT must compare every projected value.  Keeping LOB boundary
        # columns in this regression shape can exhaust MySQL sort memory before
        # the NOT IN path is reached, so make the DISTINCT key intentionally
        # narrow.  A one-column DISTINCT result is unique by definition.
        projection = (Projection(self._id(outer, "t"), "q1"),)
        unique = frozenset({frozenset({1})})
        maximum = rows[outer.name]
        ast = self._ast(
            SelectQuery(projection, TableRelation(outer.name, "t"), predicate, distinct=True),
            projection_count=len(projection),
            max_rows=maximum,
            unique_sets=unique,
        )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=2,
                depth=2,
                ctes=0,
                branches=1,
                projection=len(projection),
                predicates=1,
                scanned=rows[outer.name] + rows[inner.name],
                intermediate=max(rows[outer.name], rows[inner.name]),
                output=maximum,
            ),
            frozenset({"distinct", "not_in"}),
        )

    def _hint_lexer(self, manifest: SchemaManifest, rows: Mapping[str, int]) -> _BuiltQuery:
        table = manifest.tables[0]
        projection, unique = self._base_projection(table, "t")
        maximum = rows[table.name]
        body = SelectQuery(
            projection,
            TableRelation(table.name, "t"),
            optimizer_hint="NO_RANGE_OPTIMIZATION(t)",
        )
        ast = self._ast(
            body, projection_count=len(projection), max_rows=maximum, unique_sets=unique
        )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=1,
                depth=1,
                ctes=0,
                branches=1,
                projection=len(projection),
                predicates=0,
                scanned=maximum,
                intermediate=maximum,
                output=maximum,
            ),
            frozenset({"hint_lexer"}),
        )

    def _scene(self, manifest: SchemaManifest, rows: Mapping[str, int]) -> _BuiltQuery:
        if manifest.profile is SchemaProfile.FOREIGN_KEY_GRAPH:
            child = next((table for table in manifest.tables if table.foreign_keys), None)
            if child is None:
                raise TargetNotReachable("foreign-key scene requires a declared edge")
            edge = child.foreign_keys[0]
            parent = next(table for table in manifest.tables if table.name == edge.referenced_table)
            predicates = tuple(
                BinaryExpression(
                    self._column("c", child.column(child_name)),
                    BinaryOperator.EQ,
                    self._column("p", parent.column(parent_name)),
                    SqlType.BOOLEAN,
                )
                for child_name, parent_name in zip(
                    edge.columns, edge.referenced_columns, strict=True
                )
            )
            predicate = predicates[0]
            for extra in predicates[1:]:
                predicate = BinaryExpression(predicate, BinaryOperator.AND, extra, SqlType.BOOLEAN)
            relation = JoinRelation(
                TableRelation(child.name, "c"),
                TableRelation(parent.name, "p"),
                JoinKind.INNER,
                predicate,
            )
            projection = (
                Projection(self._id(child, "c"), "child_id"),
                Projection(self._id(parent, "p"), "parent_id"),
            )
            maximum = rows[child.name]
            ast = self._ast(
                SelectQuery(projection, relation),
                projection_count=2,
                max_rows=maximum,
                unique_sets=(
                    frozenset({frozenset({1})})
                    if self._unique_key(child) == ("id",)
                    else frozenset()
                ),
            )
            complexity = self._complexity(
                tables=2,
                depth=1,
                ctes=0,
                branches=1,
                projection=2,
                predicates=len(predicates),
                scanned=rows[child.name] + rows[parent.name],
                intermediate=maximum,
                output=maximum,
            )
            return _BuiltQuery(ast, complexity, frozenset({"foreign_key_join"}))
        if manifest.profile is SchemaProfile.FULLTEXT_INNODB:
            return self._fulltext(manifest, rows)
        if manifest.profile is SchemaProfile.SPATIAL_INNODB:
            return self._spatial(manifest, rows)
        if manifest.profile is SchemaProfile.JSON_MULTIVALUE_INNODB:
            return self._multivalue(manifest, rows)
        return self._simple(manifest, rows, top_n=False, free_random=False)

    def _multivalue(self, manifest: SchemaManifest, rows: Mapping[str, int]) -> _BuiltQuery:
        table = manifest.tables[0]
        index = next((item for item in table.indexes if item.kind is IndexKind.MULTIVALUE), None)
        if index is None:
            raise TargetNotReachable("multivalue query requires a multivalue index")
        expression_part = next((part.expression for part in index.parts if part.expression), None)
        if expression_part is None:
            raise TargetNotReachable("multivalue index lacks an array expression")
        document = self._column("t", table.column(expression_part.column_name))
        predicate = JsonMemberOf(Literal(1, SqlType.NUMERIC), document)
        projection = (
            Projection(self._id(table, "t"), "id"),
            Projection(
                FunctionCall(FunctionName.JSON_TYPE, (document,), SqlType.TEXT), "json_type"
            ),
        )
        maximum = rows[table.name]
        unique = frozenset({frozenset({1})}) if self._unique_key(table) == ("id",) else frozenset()
        ast = self._ast(
            SelectQuery(projection, TableRelation(table.name, "t"), predicate),
            projection_count=2,
            max_rows=maximum,
            unique_sets=unique,
        )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=1,
                depth=1,
                ctes=0,
                branches=1,
                projection=2,
                predicates=1,
                scanned=maximum,
                intermediate=maximum,
                output=maximum,
            ),
            frozenset({"multivalue"}),
        )

    def _index_shape(
        self, manifest: SchemaManifest, rows: Mapping[str, int], feature_id: str
    ) -> _BuiltQuery:
        if feature_id == "index_fulltext":
            return self._fulltext(manifest, rows)
        if feature_id == "index_spatial":
            return self._spatial(manifest, rows)
        if feature_id == "index_multivalue":
            return self._multivalue(manifest, rows)
        table = manifest.tables[0]
        descending: frozenset[int]
        if feature_id == "index_prefix":
            index = next(
                (
                    item
                    for item in table.indexes
                    if any(part.prefix_length is not None for part in item.parts)
                ),
                None,
            )
            if index is None:
                raise TargetNotReachable("prefix-index query requires a prefix index")
            part = next(part for part in index.parts if part.prefix_length is not None)
            assert part.column_name is not None
            predicate: Expression = BinaryExpression(
                self._column("t", table.column(part.column_name)),
                BinaryOperator.LIKE,
                Literal("a%", SqlType.TEXT),
                SqlType.BOOLEAN,
            )
            descending = frozenset()
        elif feature_id == "index_descending":
            index = next(
                (
                    item
                    for item in table.indexes
                    if any(part.direction.value == "DESC" for part in item.parts)
                ),
                None,
            )
            if index is None:
                raise TargetNotReachable("descending-index query requires a DESC key part")
            predicate = BinaryExpression(
                self._id(table, "t"),
                BinaryOperator.GE,
                Literal(0, SqlType.NUMERIC),
                SqlType.BOOLEAN,
            )
            descending = frozenset({1})
        else:
            index = next(
                (item for item in table.indexes if item.kind is IndexKind.FUNCTIONAL), None
            )
            if index is None:
                raise TargetNotReachable("functional-index query requires a functional index")
            expression_part = next(
                (part.expression for part in index.parts if part.expression), None
            )
            if expression_part is None:
                raise TargetNotReachable("functional index lacks its expression")
            if expression_part.cast_length is None:
                raise TargetNotReachable("functional LOWER index lacks a cast length")
            lowered = FunctionalLowerExpression(
                self._column("t", table.column(expression_part.column_name)),
                expression_part.cast_length,
            )
            predicate = BinaryExpression(
                lowered, BinaryOperator.EQ, Literal("alpha", SqlType.TEXT), SqlType.BOOLEAN
            )
            descending = frozenset()
        projection, unique = self._base_projection(table, "t")
        maximum = rows[table.name]
        body = SelectQuery(projection, TableRelation(table.name, "t"), predicate)
        ast = self._ast(
            body,
            projection_count=len(projection),
            max_rows=maximum,
            unique_sets=unique,
            descending=descending,
        )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=1,
                depth=1,
                ctes=0,
                branches=1,
                projection=len(projection),
                predicates=1,
                scanned=maximum,
                intermediate=maximum,
                output=maximum,
            ),
            frozenset({feature_id}),
        )

    def _type_domain(
        self, manifest: SchemaManifest, rows: Mapping[str, int], feature_id: str
    ) -> _BuiltQuery:
        table = manifest.tables[0]
        identity = self._id(table, "t")
        tags = {"type_domain"}
        projection: tuple[Projection, ...]
        if feature_id == "type_numeric_boundaries":
            expression: Expression = CastExpression(identity, "DECIMAL(65,30)", SqlType.NUMERIC)
            projection = (
                Projection(identity, "id"),
                Projection(expression, "domain_value"),
            )
        elif feature_id == "type_string_lob_boundaries":
            payload = table.column("payload")
            expression = FunctionCall(
                FunctionName.OCTET_LENGTH,
                (self._column("t", payload),),
                SqlType.NUMERIC,
            )
            projection = (
                Projection(identity, "id"),
                Projection(expression, "domain_value"),
            )
        else:
            timestamp_signature = next(
                signature
                for signature in DETERMINISTIC_FUNCTION_SIGNATURES
                if signature.sql_name == "TIMESTAMP" and len(signature.arguments) == 1
            )
            projection = (
                Projection(identity, "id"),
                Projection(
                    CastExpression(
                        Literal("1000-01-01 00:00:00.000000", SqlType.TEMPORAL),
                        "DATETIME(6)",
                        SqlType.TEMPORAL,
                    ),
                    "datetime_lower",
                ),
                Projection(
                    CastExpression(
                        Literal("9999-12-31 23:59:59.999999", SqlType.TEMPORAL),
                        "DATETIME(6)",
                        SqlType.TEMPORAL,
                    ),
                    "datetime_upper",
                ),
                Projection(
                    RegisteredFunctionCall(
                        timestamp_signature,
                        (
                            Literal(
                                "1970-01-01 00:00:01.000000",
                                SqlType.TEMPORAL,
                            ),
                        ),
                        SqlType.TEMPORAL,
                    ),
                    "timestamp_lower",
                ),
                Projection(
                    RegisteredFunctionCall(
                        timestamp_signature,
                        (
                            Literal(
                                "2038-01-19 03:14:07.499999",
                                SqlType.TEMPORAL,
                            ),
                        ),
                        SqlType.TEMPORAL,
                    ),
                    "timestamp_upper",
                ),
                Projection(Literal(None, SqlType.TEMPORAL), "temporal_null"),
            )
            tags.update(
                {
                    "type_temporal",
                    "temporal_datetime_fsp6_bounds",
                    "temporal_timestamp_fsp6_bounds",
                    "temporal_null_witness",
                }
            )
        maximum = rows[table.name]
        unique = frozenset({frozenset({1})}) if self._unique_key(table) == ("id",) else frozenset()
        ast = self._ast(
            SelectQuery(projection, TableRelation(table.name, "t")),
            projection_count=len(projection),
            max_rows=maximum,
            unique_sets=unique,
        )
        return _BuiltQuery(
            ast,
            self._complexity(
                tables=1,
                depth=1,
                ctes=0,
                branches=1,
                projection=len(projection),
                predicates=0,
                scanned=maximum,
                intermediate=maximum,
                output=maximum,
            ),
            frozenset(tags),
        )

    def _build_negative(
        self, manifest: SchemaManifest, *, rng: random.Random, rows: Mapping[str, int]
    ) -> _BuiltQuery:
        table = manifest.tables[0]
        mutation = rng.randrange(3)
        if mutation == 0:
            body: QueryBody = SelectQuery(
                (Projection(ColumnRef("t", "missing_column", SqlType.UNKNOWN), "bad"),),
                TableRelation(table.name, "t"),
            )
            expected = ExpectedError(ExpectedErrorKind.UNKNOWN_COLUMN, 1054, "42S22")
            tag = "negative_unknown_column"
        elif mutation == 1:
            left = SelectQuery(
                (Projection(Literal(1, SqlType.NUMERIC), "a"),),
                TableRelation(table.name, "t"),
            )
            right = SelectQuery(
                (
                    Projection(Literal(1, SqlType.NUMERIC), "a"),
                    Projection(Literal(2, SqlType.NUMERIC), "b"),
                )
            )
            body = SetQuery((left, right), SetOperator.UNION)
            expected = ExpectedError(ExpectedErrorKind.SET_ARITY_MISMATCH, 1222, "21000")
            tag = "negative_set_arity"
        else:
            body = SelectQuery(
                (
                    Projection(
                        InvalidFunctionArity(FunctionName.ABS),
                        "bad",
                    ),
                ),
                TableRelation(table.name, "t"),
            )
            expected = ExpectedError(
                ExpectedErrorKind.INVALID_FUNCTION_ARITY,
                1582,
                "42000",
            )
            tag = "negative_function_arity"
        ast = self._ast(body, projection_count=1, max_rows=rows[table.name])
        complexity = self._complexity(
            tables=1,
            depth=1,
            ctes=0,
            branches=2 if mutation == 1 else 1,
            projection=1,
            predicates=0,
            scanned=rows[table.name],
            intermediate=rows[table.name],
            output=rows[table.name],
        )
        return _BuiltQuery(ast, complexity, frozenset({tag}), expected)


class QueryBatchPlanner:
    """Bind configurable round size to the persistent coverage-debt schedule."""

    def __init__(self, generator: QueryGenerator) -> None:
        self.generator = generator

    def plan(
        self,
        manifest: SchemaManifest,
        *,
        scheduler: CoverageScheduler,
        run_seed: int,
        start_case_ordinal: int,
        queries_per_round: int,
        lane: QueryLane | None = None,
        budget: QueryBudget | None = None,
        estimated_rows_by_table: Mapping[str, int] | None = None,
        allow_compatible_fallback: bool = False,
        require_table_reference: bool = False,
    ) -> tuple[GeneratedQuery, ...]:
        if (
            not isinstance(queries_per_round, int)
            or isinstance(queries_per_round, bool)
            or queries_per_round <= 0
        ):
            raise ValueError("queries_per_round must be a positive integer")
        tree = SeedTree(run_seed)
        generated: list[GeneratedQuery] = []
        provisional_leaf_hits: dict[str, int] = {}
        leaf_ledger_snapshot = (
            scheduler.ledger.snapshot() if isinstance(scheduler, CoverageScheduler) else {}
        )
        for offset in range(queries_per_round):
            ordinal = start_case_ordinal + offset
            if ordinal < scheduler.plan_start_ordinal:
                raise ValueError("start_case_ordinal predates the coverage plan")
            # A round can reserve far more cases than one minimum-hit debt cycle
            # (for example 10,000 queries). Repeat the immutable debt ordering as a
            # provisional reservation without falsely recording hits before execution.
            selection_ordinal = ordinal
            if scheduler.planned_case_count:
                selection_ordinal = scheduler.plan_start_ordinal + (
                    (ordinal - scheduler.plan_start_ordinal) % scheduler.planned_case_count
                )
            attempts = max(1, scheduler.planned_case_count)
            attempted_features: set[str] = set()
            candidate_targets: list[FeatureSpec] = []
            last_failure: TargetNotReachable | QueryBudgetExceeded | None = None
            for attempt in range(attempts):
                candidate_ordinal = selection_ordinal
                if scheduler.planned_case_count:
                    candidate_ordinal = scheduler.plan_start_ordinal + (
                        (selection_ordinal - scheduler.plan_start_ordinal + attempt)
                        % scheduler.planned_case_count
                    )
                target = scheduler.choose(case_ordinal=candidate_ordinal)
                if target.feature_id in attempted_features:
                    continue
                attempted_features.add(target.feature_id)
                candidate_targets.append(target)
            if allow_compatible_fallback and isinstance(scheduler, CoverageScheduler):
                fallback_targets = sorted(
                    scheduler.enabled_specs,
                    key=lambda spec: tree.derive(
                        "query_target_fallback",
                        ordinal,
                        spec.feature_id,
                    ),
                )
                for target in fallback_targets:
                    if target.feature_id in attempted_features:
                        continue
                    attempted_features.add(target.feature_id)
                    candidate_targets.append(target)
            for target in candidate_targets:
                leaves: tuple[DirectedQueryLeaf | None, ...] = (None,)
                if isinstance(scheduler, CoverageScheduler):
                    directed_leaves = self.generator.directed_leaf_variants(target.feature_id)
                    if directed_leaves:
                        ranked_leaves = sorted(
                            directed_leaves,
                            key=lambda leaf: (
                                leaf_ledger_snapshot.get(leaf.coverage_tag, 0)
                                + provisional_leaf_hits.get(leaf.coverage_tag, 0),
                                tree.derive(
                                    "query_leaf",
                                    ordinal,
                                    target.feature_id,
                                    leaf.variant_id,
                                ),
                            ),
                        )
                        leaves = tuple(ranked_leaves)
                candidate_generated = False
                for leaf in leaves:
                    variant_id = None if leaf is None else leaf.variant_id
                    seed = tree.derive(
                        "query_case",
                        ordinal,
                        target.feature_id,
                        variant_id or "undirected",
                    )
                    try:
                        query = self.generator.generate(
                            manifest,
                            target=target,
                            seed=seed,
                            case_ordinal=ordinal,
                            lane=lane,
                            budget=budget,
                            estimated_rows_by_table=estimated_rows_by_table,
                            directed_variant=variant_id,
                        )
                    except (TargetNotReachable, QueryBudgetExceeded) as error:
                        last_failure = error
                        continue
                    if require_table_reference and query.complexity.tables == 0:
                        # Scalar leaves remain useful in the exhaustive catalog,
                        # but production fuzz cases must exercise the generated
                        # schema. Re-render a seeded, safe table-reading fallback.
                        query = self.generator.generate(
                            manifest,
                            target=target,
                            seed=tree.derive("query_table_fallback", ordinal),
                            case_ordinal=ordinal,
                            lane=QueryLane.FREE_RANDOM,
                            budget=budget,
                            estimated_rows_by_table=estimated_rows_by_table,
                            require_top_n=True,
                        )
                        if query.complexity.tables == 0:  # pragma: no cover - invariant
                            raise AssertionError("table-reading fallback produced scalar SQL")
                    if leaf is not None and query.coverage_eligible:
                        if leaf.coverage_tag not in query.feature_tags:
                            raise AssertionError(
                                "directed query leaf did not emit its coverage tag"
                            )
                        provisional_leaf_hits[leaf.coverage_tag] = (
                            provisional_leaf_hits.get(leaf.coverage_tag, 0) + 1
                        )
                    generated.append(query)
                    candidate_generated = True
                    break
                if candidate_generated:
                    break
            else:
                raise TargetNotReachable(
                    "no scheduled query target is reachable for the generated schema"
                ) from last_failure
        return tuple(generated)


__all__ = [
    "EvidenceGateError",
    "GeneratedQuery",
    "QueryBatchPlanner",
    "QueryBudget",
    "QueryBudgetExceeded",
    "QueryComplexity",
    "QueryGenerator",
    "QueryLane",
    "QueryMix",
    "SUPPORTED_VARIANT_IDS",
    "TargetNotReachable",
    "UnsupportedQueryFeature",
]
