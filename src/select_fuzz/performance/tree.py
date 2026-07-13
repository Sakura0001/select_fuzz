"""Strict parser and semantic shape gate for MySQL TREE plans."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re


NUM = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_ACTUAL = re.compile(
    rf"actual time=(?P<start>{NUM})\.\.(?P<end>{NUM})\s+"
    rf"rows=(?P<rows>{NUM})\s+loops=(?P<loops>\d+)"
)
_ESTIMATED = re.compile(rf"\bcost=.*?\brows=(?P<rows>{NUM})")


class PlanParseError(ValueError):
    pass


class Family(StrEnum):
    SCAN = "scan"
    JOIN = "join"
    AGGREGATE = "aggregate"
    SORT = "sort"
    WINDOW = "window"
    OTHER = "other"


def _family(text: str) -> Family:
    normalized = text.casefold()
    if "window" in normalized:
        return Family.WINDOW
    if "sort" in normalized:
        return Family.SORT
    if "aggregate" in normalized or "group aggregate" in normalized:
        return Family.AGGREGATE
    if "join" in normalized or "nested loop" in normalized:
        return Family.JOIN
    if "scan" in normalized or "lookup" in normalized:
        return Family.SCAN
    return Family.OTHER


@dataclass(frozen=True, slots=True)
class TreeNode:
    indent: int
    text: str
    family: Family
    estimated_rows: float | None
    start_ms: float | None
    end_ms: float | None
    actual_rows: float | None
    loops: int | None


@dataclass(frozen=True, slots=True)
class TreePlan:
    nodes: tuple[TreeNode, ...]
    root: TreeNode
    raw: str

    def estimated_work(self, family: Family) -> float:
        values = [
            node.estimated_rows
            for node in self.nodes
            if node.family is family and node.estimated_rows is not None
        ]
        if not values:
            raise PlanParseError(f"plan has no estimated work for {family.value}")
        return max(values)


def _parse_node(indent: int, text: str) -> TreeNode:
    actual = _ACTUAL.search(text)
    estimated = _ESTIMATED.search(text)
    return TreeNode(
        indent=indent,
        text=text,
        family=_family(text),
        estimated_rows=None if estimated is None else float(estimated.group("rows")),
        start_ms=None if actual is None else float(actual.group("start")),
        end_ms=None if actual is None else float(actual.group("end")),
        actual_rows=None if actual is None else float(actual.group("rows")),
        loops=None if actual is None else int(actual.group("loops")),
    )


def parse_tree(text: str, *, completed: bool = False) -> TreePlan:
    if not isinstance(text, str):
        raise TypeError("TREE plan must be text")
    nodes = tuple(
        _parse_node(line.index("->"), line.split("->", 1)[1].strip())
        for line in text.splitlines()
        if "->" in line
    )
    if not nodes:
        raise PlanParseError("TREE plan contains no iterator")
    minimum = min(node.indent for node in nodes)
    roots = tuple(node for node in nodes if node.indent == minimum)
    if len(roots) != 1:
        raise PlanParseError("TREE plan has an ambiguous root")
    if completed:
        if "never executed" in text.casefold():
            raise PlanParseError("TREE plan contains an unexecuted iterator")
        if any(node.end_ms is None or node.actual_rows is None for node in nodes):
            raise PlanParseError("TREE plan is incomplete")
        if roots[0].loops != 1:
            raise PlanParseError("TREE root iterator loops must equal one")
    return TreePlan(nodes=nodes, root=roots[0], raw=text)


@dataclass(frozen=True, slots=True)
class ShapeBoundary:
    required: frozenset[Family]
    forbidden: frozenset[Family] = frozenset()

    def validate(self, plan: TreePlan, role: str) -> None:
        families = {node.family for node in plan.nodes}
        missing = self.required - families
        forbidden = self.forbidden & families
        if missing or forbidden:
            parts: list[str] = []
            if missing:
                parts.append("missing " + ",".join(sorted(item.value for item in missing)))
            if forbidden:
                parts.append("forbidden " + ",".join(sorted(item.value for item in forbidden)))
            raise PlanParseError(f"{role} plan shape mismatch: {'; '.join(parts)}")


__all__ = [
    "Family",
    "PlanParseError",
    "ShapeBoundary",
    "TreeNode",
    "TreePlan",
    "parse_tree",
]
