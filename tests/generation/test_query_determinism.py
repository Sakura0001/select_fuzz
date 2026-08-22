from __future__ import annotations

import pytest

from select_fuzz.generation.query_determinism import assess_query_determinism


@pytest.mark.parametrize(
    "sql",
    (
        "SELECT `payload` FROM `t0` LIMIT 5",
        "TABLE `t0` ORDER BY 30 LIMIT 1, 5",
        "SELECT 1 UNION ALL SELECT `c7` FROM `t0` LIMIT 1, 100",
        "SELECT * FROM (SELECT * FROM `t0` LIMIT 2) AS `d`",
    ),
)
def test_unproved_nonzero_row_limit_is_rejected(sql: str) -> None:
    assessment = assess_query_determinism(sql)

    assert assessment.admissible is False
    assert assessment.reason == "nondeterministic_row_limit"


@pytest.mark.parametrize(
    "sql",
    (
        "SELECT * FROM `t0`",
        "SELECT * FROM `t0` LIMIT 0",
        "SELECT 'LIMIT 100' AS `text`",
        "SELECT 1 /* LIMIT 100 */",
    ),
)
def test_query_without_effective_row_limit_is_admissible(sql: str) -> None:
    assert assess_query_determinism(sql).admissible is True
