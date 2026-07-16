from __future__ import annotations

import pytest

from select_fuzz.generation.query_safety import ReadOnlyValidator, UnsafeQuery, _masked_sql


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("", "must not be empty"),
        ("SELECT `schema`.`function`(1)", "quoted stored functions"),
        ("SELECT 'unterminated", "unterminated SQL quote"),
        ('SELECT "unterminated', "unterminated SQL quote"),
        ("SELECT `unterminated", "unterminated SQL quote"),
        ("SELECT 1 /*! SET @x=1 */", "version comments"),
        ("SELECT 1 /* comment", "unterminated block comment"),
        ("SELECT 1; SELECT 2;", "multiple SQL statements"),
        ("SELECT 1; SELECT 2", "multiple SQL statements"),
        ("UPDATE items SET id = 1", "only read-only"),
        ("WITH `c` AS (VALUES ROW(1)) TABLE `c`", "must contain SELECT"),
        ("SELECT 1 FROM items UPDATE", "forbidden statement token"),
        ("SELECT @value", "variables are forbidden"),
        ("SELECT schema.func(1)", "schema-qualified"),
        ("SELECT UNKNOWN_FUNC(1)", "outside the closed allowlist"),
        ("SELECT 1 INTO OUTFILE '/tmp/x'", "SELECT INTO"),
        ("SELECT 1 FOR UPDATE", "forbidden statement token"),
        ("SELECT 1 FOR SHARE", "locking reads"),
        ("SELECT 1 LOCK IN SHARE MODE", "forbidden statement token"),
        ("SELECT RAND()", "outside the closed allowlist"),
        ("SELECT CURRENT_TIMESTAMP", "nondeterministic temporal value"),
    ],
)
def test_read_only_validator_rejects_every_unsafe_text_boundary(sql: str, message: str) -> None:
    with pytest.raises(UnsafeQuery, match=message):
        ReadOnlyValidator().validate_text(sql)


def test_read_only_validator_requires_text_and_accepts_comments_quotes_and_cte_columns() -> None:
    with pytest.raises(TypeError, match="must be text"):
        ReadOnlyValidator().validate_text(b"SELECT 1")

    valid = (
        "WITH `c` (`value`) AS (SELECT 'UPDATE; RAND()' AS `value`) "
        "SELECT `value` FROM `c`; # ignored DELETE\n"
    )
    ReadOnlyValidator().validate_text(valid)
    ReadOnlyValidator().validate_text("SELECT 1 -- ignored UPDATE\n")
    ReadOnlyValidator().validate_text("SELECT 1 /* ignored UPDATE */")
    ReadOnlyValidator().validate_text("SELECT DISTINCTROW (1) AS `q1`")
    assert len(_masked_sql("SELECT 'a\\\'b', \"c\"\"d\", `e``f`")) > 0
