from __future__ import annotations

from select_fuzz.generation.function_registry import (
    DETERMINISTIC_FUNCTION_SIGNATURES,
    FunctionFamily,
)


def test_deterministic_function_registry_is_broad_closed_and_unique() -> None:
    signatures = DETERMINISTIC_FUNCTION_SIGNATURES
    signature_ids = {signature.signature_id for signature in signatures}

    assert len(signatures) == 45
    assert len(signature_ids) == len(signatures)
    assert {signature.family for signature in signatures} == {
        FunctionFamily.MATH,
        FunctionFamily.STRING,
        FunctionFamily.TEMPORAL,
        FunctionFamily.CONTROL,
    }
    assert all(signature.arguments for signature in signatures if signature.sql_name != "PI")


def test_registry_excludes_user_omissions_and_nondeterministic_state() -> None:
    names = {signature.sql_name for signature in DETERMINISTIC_FUNCTION_SIGNATURES}

    assert not any(name.startswith("JSON") for name in names)
    assert not any(name.startswith("ST_") for name in names)
    assert names.isdisjoint(
        {
            "CONNECTION_ID",
            "CURRENT_TIMESTAMP",
            "CURDATE",
            "CURTIME",
            "FOUND_ROWS",
            "LAST_INSERT_ID",
            "NOW",
            "RAND",
            "RANDOM_BYTES",
            "ROW_COUNT",
            "SLEEP",
            "SYSDATE",
            "UNIX_TIMESTAMP",
            "UUID",
            "UUID_SHORT",
        }
    )


def test_required_deterministic_function_families_have_representative_signatures() -> None:
    ids = {signature.signature_id for signature in DETERMINISTIC_FUNCTION_SIGNATURES}

    assert {
        "math_atan_1",
        "math_atan_2",
        "math_log_1",
        "math_log_2",
        "string_strcmp_2",
        "temporal_year_1",
        "temporal_date_1",
        "control_coalesce_3",
    } <= ids


def test_function_warning_contract_is_explicit_and_closed() -> None:
    contracts = {
        signature.signature_id: signature.expected_warning_codes_by_null_position
        for signature in DETERMINISTIC_FUNCTION_SIGNATURES
        if signature.expected_warning_codes_by_null_position
    }

    assert contracts == {}
    for signature in DETERMINISTIC_FUNCTION_SIGNATURES:
        assert signature.expected_warning_codes(None) == ()
        for position in signature.null_argument_positions:
            assert signature.expected_warning_codes(position) == ()
