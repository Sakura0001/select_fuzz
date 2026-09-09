# PQ-eligible, deterministic SELECT grammar for TaurusDB / MySQL 8.0.22.
# Support is the conservative intersection of the supplied PQ whitelist and
# appended limitations. Every SELECT branch scans a relation. Runtime EXPLAIN
# and worker evidence are still required because costs/resources affect PQ.

query:
    ordinary_query

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

query_expression:
    query_expression_body
    | query_expression_body
    | query_expression_body
    | query_expression_body
    | query_expression_body outer_order_clause
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

parenthesized_query_primary:
    ( query_expression_body )
    | ( query_expression_body outer_order_clause limit_clause )
    | ( ( query_expression_body ) )

select_query_core:
    _scope_begin _prepare_relation SELECT select_modifier_list? projection_list FROM _emit_relation where_clause? _scope_end
    | _scope_begin _prepare_relation SELECT select_modifier_list? aggregate_projection_list FROM _emit_relation where_clause? aggregate_having_clause? _scope_end
    | _scope_begin _prepare_relation _prepare_group_column SELECT select_modifier_list? grouped_projection_list FROM _emit_relation where_clause? GROUP BY _group_column grouped_having_clause? _scope_end
    | _scope_begin _prepare_relation _prepare_group_columns SELECT select_modifier_list? grouped_projection_list FROM _emit_relation where_clause? GROUP BY _group_columns grouped_having_clause? _scope_end
    | _scope_begin _prepare_relation _prepare_group_columns SELECT select_modifier_list? grouped_projection_list FROM _emit_relation where_clause? GROUP BY _group_column , _group_expression grouped_having_clause? _scope_end
    | _scope_begin _prepare_relation _prepare_group_column SELECT select_modifier_list? position_grouped_projection_list FROM _emit_relation where_clause? GROUP BY 1 grouped_having_clause? _scope_end

derived_select:
    _scope_begin_isolated _prepare_relation SELECT named_projection_list FROM _emit_relation where_clause? _scope_end

derived_query_expression:
    derived_select
    | derived_select
    | query_expression

cte_outer_select:
    _scope_begin_isolated _prepare_cte_relation SELECT projection_list FROM _emit_relation where_clause? order_clause limit_clause? _scope_end

cte_query:
    _cte_frame_begin WITH _define_base_cte _scope_begin_isolated _prepare_latest_cte_relation SELECT projection_list FROM _emit_relation where_clause? _scope_end outer_order_clause limit_clause? _cte_frame_end
    | _cte_frame_begin WITH _define_base_cte , _define_independent_cte _scope_begin_isolated _prepare_cte_join_relation SELECT projection_list FROM _emit_relation where_clause? _scope_end outer_order_clause limit_clause? _cte_frame_end
    | _cte_frame_begin WITH _define_base_cte , _define_dependent_cte _scope_begin_isolated _prepare_latest_cte_relation SELECT projection_list FROM _emit_relation where_clause? _scope_end outer_order_clause limit_clause? _cte_frame_end
    | _cte_frame_begin WITH _define_base_cte _scope_begin_isolated _prepare_cte_reuse_relation SELECT projection_list FROM _emit_relation where_clause? _scope_end outer_order_clause limit_clause? _cte_frame_end
    | _prepare_cte WITH _emit_cte_name AS ( _emit_cte_body ) _emit_cte_outer _clear_cte
    | _prepare_cte WITH _emit_cte_name _emit_cte_column_list AS ( _emit_cte_body ) _emit_cte_outer _clear_cte
    | _prepare_query_expression_cte WITH _emit_cte_name _emit_cte_column_list AS ( _emit_cte_body ) _emit_cte_outer _clear_cte

set_query:
    _prepare_numeric_1_set_signature typed_set_chain _clear_set_signature
    | _prepare_numeric_2_set_signature typed_set_chain _clear_set_signature
    | _prepare_text_1_set_signature typed_set_chain _clear_set_signature
    | _prepare_text_2_set_signature typed_set_chain _clear_set_signature
    | _prepare_temporal_1_set_signature typed_set_chain _clear_set_signature
    | _prepare_temporal_2_set_signature typed_set_chain _clear_set_signature

typed_set_chain:
    _set_select_operand set_operator _set_select_operand
    | _set_select_topn_operand set_operator _set_select_operand
    | _set_select_operand set_operator _set_select_operand set_operator _set_select_operand
    | ( _set_select_operand set_operator _set_select_operand ) set_operator _set_select_operand

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
    named_projection
    | named_projection , named_projection
    | named_projection , named_projection , named_projection
    | named_projection , named_projection , named_projection , named_projection

named_projection_list:
    named_projection
    | named_projection , named_projection
    | named_projection , named_projection , named_projection

aggregate_projection_list:
    scan_aggregate_expression AS _projection_alias
    | scan_aggregate_expression AS _projection_alias , aggregate_expression AS _projection_alias
    | scan_aggregate_expression AS _projection_alias , aggregate_expression AS _projection_alias , aggregate_expression AS _projection_alias

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
    | _table STRAIGHT_JOIN _table ON predicate
    | ( _table conditional_join_type _table ON predicate ) conditional_join_type _table ON predicate
    | _derived_relation
    | _derived_relation_implicit_alias
    | _derived_relation_columns
    | _derived_query_expression_relation
    | _table conditional_join_type _derived_relation ON predicate

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

using_join_type:
    JOIN
    | INNER JOIN

where_clause:
    WHERE predicate

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
    | _any_column IS NULL
    | _any_column IS NOT NULL
    | _any_column IS TRUE
    | _any_column IS FALSE
    | ( predicate logical_operator predicate )
    | NOT ( predicate )
    | _any_column IS NULL
    | _any_column IS NOT NULL
    | _any_column IS TRUE
    | _any_column IS FALSE
    | _any_column IS UNKNOWN
    | _any_column IS NOT TRUE
    | _any_column IS NOT FALSE
    | _any_column IS NOT UNKNOWN
    | _any_column BETWEEN expression AND expression
    | _any_column NOT BETWEEN expression AND expression
    | _any_column IN ( expression_list )
    | _any_column NOT IN ( expression_list )
    | _text_column LIKE _text
    | _text_column NOT LIKE _text
    | _strict_text_column LIKE _like_escape_pattern ESCAPE _escape_char
    | _strict_text_column NOT LIKE _like_escape_pattern ESCAPE _escape_char

comparison_predicate:
    _any_column comparison_operator expression
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

atom:
    _any_column
    | _numeric_column
    | _text_column
    | _temporal_column
    | _int
    | _numeric_boundary
    | _text
    | _text_boundary
    | _temporal
    | DATE _temporal
    | CAST ( '12:34:56.123456' AS TIME ) _result_temporal
    | _bit_literal
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
    | _bit_literal
    | NULL
    | TRUE
    | FALSE

unary_expression:
    ( + unary_operand )
    | ( - unary_operand )
    | ( ! unary_operand )
    | ( NOT unary_operand )

unary_operand:
    atom
    | cast_expression
    | scalar_function
    | ( expression )

binary_expression:
    ( expression arithmetic_operator expression )
    | ( _strict_numeric_column arithmetic_operator _strict_numeric_column ) _result_numeric

typed_expression:
    ( _strict_numeric_column + _int ) _result_numeric
    | CAST ( _strict_temporal_column AS DATETIME ) _result_temporal

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
    | MOD

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
    | CAST ( '2024-02-29' AS DATE ) _result_temporal
    | CAST ( '12:34:56.123456' AS TIME ( 6 ) ) _result_temporal
    | CAST ( '2024-02-29 12:34:56.123456' AS DATETIME ( 6 ) ) _result_temporal
    | CAST ( 2024 AS YEAR ) _result_temporal

scalar_function:
    registered_scalar_function
    | registered_scalar_function
    | registered_scalar_function
    | column_scalar_function
    | column_scalar_function
    | conditional_scalar_function

column_scalar_function:
    ABS ( _numeric_column ) _result_numeric
    | CEIL ( _numeric_column ) _result_numeric
    | FLOOR ( _numeric_column ) _result_numeric
    | SQRT ( _numeric_column ) _result_numeric
    | YEAR ( _temporal_column ) _result_numeric
    | MONTH ( _temporal_column ) _result_numeric

conditional_scalar_function:
    COALESCE ( expression , expression )
    | COALESCE ( expression , expression , expression )
    | IF ( predicate , expression , expression )
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
    | _fn_math_ceil_1
    | _fn_math_ceil_1_null_0
    | _fn_math_ceiling_1
    | _fn_math_ceiling_1_null_0
    | _fn_math_cos_1
    | _fn_math_cos_1_null_0
    | _fn_math_cot_1
    | _fn_math_cot_1_null_0
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
    | _fn_math_mod_2
    | _fn_math_mod_2_null_0
    | _fn_math_mod_2_null_1
    | _fn_math_pi_0
    | _fn_math_radians_1
    | _fn_math_radians_1_null_0
    | _fn_math_round_1
    | _fn_math_round_1_null_0
    | _fn_math_round_2
    | _fn_math_round_2_null_0
    | _fn_math_round_2_null_1
    | _fn_math_sin_1
    | _fn_math_sin_1_null_0
    | _fn_math_sqrt_1
    | _fn_math_sqrt_1_null_0
    | _fn_math_tan_1
    | _fn_math_tan_1_null_0
    | _fn_math_truncate_2
    | _fn_math_truncate_2_null_0
    | _fn_math_truncate_2_null_1
    | _fn_string_strcmp_2
    | _fn_string_strcmp_2_null_0
    | _fn_string_strcmp_2_null_1
    | _fn_temporal_date_1
    | _fn_temporal_date_1_null_0
    | _fn_temporal_day_1
    | _fn_temporal_day_1_null_0
    | _fn_temporal_dayofyear_1
    | _fn_temporal_dayofyear_1_null_0
    | _fn_temporal_hour_1
    | _fn_temporal_hour_1_null_0
    | _fn_temporal_microsecond_1
    | _fn_temporal_microsecond_1_null_0
    | _fn_temporal_minute_1
    | _fn_temporal_minute_1_null_0
    | _fn_temporal_month_1
    | _fn_temporal_month_1_null_0
    | _fn_temporal_quarter_1
    | _fn_temporal_quarter_1_null_0
    | _fn_temporal_second_1
    | _fn_temporal_second_1_null_0
    | _fn_temporal_to_days_1
    | _fn_temporal_to_days_1_null_0
    | _fn_temporal_week_2
    | _fn_temporal_week_2_null_0
    | _fn_temporal_week_2_null_1
    | _fn_temporal_weekday_1
    | _fn_temporal_weekday_1_null_0
    | _fn_temporal_year_1
    | _fn_temporal_year_1_null_0
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
    | _fn_control_isnull_1
    | _fn_control_isnull_1_null_0
    | _fn_control_least_3
    | _fn_control_least_3_null_0
    | _fn_control_least_3_null_1
    | _fn_control_least_3_null_2
    | _fn_control_nullif_2
    | _fn_control_nullif_2_null_0
    | _fn_control_nullif_2_null_1

aggregate_expression:
    COUNT ( * ) _result_numeric
    | COUNT ( _any_column ) _result_numeric
    | SUM ( _strict_numeric_column ) _result_numeric
    | AVG ( _strict_numeric_column ) _result_numeric
    | MIN ( _any_column )
    | MAX ( _any_column )

scan_aggregate_expression:
    COUNT ( * ) _result_numeric
    | COUNT ( _any_column ) _result_numeric
    | SUM ( _strict_numeric_column ) _result_numeric
    | AVG ( _strict_numeric_column ) _result_numeric
