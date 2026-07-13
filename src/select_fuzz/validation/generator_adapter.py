"""Production adapter over the real catalog, schema, and directed query generator."""

from __future__ import annotations

import importlib

from select_fuzz.generation.catalog import FeatureSpec
from select_fuzz.generation.query import QueryGenerator
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
    nodes = {
        _NODE_MAP.get(node, node)
        for node in spec.ast_nodes
        if node != "predicate_expression"
    }
    nodes.add("select")
    nodes.add("order_by")
    feature_id = spec.feature_id
    if "json" in feature_id:
        nodes.add("json_function")
    if feature_id == "json_create_extract":
        nodes.update(("json_object", "json_extract"))
    if "json_value" in feature_id:
        nodes.add("json_value")
    if feature_id.startswith("cte_"):
        nodes.add("cte_recursive" if "recursive" in feature_id else "cte")
    if feature_id.startswith("set_union"):
        nodes.add("set_union")
    if feature_id.startswith("set_intersect"):
        nodes.add("set_intersect")
    if feature_id.startswith("set_except"):
        nodes.add("set_except")
    if "rollup" in feature_id:
        nodes.add("rollup")
    if "having" in feature_id:
        nodes.add("having")
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


class ProductionGeneratorAdapter:
    def __init__(
        self,
        *,
        query_generator: QueryGenerator | None = None,
        schema_generator: SchemaGenerator | None = None,
        limits: SchemaLimits | None = None,
    ) -> None:
        self.query_generator = query_generator or QueryGenerator()
        self.schema_generator = schema_generator or SchemaGenerator()
        self.limits = limits or SchemaLimits(
            min_tables=1,
            max_tables=3,
            min_columns=3,
            max_columns=6,
            max_indexes_per_table=4,
        )
        self.catalog = self.query_generator.feature_catalog()
        self._specs = {spec.feature_id: spec for spec in self.catalog}

    @classmethod
    def reload_from_disk(cls) -> ProductionGeneratorAdapter:
        """Invalidate import caches and rebuild generator classes after an external fix."""

        importlib.invalidate_caches()
        import select_fuzz.generation.catalog as catalog_module
        import select_fuzz.generation.schema as schema_module
        import select_fuzz.generation.schema_rules as schema_rules_module
        import select_fuzz.generation.query as query_module

        importlib.reload(catalog_module)
        schema_module = importlib.reload(schema_module)
        importlib.reload(schema_rules_module)
        query_module = importlib.reload(query_module)
        return cls(
            query_generator=query_module.QueryGenerator(),
            schema_generator=schema_module.SchemaGenerator(),
        )

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
        if "limit" in target:
            return CatalogCapability(
                feature_id="validation_top_n",
                nodes=frozenset({"select", "order_by", "limit"}),
                requirements=frozenset({"table", "unique_tiebreaker"}),
                evidence_ready=True,
            )
        if "scalar_literal" in requirements:
            return CatalogCapability(
                feature_id="validation_scalar_literal",
                nodes=frozenset({"select", "order_by"}),
                requirements=frozenset({"scalar_literal", "unique_tiebreaker"}),
                evidence_ready=True,
            )
        candidates = [
            self.capability_for_feature(spec.feature_id) for spec in self._specs.values()
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

    def generate_for_validation(self, feature_id: str, seed: int) -> GeneratedWitness:
        import select_fuzz.generation.query as query_runtime

        actual_feature_id = (
            "select_query_specification"
            if feature_id in {"validation_top_n", "validation_scalar_literal"}
            else feature_id
        )
        target = self._specs[actual_feature_id]
        manifest = self.schema_generator.generate(target, seed=seed, limits=self.limits)
        generated = self.query_generator.generate(
            manifest,
            target=target,
            seed=seed,
            case_ordinal=0,
            lane=query_runtime.QueryLane.VALID,
            require_top_n=feature_id == "validation_top_n",
            directed_variant=(
                "scalar_literal" if feature_id == "validation_scalar_literal" else None
            ),
        )
        return GeneratedWitness(
            sql=generated.sql,
            signature=SignatureExtractor("8.0.41").extract(generated.sql),
        )


__all__ = ["ProductionGeneratorAdapter", "normalize_catalog_nodes"]
