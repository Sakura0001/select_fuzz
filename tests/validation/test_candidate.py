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

    assert {"select", "cte", "window", "window_partition", "window_order"} <= set(window.nodes)
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
            "SELECT CAST(a AS DECIMAL(10,2)) FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.id=t.id)",
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


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("TABLE t ORDER BY 1", {"explicit_table", "order_by"}),
        (
            "TABLE t UNION VALUES ROW(1) ORDER BY 1",
            {"explicit_table", "table_value_constructor", "set_union"},
        ),
        (
            "SELECT 1 WHERE EXISTS (TABLE t) ORDER BY 1",
            {"explicit_table", "subquery", "subquery_exists"},
        ),
    ],
)
def test_signature_extracts_explicit_table_query_blocks(sql: str, expected: set[str]) -> None:
    signature = SignatureExtractor("8.0.41").extract(sql)
    assert expected <= set(signature.nodes)
    assert "table" in signature.requirements


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1 ORDER BY 1 LIMIT 0", {"limit", "limit_zero"}),
        (
            "SELECT id FROM t ORDER BY 1 LIMIT 2 OFFSET 1",
            {"limit", "offset"},
        ),
    ],
)
def test_signature_extracts_limit_boundary_shapes(sql: str, expected: set[str]) -> None:
    signature = SignatureExtractor("8.0.41").extract(sql)
    assert expected <= set(signature.nodes)


def test_signature_distinguishes_limit_zero_from_comma_offset_and_text() -> None:
    comma_offset = SignatureExtractor("8.0.41").extract("SELECT id FROM t ORDER BY 1 LIMIT 0, 10")
    text_literal = SignatureExtractor("8.0.41").extract("SELECT 'LIMIT 0' AS q1 ORDER BY 1")

    assert {"limit", "offset"} <= set(comma_offset.nodes)
    assert "limit_zero" not in comma_offset.nodes
    assert "limit" not in text_literal.nodes
    assert "limit_zero" not in text_literal.nodes


def test_signature_marks_zero_row_count_in_both_offset_syntaxes() -> None:
    comma = SignatureExtractor("8.0.41").extract("SELECT id FROM t ORDER BY 1 LIMIT 10, 0")
    keyword = SignatureExtractor("8.0.41").extract("SELECT id FROM t ORDER BY 1 LIMIT 0 OFFSET 10")

    assert {"limit", "limit_zero", "offset"} <= set(comma.nodes)
    assert {"limit", "limit_zero", "offset"} <= set(keyword.nodes)


def test_signature_preserves_real_optimizer_hint_but_not_quoted_hint_text() -> None:
    real = SignatureExtractor("8.0.41").extract(
        "SELECT /*+ INDEX(t ix_id_desc) */ id FROM t ORDER BY 1"
    )
    text = SignatureExtractor("8.0.41").extract(
        "SELECT '/*+ INDEX(t ix_id_desc) */' AS q1 ORDER BY 1"
    )

    assert "optimizer_hint" in real.nodes
    assert "optimizer_hint" not in text.nodes


def test_signature_finds_explicit_table_on_the_right_of_union_all() -> None:
    signature = SignatureExtractor("8.0.41").extract("SELECT 1 UNION ALL TABLE one_col ORDER BY 1")

    assert {"set_union", "set_union_all", "explicit_table"} <= set(signature.nodes)
    assert "table" in signature.requirements


def test_signature_distinguishes_union_distinct_from_union_all() -> None:
    distinct = SignatureExtractor("8.0.41").extract(
        "TABLE one_col UNION DISTINCT VALUES ROW(1) ORDER BY 1"
    )
    all_rows = SignatureExtractor("8.0.41").extract(
        "TABLE one_col UNION ALL VALUES ROW(1) ORDER BY 1"
    )

    assert "set_union_distinct" in distinct.nodes
    assert "set_union_all" not in distinct.nodes
    assert "set_union_all" in all_rows.nodes
    assert "set_union_distinct" not in all_rows.nodes


@pytest.mark.parametrize(
    "operator",
    [
        "UNION DISTINCT",
        "INTERSECT ALL",
        "EXCEPT DISTINCT",
    ],
)
def test_signature_finds_explicit_table_after_set_operator_modifiers(
    operator: str,
) -> None:
    signature = SignatureExtractor("8.0.41").extract(
        f"SELECT 1 {operator} TABLE one_col ORDER BY 1"
    )

    assert "explicit_table" in signature.nodes


def test_signature_extracts_derived_table_explicit_column_list() -> None:
    signature = SignatureExtractor("8.0.41").extract(
        "SELECT d.x1 FROM (SELECT id FROM t) AS d (x1) ORDER BY 1"
    )

    assert {"derived_table", "derived_explicit_columns"} <= set(signature.nodes)
    assert {"table", "unique_tiebreaker"} <= set(signature.requirements)


@pytest.mark.parametrize("body", ["VALUES ROW(1)", "TABLE one_col"])
def test_signature_extracts_explicit_columns_for_all_derived_body_kinds(
    body: str,
) -> None:
    signature = SignatureExtractor("8.0.41").extract(
        f"SELECT d.x1 FROM ({body}) AS d (x1) ORDER BY 1"
    )

    assert {"derived_table", "derived_explicit_columns"} <= set(signature.nodes)
