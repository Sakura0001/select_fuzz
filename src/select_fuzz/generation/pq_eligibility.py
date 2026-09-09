"""Conservative SQL support contract from the supplied TaurusDB PQ document.

This checks construction eligibility, not optimizer choice or actual workers.
The canonical grammar constructs this subset directly; this separate gate also
protects custom grammars and explicit SQL inputs from bypassing that subset.
"""

from __future__ import annotations

import re

from select_fuzz.generation.query_safety import _masked_sql


class PqIneligible(ValueError):
    """A query is outside the documented, conservative PQ subset."""


PQ_SCALAR_FUNCTIONS = frozenset(
    "ABS ACOS ASIN ATAN CEIL CEILING COS COT DEGREES EXP FLOOR LN LOG LOG10 "
    "MOD PI RADIANS ROUND SIN SQRT TAN TRUNCATE STRCMP CAST "
    "DATE DAY DAYNAME DAYOFYEAR HOUR MICROSECOND MINUTE MONTH MONTHNAME "
    "QUARTER SECOND TO_DAYS WEEK WEEKDAY YEAR EXTRACT ADDTIME DATE_ADD "
    "DATE_SUB TIMESTAMPADD TIMESTAMPDIFF COALESCE GREATEST IF ISNULL "
    "LEAST NULLIF ANY_VALUE".split()
)
PQ_AGGREGATES = frozenset({"COUNT", "SUM", "AVG", "MIN", "MAX"})
_TYPES = frozenset(
    "TINYINT SMALLINT MEDIUMINT INT INTEGER BIGINT BOOL BOOLEAN BIT DECIMAL NUMERIC FLOAT "
    "DOUBLE REAL YEAR DATE TIME DATETIME TIMESTAMP CHAR VARCHAR NCHAR NVARCHAR "
    "BINARY VARBINARY ENUM SET".split()
)
# Physical BINARY/VARBINARY columns use the supported STRING/VAR_STRING types;
# the BINARY operator/function and binary CAST remain excluded SQL constructs.
_FORBIDDEN = frozenset(
    "WINDOW OVER ROLLUP RECURSIVE LATERAL TABLE VALUES INTERSECT EXCEPT "
    "HIGH_PRIORITY SQL_BUFFER_RESULT SQL_CALC_FOUND_ROWS SQL_SMALL_RESULT "
    "SQL_BIG_RESULT SQL_NO_CACHE JSON BLOB TINYBLOB MEDIUMBLOB LONGBLOB "
    "TEXT TINYTEXT MEDIUMTEXT LONGTEXT GEOMETRY VECTOR BINARY VARBINARY "
    "EXISTS ANY SOME REGEXP RLIKE SOUNDS DIV NATURAL".split()
)
_SYNTAX_PAREN = frozenset(
    "SELECT FROM JOIN STRAIGHT_JOIN ON WHERE HAVING BY AS IN NOT AND OR XOR "
    "BETWEEN CASE WHEN THEN ELSE DISTINCT DISTINCTROW ALL UNION USING "
    "PARTITION INDEX CHAR VARCHAR DECIMAL TIME DATETIME".split()
)
_TOKENS = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|<=>|<>|!=|<=|>=|\S")


def is_pq_supported_type(mysql_type: str, *, generated: bool = False) -> bool:
    declaration = mysql_type.strip().upper()
    if generated or re.search(r"\b(?:GENERATED|ZEROFILL)\b", declaration):
        return False
    return declaration.split("(", 1)[0].split(" ", 1)[0] in _TYPES


class PqEligibilityValidator:
    def validate_text(self, sql: str) -> None:
        masked = _masked_sql(sql)
        matches = list(_TOKENS.finditer(masked))
        tokens = [match.group().upper() for match in matches]
        first = next((token for token in tokens if token != "("), "")
        if first not in {"SELECT", "WITH"} or "SELECT" not in tokens:
            raise PqIneligible("PQ requires a table-backed SELECT")
        unsupported = _FORBIDDEN.intersection(tokens)
        if unsupported:
            raise PqIneligible(f"PQ does not admit {sorted(unsupported)[0]}")
        if re.search(r"\b(?:LEFT|RIGHT|FULL)\s+(?:OUTER\s+)?JOIN\b", masked, re.I):
            raise PqIneligible("PQ generation requires inner joins")
        if re.search(r"\b(?:FOR\s+(?:UPDATE|SHARE)|LOCK\s+IN\s+SHARE)\b", masked, re.I):
            raise PqIneligible("PQ does not support locking reads")
        if any(token in {"&", "|", "^", "~", "@", ":="} for token in tokens):
            raise PqIneligible("PQ operator is outside the documented whitelist")
        hinted = _masked_sql(sql, preserve_optimizer_hints=True)
        for hint in re.finditer(r"/\*\+", hinted):
            end = sql.find("*/", hint.end())
            if re.search(r"\bNO_PARALLEL\b|\bPARALLEL\s*\(\s*0\s*\)", sql[hint.end() : end], re.I):
                raise PqIneligible("PQ cannot be disabled by an optimizer hint")
        for index, token in enumerate(tokens):
            if index + 1 < len(tokens) and tokens[index + 1] == "(":
                if re.fullmatch(r"[A-Z_][A-Z0-9_]*", token):
                    if token not in PQ_SCALAR_FUNCTIONS | PQ_AGGREGATES | _SYNTAX_PAREN:
                        raise PqIneligible(f"PQ function is outside the whitelist: {token}")
                if token in PQ_AGGREGATES and tokens[index + 2 : index + 3] == ["DISTINCT"]:
                    raise PqIneligible(
                        "PQ distinct aggregates are excluded by the older limitations"
                    )
                if token == "PARTITION":
                    end = tokens.index(")", index + 2)
                    if "," in tokens[index + 2 : end]:
                        raise PqIneligible("PQ requires exactly one partition per table scan")
            if token in {"WHERE", "HAVING"}:
                following = tokens[index + 1 :]
                while following and following[0] == "(":
                    following = following[1:]
                if following[:3] in (["1", "=", "0"], ["0", "=", "1"]) or following[:1] in (
                    ["FALSE"],
                    ["NULL"],
                ):
                    raise PqIneligible("PQ cannot use an intentionally empty predicate")

        # Keep one state for each parenthesized expression. A SELECT must have
        # its own FROM; an outer table must not hide a scalar-only nested query.
        states: list[dict[str, bool]] = [
            {"select": False, "from": False, "order": False, "query_wrapper": False}
        ]
        previous = ""
        for index, token in enumerate(tokens):
            state = states[-1]
            if token == "(":
                query_wrapper = (
                    previous in {"", "AS", "FROM", "JOIN", "STRAIGHT_JOIN", "UNION"}
                    or (previous == "(" and state["query_wrapper"])
                    or (
                        previous in {"ALL", "DISTINCT"}
                        and index >= 2
                        and tokens[index - 2] == "UNION"
                    )
                )
                following = tokens[index + 1 : index + 2]
                if following in (["SELECT"], ["WITH"]) and not query_wrapper:
                    raise PqIneligible("PQ conditional and scalar subqueries are excluded")
                states.append(
                    {"select": False, "from": False, "order": False, "query_wrapper": query_wrapper}
                )
            elif token == ")":
                self._check_select(state)
                if len(states) == 1:
                    raise PqIneligible("PQ query has unbalanced parentheses")
                states.pop()
            elif token == "SELECT":
                state.update(select=True, **{"from": False, "order": False})
            elif token == "FROM":
                state["from"] = True
            elif token == "UNION":
                self._check_select(state)
                state.update(select=False, **{"from": False, "order": False})
            elif token == "ORDER" and tokens[index + 1 : index + 2] == ["BY"]:
                state["order"] = True
            elif token == "LIMIT":
                if not state["order"]:
                    raise PqIneligible("PQ LIMIT requires ORDER BY in the same query expression")
                values = tokens[index + 1 : index + 4]
                count = values[2] if len(values) >= 3 and values[1] == "," else values[0]
                if not count.isdigit() or int(count) <= 0:
                    raise PqIneligible("PQ LIMIT must have a positive count")
            previous = token
        if len(states) != 1:
            raise PqIneligible("PQ query has unbalanced parentheses")
        self._check_select(states[0])

    @staticmethod
    def _check_select(state: dict[str, bool]) -> None:
        if state["select"] and not state["from"]:
            raise PqIneligible("PQ requires FROM in every SELECT branch")


__all__ = [
    "PQ_AGGREGATES",
    "PQ_SCALAR_FUNCTIONS",
    "PqEligibilityValidator",
    "PqIneligible",
    "is_pq_supported_type",
]
