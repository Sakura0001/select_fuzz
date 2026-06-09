from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List


@dataclass(frozen=True)
class Operator:
    name: str
    category: str
    implemented: bool = True


@dataclass
class OperatorRegistry:
    operators: Dict[str, Operator] = field(default_factory=dict)

    def register(self, name: str, category: str, implemented: bool = True) -> None:
        self.operators[name] = Operator(name=name, category=category, implemented=implemented)

    def has(self, name: str) -> bool:
        return name in self.operators

    def names(self) -> List[str]:
        return sorted(self.operators)

    def by_category(self, category: str) -> Iterable[Operator]:
        return (item for item in self.operators.values() if item.category == category)


def build_operator_registry() -> OperatorRegistry:
    registry = OperatorRegistry()
    for name in [
        "WITH",
        "WITH RECURSIVE",
        "SELECT ALL",
        "SELECT DISTINCT",
        "SELECT DISTINCTROW",
        "SELECT CONSTANT",
        "HIGH_PRIORITY",
        "SQL_SMALL_RESULT",
        "SQL_BIG_RESULT",
        "SQL_BUFFER_RESULT",
        "SQL_CALC_FOUND_ROWS",
        "FROM",
        "EXPLICIT PARTITION",
        "WHERE",
        "GROUP BY",
        "HAVING",
        "WINDOW",
        "PARENTHESIZED_QUERY",
        "ORDER BY ASC",
        "ORDER BY DESC",
        "LIMIT",
        "FOR UPDATE",
        "FOR SHARE",
        "UNION",
        "SUBQUERY",
        "SCALAR SUBQUERY",
        "EXISTS SUBQUERY",
        "IN SUBQUERY",
        "DERIVED_TABLE",
        "TABLE",
        "VALUES",
    ]:
        registry.register(name, "查询结构")
    for name in [
        "INNER JOIN",
        "LEFT JOIN",
        "RIGHT JOIN",
        "CROSS JOIN",
        "NATURAL JOIN",
        "STRAIGHT_JOIN",
        "JOIN ... ON",
        "JOIN ... USING",
    ]:
        registry.register(name, "JOIN")
    for name in [
        "=",
        "<=>",
        "<>",
        "!=",
        ">",
        ">=",
        "<",
        "<=",
        "BETWEEN",
        "NOT BETWEEN",
        "IN",
        "NOT IN",
        "EXISTS",
        "NOT EXISTS",
        "IS NULL",
        "IS TRUE",
        "AND",
        "OR",
        "XOR",
        "NOT",
        "LIKE",
        "NOT LIKE",
        "LIKE ESCAPE",
        "REGEXP",
        "NOT REGEXP",
        "RLIKE",
        "MEMBER OF",
    ]:
        registry.register(name, "谓词")
    for name in [
        "+",
        "-",
        "*",
        "/",
        "DIV",
        "MOD",
        "&",
        "|",
        "^",
        "~",
        "<<",
        ">>",
    ]:
        registry.register(name, "算术位运算")
    for name in [
        "JSON_ARROW",
        "JSON_ARROW_UNQUOTE",
        "CAST",
        "CONVERT",
        "CASE WHEN",
        "IF",
        "IFNULL",
        "NULLIF",
    ]:
        registry.register(name, "表达式函数")
    for name in [
        "COUNT",
        "COUNT DISTINCT",
        "SUM",
        "AVG",
        "MIN",
        "MAX",
        "BIT_AND",
        "BIT_OR",
        "BIT_XOR",
        "GROUP_CONCAT",
        "GROUP_CONCAT ORDER",
        "ROW_NUMBER",
        "RANK",
        "DENSE_RANK",
        "LAG",
        "LEAD",
        "NTILE",
        "FIRST_VALUE",
        "LAST_VALUE",
        "WINDOW FRAME",
        "JSON_EXTRACT",
        "JSON_OBJECT",
        "JSON_ARRAY",
        "JSON_ARRAYAGG",
        "JSON_TABLE",
        "JSON_CONTAINS",
        "JSON_KEYS",
        "JSON_LENGTH",
        "HEX",
        "UNHEX",
        "LENGTH",
        "CONCAT",
        "SUBSTRING",
        "LOWER",
        "COLLATE",
        "BINARY",
        "ABS",
        "ROUND",
        "FLOOR",
        "CEILING",
        "CRC32",
        "TIMESTAMPDIFF",
        "DATE_FORMAT",
        "MONTH",
        "DAYOFWEEK",
        "YEAR",
        "DATE_ADD",
        "ST_ASTEXT",
        "ST_X",
        "ST_Y",
        "MATCH_AGAINST",
        "VEC_DISTANCE_COSINE",
        "VEC_DISTANCE_EUCLIDEAN",
        "VEC_FROMTEXT",
        "VEC_TOTEXT",
    ]:
        registry.register(name, "函数")
    for name in [
        "FOR UPDATE",
        "FOR SHARE",
        "LOCK IN SHARE MODE",
        "NOWAIT",
        "SKIP LOCKED",
    ]:
        registry.register(name, "锁定读")
    return registry
