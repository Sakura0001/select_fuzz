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
from select_fuzz.generation.query_ast import (
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
    InvalidFunctionArity,
    JsonMemberOf,
    JsonTableRelation,
    JoinKind,
    JoinRelation,
    Literal,
    MatchAgainst,
    NamedRelation,
    OrderBy,
    ParenthesizedQuery,
    Projection,
    QueryAst,
    QueryBody,
    QueryScope,
    Relation,
    RowExpression,
    SelectQuery,
    SetOperator,
    SetQuery,
    SqlType,
    Star,
    SubqueryExpression,
    SubqueryOperator,
    TableRelation,
    UnaryExpression,
    UnaryOperator,
    ValuesQuery,
    WindowFunction,
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
    valid_percent: int = 90
    free_random_percent: int = 5
    negative_percent: int = 5

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
            raise QueryBudgetExceeded(
                "query exceeds hard " + ", ".join(violations) + " budget"
            )
        sql = render_query_ast(built.ast)
        self.validator.validate_text(sql)
        tags = frozenset(
            {target.feature_id, f"lane_{chosen_lane.value}", *built.extra_tags}
        )
        return GeneratedQuery(
            ast=built.ast,
            sql=sql,
            target_feature_id=target.feature_id,
            feature_tags=tags,
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
    ) -> _BuiltQuery:
        """Choose an undirected safe shape that cannot satisfy coverage debt."""

        if require_top_n:
            built = self._simple(manifest, rows, top_n=True, free_random=True)
            shape = "top_n"
        else:
            shape = rng.choice(
                ("simple", "scalar_literal", "parenthesized", "case", "function", "grouping")
            )
            if shape == "simple":
                built = self._simple(manifest, rows, top_n=False, free_random=True)
            elif shape == "scalar_literal":
                built = self._scalar_literal()
            elif shape == "parenthesized":
                built = self._parenthesized(manifest, rows)
            elif shape == "case":
                built = self._case(manifest, rows, searched=bool(rng.randrange(2)))
            elif shape == "function":
                built = self._deterministic_function(manifest, rows)
            else:
                built = self._grouping(
                    manifest,
                    rows,
                    rollup=bool(rng.randrange(2)),
                    having=bool(rng.randrange(2)),
                )
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
            raise EvidenceGateError(
                f"official evidence lock is not ready for {target.feature_id}"
            )
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
            return self._simple(manifest, rows, top_n=require_top_n, free_random=free_random)
        if feature_id == "select_parenthesized":
            return self._parenthesized(manifest, rows)
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
            return self._quantified_subquery(manifest, rows, rng)
        if feature_id == "derived_regular":
            return self._derived(manifest, rows, lateral=False)
        if feature_id == "lateral_correlated":
            return self._derived(manifest, rows, lateral=True)
        if feature_id == "cte_nonrecursive":
            return self._cte(manifest, rows, recursive=False)
        if feature_id == "cte_recursive":
            return self._cte(manifest, rows, recursive=True)
        if feature_id in {"set_union", "set_intersect", "set_except"}:
            if directed_variant == "scalar_intersect_except":
                return self._scalar_intersect_except()
            operation = {
                "set_union": SetOperator.UNION,
                "set_intersect": SetOperator.INTERSECT,
                "set_except": SetOperator.EXCEPT,
            }[feature_id]
            return self._set_operation(manifest, rows, operation, chain=False)
        if feature_id == "set_table_values":
            if directed_variant == "values_only":
                return self._values_only(limit=False)
            if directed_variant == "values_limit":
                return self._values_only(limit=True)
            return self._set_values(manifest, rows)
        if feature_id in {
            "grouping_aggregate_having",
            "grouping_with_rollup",
            "function_aggregate",
        }:
            if directed_variant == "scalar_rollup":
                return self._scalar_rollup()
            return self._grouping(
                manifest,
                rows,
                rollup=feature_id == "grouping_with_rollup",
                having=feature_id == "grouping_aggregate_having",
            )
        if feature_id in {"window_inline_named", "window_frames"}:
            return self._window(manifest, rows, frame=feature_id == "window_frames")
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
            return self._deterministic_function(manifest, rows)
        if feature_id == "function_fulltext_spatial":
            return self._profile_function(manifest, rows)
        if feature_id == "regression_8041_union_view_charset":
            return self._union_charset(manifest, rows)
        if feature_id == "regression_8041_desc_pk_index_merge":
            return self._index_merge(manifest, rows)
        if feature_id == "regression_8041_union_chain_flatten":
            return self._set_operation(manifest, rows, SetOperator.UNION, chain=True)
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
    ) -> QueryAst:
        scope = QueryScope(projection_count, unique_sets, max_rows)
        return QueryAst(
            body,
            OrderBy(tuple(range(1, projection_count + 1)), descending),
            scope,
            ctes,
            recursive,
            limit,
            windows,
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
            body = SelectQuery((Projection(count_call, "row_count"),), TableRelation(table.name, "t"))
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

    def _parenthesized(self, manifest: SchemaManifest, rows: Mapping[str, int]) -> _BuiltQuery:
        base = self._simple(manifest, rows, top_n=False, free_random=False)
        ast = QueryAst(
            ParenthesizedQuery(base.ast.body),
            base.ast.order_by,
            base.ast.scope,
        )
        return _BuiltQuery(ast, base.complexity, frozenset({"parenthesized"}))

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
        if outer:
            choices = {
                "left": JoinKind.LEFT,
                "right": JoinKind.RIGHT,
                "natural_left": JoinKind.NATURAL_LEFT,
                "natural_right": JoinKind.NATURAL_RIGHT,
            }
        else:
            choices = {
                "inner": JoinKind.INNER,
                "cross": JoinKind.CROSS,
                "straight": JoinKind.STRAIGHT,
            }
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
            raise ValueError(f"unknown directed join variant: {directed}")
        variant = directed_kind or rng.choice(sorted(choices))
        kind = choices[variant]
        equality = BinaryExpression(
            self._id(left, "t"),
            BinaryOperator.EQ,
            self._id(right, "u"),
            SqlType.BOOLEAN,
        )
        natural = kind in {JoinKind.NATURAL_LEFT, JoinKind.NATURAL_RIGHT}
        relation = JoinRelation(
            TableRelation(left.name, "t"),
            TableRelation(right.name, "u"),
            kind,
            None if natural or kind is JoinKind.CROSS else equality,
        )
        predicate: Expression | None = equality if kind is JoinKind.CROSS else None
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
        key_left = self._unique_key(left) == ("id",)
        key_right = self._unique_key(right) == ("id",)
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
        # CROSS evaluates the full Cartesian intermediate even when WHERE bounds output.
        intermediate = product if kind is JoinKind.CROSS else estimate
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
        tag = (
            f"join_{directed}"
            if directed in {"left_subquery", "inner_subquery", "inner_cast"}
            else f"join_{variant}"
        )
        return _BuiltQuery(ast, complexity, frozenset({tag}))

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
        variant = "materialized" if materialized else directed or rng.choice(("scalar", "row", "exists"))
        if not materialized and variant not in {"scalar", "row", "exists"}:
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
            maximum_call = FunctionCall(
                FunctionName.MAX, (self._id(inner, "u"),), SqlType.NUMERIC
            )
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
            predicate = SubqueryExpression(SubqueryOperator.EXISTS, inner_query)
        projection, unique = self._base_projection(outer, "t")
        body = SelectQuery(projection, TableRelation(outer.name, "t"), predicate)
        maximum = rows[outer.name]
        ast = self._ast(
            body,
            projection_count=len(projection),
            max_rows=maximum,
            unique_sets=unique,
        )
        correlated_work = rows[outer.name] * rows[inner.name]
        complexity = self._complexity(
            tables=2,
            depth=2,
            ctes=0,
            branches=1,
            projection=len(projection),
            predicates=1,
            scanned=(
                rows[outer.name] + correlated_work
                if variant == "exists"
                else rows[outer.name] + rows[inner.name]
            ),
            intermediate=(
                correlated_work
                if variant == "exists"
                else max(rows[outer.name], rows[inner.name])
            ),
            output=maximum,
        )
        tag = f"{variant}_subquery"
        return _BuiltQuery(ast, complexity, frozenset({tag}))

    def _quantified_subquery(
        self, manifest: SchemaManifest, rows: Mapping[str, int], rng: random.Random
    ) -> _BuiltQuery:
        outer = manifest.tables[0]
        inner = manifest.tables[1] if len(manifest.tables) > 1 else outer
        inner_query = SelectQuery(
            (Projection(self._id(inner, "u"), "inner_id"),),
            TableRelation(inner.name, "u"),
        )
        operator = rng.choice((SubqueryOperator.ANY, SubqueryOperator.ALL))
        predicate = SubqueryExpression(operator, inner_query, self._id(outer, "t"))
        projection, unique = self._base_projection(outer, "t")
        body = SelectQuery(projection, TableRelation(outer.name, "t"), predicate)
        maximum = rows[outer.name]
        ast = self._ast(body, projection_count=len(projection), max_rows=maximum, unique_sets=unique)
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
        self, manifest: SchemaManifest, rows: Mapping[str, int], *, lateral: bool
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
                frozenset({frozenset({1})})
                if self._unique_key(outer) == ("id",)
                else frozenset()
            )
            scanned = rows[outer.name] + rows[outer.name] * rows[inner.name]
            intermediate = rows[outer.name] * rows[inner.name]
        else:
            inner_projection, inner_unique = self._base_projection(inner, "u")
            derived_body = SelectQuery(inner_projection, TableRelation(inner.name, "u"))
            relation = DerivedRelation(derived_body, "d")
            projection = tuple(
                Projection(ColumnRef("d", item.alias or "", item.expression.sql_type), item.alias)
                for item in inner_projection
            )
            maximum = rows[inner.name]
            unique = inner_unique
            scanned = rows[inner.name]
            intermediate = rows[inner.name]
        body = SelectQuery(projection, relation)
        ast = self._ast(body, projection_count=len(projection), max_rows=maximum, unique_sets=unique)
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
        return _BuiltQuery(ast, complexity, frozenset({"lateral" if lateral else "derived"}))

    def _cte(
        self, manifest: SchemaManifest, rows: Mapping[str, int], *, recursive: bool
    ) -> _BuiltQuery:
        table = manifest.tables[0]
        if recursive:
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
            cte = Cte("r", ("n",), SetQuery((anchor, recursive_step), SetOperator.UNION, True))
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
            return _BuiltQuery(ast, complexity, frozenset({"bounded_recursion"}))
        projection, unique = self._base_projection(table, "t")
        cte_query = SelectQuery(projection, TableRelation(table.name, "t"))
        aliases = tuple(item.alias or "" for item in projection)
        cte = Cte("c0", aliases, cte_query)
        outer_projection = tuple(
            Projection(ColumnRef("c", alias, item.expression.sql_type), alias)
            for alias, item in zip(aliases, projection, strict=True)
        )
        body = SelectQuery(outer_projection, NamedRelation("c0", "c"))
        maximum = rows[table.name]
        ast = self._ast(
            body,
            projection_count=len(projection),
            max_rows=maximum,
            unique_sets=unique,
            ctes=(cte,),
        )
        complexity = self._complexity(
            tables=1,
            depth=2,
            ctes=1,
            branches=1,
            projection=len(projection),
            predicates=0,
            scanned=maximum,
            intermediate=maximum,
            output=maximum,
        )
        return _BuiltQuery(ast, complexity, frozenset({"cte_nonrecursive"}))

    def _set_operation(
        self,
        manifest: SchemaManifest,
        rows: Mapping[str, int],
        operator: SetOperator,
        *,
        chain: bool,
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
        all_rows = sum(rows[table.name] for table in tables)
        body = SetQuery(branches, operator, all=operator is SetOperator.UNION and chain)
        unique = (
            frozenset()
            if operator is SetOperator.UNION and chain
            else frozenset({frozenset({1})})
        )
        ast = self._ast(body, projection_count=1, max_rows=all_rows, unique_sets=unique)
        complexity = self._complexity(
            tables=len(tables),
            depth=1,
            ctes=0,
            branches=len(branches),
            projection=1,
            predicates=0,
            scanned=all_rows,
            intermediate=all_rows,
            output=all_rows,
        )
        return _BuiltQuery(ast, complexity, frozenset({f"set_{operator.value.lower()}"}))

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
        self, manifest: SchemaManifest, rows: Mapping[str, int], *, frame: bool
    ) -> _BuiltQuery:
        table = manifest.tables[0]
        maximum = rows[table.name]
        key = self._unique_key(table)
        if key is None:
            count = FunctionCall(FunctionName.COUNT, (Star(),), SqlType.NUMERIC)
            derived = SelectQuery(
                (Projection(count, "row_count"),), TableRelation(table.name, "t")
            )
            value = ColumnRef("d", "row_count", SqlType.NUMERIC)
            order = WindowOrder((value,), proven_unique=False, max_rows=1)
            spec = WindowSpec((), order, (1, 1) if frame else None, "w0" if not frame else None)
            window_ref: WindowSpec | str = "w0" if not frame else spec
            window = WindowFunction(
                "SUM" if frame else "ROW_NUMBER",
                value if frame else None,
                window_ref,
                SqlType.NUMERIC,
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
            spec = WindowSpec((), order, (1, 1) if frame else None, "w0" if not frame else None)
            window_ref = "w0" if not frame else spec
            window = WindowFunction(
                "SUM" if frame else "ROW_NUMBER",
                self._id(table, "t") if frame else None,
                window_ref,
                SqlType.NUMERIC,
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
        return _BuiltQuery(ast, complexity, frozenset({"window_frame" if frame else "window_named"}))

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
        ast = self._ast(body, projection_count=len(projection), max_rows=maximum, unique_sets=unique)
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
                    CastExpression(
                        Literal('{"type":"array"}', SqlType.TEXT), "JSON", SqlType.JSON
                    ),
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
        return _BuiltQuery(ast, complexity, frozenset({"case_searched" if searched else "case_simple"}))

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
            index = next((item for item in table.indexes if not item.primary), None)
            if index is None or index.name != "ix_id_desc":
                raise TargetNotReachable("INDEX hint target ix_id_desc is absent")
            projection, unique = self._base_projection(table, "t")
            body = SelectQuery(
                projection,
                TableRelation(table.name, "t"),
                optimizer_hint="INDEX(t ix_id_desc)",
            )
            ast = self._ast(body, projection_count=len(projection), max_rows=maximum, unique_sets=unique)
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
            ast = self._ast(body, projection_count=len(projection), max_rows=maximum, unique_sets=unique)
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
        return _BuiltQuery(ast, complexity, frozenset({"optimizer_hint"}))

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
        ast = self._ast(body, projection_count=len(projection), max_rows=maximum, unique_sets=unique)
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

    def _deterministic_function(
        self, manifest: SchemaManifest, rows: Mapping[str, int]
    ) -> _BuiltQuery:
        table = manifest.tables[0]
        identity = self._id(table, "t")
        expression = FunctionCall(
            FunctionName.COALESCE,
            (
                FunctionCall(FunctionName.ABS, (identity,), SqlType.NUMERIC),
                Literal(0, SqlType.NUMERIC),
            ),
            SqlType.NUMERIC,
        )
        projection = (Projection(identity, "id"), Projection(expression, "function_value"))
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
            predicates=0,
            scanned=maximum,
            intermediate=maximum,
            output=maximum,
        )
        return _BuiltQuery(ast, complexity, frozenset({"deterministic_function"}))

    def _profile_function(
        self, manifest: SchemaManifest, rows: Mapping[str, int]
    ) -> _BuiltQuery:
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
            BinaryExpression(score, BinaryOperator.GT, Literal(0, SqlType.NUMERIC), SqlType.BOOLEAN),
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

    def _union_charset(
        self, manifest: SchemaManifest, rows: Mapping[str, int]
    ) -> _BuiltQuery:
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
            part.direction.value == "DESC"
            for index in table.indexes
            for part in index.parts
        )
        if not has_primary or not has_descending:
            raise TargetNotReachable(
                "DESC primary/index-merge regression requires PRIMARY and a DESC index"
            )
        identity = self._id(table, "t")
        predicate = BinaryExpression(
            BinaryExpression(identity, BinaryOperator.EQ, Literal(1, SqlType.NUMERIC), SqlType.BOOLEAN),
            BinaryOperator.OR,
            BinaryExpression(identity, BinaryOperator.GT, Literal(5, SqlType.NUMERIC), SqlType.BOOLEAN),
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

    def _distinct_not_in(
        self, manifest: SchemaManifest, rows: Mapping[str, int]
    ) -> _BuiltQuery:
        outer = manifest.tables[0]
        inner = manifest.tables[1] if len(manifest.tables) > 1 else outer
        subquery = SelectQuery(
            (Projection(self._id(inner, "u"), "id"),),
            TableRelation(inner.name, "u"),
            distinct=True,
        )
        predicate = SubqueryExpression(
            SubqueryOperator.NOT_IN, subquery, self._id(outer, "t")
        )
        projection, unique = self._base_projection(outer, "t")
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
        ast = self._ast(body, projection_count=len(projection), max_rows=maximum, unique_sets=unique)
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
            Projection(FunctionCall(FunctionName.JSON_TYPE, (document,), SqlType.TEXT), "json_type"),
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
            index = next((item for item in table.indexes if item.kind is IndexKind.FUNCTIONAL), None)
            if index is None:
                raise TargetNotReachable("functional-index query requires a functional index")
            expression_part = next((part.expression for part in index.parts if part.expression), None)
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
        if (
            feature_id == "type_temporal_json_spatial"
            and manifest.profile is SchemaProfile.SPATIAL_INNODB
        ):
            return self._spatial(manifest, rows)
        table = manifest.tables[0]
        identity = self._id(table, "t")
        extra_projection: tuple[Projection, ...] = ()
        if feature_id == "type_numeric_boundaries":
            expression: Expression = CastExpression(identity, "DECIMAL(65,30)", SqlType.NUMERIC)
        elif feature_id == "type_string_lob_boundaries":
            payload = table.column("payload")
            expression = FunctionCall(
                FunctionName.OCTET_LENGTH,
                (self._column("t", payload),),
                SqlType.NUMERIC,
            )
        else:
            expression = FunctionCall(
                FunctionName.JSON_TYPE,
                (CastExpression(Literal("null", SqlType.TEXT), "JSON", SqlType.JSON),),
                SqlType.TEXT,
            )
            extra_projection = (
                Projection(
                    CastExpression(
                        Literal("2000-01-01 00:00:00.000000", SqlType.TEXT),
                        "DATETIME(6)",
                        SqlType.TEMPORAL,
                    ),
                    "temporal_value",
                ),
            )
        projection = (
            Projection(identity, "id"),
            Projection(expression, "domain_value"),
            *extra_projection,
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
            frozenset({"type_domain"}),
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
            left = SelectQuery((Projection(Literal(1, SqlType.NUMERIC), "a"),))
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
                )
            )
            expected = ExpectedError(ExpectedErrorKind.INVALID_FUNCTION_ARITY)
            tag = "negative_function_arity"
        ast = self._ast(body, projection_count=1, max_rows=rows[table.name])
        complexity = self._complexity(
            tables=1 if mutation == 0 else 0,
            depth=1,
            ctes=0,
            branches=2 if mutation == 1 else 1,
            projection=1,
            predicates=0,
            scanned=rows[table.name] if mutation == 0 else 0,
            intermediate=rows[table.name] if mutation == 0 else 1,
            output=rows[table.name] if mutation == 0 else 1,
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
    ) -> tuple[GeneratedQuery, ...]:
        if (
            not isinstance(queries_per_round, int)
            or isinstance(queries_per_round, bool)
            or queries_per_round <= 0
        ):
            raise ValueError("queries_per_round must be a positive integer")
        tree = SeedTree(run_seed)
        generated: list[GeneratedQuery] = []
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
                    (ordinal - scheduler.plan_start_ordinal)
                    % scheduler.planned_case_count
                )
            target = scheduler.choose(case_ordinal=selection_ordinal)
            seed = tree.derive("query_case", ordinal, target.feature_id)
            generated.append(
                self.generator.generate(
                    manifest,
                    target=target,
                    seed=seed,
                    case_ordinal=ordinal,
                    lane=lane,
                    budget=budget,
                    estimated_rows_by_table=estimated_rows_by_table,
                )
            )
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
