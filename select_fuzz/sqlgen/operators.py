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
        "FROM",
        "WHERE",
        "GROUP BY",
        "HAVING",
        "WINDOW",
        "ORDER BY ASC",
        "ORDER BY DESC",
        "LIMIT",
        "FOR UPDATE",
        "FOR SHARE",
        "UNION",
        "INTERSECT",
        "EXCEPT",
        "SUBQUERY",
        "DERIVED_TABLE",
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
        "+",
        "-",
        "*",
        "/",
        "DIV",
        "MOD",
        "=",
        "<=>",
        "<>",
        "!=",
        ">",
        ">=",
        "<",
        "<=",
        "BETWEEN",
        "IN",
        "EXISTS",
        "IS NULL",
        "AND",
        "OR",
        "XOR",
        "NOT",
        "&",
        "|",
        "^",
        "~",
        "<<",
        ">>",
        "LIKE",
        "REGEXP",
        "JSON_ARROW",
        "JSON_ARROW_UNQUOTE",
        "MEMBER OF",
        "CAST",
        "CONVERT",
        "CASE WHEN",
        "IF",
        "IFNULL",
        "NULLIF",
    ]:
        registry.register(name, "表达式")
    for name in [
        "COUNT",
        "SUM",
        "AVG",
        "MIN",
        "MAX",
        "GROUP_CONCAT",
        "ROW_NUMBER",
        "RANK",
        "JSON_EXTRACT",
        "JSON_ARRAYAGG",
        "MATCH_AGAINST",
        "DISTANCE_COSINE",
        "DISTANCE_EUCLIDEAN",
        "DISTANCE_DOT",
        "STRING_TO_VECTOR",
        "VECTOR_TO_STRING",
    ]:
        registry.register(name, "函数")
    return registry
