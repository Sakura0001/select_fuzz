# Grammar-driven deterministic read-only query expressions for MySQL 8.0.22.
#
# Duplicate alternatives are intentional weights, matching DDL Check behavior.
# Semantic symbols start with `_`; they bind only schema objects visible in the
# current query scope. Runtime-random SQL functions and locking/side-effecting
# clauses are intentionally absent.

query:
    ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | ordinary_query
    | fulltext_query

ordinary_query:
    query_expression
    | query_expression
    | query_expression
    | query_expression
    | query_expression
    | query_expression
    | query_expression
    | query_expression
    | cte_query
    | cte_query
    | recursive_cte_query
    | boundary_query

# FULLTEXT index membership cannot be inferred from the deliberately minimal
# table/column/type snapshot. Keep the syntax as a low-weight root lane so it
# remains fuzzed without multiplying the same expected failure recursively.
fulltext_query:
    _scope_begin _prepare_base_relation SELECT projection_list FROM _emit_relation WHERE MATCH ( _strict_text_column ) AGAINST ( _text ) order_clause? limit_clause? _scope_end

query_expression:
    query_expression_body
    | query_expression_body
    | query_expression_body
    | query_expression_body
    | query_expression_body outer_order_clause
    | query_expression_body limit_clause
    | query_expression_body outer_order_clause limit_clause

query_expression_body:
    select_query_core
    | select_query_core
    | select_query_core
    | select_query_core
    | select_query_core
    | select_query_core
    | parenthesized_query_primary
    | set_query
    | set_query
    | table_query_core
    | values_query_core
    | boundary_query

parenthesized_query_primary:
    ( query_expression_body )
    | ( query_expression_body outer_order_clause limit_clause )
    | ( ( query_expression_body ) )

select_query_core:
    _scope_begin _prepare_relation SELECT select_modifier_list? projection_list FROM _emit_relation where_clause? _scope_end
    | _scope_begin _prepare_relation SELECT select_modifier_list? aggregate_projection_list FROM _emit_relation where_clause? aggregate_having_clause? _scope_end
    | _scope_begin _prepare_relation _prepare_group_column SELECT select_modifier_list? grouped_projection_list FROM _emit_relation where_clause? GROUP BY _group_column rollup_suffix? grouped_having_clause? _scope_end
    | _scope_begin _prepare_relation _prepare_group_columns SELECT select_modifier_list? grouped_projection_list FROM _emit_relation where_clause? GROUP BY _group_columns rollup_suffix? grouped_having_clause? _scope_end
    | _scope_begin _prepare_relation _prepare_group_columns SELECT select_modifier_list? grouped_projection_list FROM _emit_relation where_clause? GROUP BY _group_column , _group_expression grouped_having_clause? _scope_end
    | _scope_begin _prepare_relation _prepare_group_column SELECT select_modifier_list? position_grouped_projection_list FROM _emit_relation where_clause? GROUP BY 1 grouped_having_clause? _scope_end
    | grouping_query
    | _scope_begin _prepare_relation _scope_enable_named_window SELECT select_modifier_list? projection_list FROM _emit_relation where_clause? named_window_clause _scope_end
    | _scope_begin SELECT scalar_projection _scope_end
    | _scope_begin SELECT _int AS _projection_alias WHERE _int comparison_operator _int _scope_end
    | _scope_begin SELECT _text AS _projection_alias GROUP BY 1 HAVING `q1` IS NOT NULL _scope_end

grouping_query:
    _scope_begin _prepare_relation _prepare_group_column SELECT _group_column AS _projection_alias , GROUPING ( _group_column ) _result_numeric AS _projection_alias , COUNT ( * ) _result_numeric AS _projection_alias FROM _emit_relation GROUP BY _group_column WITH ROLLUP _scope_end

boundary_query:
    _scope_begin SELECT _numeric_boundary AS _projection_alias , _text_boundary AS _projection_alias , _temporal_boundary AS _projection_alias , _binary_literal AS _projection_alias , NULL AS _projection_alias _scope_end

derived_select:
    _scope_begin_isolated _prepare_relation SELECT named_projection_list FROM _emit_relation where_clause? _scope_end

derived_query_expression:
    derived_select
    | derived_select
    | query_expression

lateral_derived_select:
    _scope_begin _prepare_relation SELECT named_projection_list FROM _emit_relation where_clause? _scope_end

scalar_subquery:
    _scope_begin _prepare_relation SELECT expression AS _projection_alias FROM _emit_relation where_clause? LIMIT 1 _scope_end

membership_subquery:
    _scope_begin _prepare_relation SELECT expression AS _projection_alias FROM _emit_relation where_clause? _scope_end

typed_membership_subquery:
    _scope_begin _prepare_relation SELECT _membership_rhs_projection FROM _emit_relation where_clause? _scope_end

empty_membership_subquery:
    _scope_begin _prepare_relation SELECT _membership_rhs_projection FROM _emit_relation WHERE ( 1 = 0 ) _scope_end

nullable_membership_subquery:
    _scope_begin _prepare_relation SELECT _membership_rhs_projection FROM _emit_relation where_clause? _scope_end UNION ALL VALUES ROW ( NULL )

row_scalar_subquery:
    _scope_begin _prepare_relation SELECT _row_rhs_projection FROM _emit_relation where_clause? LIMIT 1 _scope_end

row_membership_subquery:
    _scope_begin _prepare_relation SELECT _row_rhs_projection FROM _emit_relation where_clause? _scope_end

table_membership_subquery:
    TABLE _query_table

cte_outer_select:
    _scope_begin_isolated _prepare_cte_relation SELECT projection_list FROM _emit_relation where_clause? order_clause? limit_clause? _scope_end

cte_query:
    _cte_frame_begin WITH _define_base_cte _scope_begin_isolated _prepare_latest_cte_relation SELECT projection_list FROM _emit_relation where_clause? _scope_end outer_order_clause? limit_clause? _cte_frame_end
    | _cte_frame_begin WITH _define_base_cte , _define_independent_cte _scope_begin_isolated _prepare_cte_join_relation SELECT projection_list FROM _emit_relation where_clause? _scope_end outer_order_clause? limit_clause? _cte_frame_end
    | _cte_frame_begin WITH _define_base_cte , _define_dependent_cte _scope_begin_isolated _prepare_latest_cte_relation SELECT projection_list FROM _emit_relation where_clause? _scope_end outer_order_clause? limit_clause? _cte_frame_end
    | _cte_frame_begin WITH _define_base_cte _scope_begin_isolated _prepare_cte_reuse_relation SELECT projection_list FROM _emit_relation where_clause? _scope_end outer_order_clause? limit_clause? _cte_frame_end
    | _prepare_cte WITH _emit_cte_name AS ( _emit_cte_body ) _emit_cte_outer _clear_cte
    | _prepare_cte WITH _emit_cte_name _emit_cte_column_list AS ( _emit_cte_body ) _emit_cte_outer _clear_cte
    | _prepare_query_expression_cte WITH _emit_cte_name _emit_cte_column_list AS ( _emit_cte_body ) _emit_cte_outer _clear_cte

recursive_cte_query:
    _prepare_recursive_cte WITH RECURSIVE _emit_cte_name ( `n` ) AS ( SELECT 1 recursive_union_operator SELECT `n` + 1 FROM _emit_cte_name WHERE `n` < _recursive_limit ) SELECT `n` AS `q1` FROM _emit_cte_name ORDER BY `q1` _clear_cte
    | _prepare_recursive_pair_cte WITH RECURSIVE _emit_cte_name ( `n` , `total` ) AS ( SELECT 1 , 1 recursive_union_operator SELECT `n` + 1 , `total` + `n` FROM _emit_cte_name WHERE `n` < _recursive_limit ) SELECT `n` AS `q1` , `total` AS `q2` FROM _emit_cte_name ORDER BY `q1` _clear_cte

recursive_union_operator:
    UNION ALL
    | UNION DISTINCT

table_query_core:
    TABLE _query_table

values_query_core:
    _prepare_numeric_1_set_signature VALUES values_rows _clear_set_signature
    | _prepare_numeric_2_set_signature VALUES values_rows _clear_set_signature
    | _prepare_text_1_set_signature VALUES values_rows _clear_set_signature
    | _prepare_temporal_1_set_signature VALUES values_rows _clear_set_signature
    | _prepare_binary_1_set_signature VALUES values_rows _clear_set_signature

values_rows:
    _values_row
    | _values_row , _values_row

set_query:
    _prepare_table_set_signature table_set_chain _clear_set_signature
    | _prepare_numeric_1_set_signature typed_set_chain _clear_set_signature
    | _prepare_numeric_2_set_signature typed_set_chain _clear_set_signature
    | _prepare_text_1_set_signature typed_set_chain _clear_set_signature
    | _prepare_text_2_set_signature typed_set_chain _clear_set_signature
    | _prepare_temporal_1_set_signature typed_set_chain _clear_set_signature
    | _prepare_temporal_2_set_signature typed_set_chain _clear_set_signature
    | _prepare_binary_1_set_signature typed_set_chain _clear_set_signature
    | _prepare_binary_2_set_signature typed_set_chain _clear_set_signature

table_set_chain:
    _set_table_operand set_operator _set_table_operand
    | _set_table_operand set_operator _set_values_operand
    | _set_select_operand set_operator _set_table_operand
    | _set_table_operand set_operator _set_values_operand set_operator _set_select_operand
    | ( _set_table_operand set_operator _set_values_operand ) set_operator _set_select_operand
    | _set_table_operand set_operator _set_values_operand set_operator _set_select_operand set_operator _set_scalar_operand
    | ( _set_table_operand set_operator _set_values_operand ) set_operator ( _set_select_operand set_operator _set_scalar_operand )

typed_set_chain:
    _set_select_operand set_operator _set_values_operand
    | _set_scalar_operand set_operator _set_select_operand
    | _set_values_operand set_operator _set_scalar_operand
    | _set_select_topn_operand set_operator _set_values_operand
    | _set_select_operand set_operator _set_values_operand set_operator _set_scalar_operand
    | ( _set_select_operand set_operator _set_values_operand ) set_operator _set_scalar_operand
    | _set_select_operand set_operator _set_values_operand set_operator _set_scalar_operand set_operator _set_select_operand
    | ( _set_select_operand set_operator _set_values_operand ) set_operator ( _set_scalar_operand set_operator _set_select_operand )

set_operator:
    UNION
    | UNION ALL
    | UNION DISTINCT

select_modifier_list:
    row_qualifier
    | _optimizer_hint

row_qualifier:
    ALL
    | DISTINCT
    | DISTINCTROW

projection_list:
    projection_no_bare_star
    | projection_no_bare_star , projection_no_bare_star
    | projection_no_bare_star , projection_no_bare_star , projection_no_bare_star
    | projection_no_bare_star , projection_no_bare_star , projection_no_bare_star , projection_no_bare_star
    | _bare_star
    | _bare_star , projection_no_bare_star
    | _bare_star , projection_no_bare_star , projection_no_bare_star

named_projection_list:
    named_projection
    | named_projection , named_projection
    | named_projection , named_projection , named_projection

projection_no_bare_star:
    named_projection
    | window_expression AS _projection_alias
    | window_expression _projection_alias
    | _table_alias_star

aggregate_projection_list:
    aggregate_expression AS _projection_alias
    | aggregate_expression AS _projection_alias , aggregate_expression AS _projection_alias
    | aggregate_expression AS _projection_alias , aggregate_expression AS _projection_alias , aggregate_expression AS _projection_alias

grouped_projection_list:
    _group_column AS _projection_alias
    | _group_column AS _projection_alias , aggregate_expression AS _projection_alias
    | aggregate_expression AS _projection_alias , _group_column AS _projection_alias
    | _group_column AS _projection_alias , aggregate_expression AS _projection_alias , aggregate_expression AS _projection_alias

position_grouped_projection_list:
    _group_column AS _projection_alias
    | _group_column AS _projection_alias , aggregate_expression AS _projection_alias
    | _group_column AS _projection_alias , aggregate_expression AS _projection_alias , aggregate_expression AS _projection_alias

named_projection:
    _any_column AS _projection_alias
    | _any_column _projection_alias
    | expression AS _projection_alias
    | expression _projection_alias
    | case_expression AS _projection_alias
    | case_expression _projection_alias
    | cast_expression AS _projection_alias
    | cast_expression _projection_alias

scalar_projection:
    _int AS _projection_alias
    | _text AS _projection_alias
    | expression AS _projection_alias
    | aggregate_expression AS _projection_alias

relation:
    _table
    | _table
    | _table
    | _table_implicit_alias
    | _table_partition
    | _table_index_hint
    | _table_partition_index_hint
    | _table conditional_join_type _table ON predicate
    | _table conditional_join_type _table ON predicate
    | _table conditionless_join_type _table
    | _table , _table
    | _table using_join_type _table USING ( _common_column )
    | _table using_join_type _table USING ( _common_columns )
    | _table LEFT JOIN _table ON predicate
    | _table LEFT OUTER JOIN _table ON predicate
    | _table RIGHT JOIN _table ON predicate
    | _table RIGHT OUTER JOIN _table ON predicate
    | _natural_join_relation
    | _table STRAIGHT_JOIN _table ON predicate
    | ( _table conditional_join_type _table ON predicate ) conditional_join_type _table ON predicate
    | ( ( _table conditional_join_type _table ON predicate ) LEFT JOIN _table ON predicate ) RIGHT JOIN _table ON predicate
    | _derived_relation
    | _derived_relation_implicit_alias
    | _derived_relation_columns
    | _derived_query_expression_relation
    | _table conditional_join_type _derived_relation ON predicate
    | _table lateral_join_type _lateral_derived_relation ON predicate
    | _table LEFT JOIN _lateral_derived_relation ON predicate
    | _table LEFT OUTER JOIN _lateral_derived_relation ON predicate
    | _right_lateral_join_relation
    | _json_table_relation
    | _json_table_literal_relation
    | _json_table_exists_relation
    | _json_table_nested_relation
    | _table conditional_join_type _json_table_relation ON predicate

conditional_join_type:
    JOIN
    | INNER JOIN
    | CROSS JOIN
    | STRAIGHT_JOIN

conditionless_join_type:
    JOIN
    | INNER JOIN
    | CROSS JOIN
    | STRAIGHT_JOIN

lateral_join_type:
    JOIN
    | INNER JOIN
    | CROSS JOIN

using_join_type:
    JOIN
    | INNER JOIN
    | LEFT JOIN
    | LEFT OUTER JOIN
    | RIGHT JOIN
    | RIGHT OUTER JOIN

natural_join_type:
    NATURAL JOIN
    | NATURAL INNER JOIN
    | NATURAL LEFT JOIN
    | NATURAL LEFT OUTER JOIN
    | NATURAL RIGHT JOIN
    | NATURAL RIGHT OUTER JOIN

where_clause:
    WHERE predicate

rollup_suffix:
    WITH ROLLUP

aggregate_having_clause:
    HAVING aggregate_expression comparison_operator constant_atom
    | HAVING aggregate_expression IS NULL
    | HAVING aggregate_expression IS NOT NULL

grouped_having_clause:
    HAVING _group_column comparison_operator constant_atom
    | HAVING aggregate_expression comparison_operator constant_atom
    | HAVING _group_column IS NULL
    | HAVING _group_column IS NOT NULL

outer_order_clause:
    ORDER BY _query_output_item direction?
    | ORDER BY _query_output_item direction? , _query_output_item direction?
    | ORDER BY _query_output_item direction? , _query_output_item direction? , _query_output_item direction?
    | ORDER BY _query_output_item direction? , _query_output_item direction? , _query_output_item direction? , _query_output_item direction?

order_clause:
    ORDER BY order_item
    | ORDER BY order_item , order_item
    | ORDER BY order_item , order_item , order_item
    | ORDER BY order_item , order_item , order_item , order_item
    | ORDER BY order_item , order_item , order_item , order_item , order_item

order_item:
    _order_item direction?
    | _order_item direction?
    | _order_item direction?
    | _order_item direction?
    | ( expression + 0 ) direction?

direction:
    ASC
    | DESC

limit_clause:
    LIMIT _limit
    | LIMIT _limit OFFSET _offset
    | LIMIT _offset , _limit

predicate:
    comparison_predicate
    | comparison_predicate
    | comparison_predicate
    | comparison_predicate
    | comparison_predicate
    | comparison_predicate
    | comparison_predicate
    | comparison_predicate
    | comparison_predicate
    | comparison_predicate
    | expression IS NULL
    | expression IS NOT NULL
    | expression IS TRUE
    | expression IS FALSE
    | ( predicate logical_operator predicate )
    | NOT ( predicate )
    | expression IS NULL
    | expression IS NOT NULL
    | expression IS TRUE
    | expression IS FALSE
    | expression IS UNKNOWN
    | expression IS NOT TRUE
    | expression IS NOT FALSE
    | expression IS NOT UNKNOWN
    | expression BETWEEN expression AND expression
    | expression NOT BETWEEN expression AND expression
    | expression IN ( expression_list )
    | expression NOT IN ( expression_list )
    | expression IN ( _membership_subquery )
    | expression NOT IN ( _membership_subquery )
    | anti_membership_predicate
    | _prepare_membership_signature _membership_lhs IN ( typed_membership_subquery ) _clear_membership_signature
    | _prepare_membership_signature _membership_lhs NOT IN ( typed_membership_subquery ) _clear_membership_signature
    | _prepare_membership_signature _membership_lhs NOT IN ( nullable_membership_subquery ) _clear_membership_signature
    | _prepare_membership_signature _membership_lhs IN ( empty_membership_subquery ) _clear_membership_signature
    | EXISTS ( _membership_subquery )
    | NOT EXISTS ( _membership_subquery )
    | EXISTS ( table_membership_subquery )
    | NOT EXISTS ( empty_membership_subquery )
    | expression comparison_operator quantifier ( _membership_subquery )
    | _prepare_membership_signature _membership_lhs comparison_operator quantifier ( typed_membership_subquery ) _clear_membership_signature
    | _prepare_row_signature _row_lhs comparison_operator ( row_scalar_subquery ) _clear_row_signature
    | _prepare_row_signature _row_lhs row_quantified_operator ( row_membership_subquery ) _clear_row_signature
    | _text_column LIKE _text
    | _text_column NOT LIKE _text
    | _strict_text_column LIKE _like_escape_pattern ESCAPE _escape_char
    | _strict_text_column NOT LIKE _like_escape_pattern ESCAPE _escape_char
    | _strict_text_column REGEXP _regexp_pattern
    | _strict_text_column NOT REGEXP _regexp_pattern
    | _strict_text_column RLIKE _regexp_pattern
    | _strict_text_column NOT RLIKE _regexp_pattern
    | _strict_text_column SOUNDS LIKE _strict_text_column
    | REGEXP_LIKE ( _strict_text_column , _regexp_pattern )
    | ( NOT REGEXP_LIKE ( _strict_text_column , _regexp_pattern ) )
    | JSON_OVERLAPS ( _strict_json_column , _strict_json_column )
    | expression MEMBER OF ( _strict_json_column )
    | ST_ISVALID ( _strict_spatial_column )

# The anti-subquery matrix is deliberately explicit.  These alternatives keep
# cardinality and NULL semantics reproducible while still binding every inner
# relation through the normal scope/table machinery.
anti_membership_predicate:
    1 NOT IN ( anti_empty_subquery )
    | 1 NOT IN ( anti_single_membership_subquery )
    | 1 NOT IN ( anti_multi_subquery )
    | NULL NOT IN ( anti_single_membership_subquery )
    | 1 NOT IN ( anti_nullable_subquery )
    | NULL NOT IN ( anti_nullable_subquery )
    | NOT EXISTS ( anti_empty_subquery )
    | NOT EXISTS ( anti_single_exists_subquery )
    | NOT EXISTS ( anti_multi_subquery )
    | NOT EXISTS ( anti_nested_not_in_subquery )
    | 1 NOT IN ( anti_nested_not_exists_subquery )
    | ( 1 <> ALL ( anti_multi_subquery ) AND NOT EXISTS ( anti_empty_subquery ) )

anti_empty_subquery:
    _scope_begin _prepare_relation SELECT 1 AS _projection_alias FROM _emit_relation WHERE ( 1 = 0 ) _scope_end

anti_single_subquery:
    _scope_begin _prepare_relation SELECT 1 AS _projection_alias FROM _emit_relation LIMIT 1 _scope_end

anti_single_membership_subquery:
    _scope_begin _prepare_relation SELECT 1 AS _projection_alias FROM _emit_relation GROUP BY 1 _scope_end

anti_single_exists_subquery:
    anti_single_subquery

anti_multi_subquery:
    _scope_begin _prepare_relation SELECT 1 AS _projection_alias FROM _emit_relation _scope_end

anti_nullable_subquery:
    _scope_begin _prepare_relation SELECT NULL AS _projection_alias FROM _emit_relation _scope_end

anti_nested_not_in_subquery:
    _scope_begin _prepare_relation SELECT 1 AS _projection_alias FROM _emit_relation WHERE 1 NOT IN ( anti_nullable_subquery ) _scope_end

anti_nested_not_exists_subquery:
    _scope_begin _prepare_relation SELECT 1 AS _projection_alias FROM _emit_relation WHERE NOT EXISTS ( anti_empty_subquery ) _scope_end

comparison_predicate:
    expression comparison_operator expression
    | row_expression comparison_operator row_expression
    | _numeric_column <=> expression

comparison_operator:
    =
    | <>
    | !=
    | <
    | <=
    | >
    | >=

logical_operator:
    AND
    | OR
    | XOR
    | &&
    | ||

quantifier:
    ANY
    | ALL
    | SOME

# MySQL 8.0.22 only accepts the IN-equivalent row quantified forms.  Other
# operator/quantifier cross-products raise ER_OPERAND_COLUMNS even at EXPLAIN.
row_quantified_operator:
    = ANY
    | = SOME
    | <> ALL
    | != ALL

expression_list:
    non_subquery_expression
    | non_subquery_expression , non_subquery_expression
    | non_subquery_expression , non_subquery_expression , non_subquery_expression
    | non_subquery_expression , NULL

non_subquery_expression:
    atom
    | unary_expression
    | binary_expression
    | typed_expression
    | interval_expression
    | case_expression
    | cast_expression
    | scalar_function

expression:
    atom
    | atom
    | atom
    | unary_expression
    | binary_expression
    | typed_expression
    | interval_expression
    | case_expression
    | cast_expression
    | scalar_function
    | ( _scalar_subquery )

atom:
    _any_column
    | _numeric_column
    | _text_column
    | _temporal_column
    | _binary_column
    | _json_column
    | _int
    | _numeric_boundary
    | _text
    | _text_boundary
    | _temporal
    | DATE _temporal
    | CAST ( '12:34:56.123456' AS TIME ) _result_temporal
    | _binary_literal
    | _bit_literal
    | _json_literal
    | NULL
    | TRUE
    | FALSE

constant_atom:
    _int
    | _numeric_boundary
    | _text
    | _text_boundary
    | _temporal
    | DATE _temporal
    | _binary_literal
    | _bit_literal
    | _json_literal
    | NULL
    | TRUE
    | FALSE

unary_expression:
    ( + unary_operand )
    | ( - unary_operand )
    | ( ~ unary_operand )
    | ( ! unary_operand )
    | ( NOT unary_operand )
    | ( BINARY unary_operand )

unary_operand:
    atom
    | cast_expression
    | scalar_function
    | ( expression )

binary_expression:
    ( expression arithmetic_operator expression )
    | ( expression bit_operator expression )
    | ( _strict_numeric_column arithmetic_operator _strict_numeric_column ) _result_numeric
    | ( _strict_numeric_column bit_operator _strict_numeric_column ) _result_numeric
    | ( _strict_binary_column bit_operator _strict_binary_column ) _result_binary
    | ( _strict_json_column -> '$.k' ) _result_json
    | ( _strict_json_column ->> '$.k' ) _result_text

typed_expression:
    ( _strict_numeric_column + _int ) _result_numeric
    | CONCAT ( _strict_text_column , _text ) _result_text
    | CAST ( _strict_binary_column AS BINARY ) _result_binary
    | CAST ( _strict_temporal_column AS DATETIME ) _result_temporal
    | JSON_EXTRACT ( _strict_json_column , '$.k' ) _result_json
    | ST_ASBINARY ( _strict_spatial_column ) _result_binary

interval_expression:
    ( _strict_temporal_column + INTERVAL _positive_uint DAY ) _result_temporal
    | ( _strict_temporal_column - INTERVAL _positive_uint DAY ) _result_temporal
    | DATE_ADD ( _strict_temporal_column , INTERVAL _positive_uint DAY ) _result_temporal
    | DATE_SUB ( _strict_temporal_column , INTERVAL _positive_uint HOUR ) _result_temporal
    | TIMESTAMPADD ( MINUTE , _positive_uint , _strict_temporal_column ) _result_temporal
    | TIMESTAMPDIFF ( DAY , _strict_temporal_column , _strict_temporal_column ) _result_numeric
    | ( _strict_temporal_column + INTERVAL _positive_uint MICROSECOND ) _result_temporal
    | ( _strict_temporal_column + INTERVAL _positive_uint SECOND ) _result_temporal
    | ( _strict_temporal_column + INTERVAL _positive_uint WEEK ) _result_temporal
    | ( _strict_temporal_column + INTERVAL _positive_uint MONTH ) _result_temporal
    | ( _strict_temporal_column + INTERVAL _positive_uint QUARTER ) _result_temporal
    | ( _strict_temporal_column + INTERVAL _positive_uint YEAR ) _result_temporal
    | DATE_ADD ( _strict_temporal_column , INTERVAL '1-2' YEAR_MONTH ) _result_temporal
    | DATE_ADD ( _strict_temporal_column , INTERVAL '1 02' DAY_HOUR ) _result_temporal
    | DATE_ADD ( _strict_temporal_column , INTERVAL '1 02:03' DAY_MINUTE ) _result_temporal
    | DATE_ADD ( _strict_temporal_column , INTERVAL '1 02:03:04' DAY_SECOND ) _result_temporal
    | DATE_ADD ( _strict_temporal_column , INTERVAL '1 02:03:04.000005' DAY_MICROSECOND ) _result_temporal
    | DATE_SUB ( _strict_temporal_column , INTERVAL '02:03' HOUR_MINUTE ) _result_temporal
    | DATE_SUB ( _strict_temporal_column , INTERVAL '02:03:04' HOUR_SECOND ) _result_temporal
    | DATE_SUB ( _strict_temporal_column , INTERVAL '02:03:04.000005' HOUR_MICROSECOND ) _result_temporal
    | DATE_ADD ( _strict_temporal_column , INTERVAL '03:04' MINUTE_SECOND ) _result_temporal
    | DATE_ADD ( _strict_temporal_column , INTERVAL '03:04.000005' MINUTE_MICROSECOND ) _result_temporal
    | DATE_ADD ( _strict_temporal_column , INTERVAL '04.000005' SECOND_MICROSECOND ) _result_temporal
    | TIMESTAMPADD ( SECOND , _positive_uint , _strict_temporal_column ) _result_temporal
    | TIMESTAMPADD ( HOUR , _positive_uint , _strict_temporal_column ) _result_temporal
    | TIMESTAMPADD ( MONTH , _positive_uint , _strict_temporal_column ) _result_temporal
    | TIMESTAMPDIFF ( MICROSECOND , _strict_temporal_column , _strict_temporal_column ) _result_numeric
    | TIMESTAMPDIFF ( MONTH , _strict_temporal_column , _strict_temporal_column ) _result_numeric
    | TIMESTAMPDIFF ( YEAR , _strict_temporal_column , _strict_temporal_column ) _result_numeric

arithmetic_operator:
    +
    | -
    | *
    | /
    | %
    | DIV
    | MOD

bit_operator:
    &
    | |
    | ^
    | <<
    | >>

row_expression:
    ROW ( expression , expression )
    | ( expression , expression )

case_expression:
    CASE expression WHEN expression THEN expression ELSE expression END
    | CASE WHEN predicate THEN expression ELSE expression END
    | CASE WHEN predicate THEN expression WHEN predicate THEN expression ELSE expression END

cast_expression:
    CAST ( _int AS SIGNED ) _result_numeric
    | CAST ( _uint AS UNSIGNED ) _result_numeric
    | CAST ( _int AS DECIMAL ( 20 , 6 ) ) _result_numeric
    | CAST ( _int AS FLOAT ) _result_numeric
    | CAST ( _int AS DOUBLE ) _result_numeric
    | CAST ( _text AS CHAR ( 64 ) CHARACTER SET utf8mb4 ) _result_text
    | CAST ( _binary_literal AS BINARY ( 64 ) ) _result_binary
    | CAST ( '2024-02-29' AS DATE ) _result_temporal
    | CAST ( '12:34:56.123456' AS TIME ( 6 ) ) _result_temporal
    | CAST ( '2024-02-29 12:34:56.123456' AS DATETIME ( 6 ) ) _result_temporal
    | CAST ( 2024 AS YEAR ) _result_temporal
    | CAST ( '{"k":1}' AS JSON ) _result_json
    | CAST ( ST_GEOMFROMTEXT ( 'POINT(0 0)' ) AS POINT ) _result_spatial
    | CONVERT ( _int , SIGNED ) _result_numeric
    | CONVERT ( _text , CHAR ( 64 ) ) _result_text
    | CONVERT ( _text USING utf8mb4 ) _result_text
    | BINARY atom

scalar_function:
    registered_scalar_function
    | registered_scalar_function
    | registered_scalar_function
    | column_scalar_function
    | column_scalar_function
    | json_scalar_function
    | spatial_scalar_function
    | conditional_scalar_function

column_scalar_function:
    ABS ( _numeric_column ) _result_numeric
    | CEIL ( _numeric_column ) _result_numeric
    | FLOOR ( _numeric_column ) _result_numeric
    | SIGN ( _numeric_column ) _result_numeric
    | SQRT ( _numeric_column ) _result_numeric
    | BIT_COUNT ( _numeric_column ) _result_numeric
    | LOWER ( _text_column ) _result_text
    | ASCII ( _text_column ) _result_numeric
    | CHAR_LENGTH ( _text_column ) _result_numeric
    | HEX ( expression ) _result_text
    | REVERSE ( _text_column ) _result_text
    | OCTET_LENGTH ( expression ) _result_numeric
    | YEAR ( _temporal_column ) _result_numeric
    | MONTH ( _temporal_column ) _result_numeric
    | DATEDIFF ( _temporal_column , _temporal_column ) _result_numeric
    | MD5 ( _text_column ) _result_text
    | SHA2 ( _text_column , 256 ) _result_text
    | INET_ATON ( _text_column ) _result_numeric

json_scalar_function:
    JSON_EXTRACT ( _strict_json_column , '$' ) _result_json
    | JSON_OBJECT ( 'k' , expression ) _result_json
    | JSON_ARRAY ( expression , expression ) _result_json
    | JSON_TYPE ( _strict_json_column ) _result_text
    | JSON_VALUE ( _strict_json_column , '$' ) _result_text
    | JSON_UNQUOTE ( JSON_EXTRACT ( _strict_json_column , '$.k' ) ) _result_text
    | JSON_SCHEMA_VALID ( '{}' , _strict_json_column ) _result_numeric

spatial_scalar_function:
    ST_ASBINARY ( _strict_spatial_column ) _result_binary
    | ST_ASTEXT ( _strict_spatial_column ) _result_text
    | ST_ISVALID ( _strict_spatial_column ) _result_numeric
    | ST_GEOMFROMTEXT ( 'POINT(0 0)' ) _result_spatial

conditional_scalar_function:
    COALESCE ( expression , expression )
    | COALESCE ( expression , expression , expression )
    | CONCAT ( expression , expression ) _result_text
    | IF ( predicate , expression , expression )
    | IFNULL ( expression , expression )
    | NULLIF ( expression , expression )
    | GREATEST ( expression , expression )
    | LEAST ( expression , expression )

registered_scalar_function:
    _fn_math_abs_1
    | _fn_math_abs_1_null_0
    | _fn_math_acos_1
    | _fn_math_acos_1_null_0
    | _fn_math_asin_1
    | _fn_math_asin_1_null_0
    | _fn_math_atan_1
    | _fn_math_atan_1_null_0
    | _fn_math_atan_2
    | _fn_math_atan_2_null_0
    | _fn_math_atan_2_null_1
    | _fn_math_atan2_2
    | _fn_math_atan2_2_null_0
    | _fn_math_atan2_2_null_1
    | _fn_math_bit_count_1
    | _fn_math_bit_count_1_null_0
    | _fn_math_ceil_1
    | _fn_math_ceil_1_null_0
    | _fn_math_ceiling_1
    | _fn_math_ceiling_1_null_0
    | _fn_math_conv_3
    | _fn_math_conv_3_null_0
    | _fn_math_conv_3_null_1
    | _fn_math_conv_3_null_2
    | _fn_math_cos_1
    | _fn_math_cos_1_null_0
    | _fn_math_cot_1
    | _fn_math_cot_1_null_0
    | _fn_math_crc32_1
    | _fn_math_crc32_1_null_0
    | _fn_math_degrees_1
    | _fn_math_degrees_1_null_0
    | _fn_math_exp_1
    | _fn_math_exp_1_null_0
    | _fn_math_floor_1
    | _fn_math_floor_1_null_0
    | _fn_math_ln_1
    | _fn_math_ln_1_null_0
    | _fn_math_log_1
    | _fn_math_log_1_null_0
    | _fn_math_log_2
    | _fn_math_log_2_null_0
    | _fn_math_log_2_null_1
    | _fn_math_log10_1
    | _fn_math_log10_1_null_0
    | _fn_math_log2_1
    | _fn_math_log2_1_null_0
    | _fn_math_mod_2
    | _fn_math_mod_2_null_0
    | _fn_math_mod_2_null_1
    | _fn_math_pi_0
    | _fn_math_pow_2
    | _fn_math_pow_2_null_0
    | _fn_math_pow_2_null_1
    | _fn_math_power_2
    | _fn_math_power_2_null_0
    | _fn_math_power_2_null_1
    | _fn_math_radians_1
    | _fn_math_radians_1_null_0
    | _fn_math_round_1
    | _fn_math_round_1_null_0
    | _fn_math_round_2
    | _fn_math_round_2_null_0
    | _fn_math_round_2_null_1
    | _fn_math_sign_1
    | _fn_math_sign_1_null_0
    | _fn_math_sin_1
    | _fn_math_sin_1_null_0
    | _fn_math_sqrt_1
    | _fn_math_sqrt_1_null_0
    | _fn_math_tan_1
    | _fn_math_tan_1_null_0
    | _fn_math_truncate_2
    | _fn_math_truncate_2_null_0
    | _fn_math_truncate_2_null_1
    | _fn_string_ascii_1
    | _fn_string_ascii_1_null_0
    | _fn_string_bin_1
    | _fn_string_bin_1_null_0
    | _fn_string_bit_length_1
    | _fn_string_bit_length_1_null_0
    | _fn_string_char_length_1
    | _fn_string_char_length_1_null_0
    | _fn_string_character_length_1
    | _fn_string_character_length_1_null_0
    | _fn_string_concat_2
    | _fn_string_concat_2_null_0
    | _fn_string_concat_2_null_1
    | _fn_string_concat_3
    | _fn_string_concat_3_null_0
    | _fn_string_concat_3_null_1
    | _fn_string_concat_3_null_2
    | _fn_string_concat_ws_3
    | _fn_string_concat_ws_3_null_0
    | _fn_string_concat_ws_3_null_1
    | _fn_string_concat_ws_3_null_2
    | _fn_string_elt_3
    | _fn_string_elt_3_null_0
    | _fn_string_elt_3_null_1
    | _fn_string_elt_3_null_2
    | _fn_string_export_set_3
    | _fn_string_export_set_3_null_0
    | _fn_string_export_set_3_null_1
    | _fn_string_export_set_3_null_2
    | _fn_string_field_3
    | _fn_string_field_3_null_0
    | _fn_string_field_3_null_1
    | _fn_string_field_3_null_2
    | _fn_string_find_in_set_2
    | _fn_string_find_in_set_2_null_0
    | _fn_string_find_in_set_2_null_1
    | _fn_string_from_base64_1
    | _fn_string_from_base64_1_null_0
    | _fn_string_hex_1
    | _fn_string_hex_1_null_0
    | _fn_string_instr_2
    | _fn_string_instr_2_null_0
    | _fn_string_instr_2_null_1
    | _fn_string_lcase_1
    | _fn_string_lcase_1_null_0
    | _fn_string_left_2
    | _fn_string_left_2_null_0
    | _fn_string_left_2_null_1
    | _fn_string_length_1
    | _fn_string_length_1_null_0
    | _fn_string_locate_2
    | _fn_string_locate_2_null_0
    | _fn_string_locate_2_null_1
    | _fn_string_locate_3
    | _fn_string_locate_3_null_0
    | _fn_string_locate_3_null_1
    | _fn_string_locate_3_null_2
    | _fn_string_lower_1
    | _fn_string_lower_1_null_0
    | _fn_string_lpad_3
    | _fn_string_lpad_3_null_0
    | _fn_string_lpad_3_null_1
    | _fn_string_lpad_3_null_2
    | _fn_string_ltrim_1
    | _fn_string_ltrim_1_null_0
    | _fn_string_make_set_3
    | _fn_string_make_set_3_null_0
    | _fn_string_make_set_3_null_1
    | _fn_string_make_set_3_null_2
    | _fn_string_mid_2
    | _fn_string_mid_2_null_0
    | _fn_string_mid_2_null_1
    | _fn_string_mid_3
    | _fn_string_mid_3_null_0
    | _fn_string_mid_3_null_1
    | _fn_string_mid_3_null_2
    | _fn_string_oct_1
    | _fn_string_oct_1_null_0
    | _fn_string_octet_length_1
    | _fn_string_octet_length_1_null_0
    | _fn_string_ord_1
    | _fn_string_ord_1_null_0
    | _fn_string_quote_1
    | _fn_string_quote_1_null_0
    | _fn_string_repeat_2
    | _fn_string_repeat_2_null_0
    | _fn_string_repeat_2_null_1
    | _fn_string_replace_3
    | _fn_string_replace_3_null_0
    | _fn_string_replace_3_null_1
    | _fn_string_replace_3_null_2
    | _fn_string_reverse_1
    | _fn_string_reverse_1_null_0
    | _fn_string_right_2
    | _fn_string_right_2_null_0
    | _fn_string_right_2_null_1
    | _fn_string_rpad_3
    | _fn_string_rpad_3_null_0
    | _fn_string_rpad_3_null_1
    | _fn_string_rpad_3_null_2
    | _fn_string_rtrim_1
    | _fn_string_rtrim_1_null_0
    | _fn_string_soundex_1
    | _fn_string_soundex_1_null_0
    | _fn_string_space_1
    | _fn_string_space_1_null_0
    | _fn_string_strcmp_2
    | _fn_string_strcmp_2_null_0
    | _fn_string_strcmp_2_null_1
    | _fn_string_substr_2
    | _fn_string_substr_2_null_0
    | _fn_string_substr_2_null_1
    | _fn_string_substr_3
    | _fn_string_substr_3_null_0
    | _fn_string_substr_3_null_1
    | _fn_string_substr_3_null_2
    | _fn_string_substring_2
    | _fn_string_substring_2_null_0
    | _fn_string_substring_2_null_1
    | _fn_string_substring_3
    | _fn_string_substring_3_null_0
    | _fn_string_substring_3_null_1
    | _fn_string_substring_3_null_2
    | _fn_string_substring_index_3
    | _fn_string_substring_index_3_null_0
    | _fn_string_substring_index_3_null_1
    | _fn_string_substring_index_3_null_2
    | _fn_string_to_base64_1
    | _fn_string_to_base64_1_null_0
    | _fn_string_trim_1
    | _fn_string_trim_1_null_0
    | _fn_string_ucase_1
    | _fn_string_ucase_1_null_0
    | _fn_string_unhex_1
    | _fn_string_unhex_1_null_0
    | _fn_string_upper_1
    | _fn_string_upper_1_null_0
    | _fn_temporal_date_1
    | _fn_temporal_date_1_null_0
    | _fn_temporal_datediff_2
    | _fn_temporal_datediff_2_null_0
    | _fn_temporal_datediff_2_null_1
    | _fn_temporal_day_1
    | _fn_temporal_day_1_null_0
    | _fn_temporal_dayofmonth_1
    | _fn_temporal_dayofmonth_1_null_0
    | _fn_temporal_dayofweek_1
    | _fn_temporal_dayofweek_1_null_0
    | _fn_temporal_dayofyear_1
    | _fn_temporal_dayofyear_1_null_0
    | _fn_temporal_from_days_1
    | _fn_temporal_from_days_1_null_0
    | _fn_temporal_hour_1
    | _fn_temporal_hour_1_null_0
    | _fn_temporal_last_day_1
    | _fn_temporal_last_day_1_null_0
    | _fn_temporal_makedate_2
    | _fn_temporal_makedate_2_null_0
    | _fn_temporal_makedate_2_null_1
    | _fn_temporal_maketime_3
    | _fn_temporal_maketime_3_null_0
    | _fn_temporal_maketime_3_null_1
    | _fn_temporal_maketime_3_null_2
    | _fn_temporal_microsecond_1
    | _fn_temporal_microsecond_1_null_0
    | _fn_temporal_minute_1
    | _fn_temporal_minute_1_null_0
    | _fn_temporal_month_1
    | _fn_temporal_month_1_null_0
    | _fn_temporal_period_add_2
    | _fn_temporal_period_add_2_null_0
    | _fn_temporal_period_add_2_null_1
    | _fn_temporal_period_diff_2
    | _fn_temporal_period_diff_2_null_0
    | _fn_temporal_period_diff_2_null_1
    | _fn_temporal_quarter_1
    | _fn_temporal_quarter_1_null_0
    | _fn_temporal_second_1
    | _fn_temporal_second_1_null_0
    | _fn_temporal_sec_to_time_1
    | _fn_temporal_sec_to_time_1_null_0
    | _fn_temporal_time_1
    | _fn_temporal_time_1_null_0
    | _fn_temporal_time_to_sec_1
    | _fn_temporal_time_to_sec_1_null_0
    | _fn_temporal_timediff_2
    | _fn_temporal_timediff_2_null_0
    | _fn_temporal_timediff_2_null_1
    | _fn_temporal_timestamp_1
    | _fn_temporal_timestamp_1_null_0
    | _fn_temporal_timestamp_2
    | _fn_temporal_timestamp_2_null_0
    | _fn_temporal_timestamp_2_null_1
    | _fn_temporal_to_days_1
    | _fn_temporal_to_days_1_null_0
    | _fn_temporal_to_seconds_1
    | _fn_temporal_to_seconds_1_null_0
    | _fn_temporal_week_2
    | _fn_temporal_week_2_null_0
    | _fn_temporal_week_2_null_1
    | _fn_temporal_weekday_1
    | _fn_temporal_weekday_1_null_0
    | _fn_temporal_weekofyear_1
    | _fn_temporal_weekofyear_1_null_0
    | _fn_temporal_year_1
    | _fn_temporal_year_1_null_0
    | _fn_temporal_yearweek_2
    | _fn_temporal_yearweek_2_null_0
    | _fn_temporal_yearweek_2_null_1
    | _fn_control_coalesce_3
    | _fn_control_coalesce_3_null_0
    | _fn_control_coalesce_3_null_1
    | _fn_control_coalesce_3_null_2
    | _fn_control_greatest_3
    | _fn_control_greatest_3_null_0
    | _fn_control_greatest_3_null_1
    | _fn_control_greatest_3_null_2
    | _fn_control_if_3
    | _fn_control_if_3_null_0
    | _fn_control_if_3_null_1
    | _fn_control_if_3_null_2
    | _fn_control_ifnull_2
    | _fn_control_ifnull_2_null_0
    | _fn_control_ifnull_2_null_1
    | _fn_control_isnull_1
    | _fn_control_isnull_1_null_0
    | _fn_control_least_3
    | _fn_control_least_3_null_0
    | _fn_control_least_3_null_1
    | _fn_control_least_3_null_2
    | _fn_control_nullif_2
    | _fn_control_nullif_2_null_0
    | _fn_control_nullif_2_null_1
    | _fn_encoding_md5_1
    | _fn_encoding_md5_1_null_0
    | _fn_encoding_sha1_1
    | _fn_encoding_sha1_1_null_0
    | _fn_encoding_sha2_2
    | _fn_encoding_sha2_2_null_0
    | _fn_encoding_sha2_2_null_1
    | _fn_encoding_statement_digest_1
    | _fn_encoding_statement_digest_1_null_0
    | _fn_encoding_statement_digest_text_1
    | _fn_encoding_statement_digest_text_1_null_0
    | _fn_network_inet_aton_1
    | _fn_network_inet_aton_1_null_0
    | _fn_network_inet_ntoa_1
    | _fn_network_inet_ntoa_1_null_0
    | _fn_network_inet6_aton_1
    | _fn_network_inet6_aton_1_null_0
    | _fn_network_inet6_ntoa_1
    | _fn_network_inet6_ntoa_1_null_0
    | _fn_network_is_ipv4_1
    | _fn_network_is_ipv4_1_null_0
    | _fn_network_is_ipv4_compat_1
    | _fn_network_is_ipv4_compat_1_null_0
    | _fn_network_is_ipv4_mapped_1
    | _fn_network_is_ipv4_mapped_1_null_0
    | _fn_network_is_ipv6_1
    | _fn_network_is_ipv6_1_null_0

aggregate_expression:
    COUNT ( * ) _result_numeric
    | COUNT ( ALL * ) _result_numeric
    | COUNT ( expression ) _result_numeric
    | COUNT ( ALL expression ) _result_numeric
    | COUNT ( DISTINCT _any_column ) _result_numeric
    | COUNT ( NULL ) _result_numeric
    | SUM ( _numeric_column ) _result_numeric
    | SUM ( ALL _numeric_column ) _result_numeric
    | SUM ( DISTINCT _numeric_column ) _result_numeric
    | SUM ( NULL ) _result_numeric
    | AVG ( _numeric_column ) _result_numeric
    | AVG ( ALL _numeric_column ) _result_numeric
    | AVG ( DISTINCT _numeric_column ) _result_numeric
    | AVG ( NULL ) _result_numeric
    | MIN ( _any_column )
    | MIN ( ALL _any_column )
    | MIN ( DISTINCT _any_column )
    | MIN ( NULL )
    | MAX ( _any_column )
    | MAX ( ALL _any_column )
    | MAX ( DISTINCT _any_column )
    | MAX ( NULL )
    | BIT_AND ( _numeric_column ) _result_numeric
    | BIT_OR ( _numeric_column ) _result_numeric
    | BIT_XOR ( _numeric_column ) _result_numeric
    | STDDEV_POP ( _numeric_column ) _result_numeric
    | STDDEV_SAMP ( _numeric_column ) _result_numeric
    | VAR_POP ( _numeric_column ) _result_numeric
    | VAR_SAMP ( _numeric_column ) _result_numeric
    | _deterministic_group_concat
    | JSON_ARRAYAGG ( 1 ) _result_json
    | _json_object_aggregate

window_expression:
    ranking_window_function OVER ( window_spec ) _result_numeric
    | value_window_function OVER ( window_spec ) _result_window_value
    | aggregate_window_function OVER ( window_spec )
    | peer_safe_ranking_window_function OVER ( ) _result_numeric
    | aggregate_window_function OVER ( )
    | peer_safe_ranking_window_function OVER ( PARTITION BY window_partition_list ) _result_numeric
    | aggregate_window_function OVER ( PARTITION BY window_partition_list )
    | peer_safe_ranking_window_function OVER _window_name _result_numeric
    | ranking_window_function OVER _window_name2 _result_numeric
    | value_window_function OVER _window_name2 _result_window_value
    | aggregate_window_function OVER _window_name
    | aggregate_window_function OVER _window_name2

ranking_window_function:
    ROW_NUMBER ( )
    | RANK ( )
    | DENSE_RANK ( )
    | CUME_DIST ( )
    | PERCENT_RANK ( )
    | NTILE ( _positive_uint )

peer_safe_ranking_window_function:
    RANK ( )
    | DENSE_RANK ( )
    | CUME_DIST ( )
    | PERCENT_RANK ( )

value_window_function:
    LAG ( _window_value_column )
    | LAG ( _window_value_column , _positive_uint )
    | LAG ( _window_value_column , _positive_uint , NULL )
    | LEAD ( _window_value_column )
    | LEAD ( _window_value_column , _positive_uint )
    | LEAD ( _window_value_column , _positive_uint , NULL )
    | FIRST_VALUE ( _window_value_column )
    | LAST_VALUE ( _window_value_column )
    | NTH_VALUE ( _window_value_column , _positive_uint )
    | NTH_VALUE ( _window_value_column , _positive_uint ) FROM FIRST

aggregate_window_function:
    COUNT ( * ) _result_numeric
    | COUNT ( _any_column ) _result_numeric
    | SUM ( _numeric_column ) _result_numeric
    | AVG ( _numeric_column ) _result_numeric
    | MIN ( _any_column )
    | MAX ( _any_column )
    | BIT_AND ( _numeric_column ) _result_numeric
    | BIT_OR ( _numeric_column ) _result_numeric
    | BIT_XOR ( _numeric_column ) _result_numeric

window_spec:
    ORDER BY _window_total_order
    | PARTITION BY window_partition_list ORDER BY _window_total_order
    | ORDER BY _window_total_order frame_clause
    | PARTITION BY window_partition_list ORDER BY _window_total_order frame_clause
    | ORDER BY _window_numeric_order numeric_range_frame_clause
    | ORDER BY _window_temporal_order temporal_range_frame_clause

window_partition_list:
    _window_partition_list

frame_clause:
    ROWS UNBOUNDED PRECEDING
    | ROWS _uint PRECEDING
    | ROWS CURRENT ROW
    | ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    | ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
    | ROWS BETWEEN _uint PRECEDING AND CURRENT ROW
    | ROWS BETWEEN _uint PRECEDING AND _uint FOLLOWING
    | ROWS BETWEEN CURRENT ROW AND _uint FOLLOWING
    | ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
    | ROWS BETWEEN 2 PRECEDING AND 1 PRECEDING
    | ROWS BETWEEN _uint PRECEDING AND UNBOUNDED FOLLOWING
    | ROWS BETWEEN CURRENT ROW AND CURRENT ROW
    | ROWS BETWEEN 1 FOLLOWING AND 2 FOLLOWING
    | ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING
    | RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    | RANGE BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING
    | RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING

numeric_range_frame_clause:
    RANGE BETWEEN 2 PRECEDING AND 1 PRECEDING
    | RANGE BETWEEN 1 PRECEDING AND CURRENT ROW
    | RANGE BETWEEN CURRENT ROW AND 1 FOLLOWING
    | RANGE BETWEEN 1 FOLLOWING AND 2 FOLLOWING

temporal_range_frame_clause:
    RANGE BETWEEN INTERVAL 2 DAY PRECEDING AND INTERVAL 1 DAY PRECEDING
    | RANGE BETWEEN INTERVAL 1 DAY PRECEDING AND CURRENT ROW
    | RANGE BETWEEN CURRENT ROW AND INTERVAL 1 DAY FOLLOWING
    | RANGE BETWEEN INTERVAL 1 DAY FOLLOWING AND INTERVAL 2 DAY FOLLOWING

named_window_clause:
    WINDOW _window_name AS ( PARTITION BY window_partition_list ) , _window_name2 AS ( _window_name ORDER BY _window_total_order )
    | WINDOW _window_name AS ( ORDER BY _window_total_order ) , _window_name2 AS ( _window_name frame_clause )
    | WINDOW _window_name AS ( ORDER BY _window_total_order ) , _window_name2 AS ( _window_name )
