from __future__ import annotations

import random
import re

import pytest

from select_fuzz.generation.function_registry import DETERMINISTIC_FUNCTION_SIGNATURES
from select_fuzz.generation.query_grammar import (
    CandidateRejected,
    GrammarColumn,
    GrammarQueryConfig,
    GrammarQueryGenerator,
    GrammarSchema,
    GrammarTable,
    SelectGrammar,
    _ColumnBinding,
    _GenerationContext,
    _QueryScope,
)
from select_fuzz.generation.query_safety import UnsafeQuery


def _schema() -> GrammarSchema:
    return GrammarSchema(
        (
            GrammarTable(
                "t1",
                (
                    GrammarColumn("id", "BIGINT"),
                    GrammarColumn("s", "VARCHAR(20)"),
                    GrammarColumn("j", "JSON"),
                    GrammarColumn("d", "DATETIME"),
                ),
            ),
            GrammarTable(
                "t2",
                (
                    GrammarColumn("id", "INT"),
                    GrammarColumn("x", "DECIMAL(10,2)"),
                    GrammarColumn("b", "BLOB"),
                    GrammarColumn("g", "POINT"),
                ),
            ),
        )
    )


def test_duplicate_alternatives_remain_explicit_grammar_weights() -> None:
    grammar = SelectGrammar.from_text(
        """
query:
    SELECT 1
    | SELECT 1
    | VALUES ROW ( 1 )
"""
    )

    alternatives = grammar.productions["query"].alternatives

    assert len(alternatives) == 3
    assert alternatives[0].symbols == alternatives[1].symbols
    assert alternatives[0].source_line != alternatives[1].source_line


def test_type_compatibility_ratio_can_force_either_soft_lane() -> None:
    grammar = SelectGrammar.from_text(
        """
query:
    _scope_begin _prepare_relation SELECT _numeric_column AS _projection_alias FROM _emit_relation _scope_end
relation:
    _table
"""
    )
    schema = GrammarSchema(
        (
            GrammarTable(
                "types",
                (
                    GrammarColumn("n", "BIGINT"),
                    GrammarColumn("s", "VARCHAR(20)"),
                ),
            ),
        )
    )

    compatible = GrammarQueryGenerator(
        grammar,
        config=GrammarQueryConfig(compatible_type_percent=100),
    ).generate(schema, seed=7)
    cross_type = GrammarQueryGenerator(
        grammar,
        config=GrammarQueryConfig(compatible_type_percent=0),
    ).generate(schema, seed=7)

    assert ".`n`" in compatible.sql
    assert ".`s`" in cross_type.sql


def test_single_table_optimizer_hint_always_names_a_real_alias() -> None:
    grammar = SelectGrammar.from_text(
        """
query:
    _scope_begin _prepare_relation SELECT _optimizer_hint _any_column AS _projection_alias FROM _emit_relation _scope_end
relation:
    _table
"""
    )

    candidate = GrammarQueryGenerator(grammar).generate(_schema(), seed=7)

    assert "/*+ NO_ICP(`r1`) */" in candidate.sql
    assert "NO_ICP()" not in candidate.sql


def test_generated_identifiers_are_bound_to_real_schema_or_derived_outputs() -> None:
    generator = GrammarQueryGenerator()
    schema = _schema()
    base_columns = {
        "t1": {"id", "s", "j", "d"},
        "t2": {"id", "x", "b", "g"},
    }
    definition = re.compile(r"`(t1|t2)`(?: AS)? `(r\d+)`")
    any_alias_definition = re.compile(r"(?:\bAS|`(?:t1|t2)`|\))\s+`(r\d+)`")
    qualified_reference = re.compile(r"`(r\d+)`\.`([^`]+)`")

    for seed in range(250):
        candidate = generator.generate(schema, seed=seed)
        defined_aliases = set(any_alias_definition.findall(candidate.sql))
        base_bindings = {
            alias: base_columns[table] for table, alias in definition.findall(candidate.sql)
        }
        for alias, column in qualified_reference.findall(candidate.sql):
            assert alias in defined_aliases, candidate.sql
            if alias in base_bindings:
                assert column in base_bindings[alias], candidate.sql
            else:
                assert re.fullmatch(r"(?:q|d|c)\d+|jt_(?:ord|value|exists)", column), candidate.sql


def test_mysql_8041_grammar_reaches_ported_and_enhanced_families() -> None:
    grammar = SelectGrammar.default()
    # Semantic hooks deliberately expand these productions from Python rather
    # than through a grammar symbol. They form explicit, audited reachability
    # roots in addition to the grammar's public root.
    semantic_targets = {
        "cte_outer_select",
        "derived_query_expression",
        "derived_select",
        "lateral_derived_select",
        "membership_subquery",
        "natural_join_type",
        "relation",
        "scalar_subquery",
    }
    pending = [grammar.root, *sorted(semantic_targets)]
    reachable: set[str] = set()
    terminals: set[str] = set()
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable.add(name)
        for alternative in grammar.productions[name].alternatives:
            for symbol in alternative.symbols:
                if symbol.value in grammar.productions:
                    pending.append(symbol.value)
                else:
                    terminals.add(symbol.value)

    assert reachable == set(grammar.productions)

    for syntax in (
        {"RECURSIVE"},
        {"JSON_TABLE", "_json_table_relation"},
        {"LATERAL", "_lateral_derived_relation"},
        {"WINDOW"},
        {"INTERSECT"},
        {"EXCEPT"},
        {"MATCH"},
        {"ST_ISVALID"},
    ):
        assert syntax & terminals

    for production in (
        "query_expression",
        "typed_set_chain",
        "registered_scalar_function",
        "typed_expression",
        "interval_expression",
        "aggregate_expression",
        "ranking_window_function",
        "value_window_function",
        "aggregate_window_function",
        "peer_safe_ranking_window_function",
        "numeric_range_frame_clause",
        "temporal_range_frame_clause",
    ):
        assert production in grammar.productions

    for token in (
        "SQL_NO_CACHE",
        "ESCAPE",
        "REGEXP",
        "RLIKE",
        "SOUNDS",
        "TABLE",
        "VALUES",
        "JSON_ARRAYAGG",
        "LAG",
        "LEAD",
        "GROUPING",
        "YEAR_MONTH",
        "DAY_MICROSECOND",
    ):
        assert token in terminals

    for semantic_hook in (
        "_define_independent_cte",
        "_define_dependent_cte",
        "_prepare_cte_reuse_relation",
        "_prepare_row_signature",
        "_prepare_membership_signature",
        "_result_window_value",
        "_table_partition_index_hint",
        "_right_lateral_join_relation",
        "_window_total_order",
        "_json_object_aggregate",
        "_deterministic_group_concat",
    ):
        assert semantic_hook in terminals


def test_alternative_coverage_identity_is_stable_across_line_number_changes() -> None:
    original = SelectGrammar.from_text(
        """
query:
    SELECT 1
    | SELECT 2
"""
    )
    shifted = SelectGrammar.from_text(
        """
# unrelated leading comment

query:
    SELECT 1
    | SELECT 2
    | SELECT 3
"""
    )

    original_trace = f"query@{original.productions['query'].alternatives[0].source_line}"
    shifted_trace = f"query@{shifted.productions['query'].alternatives[0].source_line}"

    assert original.stable_alternative_id(original_trace) == shifted.stable_alternative_id(
        shifted_trace
    )
    assert original.stable_alternative_id(original_trace).startswith("v1:query:")


def test_mysql_8041_grammar_exposes_every_registered_function_and_null_lane() -> None:
    grammar = SelectGrammar.default()
    alternatives = grammar.productions["registered_scalar_function"].alternatives
    actual = {
        alternative.symbols[0].value
        for alternative in alternatives
        if len(alternative.symbols) == 1
    }
    expected: set[str] = set()
    for signature in DETERMINISTIC_FUNCTION_SIGNATURES:
        expected.add(f"_fn_{signature.signature_id}")
        expected.update(
            f"_fn_{signature.signature_id}_null_{position}"
            for position in signature.null_argument_positions
        )

    assert len(alternatives) == len(expected) == 335
    assert actual == expected


def test_candidates_exclude_runtime_randomness_and_side_effects() -> None:
    generator = GrammarQueryGenerator()
    forbidden = re.compile(
        r"\b(?:RAND|UUID|UUID_SHORT|NOW|CURDATE|CURTIME|SYSDATE|SLEEP|BENCHMARK)\s*\(",
        re.IGNORECASE,
    )

    for seed in range(200):
        candidate = generator.generate(_schema(), seed=seed)
        assert forbidden.search(candidate.sql) is None
        assert ";" not in candidate.sql
        assert candidate.grammar_hash == generator.grammar.sha256
        assert candidate.production_trace


def test_safety_rejection_keeps_rendered_candidate_for_opt_in_diagnostics() -> None:
    class RejectingValidator:
        def validate_text(self, sql: str) -> None:
            raise UnsafeQuery(f"diagnostic rejection: {sql}")

    generator = GrammarQueryGenerator(
        SelectGrammar.from_text("query:\n    SELECT 1"),
        validator=RejectingValidator(),  # type: ignore[arg-type]
    )

    with pytest.raises(CandidateRejected) as captured:
        generator.generate(_schema(), seed=11)

    assert captured.value.candidate is not None
    assert captured.value.candidate.sql == "SELECT 1"
    assert captured.value.candidate.production_trace


def test_nested_scope_avoids_mysql_unsupported_repeated_grandparent_reference() -> None:
    """MySQL 8.0.41 rejects one ancestor binding used at two nesting levels."""

    generator = GrammarQueryGenerator(SelectGrammar.from_text("query:\n    SELECT 1"))
    grandparent = _ColumnBinding("r1", GrammarColumn("s", "VARCHAR(20)"))
    immediate = _ColumnBinding("r2", GrammarColumn("s", "VARCHAR(20)"))
    parent = _QueryScope(
        outer_columns=[grandparent],
        local_columns=[immediate],
    )
    context = _GenerationContext(
        schema=_schema(),
        rng=random.Random(11),
        config=GrammarQueryConfig(correlated_column_percent=100),
        scopes=[parent],
    )

    # If the parent already selected its outer binding, a nested child can
    # still correlate to the immediate parent, but not reuse the grandparent.
    parent.selected_outer_bindings.add(grandparent.identity)
    generator._semantic("_scope_begin", context, depth=0)
    assert grandparent not in context.scope.outer_columns
    assert immediate in context.scope.outer_columns
    generator._semantic("_scope_end", context, depth=0)

    # The reverse ordering is also protected: once a child used the
    # grandparent, the parent cannot select that same binding afterwards.
    parent.selected_outer_bindings.clear()
    generator._semantic("_scope_begin", context, depth=0)
    context.scope.selected_outer_bindings.add(grandparent.identity)
    generator._semantic("_scope_end", context, depth=0)
    assert grandparent.identity in parent.blocked_outer_bindings
    assert grandparent not in generator._visible_column_pool(context)
    assert immediate in generator._visible_column_pool(context)


def test_query_result_ordinal_comes_from_real_projection_width() -> None:
    grammar = SelectGrammar.from_text(
        """
query:
    _scope_begin SELECT _int AS _projection_alias , _text AS _projection_alias _scope_end ORDER BY _query_output_ordinal
"""
    )

    for seed in range(20):
        candidate = GrammarQueryGenerator(grammar).generate(_schema(), seed=seed)
        assert candidate.sql.endswith(("ORDER BY 1", "ORDER BY 2"))


def test_outer_order_uses_safe_ordinal_when_projection_contains_star() -> None:
    grammar = SelectGrammar.from_text(
        """
query:
    _scope_begin _prepare_relation SELECT _bare_star , _int AS _projection_alias FROM _emit_relation _scope_end ORDER BY _query_output_item
relation:
    _table NATURAL JOIN _table
"""
    )

    for seed in range(20):
        candidate = GrammarQueryGenerator(grammar).generate(_schema(), seed=seed)
        assert candidate.sql.endswith("ORDER BY 1")


def test_typed_set_signature_keeps_every_operand_at_the_same_arity() -> None:
    grammar = SelectGrammar.from_text(
        """
query:
    _prepare_numeric_2_set_signature _set_select_operand UNION ALL _set_values_operand INTERSECT _set_scalar_operand _clear_set_signature
"""
    )

    candidate = GrammarQueryGenerator(grammar).generate(_schema(), seed=31)

    assert candidate.sql.count(" AS `q") == 4
    assert "VALUES ROW(7, 7)" in candidate.sql
    assert candidate.sql.count(",") >= 3


def test_row_subquery_signature_uses_real_columns_and_matching_arity() -> None:
    grammar = SelectGrammar.from_text(
        """
query:
    _scope_begin _prepare_relation SELECT _any_column AS _projection_alias FROM _emit_relation WHERE _prepare_row_signature _row_lhs = ( row_rhs ) _clear_row_signature _scope_end
relation:
    _table
row_rhs:
    _scope_begin _prepare_relation SELECT _row_rhs_projection FROM _emit_relation LIMIT 1 _scope_end
"""
    )

    candidate = GrammarQueryGenerator(grammar).generate(_schema(), seed=41)

    assert "ROW(" in candidate.sql
    assert candidate.sql.count(" AS `q") == 3
    assert "LIMIT 1" in candidate.sql


def test_every_registered_function_signature_and_null_lane_is_renderable() -> None:
    for signature in DETERMINISTIC_FUNCTION_SIGNATURES:
        symbols = [f"_fn_{signature.signature_id}"]
        symbols.extend(
            f"_fn_{signature.signature_id}_null_{position}"
            for position in sorted(signature.null_argument_positions)
        )
        for ordinal, symbol in enumerate(symbols):
            grammar = SelectGrammar.from_text(
                f"query:\n    _scope_begin SELECT {symbol} AS _projection_alias _scope_end"
            )
            candidate = GrammarQueryGenerator(grammar).generate(
                _schema(),
                seed=ordinal,
            )
            assert candidate.sql.startswith(f"SELECT {signature.sql_name}(")
            assert "RAND(" not in candidate.sql
            assert "NOW(" not in candidate.sql


@pytest.mark.parametrize(
    ("definitions", "relation_hook", "expected"),
    (
        (
            "_define_base_cte , _define_independent_cte",
            "_prepare_cte_join_relation",
            "`cte1` AS",
        ),
        (
            "_define_base_cte , _define_dependent_cte",
            "_prepare_latest_cte_relation",
            "FROM `cte1` AS",
        ),
        (
            "_define_base_cte",
            "_prepare_cte_reuse_relation",
            "`cte1` AS",
        ),
    ),
)
def test_cte_registry_supports_multiple_dependent_and_reused_bindings(
    definitions: str,
    relation_hook: str,
    expected: str,
) -> None:
    grammar = SelectGrammar.from_text(
        f"""
query:
    _cte_frame_begin WITH {definitions} _scope_begin_isolated {relation_hook} SELECT _any_column AS _projection_alias FROM _emit_relation _scope_end _cte_frame_end
derived_select:
    _scope_begin_isolated _prepare_relation SELECT _any_column AS _projection_alias FROM _emit_relation _scope_end
relation:
    _table
"""
    )

    candidate = GrammarQueryGenerator(grammar).generate(_schema(), seed=53)

    assert candidate.sql.startswith("WITH `cte1` AS")
    assert expected in candidate.sql
    if relation_hook in {"_prepare_cte_join_relation", "_prepare_cte_reuse_relation"}:
        assert " JOIN " in candidate.sql
