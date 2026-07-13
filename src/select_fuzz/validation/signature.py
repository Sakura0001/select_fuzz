"""Conservative SQL-shape signature extraction for offline candidates."""

from __future__ import annotations

import re

from select_fuzz.generation.query_safety import _masked_sql
from select_fuzz.validation.candidate import CandidateExtractor
from select_fuzz.validation.models import FeatureSignature


_STRUCTURAL_TOKEN = re.compile(r"[A-Z_][A-Z0-9_]*|[()]")
_SET_OPERATORS = frozenset({"UNION", "INTERSECT", "EXCEPT"})


def _matching_close(text: str) -> int | None:
    if not text.startswith("("):
        return None
    depth = 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _has_direct_order_limit(query_expression: str) -> bool:
    tokens: list[tuple[str, int]] = []
    depth = 0
    for match in _STRUCTURAL_TOKEN.finditer(query_expression):
        token = match.group(0)
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        elif depth == 0:
            tokens.append((token, match.start()))
    normalized = query_expression.lstrip()
    starts_with_query = bool(
        re.match(r"(?:SELECT|TABLE|VALUES|WITH)\b", normalized)
        or normalized.startswith("(")
    )
    if not starts_with_query:
        return False
    order_positions = {
        position
        for (token, position), (next_token, _) in zip(tokens, tokens[1:])
        if token == "ORDER" and next_token == "BY"
    }
    limit_positions = {position for token, position in tokens if token == "LIMIT"}
    return any(order < limit for order in order_positions for limit in limit_positions)


def _parenthesized_order_limit_depth(sql: str) -> int:
    normalized = sql.strip()
    depth = 0
    while normalized.startswith("("):
        close = _matching_close(normalized)
        if close is None:
            break
        body = normalized[1:close]
        if _has_direct_order_limit(body):
            depth += 1
        normalized = body.strip()
    return depth


def _parenthesized_contents(sql: str) -> tuple[str, ...]:
    stack: list[int] = []
    contents: list[str] = []
    for index, char in enumerate(sql):
        if char == "(":
            stack.append(index)
        elif char == ")" and stack:
            start = stack.pop()
            contents.append(sql[start + 1 : index])
    return tuple(contents)


def _max_parenthesized_order_limit_depth(sql: str) -> int:
    return max(
        (_parenthesized_order_limit_depth(scope) for scope in (sql, *_parenthesized_contents(sql))),
        default=0,
    )


def _parenthesized_operand_has_local_top_n(
    operand: str, *, require_whole_operand: bool
) -> bool:
    normalized = operand.strip()
    close = _matching_close(normalized)
    if close is None or (require_whole_operand and close != len(normalized) - 1):
        return False
    return _has_direct_order_limit(normalized[1:close])


def _top_level_set_operators(sql: str) -> tuple[tuple[int, int], ...]:
    operators: list[tuple[int, int]] = []
    depth = 0
    tokens = tuple(_STRUCTURAL_TOKEN.finditer(sql))
    for index, match in enumerate(tokens):
        token = match.group(0)
        if token == "(":
            depth += 1
            continue
        if token == ")":
            depth -= 1
            continue
        if depth != 0 or token not in _SET_OPERATORS:
            continue
        end = match.end()
        if index + 1 < len(tokens):
            modifier = tokens[index + 1]
            if modifier.group(0) in {"ALL", "DISTINCT"}:
                end = modifier.end()
        operators.append((match.start(), end))
    return tuple(operators)


def _scope_has_branch_local_order_limit(sql: str) -> bool:
    operators = _top_level_set_operators(sql)
    for index, (operator_start, operator_end) in enumerate(operators):
        left_start = 0 if index == 0 else operators[index - 1][1]
        if _parenthesized_operand_has_local_top_n(
            sql[left_start:operator_start], require_whole_operand=True
        ):
            return True
        right_end = operators[index + 1][0] if index + 1 < len(operators) else len(sql)
        if _parenthesized_operand_has_local_top_n(
            sql[operator_end:right_end], require_whole_operand=False
        ):
            return True
    return False


def _has_branch_local_order_limit(sql: str) -> bool:
    return any(
        _scope_has_branch_local_order_limit(scope)
        for scope in (sql, *_parenthesized_contents(sql))
    )


def _starts_with_parenthesized_query_expression(sql: str) -> bool:
    normalized = sql.strip()
    while normalized.startswith("("):
        close = _matching_close(normalized)
        if close is None:
            return False
        normalized = normalized[1:close].strip()
        if re.match(r"(?:SELECT|TABLE|VALUES|WITH)\b", normalized):
            return True
    return False


def _has_nonboundary_query_parenthesis(sql: str) -> bool:
    """Find query parentheses used as subqueries rather than query-expression wrappers."""

    tokens = tuple(_STRUCTURAL_TOKEN.finditer(sql))
    wrapper_context = [True]
    history: list[list[str]] = [[]]
    for index, match in enumerate(tokens):
        token = match.group(0)
        if token == "(":
            previous = history[-1]
            follows_set_operator = bool(
                previous
                and (
                    previous[-1] in _SET_OPERATORS
                    or (
                        previous[-1] in {"ALL", "DISTINCT"}
                        and len(previous) >= 2
                        and previous[-2] in _SET_OPERATORS
                    )
                )
            )
            at_wrapper_start = not previous and wrapper_context[-1]
            is_query_boundary = at_wrapper_start or follows_set_operator
            next_token = tokens[index + 1].group(0) if index + 1 < len(tokens) else None
            if next_token in {"SELECT", "TABLE", "VALUES", "WITH"} and not is_query_boundary:
                return True
            wrapper_context.append(is_query_boundary)
            history.append([])
            continue
        if token == ")":
            if len(history) > 1:
                history.pop()
                wrapper_context.pop()
            history[-1].append(token)
            continue
        history[-1].append(token)
    return False


class SignatureExtractor:
    def __init__(self, version: str = "8.0.41") -> None:
        self.version = version
        self._safety = CandidateExtractor()

    def extract(self, sql: str) -> FeatureSignature:
        candidate = self._safety.from_text(sql)
        upper = re.sub(
            r"\s+",
            " ",
            _masked_sql(candidate.sql, preserve_optimizer_hints=True).upper(),
        )
        nodes: set[str] = {"select"}
        requirements: set[str] = set()

        patterns = (
            (r"\bWITH\s+RECURSIVE\b", "cte_recursive"),
            (r"^\s*WITH\b", "cte"),
            (r"\bOVER\s*\(", "window"),
            (r"\bPARTITION\s+BY\b", "window_partition"),
            (r"\bOVER\s*\([^)]*\bORDER\s+BY\b", "window_order"),
            (r"\bUNION\s+ALL\b", "set_union_all"),
            (r"\bUNION(?:\s+DISTINCT)?\b(?!\s+ALL\b)", "set_union_distinct"),
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
            (
                r"(?:\bLIMIT\s+0+\b(?!\s*,)|\bLIMIT\s+\d+\s*,\s*0+\b)",
                "limit_zero",
            ),
            (r"(?:\bOFFSET\s+\d+\b|\bLIMIT\s+\d+\s*,\s*\d+)", "offset"),
            (r"\bJSON_[A-Z0-9_]+\s*\(", "json_function"),
            (r"\bJSON_OBJECT\s*\(", "json_object"),
            (r"\bJSON_EXTRACT\s*\(", "json_extract"),
            (r"\bJSON_VALUE\s*\(", "json_value"),
            (r"\bJSON_TABLE\s*\(", "json_table"),
            (r"\bCASE\b", "case_expression"),
            (r"\bEXISTS\s*\(", "subquery_exists"),
            (r"\bROLLUP\b", "rollup"),
            (r"\b(?:COUNT|SUM|AVG|MIN|MAX|GROUP_CONCAT)\s*\(", "aggregate"),
            (r"\b(?:CAST|CONVERT)\s*\(", "type_cast"),
            (
                r"\b(?:ABS|CAST|COALESCE|CONCAT|COUNT|JSON_[A-Z0-9_]+|LOWER|MAX|MIN|"
                r"OCTET_LENGTH|ROW_NUMBER|ST_[A-Z0-9_]+|SUM)\s*\(",
                "function_expression",
            ),
            (r"^\s*VALUES\b", "table_value_constructor"),
            (r"\bFROM\s*\(\s*(?:SELECT|VALUES|TABLE)\b", "derived_table"),
            (r"/\*\+", "optimizer_hint"),
            (r"\bPARTITION\s*\(", "partition_selection"),
            (r"\bMATCH\s*\([^)]*\)\s+AGAINST\s*\(", "fulltext_predicate"),
            (r"\bST_[A-Z0-9_]+\s*\(", "spatial_function"),
            (
                r"(?:^|\(|\b(?:UNION|INTERSECT|EXCEPT)"
                r"(?:\s+(?:ALL|DISTINCT))?\s+)\s*TABLE\s+",
                "explicit_table",
            ),
            (r"\bVALUES\s+ROW\s*\(", "table_value_constructor"),
            (r"\bIN\s*\(\s*TABLE\b", "subquery_in_table"),
            (
                r"\bFROM\s*\(\s*(?:SELECT|VALUES|TABLE)\b.*\)\s+(?:AS\s+)?"
                r"(?:0|[A-Z_][A-Z0-9_]*)\s*\(\s*(?:0|[A-Z_][A-Z0-9_]*)",
                "derived_explicit_columns",
            ),
        )
        for pattern, node in patterns:
            if re.search(pattern, upper):
                nodes.add(node)
        if _has_branch_local_order_limit(upper):
            nodes.add("branch_local_order_limit")
        if _starts_with_parenthesized_query_expression(upper):
            nodes.add("parenthesized_query")
        if _max_parenthesized_order_limit_depth(upper) >= 2:
            nodes.update(("nested_parenthesized_order_limit", "parenthesized_query"))
        if _has_nonboundary_query_parenthesis(upper):
            nodes.add("subquery")
        if "cte" in nodes or "cte_recursive" in nodes:
            nodes.discard("subquery")
        if "branch_local_order_limit" in nodes:
            nodes.add("parenthesized_query")

        if re.search(r"\bFROM\s+(?:0|[A-Z_])", upper) or "explicit_table" in nodes:
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
