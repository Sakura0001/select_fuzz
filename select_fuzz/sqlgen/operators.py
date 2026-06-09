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
        "SUBQUERY",
        "SCALAR SUBQUERY",
        "EXISTS SUBQUERY",
        "IN SUBQUERY",
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
        "LIKE",
        "REGEXP",
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
        "SUM",
        "AVG",
        "MIN",
        "MAX",
        "GROUP_CONCAT",
        "ROW_NUMBER",
        "RANK",
        "DENSE_RANK",
        "JSON_EXTRACT",
        "JSON_OBJECT",
        "JSON_ARRAY",
        "JSON_ARRAYAGG",
        "HEX",
        "UNHEX",
        "LENGTH",
        "CONCAT",
        "SUBSTRING",
        "LOWER",
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
