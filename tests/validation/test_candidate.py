from __future__ import annotations

import pytest

from select_fuzz.validation.candidate import CandidateExtractor, CandidateSafetyError
from select_fuzz.validation.signature import SignatureExtractor


def test_web_sql_is_offline_only_and_dangerous_blocks_are_discarded() -> None:
    html = b"""
      <pre>WITH c AS (SELECT id FROM t) SELECT * FROM c ORDER BY 1</pre>
      <code>SELECT 1; DROP TABLE users</code>
      <code>SELECT LOAD_FILE('/tmp/x')</code>
    """
    candidates = CandidateExtractor().from_html(html)

    assert len(candidates) == 1
    assert candidates[0].sql.startswith("WITH c")
    assert candidates[0].executable is False


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SLEEP(1)",
        "SELECT 1 INTO OUTFILE '/tmp/x'",
        "SELECT * FROM t FOR UPDATE",
        "SELECT @x := 1",
        "SELECT 1; SELECT 2",
        "DELETE FROM t",
        "SELECT RAND()",
        "SELECT UUID()",
        "SELECT NOW()",
        "SELECT CURRENT_TIMESTAMP",
        "SELECT CURRENT_ROLE()",
        "SELECT IS_FREE_LOCK('x')",
        "SELECT MASTER_POS_WAIT('x', 1)",
        "SELECT arbitrary_udf()",
        "VALUES ROW(RAND())",
        "VALUES ROW(arbitrary_udf())",
    ],
)
def test_safety_envelope_rejects_side_effect_or_multistatement_sql(sql: str) -> None:
    with pytest.raises(CandidateSafetyError):
        CandidateExtractor().from_text(sql)


def test_signature_extracts_cte_window_set_json_and_requirements() -> None:
    extractor = SignatureExtractor(version="8.0.41")
    window = extractor.extract(
        "WITH c AS (SELECT id, ROW_NUMBER() OVER (PARTITION BY grp ORDER BY id) rn "
        "FROM t) SELECT * FROM c ORDER BY 1"
    )
    set_json = extractor.extract(
        "SELECT JSON_EXTRACT(doc, '$.a') FROM t UNION ALL SELECT doc FROM u ORDER BY 1"
    )

    assert {"select", "cte", "window", "window_partition", "window_order"} <= set(
        window.nodes
    )
    assert {"table", "unique_tiebreaker"} <= set(window.requirements)
    assert {"set_union_all", "json_function", "order_by"} <= set(set_json.nodes)


def test_signature_extraction_reuses_candidate_safety_envelope() -> None:
    with pytest.raises(CandidateSafetyError):
        SignatureExtractor("8.0.41").extract("SELECT GET_LOCK('x', 1)")


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            "SELECT t.a, COUNT(*) FROM t LEFT JOIN u ON t.id=u.id "
            "GROUP BY t.a WITH ROLLUP HAVING COUNT(*) > 1 ORDER BY 1 LIMIT 5",
            {"join", "join_left", "aggregate", "group_by", "rollup", "having", "limit"},
        ),
        (
            "SELECT CAST(a AS DECIMAL(10,2)) FROM t WHERE EXISTS "
            "(SELECT 1 FROM u WHERE u.id=t.id)",
            {"type_cast", "subquery", "subquery_exists"},
        ),
        (
            "SELECT * FROM JSON_TABLE(doc, '$[*]' COLUMNS(x INT PATH '$')) jt",
            {"json_table", "json_function", "function_expression"},
        ),
        ("VALUES ROW(1), ROW(2)", {"table_value_constructor"}),
    ],
)
def test_signature_covers_major_query_shape_families(sql: str, expected: set[str]) -> None:
    signature = SignatureExtractor("8.0.41").extract(sql)
    assert expected <= set(signature.nodes)
