from __future__ import annotations

from select_fuzz.generation.function_registry import (
    DETERMINISTIC_FUNCTION_SIGNATURES,
    DeterministicFunctionSignature,
    FunctionArgument,
    FunctionFamily,
    FunctionResult,
)
from select_fuzz.generation.catalog import CapabilityStatus, FeatureSpec
from select_fuzz.generation.query import QueryGenerator, QueryLane
from select_fuzz.generation.query_ast import Literal, RegisteredFunctionCall, SqlType
from select_fuzz.generation.query_safety import ReadOnlyValidator
from select_fuzz.generation.schema import (
    ColumnDef,
    IndexDef,
    IndexPart,
    SchemaManifest,
    SchemaProfile,
    TableDef,
)


def _manifest() -> SchemaManifest:
    return SchemaManifest(
        SchemaProfile.REGULAR_INNODB,
        "function_deterministic_scalar",
        1,
        (
            TableDef(
                "items",
                False,
                (ColumnDef("id", "BIGINT", False),),
                (
                    IndexDef(
                        "PRIMARY",
                        (IndexPart(column_name="id"),),
                        unique=True,
                        primary=True,
                    ),
                ),
            ),
        ),
    )


def _target() -> FeatureSpec:
    return FeatureSpec(
        "function_deterministic_scalar",
        "functions_operators",
        (8, 0, 0),
        frozenset({SchemaProfile.REGULAR_INNODB.value}),
        frozenset({"function_expression"}),
        frozenset({"deterministic_expression", "read_only_select"}),
        capability_status=CapabilityStatus.GENERATOR_SUPPORTED,
        evidence_lock_ready=True,
    )


def test_deterministic_function_registry_is_broad_closed_and_unique() -> None:
    signatures = DETERMINISTIC_FUNCTION_SIGNATURES
    signature_ids = {signature.signature_id for signature in signatures}

    assert len(signatures) >= 100
    assert len(signature_ids) == len(signatures)
    assert {signature.family for signature in signatures} == set(FunctionFamily)
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
    ids = {
        signature.signature_id for signature in DETERMINISTIC_FUNCTION_SIGNATURES
    }

    assert {
        "math_atan_1",
        "math_atan_2",
        "math_log_1",
        "math_log_2",
        "string_locate_2",
        "string_locate_3",
        "string_substring_2",
        "string_substring_3",
        "temporal_timestamp_1",
        "temporal_timestamp_2",
        "control_coalesce_3",
        "encoding_sha2_2",
        "network_inet6_ntoa_1",
    } <= ids


def test_function_warning_contract_is_explicit_and_closed() -> None:
    contracts = {
        signature.signature_id: signature.expected_warning_codes_by_null_position
        for signature in DETERMINISTIC_FUNCTION_SIGNATURES
        if signature.expected_warning_codes_by_null_position
    }

    assert contracts == {"encoding_sha2_2": ((1, (1583,)),)}
    sha2 = next(
        signature
        for signature in DETERMINISTIC_FUNCTION_SIGNATURES
        if signature.signature_id == "encoding_sha2_2"
    )
    assert sha2.expected_warning_codes(None) == ()
    assert sha2.expected_warning_codes(1) == (1583,)


def test_every_registered_signature_has_a_directed_read_only_query_witness() -> None:
    generator = QueryGenerator()
    manifest = _manifest()
    target = _target()

    for ordinal, signature in enumerate(DETERMINISTIC_FUNCTION_SIGNATURES):
        generated = generator.generate(
            manifest,
            target=target,
            seed=ordinal,
            case_ordinal=ordinal,
            lane=QueryLane.VALID,
            directed_variant=signature.signature_id,
            estimated_rows_by_table={"items": 3},
        )

        assert f"{signature.sql_name}(" in generated.sql
        assert f"fn_{signature.signature_id}" in generated.feature_tags
        ReadOnlyValidator().validate_text(generated.sql)


def test_every_nullable_argument_position_has_a_directed_null_witness() -> None:
    generator = QueryGenerator()
    manifest = _manifest()
    target = _target()

    for signature in DETERMINISTIC_FUNCTION_SIGNATURES:
        for position in signature.null_argument_positions:
            generated = generator.generate(
                manifest,
                target=target,
                seed=position,
                lane=QueryLane.VALID,
                directed_variant=f"{signature.signature_id}_null_{position}",
                estimated_rows_by_table={"items": 3},
            )
            assert "NULL" in generated.sql
            assert f"function_null_argument_{position}" in generated.feature_tags
            assert (
                f"fn_{signature.signature_id}_null_{position}"
                in generated.feature_tags
            )
            ReadOnlyValidator().validate_text(generated.sql)


def test_registered_function_ast_rejects_a_signature_outside_the_registry() -> None:
    forged = DeterministicFunctionSignature(
        "math_forged_1",
        FunctionFamily.MATH,
        "FORGED",
        (FunctionArgument.INTEGER,),
        FunctionResult.NUMERIC,
        frozenset({0}),
    )

    try:
        RegisteredFunctionCall(
            forged,
            (Literal(1, SqlType.NUMERIC),),
            SqlType.NUMERIC,
        )
    except ValueError as error:
        assert "registry" in str(error)
    else:  # pragma: no cover - assertion form keeps the forged object visible
        raise AssertionError("forged deterministic function signature was accepted")
