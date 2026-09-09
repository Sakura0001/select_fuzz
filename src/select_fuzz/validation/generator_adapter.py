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
    "grouping_aggregate_having": "SELECT `t`.`payload`, COUNT(*) FROM `t0` AS `t` GROUP BY `t`.`payload` HAVING COUNT(*) > 0 ORDER BY 1, 2",
    "set_union": "SELECT `t`.`id` FROM `t0` AS `t` UNION SELECT `u`.`id` FROM `t1` AS `u` ORDER BY 1",
    "validation_top_n": "SELECT `t`.`id` AS `q1` FROM `t0` AS `t` ORDER BY 1 LIMIT 10",
    "validation_set_branch_local_top_n": "(SELECT `s0`.`id` FROM `t0` AS `s0` ORDER BY 1 LIMIT 2) UNION (SELECT `s1`.`id` FROM `t1` AS `s1` ORDER BY 1 LIMIT 2) ORDER BY 1",
    "validation_nested_parenthesized_top_n": "((SELECT `t`.`id` FROM `t0` AS `t` ORDER BY 1 LIMIT 5) ORDER BY 1 LIMIT 3) ORDER BY 1 LIMIT 2",
    "validation_table_offset_limit": "SELECT `t`.`id` FROM `t0` AS `t` ORDER BY 1 LIMIT 1 OFFSET 1",
    "validation_derived_explicit_columns": "SELECT `d`.`dq1` FROM (SELECT `u`.`id` AS `q1` FROM `t1` AS `u`) AS `d` (`dq1`) ORDER BY 1",
    "validation_join_cast": "SELECT `t`.`id`, CAST(`u`.`id` AS SIGNED) FROM `t0` AS `t` INNER JOIN `t1` AS `u` ON (`t`.`id` = `u`.`id`) ORDER BY 1, 2",
}


# The official source catalog remains broad reference metadata. Only these
# query features have automatic witness construction under the PQ contract.
_PQ_QUERY_FEATURES = frozenset(
    {
        "select_query_specification",
        "select_parenthesized",
        "select_nested_parenthesized_top_n",
        "join_inner_cross_straight",
        "derived_regular",
        "derived_explicit_columns",
        "cte_nonrecursive",
        "set_union",
        "set_branch_local_top_n",
        "grouping_aggregate_having",
        "case_simple",
        "case_searched",
        "optimizer_hint_join_order",
        "optimizer_hint_index_level",
        "optimizer_hint_derived_pushdown",
        "partition_explicit_selection",
        "function_deterministic_scalar",
        "function_aggregate",
        "regression_8041_union_view_charset",
        "regression_8041_union_chain_flatten",
        "regression_8041_hint_lexer",
    }
)


def _excluded_capability() -> CatalogCapability:
    return CatalogCapability(
        feature_id="pq_excluded",
        nodes=frozenset({"select"}),
        requirements=frozenset(),
        evidence_ready=True,
    )


def _pq_signature_supported(signature: FeatureSignature) -> bool:
    if {"scalar_literal", "bounded_recursion", "json_column"} & set(signature.requirements):
        return False
    nodes = set(signature.nodes)
    if any(
        node.startswith(
            (
                "window",
                "json",
                "subquery_",
                "join_left",
                "join_right",
                "join_natural",
                "set_intersect",
                "set_except",
            )
        )
        for node in nodes
    ):
        return False
    if nodes & {
        "cte_recursive",
        "rollup",
        "lateral_derived_table",
        "anti_join",
        "limit_zero",
        "explicit_table",
        "table_value_constructor",
        "row_constructor",
        "scene_profile",
        "type_domain",
    }:
        return False
    return "subquery" not in nodes or bool(nodes & {"derived_table", "cte"})


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
        if feature_id not in _PQ_QUERY_FEATURES:
            return _excluded_capability()
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
        if not _pq_signature_supported(signature):
            return _excluded_capability()
        target = set(signature.nodes)
        for node, directed_id, evidence_id in (
            (
                "nested_parenthesized_order_limit",
                "validation_nested_parenthesized_top_n",
                "select_nested_parenthesized_top_n",
            ),
            (
                "branch_local_order_limit",
                "validation_set_branch_local_top_n",
                "set_branch_local_top_n",
            ),
            (
                "derived_explicit_columns",
                "validation_derived_explicit_columns",
                "derived_explicit_columns",
            ),
            ("offset", "validation_table_offset_limit", "select_query_specification"),
            ("limit", "validation_top_n", "select_query_specification"),
        ):
            if node in target:
                return self._directed_capability(directed_id, evidence_id)
        if "join" in target and "type_cast" in target:
            return self._directed_capability("validation_join_cast", "join_inner_cross_straight")
        candidates = [
            self.capability_for_feature(feature_id) for feature_id in sorted(_PQ_QUERY_FEATURES)
        ]
        return max(
            candidates,
            key=lambda capability: (
                target <= set(capability.nodes),
                len(target & set(capability.nodes)),
                -len(set(capability.nodes) - target),
                capability.feature_id,
            ),
        )

    def _directed_capability(self, feature_id: str, evidence_id: str) -> CatalogCapability:
        signature = SignatureExtractor("8.0.41").extract(_VALIDATION_SQL[feature_id])
        evidence = self._specs[evidence_id]
        return CatalogCapability(
            feature_id=feature_id,
            nodes=frozenset(signature.nodes),
            requirements=frozenset(signature.requirements),
            evidence_ready=evidence.evidence_lock_ready,
            evidence_ids=tuple(sorted(evidence.unverified_evidence_sources)),
        )

    def generate_for_validation(self, feature_id: str, seed: int) -> GeneratedWitness:
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise TypeError("seed must be an integer")
        if feature_id not in _PQ_QUERY_FEATURES and feature_id not in _VALIDATION_SQL:
            raise ValueError(f"PQ validation does not construct unsupported feature: {feature_id}")
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
                if set(desired.nodes) <= set(signature.nodes) and set(desired.requirements) <= set(
                    signature.requirements
                ):
                    return GeneratedWitness(candidate.sql, signature)
        raise ValueError(
            f"grammar produced no validation witness for {feature_id} within the search budget"
        )


__all__ = ["ProductionGeneratorAdapter", "normalize_catalog_nodes"]
