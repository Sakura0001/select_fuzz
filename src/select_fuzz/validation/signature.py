"""Conservative SQL-shape signature extraction for offline candidates."""

from __future__ import annotations

import re

from select_fuzz.validation.candidate import CandidateExtractor
from select_fuzz.validation.models import FeatureSignature


class SignatureExtractor:
    def __init__(self, version: str = "8.0.41") -> None:
        self.version = version
        self._safety = CandidateExtractor()

    def extract(self, sql: str) -> FeatureSignature:
        candidate = self._safety.from_text(sql)
        upper = re.sub(r"\s+", " ", candidate.sql.upper())
        nodes: set[str] = {"select"}
        requirements: set[str] = set()

        patterns = (
            (r"\bWITH\s+RECURSIVE\b", "cte_recursive"),
            (r"^\s*WITH\b", "cte"),
            (r"\bOVER\s*\(", "window"),
            (r"\bPARTITION\s+BY\b", "window_partition"),
            (r"\bOVER\s*\([^)]*\bORDER\s+BY\b", "window_order"),
            (r"\bUNION\s+ALL\b", "set_union_all"),
            (r"\bUNION\b", "set_union"),
            (r"\bINTERSECT\s+ALL\b", "set_intersect_all"),
            (r"\bINTERSECT\b", "set_intersect"),
            (r"\bEXCEPT\s+ALL\b", "set_except_all"),
            (r"\bEXCEPT\b", "set_except"),
            (r"\b(?:LEFT|RIGHT|INNER|CROSS|STRAIGHT)?\s*JOIN\b", "join"),
            (r"\b(?:INNER\s+JOIN|STRAIGHT_JOIN)\b", "join_inner"),
            (r"\bLEFT(?:\s+OUTER)?\s+JOIN\b", "join_left"),
            (r"\bRIGHT(?:\s+OUTER)?\s+JOIN\b", "join_right"),
            (r"\bCROSS\s+JOIN\b", "join_cross"),
            (r"\bGROUP\s+BY\b", "group_by"),
            (r"\bHAVING\b", "having"),
            (r"\bORDER\s+BY\b", "order_by"),
            (r"\bLIMIT\b", "limit"),
            (r"\bJSON_[A-Z0-9_]+\s*\(", "json_function"),
            (r"\bJSON_OBJECT\s*\(", "json_object"),
            (r"\bJSON_EXTRACT\s*\(", "json_extract"),
            (r"\bJSON_VALUE\s*\(", "json_value"),
            (r"\bJSON_TABLE\s*\(", "json_table"),
            (r"\bCASE\b", "case_expression"),
            (r"\bEXISTS\s*\(", "subquery_exists"),
            (r"\(\s*SELECT\b", "subquery"),
            (r"\bROLLUP\b", "rollup"),
            (r"\b(?:COUNT|SUM|AVG|MIN|MAX|GROUP_CONCAT)\s*\(", "aggregate"),
            (r"\b(?:CAST|CONVERT)\s*\(", "type_cast"),
            (
                r"\b(?:ABS|CAST|COALESCE|CONCAT|COUNT|JSON_[A-Z0-9_]+|LOWER|MAX|MIN|"
                r"OCTET_LENGTH|ROW_NUMBER|ST_[A-Z0-9_]+|SUM)\s*\(",
                "function_expression",
            ),
            (r"^\s*VALUES\b", "table_value_constructor"),
            (r"\bFROM\s*\(\s*SELECT\b", "derived_table"),
            (r"/\*\+", "optimizer_hint"),
            (r"\bPARTITION\s*\(", "partition_selection"),
            (r"\bMATCH\s*\([^)]*\)\s+AGAINST\s*\(", "fulltext_predicate"),
            (r"\bST_[A-Z0-9_]+\s*\(", "spatial_function"),
        )
        for pattern, node in patterns:
            if re.search(pattern, upper):
                nodes.add(node)
        if "cte" in nodes or "cte_recursive" in nodes:
            nodes.discard("subquery")

        if re.search(r"\bFROM\s+[`A-Z_]", upper):
            requirements.add("table")
        else:
            requirements.add("scalar_literal")
        if "join" in nodes or any(node.startswith("set_") for node in nodes):
            requirements.add("two_compatible_relations")
        if "window_order" in nodes or "order_by" in nodes:
            requirements.add("unique_tiebreaker")
        if "json_function" in nodes or "json_table" in nodes:
            requirements.add("json_column")
        if "cte_recursive" in nodes:
            requirements.add("bounded_recursion")
        if "type_cast" in nodes:
            requirements.add("compatible_types")
        if "aggregate" in nodes or "group_by" in nodes:
            requirements.add("grouping_legal")
        return FeatureSignature(
            version=self.version,
            nodes=tuple(nodes),
            requirements=tuple(requirements),
        )


__all__ = ["SignatureExtractor"]
