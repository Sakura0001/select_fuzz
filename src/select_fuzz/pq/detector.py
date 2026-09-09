"""Detect whether an EXPLAIN plan indicates Parallel Query execution.

On this engine, a triggered PQ plan shows one of:
  - traditional EXPLAIN: table column ``<gatherN>`` and Extra
    ``Parallel execute (N workers, <db>.<table>)``
  - EXPLAIN FORMAT=TREE: ``Gather: N workers, parallel scan on <alias>`` and
    ``Parallel (table )?index? ?scan on`` / ``Parallel index lookup on``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PQMarkers:
    """Regular-expression markers that signal a PQ plan."""

    patterns: tuple[re.Pattern[str], ...]

    def matches(self, text: str) -> bool:
        return any(pattern.search(text) for pattern in self.patterns)


_DEFAULT_PATTERNS = (
    re.compile(r"Parallel execute\s*\([1-9]\d* workers", re.IGNORECASE),
    re.compile(r"^\s*(?:->\s*)?Gather:\s*[1-9]\d* workers", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*(?:->\s*)?Parallel (?:table|index) scan on", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*(?:->\s*)?Parallel index lookup on", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*(?:->\s*)?Parallel index range scan on", re.IGNORECASE | re.MULTILINE),
)

_QUOTED = re.compile(r"'(?:''|\\.|[^'\\])*'|\"(?:\"\"|\\.|[^\"\\])*\"|`(?:``|[^`])*`")


def _unquoted_plan(text: str) -> str:
    # Values and quoted aliases in predicates are not physical plan operators.
    return _QUOTED.sub(lambda match: " " * len(match.group()), text)

DEFAULT_MARKERS = PQMarkers(patterns=_DEFAULT_PATTERNS)


def detect_pq(explain_text: str, markers: PQMarkers = DEFAULT_MARKERS) -> bool:
    """Return True when the EXPLAIN text shows a PQ plan."""

    if not explain_text or explain_text.lstrip().upper().startswith(("EXPLAIN-ERR", "ERROR")):
        return False
    return markers.matches(_unquoted_plan(explain_text))


@dataclass(frozen=True, slots=True)
class ExplainInfo:
    text: str
    triggered: bool
    error: str = ""
    dop: int = 0
    scan_rows: int | None = None
    rows: tuple[tuple[Any, ...], ...] = ()
    columns: tuple[str, ...] = ()


def parse_explain(rows: tuple[tuple[Any, ...], ...], columns: tuple[str, ...]) -> ExplainInfo:
    """Keep plan columns intact; scan_rows is an estimate, never rows examined."""
    text = "\t".join(columns) + "\n" + "\n".join(
        "\t".join("NULL" if c is None else str(c) for c in row) for row in rows
    )
    names = tuple(c.lower() for c in columns)
    # Traditional EXPLAIN markers belong to table/Extra, not query aliases.
    relevant = [i for i, c in enumerate(names) if c in {"extra", "explain"}]
    marker_text = "\n".join(str(row[i]) for row in rows for i in relevant if i < len(row))
    marker_text = _unquoted_plan(marker_text)
    workers = re.findall(r"(?:Parallel execute\s*\(|Gather:\s*)(\d+) workers", marker_text, re.I)
    estimates: list[int] = []
    if "rows" in names:
        idx = names.index("rows")
        for row in rows:
            try:
                estimates.append(int(row[idx]))
            except (ValueError, TypeError, IndexError):
                continue
    return ExplainInfo(
        text=text, triggered=detect_pq(marker_text), dop=max(map(int, workers), default=0),
        scan_rows=max(estimates, default=None), rows=rows, columns=columns,
    )


# Backwards-friendly alias.
pq_triggered = detect_pq


__all__ = ["DEFAULT_MARKERS", "PQMarkers", "detect_pq", "pq_triggered", "ExplainInfo", "parse_explain"]
