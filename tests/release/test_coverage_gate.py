from __future__ import annotations

import pytest

from select_fuzz.coverage_gate import coverage_percentages


def test_coverage_gate_reads_independent_line_and_branch_percentages() -> None:
    assert coverage_percentages(
        {
            "totals": {
                "percent_statements_covered": 91.25,
                "percent_branches_covered": 86.5,
            }
        }
    ) == (91.25, 86.5)


def test_coverage_gate_rejects_missing_totals() -> None:
    with pytest.raises(ValueError, match="line and branch totals"):
        coverage_percentages({"totals": {}})
