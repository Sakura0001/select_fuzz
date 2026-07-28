"""Production adapter over the real catalog, schema, and directed query generator."""

from __future__ import annotations

from select_fuzz.domain import SeedTree
from select_fuzz.generation.catalog import FeatureCatalog, FeatureSpec
from select_fuzz.generation.catalog_schema import REVIEWED_VARIANT_IDS
from select_fuzz.generation.query_grammar import (
    CandidateRejected,
    GrammarColumn,
    GrammarQueryGenerator,
    GrammarSchema,
    GrammarTable,
    SelectGrammar,
)
from select_fuzz.generation.schema import SchemaGenerator, SchemaLimits
from select_fuzz.validation.models import FeatureSignature
from select_fuzz.validation.reachability import CatalogCapability, GeneratedWitness
from select_fuzz.validation.signature import SignatureExtractor


_NODE_MAP = {
    "query_expression": "select",
    "query_specification": "select",
    "predicate_expression": "predicate",
    "common_table_expression": "cte",
    "recursive_common_table_expression": "cte_recursive",
    "joined_table": "join",
    "subquery_expression": "subquery",
    "derived_table": "derived_table",
    "set_operation": "set_operation",
    "grouping_clause": "group_by",
    "aggregate_expression": "aggregate",
    "function_expression": "function_expression",
    "case_expression": "case_expression",
    "explicit_partition": "partition_selection",
    "explicit_table": "explicit_table",
    "anti_join": "anti_join",
    "hint_comment": "optimizer_hint",
    "window_clause": "window",
    "window_function": "window",
    "frame_clause": "window_frame",
    "json_table_function": "json_table",
    "table_value_constructor": "table_value_constructor",
    "row_constructor": "row_constructor",
    "parenthesized_query_expression": "parenthesized_query",
    "lateral_derived_table": "lateral_derived_table",
    "scene_profile": "scene_profile",
    "type_domain": "type_domain",
}


def normalize_catalog_nodes(spec: FeatureSpec) -> frozenset[str]:
    nodes = {_NODE_MAP.get(node, node) for node in spec.ast_nodes if node != "predicate_expression"}
    nodes.add("select")
    nodes.add("order_by")
    feature_id = spec.feature_id
    if "json" in feature_id:
        nodes.add("json_function")
    if feature_id.startswith("json_table"):
        nodes.add("function_expression")
    if feature_id == "json_create_extract":
        nodes.update(("json_object", "json_extract"))
    if "json_value" in feature_id:
        nodes.add("json_value")
    if feature_id.startswith("cte_"):
        nodes.add("cte_recursive" if feature_id == "cte_recursive" else "cte")
    if feature_id.startswith("set_union"):
        nodes.add("set_union")
    if feature_id == "set_union":
        nodes.add("set_union_distinct")
    if feature_id == "set_branch_local_top_n":
        nodes.update(
            (
                "branch_local_order_limit",
                "limit",
                "parenthesized_query",
                "set_union",
                "set_union_distinct",
            )
        )
    if feature_id == "select_nested_parenthesized_top_n":
        nodes.update(
            (
                "limit",
                "nested_parenthesized_order_limit",
                "parenthesized_query",
            )
        )
    if feature_id.startswith("set_intersect"):
        nodes.add("set_intersect")
    if feature_id.startswith("set_except"):
        nodes.add("set_except")
    if "rollup" in feature_id:
        nodes.add("rollup")
    if "having" in feature_id:
        nodes.add("having")
    if "derived_table" in nodes:
        nodes.add("subquery")
    if feature_id == "derived_explicit_columns":
        nodes.add("derived_explicit_columns")
    if feature_id.startswith("join_inner"):
        nodes.add("join_inner")
    if "window" in feature_id:
        nodes.update(("window", "window_order", "function_expression"))
    if "top_n" in feature_id:
        nodes.update(("order_by", "limit"))
    if "aggregate" in nodes:
        nodes.add("function_expression")
    return frozenset(nodes)


def _requirements(spec: FeatureSpec) -> frozenset[str]:
    requirements = {"table", "unique_tiebreaker"}
    if "compatible_types" in spec.guards:
        requirements.add("compatible_types")
    if {"joined_table", "set_operation"}.intersection(spec.ast_nodes):
        requirements.add("two_compatible_relations")
    if "bounded_recursion" in spec.guards:
        requirements.add("bounded_recursion")
    if "json" in spec.feature_id:
        requirements.add("json_column")
    if "aggregate_expression" in spec.ast_nodes or "grouping_clause" in spec.ast_nodes:
        requirements.add("grouping_legal")
    return frozenset(requirements)


_VALIDATION_SCHEMA = GrammarSchema(
    (
        GrammarTable(
            "t0",
            (
                GrammarColumn("id", "BIGINT"),
                GrammarColumn("payload", "VARCHAR(64)"),
                GrammarColumn("c2", "BIGINT"),
            ),
        ),
        GrammarTable(
            "t1",
            (
                GrammarColumn("id", "BIGINT"),
                GrammarColumn("payload", "VARCHAR(64)"),
                GrammarColumn("c2", "BIGINT"),
            ),
        ),
    )
)


_VALIDATION_SQL: dict[str, str] = {
    "grouping_aggregate_having": (
        "SELECT `t`.`payload`, COUNT(*) FROM `t0` AS `t` "
        "GROUP BY `t`.`payload` HAVING COUNT(*) > 0 ORDER BY 1, 2"
    ),
    "set_union": (
        "SELECT `t`.`id` FROM `t0` AS `t` UNION "
        "SELECT `u`.`id` FROM `t1` AS `u` ORDER BY 1"
    ),
    "validation_top_n": (
        "SELECT `t`.`id` AS `q1` FROM `t0` AS `t` ORDER BY 1 LIMIT 10"
    ),
    "validation_scalar_literal": "SELECT 1 AS `q1` ORDER BY 1",
    "validation_scalar_aggregate": "SELECT COUNT(*) AS `row_count` ORDER BY 1",
    "validation_join_left": (
        "SELECT `t`.`id`, `u`.`id` FROM `t0` AS `t` LEFT JOIN `t1` AS `u` "
        "ON (`t`.`id` = `u`.`id`) ORDER BY 1, 2"
    ),
    "validation_join_left_subquery": (
        "SELECT `t`.`id`, `u`.`id` FROM `t0` AS `t` LEFT JOIN `t1` AS `u` "
        "ON (`t`.`id` = `u`.`id`) WHERE EXISTS "
        "(SELECT 1 FROM `t1` AS `v` WHERE `v`.`id` = `t`.`id`) ORDER BY 1, 2"
    ),
    "validation_values_only": "VALUES ROW(0) ORDER BY 1",
    "validation_values_limit": "VALUES ROW(0) ORDER BY 1 LIMIT 1",
    "validation_table_only": "TABLE `t0` ORDER BY 1",
    "validation_table_values_union_all": (
        "TABLE `t0` UNION ALL VALUES ROW(NULL, NULL, NULL) ORDER BY 1, 2"
    ),
    "validation_table_values_union_distinct": (
        "TABLE `t0` UNION VALUES ROW(NULL, NULL, NULL) ORDER BY 1, 2"
    ),
    "validation_set_branch_local_top_n": (
        "(SELECT `s0`.`id` FROM `t0` AS `s0` ORDER BY 1 LIMIT 2) UNION "
        "(SELECT `s1`.`id` FROM `t1` AS `s1` ORDER BY 1 LIMIT 2) ORDER BY 1"
    ),
    "validation_scalar_set_branch_local_top_n": (
        "(SELECT 1 AS `id` ORDER BY 1 LIMIT 1) UNION "
        "(SELECT 2 AS `id` ORDER BY 1 LIMIT 1) ORDER BY 1"
    ),
    "validation_nested_parenthesized_top_n": (
        "((SELECT `t`.`id` FROM `t0` AS `t` ORDER BY 1 LIMIT 5) "
        "ORDER BY 1 LIMIT 3) ORDER BY 1 LIMIT 2"
    ),
    "validation_table_subquery": "SELECT 1 WHERE EXISTS (TABLE `t0`) ORDER BY 1",
    "validation_scalar_limit_zero": "SELECT 1 ORDER BY 1 LIMIT 0",
    "validation_table_limit_zero": (
        "SELECT COUNT(*) FROM `t0` AS `t` ORDER BY 1 LIMIT 0"
    ),
    "validation_scalar_offset_limit_zero": "SELECT 1 ORDER BY 1 LIMIT 0 OFFSET 1",
    "validation_table_offset_limit_zero": (
        "SELECT COUNT(*) FROM `t0` AS `t` ORDER BY 1 LIMIT 0 OFFSET 1"
    ),
    "validation_scalar_offset_limit": "SELECT 1 ORDER BY 1 LIMIT 1 OFFSET 1",
    "validation_table_offset_limit": (
        "SELECT `t`.`id` FROM `t0` AS `t` ORDER BY 1 LIMIT 1 OFFSET 1"
    ),
    "validation_derived_explicit_columns": (
        "SELECT `d`.`dq1` FROM (SELECT `u`.`id` AS `q1` FROM `t1` AS `u`) "
        "AS `d` (`dq1`) ORDER BY 1"
    ),
    "validation_join_cast": (
        "SELECT `t`.`id`, CAST(`u`.`id` AS SIGNED) FROM `t0` AS `t` "
        "INNER JOIN `t1` AS `u` ON (`t`.`id` = `u`.`id`) ORDER BY 1, 2"
    ),
    "validation_join_inner_subquery": (
        "SELECT `t`.`id`, `u`.`id` FROM `t0` AS `t` INNER JOIN `t1` AS `u` "
        "ON (`t`.`id` = `u`.`id`) WHERE EXISTS "
        "(SELECT 1 FROM `t1` AS `v` WHERE `v`.`id` = `t`.`id`) ORDER BY 1, 2"
    ),
    "validation_scalar_intersect_except": (
        "(SELECT 1 INTERSECT SELECT 1) EXCEPT SELECT 2 ORDER BY 1"
    ),
    "validation_scalar_subquery_limit": "SELECT (SELECT 1) ORDER BY 1 LIMIT 1",
    "validation_table_subquery_limit": (
        "SELECT COUNT(*) FROM `t0` AS `t` WHERE EXISTS "
        "(SELECT 1 FROM `t1` AS `u` WHERE `u`.`id` = `t`.`id`) ORDER BY 1 LIMIT 1"
    ),
    "validation_scalar_rollup": (
        "SELECT 1, COUNT(*) GROUP BY 1 WITH ROLLUP ORDER BY 1, 2"
    ),
}


class ProductionGeneratorAdapter:
    def __init__(
        self,
        *,
        grammar_query_generator: GrammarQueryGenerator | None = None,
        schema_generator: SchemaGenerator | None = None,
        limits: SchemaLimits | None = None,
    ) -> None:
        self.grammar_query_generator = grammar_query_generator or GrammarQueryGenerator()
        self.schema_generator = schema_generator or SchemaGenerator()
        self.limits = limits or SchemaLimits(
            min_tables=1,
            max_tables=3,
            min_columns=3,
            max_columns=6,
            max_indexes_per_table=4,
        )
        self.catalog = FeatureCatalog.default(generator_supported_ids=REVIEWED_VARIANT_IDS)
        self._specs = {spec.feature_id: spec for spec in self.catalog}

    def signature_for_feature(self, feature_id: str) -> FeatureSignature:
        spec = self._specs[feature_id]
        return FeatureSignature(
            "8.0.41",
            tuple(normalize_catalog_nodes(spec)),
            tuple(_requirements(spec)),
        )

    def capability_for_feature(self, feature_id: str) -> CatalogCapability:
        spec = self._specs[feature_id]
        signature = self.signature_for_feature(feature_id)
        return CatalogCapability(
            feature_id=feature_id,
            nodes=frozenset(signature.nodes),
            requirements=frozenset(signature.requirements),
            evidence_ready=spec.evidence_lock_ready,
            evidence_ids=tuple(sorted(spec.unverified_evidence_sources)),
        )

    def find_capability(self, signature: FeatureSignature) -> CatalogCapability:
        target = set(signature.nodes)
        requirements = set(signature.requirements)
        if "nested_parenthesized_order_limit" in target:
            evidence = self._specs["select_nested_parenthesized_top_n"]
            return CatalogCapability(
                feature_id="validation_nested_parenthesized_top_n",
                nodes=frozenset(
                    {
                        "select",
                        "order_by",
                        "limit",
                        "parenthesized_query",
                        "nested_parenthesized_order_limit",
                    }
                ),
                requirements=frozenset({"table", "unique_tiebreaker"}),
                evidence_ready=evidence.evidence_lock_ready,
                evidence_ids=tuple(sorted(evidence.unverified_evidence_sources)),
            )
        if "branch_local_order_limit" in target:
            evidence = self._specs["set_branch_local_top_n"]
            scalar = "scalar_literal" in requirements
            return CatalogCapability(
                feature_id=(
                    "validation_scalar_set_branch_local_top_n"
                    if scalar
                    else "validation_set_branch_local_top_n"
                ),
                nodes=frozenset(
                    {
                        "select",
                        "order_by",
                        "limit",
                        "parenthesized_query",
                        "branch_local_order_limit",
                        "set_union",
                        "set_union_distinct",
                    }
                ),
                requirements=frozenset(
                    {
                        "scalar_literal" if scalar else "table",
                        "two_compatible_relations",
                        "unique_tiebreaker",
                    }
                ),
                evidence_ready=evidence.evidence_lock_ready,
                evidence_ids=tuple(sorted(evidence.unverified_evidence_sources)),
            )
        if "derived_explicit_columns" in target:
            evidence = self._specs["derived_explicit_columns"]
            return CatalogCapability(
                feature_id="validation_derived_explicit_columns",
                nodes=frozenset(
                    {
                        "select",
                        "order_by",
                        "derived_table",
                        "derived_explicit_columns",
                        "subquery",
                    }
                ),
                requirements=frozenset({"table", "unique_tiebreaker"}),
                evidence_ready=evidence.evidence_lock_ready,
                evidence_ids=tuple(sorted(evidence.unverified_evidence_sources)),
            )
        if "explicit_table" in target:
            if "table_value_constructor" in target:
                evidence = self._specs["table_values_union"]
                distinct = "set_union_distinct" in target
                feature_id = (
                    "validation_table_values_union_distinct"
                    if distinct
                    else "validation_table_values_union_all"
                )
                nodes = {
                    "select",
                    "order_by",
                    "explicit_table",
                    "table_value_constructor",
                    "set_union",
                    "set_union_distinct" if distinct else "set_union_all",
                }
                capability_requirements = {
                    "table",
                    "two_compatible_relations",
                    "unique_tiebreaker",
                }
            elif "subquery_exists" in target:
                evidence = self._specs["table_subquery_exists"]
                feature_id = "validation_table_subquery"
                nodes = {
                    "select",
                    "order_by",
                    "explicit_table",
                    "subquery",
                    "subquery_exists",
                }
                capability_requirements = {"table", "unique_tiebreaker"}
            else:
                evidence = self._specs["table_explicit"]
                feature_id = "validation_table_only"
                nodes = {"select", "order_by", "explicit_table"}
                capability_requirements = {"table", "unique_tiebreaker"}
            return CatalogCapability(
                feature_id=feature_id,
                nodes=frozenset(nodes),
                requirements=frozenset(capability_requirements),
                evidence_ready=evidence.evidence_lock_ready,
                evidence_ids=tuple(sorted(evidence.unverified_evidence_sources)),
            )
        if "table_value_constructor" in target:
            nodes = {"select", "order_by", "table_value_constructor"}
            if "limit" in target:
                nodes.add("limit")
            return CatalogCapability(
                feature_id=(
                    "validation_values_limit" if "limit" in target else "validation_values_only"
                ),
                nodes=frozenset(nodes),
                requirements=frozenset({"scalar_literal", "unique_tiebreaker"}),
                evidence_ready=self._specs["set_table_values"].evidence_lock_ready,
                evidence_ids=tuple(
                    sorted(self._specs["set_table_values"].unverified_evidence_sources)
                ),
            )
        if "join_left" in target:
            composite = "subquery" in target
            nodes = {"select", "order_by", "join", "join_left"}
            if composite:
                nodes.update(("subquery", "subquery_exists"))
            return CatalogCapability(
                feature_id=(
                    "validation_join_left_subquery" if composite else "validation_join_left"
                ),
                nodes=frozenset(nodes),
                requirements=frozenset({"table", "two_compatible_relations", "unique_tiebreaker"}),
                evidence_ready=self._specs["join_outer_natural"].evidence_lock_ready,
                evidence_ids=tuple(
                    sorted(self._specs["join_outer_natural"].unverified_evidence_sources)
                ),
            )
        if "join" in target and "type_cast" in target:
            evidence = self._specs["join_inner_cross_straight"]
            return CatalogCapability(
                feature_id="validation_join_cast",
                nodes=frozenset(
                    {
                        "select",
                        "order_by",
                        "join",
                        "join_inner",
                        "type_cast",
                        "function_expression",
                    }
                ),
                requirements=frozenset(
                    {
                        "table",
                        "two_compatible_relations",
                        "compatible_types",
                        "unique_tiebreaker",
                    }
                ),
                evidence_ready=evidence.evidence_lock_ready,
                evidence_ids=tuple(sorted(evidence.unverified_evidence_sources)),
            )
        if "join_inner" in target and "subquery" in target:
            evidence = self._specs["join_inner_cross_straight"]
            return CatalogCapability(
                feature_id="validation_join_inner_subquery",
                nodes=frozenset(
                    {
                        "select",
                        "order_by",
                        "join",
                        "join_inner",
                        "subquery",
                        "subquery_exists",
                    }
                ),
                requirements=frozenset({"table", "two_compatible_relations", "unique_tiebreaker"}),
                evidence_ready=evidence.evidence_lock_ready,
                evidence_ids=tuple(sorted(evidence.unverified_evidence_sources)),
            )
        if {"set_intersect", "set_except"} <= target:
            specs = (self._specs["set_intersect"], self._specs["set_except"])
            return CatalogCapability(
                feature_id="validation_scalar_intersect_except",
                nodes=frozenset({"select", "order_by", "set_intersect", "set_except"}),
                requirements=frozenset(
                    {"scalar_literal", "two_compatible_relations", "unique_tiebreaker"}
                ),
                evidence_ready=all(spec.evidence_lock_ready for spec in specs),
                evidence_ids=tuple(
                    sorted(
                        {source for spec in specs for source in spec.unverified_evidence_sources}
                    )
                ),
            )
        if "subquery" in target and "limit" in target:
            scalar = "scalar_literal" in requirements
            evidence = self._specs["subquery_result_kinds"]
            nodes = {"select", "order_by", "limit", "subquery"}
            if not scalar:
                nodes.update(("subquery_exists", "aggregate", "function_expression"))
            return CatalogCapability(
                feature_id=(
                    "validation_scalar_subquery_limit"
                    if scalar
                    else "validation_table_subquery_limit"
                ),
                nodes=frozenset(nodes),
                requirements=frozenset(
                    ({"scalar_literal"} if scalar else {"table"}) | {"unique_tiebreaker"}
                ),
                evidence_ready=evidence.evidence_lock_ready,
                evidence_ids=tuple(sorted(evidence.unverified_evidence_sources)),
            )
        if "limit_zero" in target and "offset" in target:
            evidence = self._specs["select_query_specification"]
            scalar = "scalar_literal" in requirements
            return CatalogCapability(
                feature_id=(
                    "validation_scalar_offset_limit_zero"
                    if scalar
                    else "validation_table_offset_limit_zero"
                ),
                nodes=frozenset({"select", "order_by", "limit", "limit_zero", "offset"}),
                requirements=frozenset(
                    ({"scalar_literal"} if scalar else {"table"}) | {"unique_tiebreaker"}
                ),
                evidence_ready=evidence.evidence_lock_ready,
                evidence_ids=tuple(sorted(evidence.unverified_evidence_sources)),
            )
        if "limit_zero" in target:
            evidence = self._specs["select_query_specification"]
            scalar = "scalar_literal" in requirements
            return CatalogCapability(
                feature_id=(
                    "validation_scalar_limit_zero" if scalar else "validation_table_limit_zero"
                ),
                nodes=frozenset({"select", "order_by", "limit", "limit_zero"}),
                requirements=frozenset(
                    ({"scalar_literal"} if scalar else {"table"}) | {"unique_tiebreaker"}
                ),
                evidence_ready=evidence.evidence_lock_ready,
                evidence_ids=tuple(sorted(evidence.unverified_evidence_sources)),
            )
        if "offset" in target:
            evidence = self._specs["select_query_specification"]
            scalar = "scalar_literal" in requirements
            return CatalogCapability(
                feature_id=(
                    "validation_scalar_offset_limit" if scalar else "validation_table_offset_limit"
                ),
                nodes=frozenset({"select", "order_by", "limit", "offset"}),
                requirements=frozenset(
                    ({"scalar_literal"} if scalar else {"table"}) | {"unique_tiebreaker"}
                ),
                evidence_ready=evidence.evidence_lock_ready,
                evidence_ids=tuple(sorted(evidence.unverified_evidence_sources)),
            )
        if "rollup" in target and "scalar_literal" in requirements:
            evidence = self._specs["grouping_with_rollup"]
            return CatalogCapability(
                feature_id="validation_scalar_rollup",
                nodes=frozenset(
                    {
                        "select",
                        "order_by",
                        "group_by",
                        "rollup",
                        "aggregate",
                        "function_expression",
                    }
                ),
                requirements=frozenset({"scalar_literal", "grouping_legal", "unique_tiebreaker"}),
                evidence_ready=evidence.evidence_lock_ready,
                evidence_ids=tuple(sorted(evidence.unverified_evidence_sources)),
            )
        if "limit" in target:
            evidence = self._specs["select_query_specification"]
            return CatalogCapability(
                feature_id="validation_top_n",
                nodes=frozenset({"select", "order_by", "limit"}),
                requirements=frozenset({"table", "unique_tiebreaker"}),
                evidence_ready=evidence.evidence_lock_ready,
                evidence_ids=tuple(sorted(evidence.unverified_evidence_sources)),
            )
        if "scalar_literal" in requirements:
            if "aggregate" in target:
                evidence = self._specs["select_query_specification"]
                return CatalogCapability(
                    feature_id="validation_scalar_aggregate",
                    nodes=frozenset(
                        {
                            "select",
                            "order_by",
                            "aggregate",
                            "function_expression",
                        }
                    ),
                    requirements=frozenset(
                        {"scalar_literal", "grouping_legal", "unique_tiebreaker"}
                    ),
                    evidence_ready=evidence.evidence_lock_ready,
                    evidence_ids=tuple(sorted(evidence.unverified_evidence_sources)),
                )
            evidence = self._specs["select_query_specification"]
            return CatalogCapability(
                feature_id="validation_scalar_literal",
                nodes=frozenset({"select", "order_by"}),
                requirements=frozenset({"scalar_literal", "unique_tiebreaker"}),
                evidence_ready=evidence.evidence_lock_ready,
                evidence_ids=tuple(sorted(evidence.unverified_evidence_sources)),
            )
        candidates = [self.capability_for_feature(spec.feature_id) for spec in self._specs.values()]
        return max(
            candidates,
            key=lambda capability: (
                target <= set(capability.nodes),
                len(target & set(capability.nodes)),
                -len(set(capability.nodes) - target),
                capability.feature_id,
            ),
        )

    def generate_for_validation(self, feature_id: str, seed: int) -> GeneratedWitness:
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise TypeError("seed must be an integer")
        extractor = SignatureExtractor("8.0.41")
        directed_sql = _VALIDATION_SQL.get(feature_id)
        if directed_sql is not None:
            grammar = SelectGrammar.from_text(f"query:\n    {directed_sql}")
            candidate = GrammarQueryGenerator(grammar).generate(_VALIDATION_SCHEMA, seed=seed)
            return GeneratedWitness(candidate.sql, extractor.extract(candidate.sql))

        target = self._specs[feature_id]
        desired = self.signature_for_feature(feature_id)
        tree = SeedTree(seed)
        for schema_attempt in range(8):
            manifest = self.schema_generator.generate(
                target,
                seed=tree.derive("validation_schema", schema_attempt),
                limits=self.limits,
            )
            for candidate_attempt in range(2_048):
                candidate_seed = tree.derive(
                    "validation_grammar_candidate",
                    schema_attempt,
                    candidate_attempt,
                )
                try:
                    candidate = self.grammar_query_generator.generate(
                        manifest,
                        seed=candidate_seed,
                    )
                except CandidateRejected:
                    continue
                try:
                    signature = extractor.extract(candidate.sql)
                except ValueError:
                    continue
                if set(desired.nodes) <= set(signature.nodes) and set(
                    desired.requirements
                ) <= set(signature.requirements):
                    return GeneratedWitness(candidate.sql, signature)
        raise ValueError(
            f"grammar produced no validation witness for {feature_id} within the search budget"
        )


__all__ = ["ProductionGeneratorAdapter", "normalize_catalog_nodes"]
