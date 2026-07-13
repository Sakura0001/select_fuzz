"""Independent line and branch coverage release thresholds."""

from __future__ import annotations

from typing import Any


def coverage_percentages(report: dict[str, Any]) -> tuple[float, float]:
    try:
        totals = report["totals"]
        lines = float(totals["percent_statements_covered"])
        branches = float(totals["percent_branches_covered"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("coverage JSON does not contain line and branch totals") from error
    return lines, branches


__all__ = ["coverage_percentages"]
