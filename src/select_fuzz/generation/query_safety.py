"""Quote-aware single-statement/read-only validation for generated SELECT text."""

from __future__ import annotations

import re

from select_fuzz.generation.function_registry import DETERMINISTIC_FUNCTION_SIGNATURES


class UnsafeQuery(ValueError):
    """SQL is not an admissible deterministic read-only query."""


_FORBIDDEN_STATEMENT_WORDS = frozenset(
    {
        "ALTER",
        "ANALYZE",
        "CALL",
        "CREATE",
        "DELETE",
        "DO",
        "DROP",
        "GRANT",
        "HANDLER",
        "INSERT",
        "LOAD",
        "LOCK",
        "OPTIMIZE",
        "RENAME",
        "REPAIR",
        "REPLACE",
        "REVOKE",
        "TRUNCATE",
        "UNLOCK",
        "UPDATE",
        "USE",
    }
)

_NONDETERMINISTIC_FUNCTIONS = frozenset(
    {
        "BENCHMARK",
        "CONNECTION_ID",
        "CURRENT_ROLE",
        "CURRENT_USER",
        "CURDATE",
        "CURTIME",
        "FOUND_ROWS",
        "GET_LOCK",
        "IS_FREE_LOCK",
        "IS_USED_LOCK",
        "LAST_INSERT_ID",
        "LOAD_FILE",
        "MASTER_POS_WAIT",
        "NOW",
        "RAND",
        "RANDOM_BYTES",
        "RELEASE_ALL_LOCKS",
        "RELEASE_LOCK",
        "ROW_COUNT",
        "SESSION_USER",
        "SLEEP",
        "SYSDATE",
        "SYSTEM_USER",
        "UNIX_TIMESTAMP",
        "USER",
        "UTC_DATE",
        "UTC_TIME",
        "UTC_TIMESTAMP",
        "UUID",
        "UUID_SHORT",
    }
)

_NONDETERMINISTIC_KEYWORDS = frozenset(
    {
        "CURRENT_DATE",
        "CURRENT_TIME",
        "CURRENT_TIMESTAMP",
        "CURRENT_USER",
        "LOCALTIME",
        "LOCALTIMESTAMP",
        "SESSION_USER",
        "SYSTEM_USER",
        "USER",
    }
)

_REGISTERED_FUNCTION_NAMES = frozenset(
    signature.sql_name for signature in DETERMINISTIC_FUNCTION_SIGNATURES
)

_ALLOWED_CALL_TOKENS = (
    frozenset(
        {
            # Closed deterministic function set rendered by query_ast/query_render.
            "ABS",
            "AGAINST",
            "ALL",
            "ANY",
            "AVG",
            "BIT_AND",
            "BIT_OR",
            "BIT_XOR",
            "CAST",
            "CHAR",
            "COALESCE",
            "CONCAT",
            "CONVERT",
            "COUNT",
            "COLUMNS",
            "CUME_DIST",
            "DATE_ADD",
            "DATE_SUB",
            "DATETIME",
            "DECIMAL",
            "DENSE_RANK",
            "FIRST_VALUE",
            "GROUP_CONCAT",
            "GROUPING",
            "JSON_EXTRACT",
            "JSON_ARRAY",
            "JSON_ARRAYAGG",
            "JSON_OBJECT",
            "JSON_OBJECTAGG",
            "JSON_OVERLAPS",
            "JSON_SCHEMA_VALID",
            "JSON_TABLE",
            "JSON_TYPE",
            "JSON_UNQUOTE",
            "JSON_VALUE",
            "LAG",
            "LAST_VALUE",
            "LEAD",
            "LOWER",
            "MATCH",
            "MAX",
            "MIN",
            "NTH_VALUE",
            "NTILE",
            "OCTET_LENGTH",
            "PERCENT_RANK",
            "ROW",
            "ROW_NUMBER",
            "RANK",
            "REGEXP_LIKE",
            "ST_ASBINARY",
            "ST_ASTEXT",
            "ST_GEOMFROMTEXT",
            "ST_ISVALID",
            "STDDEV_POP",
            "STDDEV_SAMP",
            "SUM",
            "TIMESTAMPADD",
            "TIMESTAMPDIFF",
            "TIME",
            "VAR_POP",
            "VAR_SAMP",
            # Grammar tokens that may immediately precede a parenthesized node.
            "AND",
            "AS",
            "BETWEEN",
            "BINARY",
            "BY",
            "CASE",
            "DISTINCT",
            "DISTINCTROW",
            "DIV",
            "ELSE",
            "EXCEPT",
            "EXISTS",
            "FROM",
            "HAVING",
            "HIGH_PRIORITY",
            "IN",
            "INDEX",
            "INTERSECT",
            "JOIN",
            "LATERAL",
            "LIKE",
            "MOD",
            "NOT",
            "OF",
            "ON",
            "OR",
            "OVER",
            "PARTITION",
            "SELECT",
            "SQL_BIG_RESULT",
            "SQL_BUFFER_RESULT",
            "SQL_CALC_FOUND_ROWS",
            "SQL_SMALL_RESULT",
            "SOME",
            "STRAIGHT_JOIN",
            "THEN",
            "UNION",
            "USING",
            "VALUES",
            "WHEN",
            "WHERE",
            "VARCHAR",
            "XOR",
        }
    )
    | _REGISTERED_FUNCTION_NAMES
)

_QUOTED_CALL = re.compile(r"`(?:``|[^`])+`\s*\(")
_CTE_WITH_COLUMNS = re.compile(
    r"`[a-z][a-z0-9_]*`\s*\(\s*"
    r"(?:`[a-z][a-z0-9_]*`\s*(?:,\s*`[a-z][a-z0-9_]*`\s*)*)?"
    r"\)\s+AS\s*\(",
    re.IGNORECASE,
)
_DERIVED_WITH_COLUMNS = re.compile(
    r"`[a-z][a-z0-9_]*`\s*\(\s*"
    r"`[a-z][a-z0-9_]*`\s*(?:,\s*`[a-z][a-z0-9_]*`\s*)*\)",
    re.IGNORECASE,
)
_DERIVED_ALIAS_PREFIX = re.compile(r"\)\s+(?:AS\s+)?$", re.IGNORECASE)
_UNQUOTED_DERIVED_WITH_COLUMNS = re.compile(
    r"\)\s+(?:AS\s+)?([A-Z_][A-Z0-9_]*)\s*\(\s*"
    r"[A-Z_][A-Z0-9_]*\s*(?:,\s*[A-Z_][A-Z0-9_]*\s*)*\)"
)


def _masked_sql(sql: str, *, preserve_optimizer_hints: bool = False) -> str:
    """Replace quoted/comment payload with spaces while preserving token offsets."""

    masked = list(sql)
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char in {"'", '"', "`"}:
            quote = char
            # Preserve one harmless identifier token for backticks. Removing the
            # whole identifier can accidentally turn ``x`.`y` HAVING (...)`` into
            # the apparent schema-function token ``BY.HAVING(...)``.
            masked[index] = "0" if quote == "`" else " "
            index += 1
            while index < length:
                masked[index] = " "
                if sql[index] == "\\" and quote != "`":
                    index += 1
                    if index < length:
                        masked[index] = " "
                        index += 1
                    continue
                if sql[index] == quote:
                    if index + 1 < length and sql[index + 1] == quote:
                        masked[index + 1] = " "
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            else:
                raise UnsafeQuery("unterminated SQL quote")
            continue
        if sql.startswith("/*", index):
            if sql.startswith("/*!", index):
                raise UnsafeQuery("executable version comments are forbidden")
            is_optimizer_hint = preserve_optimizer_hints and sql.startswith("/*+", index)
            end = sql.find("*/", index + 2)
            if end < 0:
                raise UnsafeQuery("unterminated block comment")
            for offset in range(index, end + 2):
                masked[offset] = " "
            if is_optimizer_hint:
                masked[index : index + 3] = "/*+"
            index = end + 2
            continue
        if char == "#" or (
            sql.startswith("--", index) and index + 2 < length and sql[index + 2].isspace()
        ):
            end = sql.find("\n", index)
            if end < 0:
                end = length
            for offset in range(index, end):
                masked[offset] = " "
            index = end
            continue
        index += 1
    return "".join(masked)


class ReadOnlyValidator:
    """Reject side effects, external access, nondeterminism, and statement stacking."""

    def validate_text(self, sql: str) -> None:
        if not isinstance(sql, str):
            raise TypeError("sql must be text")
        if not sql.strip():
            raise UnsafeQuery("query must not be empty")
        for quoted_call in _QUOTED_CALL.finditer(sql):
            is_cte_columns = _CTE_WITH_COLUMNS.match(sql, quoted_call.start()) is not None
            is_derived_columns = (
                _DERIVED_WITH_COLUMNS.match(sql, quoted_call.start()) is not None
                and _DERIVED_ALIAS_PREFIX.search(sql[: quoted_call.start()]) is not None
            )
            if not is_cte_columns and not is_derived_columns:
                raise UnsafeQuery("quoted stored functions are forbidden")
        masked = _masked_sql(sql)
        semicolons = [index for index, char in enumerate(masked) if char == ";"]
        if len(semicolons) > 1:
            raise UnsafeQuery("multiple SQL statements are forbidden")
        if semicolons and masked[semicolons[0] + 1 :].strip():
            raise UnsafeQuery("multiple SQL statements are forbidden")
        masked = masked[: semicolons[0]] if semicolons else masked
        upper = masked.upper()
        words = re.findall(r"[A-Z_][A-Z0-9_]*", upper)
        if not words or words[0] not in {"SELECT", "WITH", "TABLE", "VALUES"}:
            raise UnsafeQuery("only read-only query expressions are allowed")
        if words[0] == "WITH" and "SELECT" not in words:
            raise UnsafeQuery("WITH query must contain SELECT")
        forbidden = _FORBIDDEN_STATEMENT_WORDS & set(words)
        for function_name in forbidden & _REGISTERED_FUNCTION_NAMES:
            word_count = len(re.findall(rf"\b{re.escape(function_name)}\b", upper))
            call_count = len(re.findall(rf"\b{re.escape(function_name)}\s*\(", upper))
            if word_count == call_count:
                forbidden = forbidden - {function_name}
        if "USE" in forbidden:
            use_count = len(re.findall(r"\bUSE\b", upper))
            use_index_count = len(re.findall(r"\bUSE\s+INDEX\b", upper))
            if use_count == use_index_count:
                forbidden = forbidden - {"USE"}
        if forbidden:
            raise UnsafeQuery(f"forbidden statement token: {sorted(forbidden)[0]}")
        if "@" in masked:
            raise UnsafeQuery("user and system variables are forbidden")
        if re.search(r"\b[A-Z_][A-Z0-9_]*\s*\.\s*[A-Z_][A-Z0-9_]*\s*\(", upper):
            raise UnsafeQuery("schema-qualified stored functions are forbidden")
        call_tokens = set(re.findall(r"\b([A-Z_][A-Z0-9_]*)\s*\(", upper))
        call_tokens.difference_update(
            match.group(1) for match in _UNQUOTED_DERIVED_WITH_COLUMNS.finditer(upper)
        )
        unknown_calls = sorted(call_tokens - _ALLOWED_CALL_TOKENS)
        if unknown_calls:
            raise UnsafeQuery(f"function is outside the closed allowlist: {unknown_calls[0]}")
        if re.search(r"\bINTO\b", upper):
            raise UnsafeQuery("SELECT INTO is forbidden")
        if re.search(r"\bFOR\s+(?:UPDATE|SHARE)\b", upper):
            raise UnsafeQuery("locking reads are forbidden")
        if re.search(r"\bLOCK\s+IN\s+SHARE\s+MODE\b", upper):
            raise UnsafeQuery("locking reads are forbidden")
        for function in _NONDETERMINISTIC_FUNCTIONS:
            if re.search(rf"\b{re.escape(function)}\s*\(", upper):
                raise UnsafeQuery(f"nondeterministic or unsafe function: {function}")
        for keyword in _NONDETERMINISTIC_KEYWORDS:
            if re.search(rf"\b{re.escape(keyword)}\b", upper):
                raise UnsafeQuery(f"nondeterministic temporal value: {keyword}")


__all__ = ["ReadOnlyValidator", "UnsafeQuery"]
