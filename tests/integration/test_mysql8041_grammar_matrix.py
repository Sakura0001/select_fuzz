from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

import mysql.connector
import pytest

from select_fuzz.generation.data import DataScenario
from select_fuzz.generation.function_registry import DETERMINISTIC_FUNCTION_SIGNATURES
from select_fuzz.generation.query_grammar import (
    CandidateQuery,
    FunctionValueProfile,
    GrammarAlternative,
    GrammarQueryConfig,
    GrammarProduction,
    GrammarQueryGenerator,
    GrammarSymbol,
    SelectGrammar,
)
from select_fuzz.generation.setup import SetupBundleBuilder

from test_mysql8041_grammar_p1 import _manifest


_ROOT = Path(__file__).resolve().parents[2]
_CANONICAL_GRAMMAR_PATH = _ROOT / "catalog" / "mysql-8.0.22-select.grammar.yy"
_CANONICAL_SOURCE_TEXT = _CANONICAL_GRAMMAR_PATH.read_text(encoding="utf-8")
_CANONICAL_GRAMMAR = SelectGrammar.from_text(_CANONICAL_SOURCE_TEXT)


@dataclass(frozen=True, slots=True)
class _Case:
    name: str
    candidate: CandidateQuery
    expected_fragments: tuple[str, ...]
    expected_warning_codes: tuple[int, ...] | None = None


def _sockets() -> tuple[str, ...]:
    if os.environ.get("SELECT_FUZZ_MYSQL_SOCKET_INTEGRATION") != "1":
        pytest.skip("set SELECT_FUZZ_MYSQL_SOCKET_INTEGRATION=1 and socket list")
    sockets_value = os.environ.get("SELECT_FUZZ_MYSQL_SOCKETS")
    if sockets_value is None:
        pytest.skip("SELECT_FUZZ_MYSQL_SOCKETS is unset")
    sockets = tuple(item for item in sockets_value.split(",") if item)
    if len(sockets) != 3:
        pytest.skip("SELECT_FUZZ_MYSQL_SOCKETS must contain exactly three paths")
    return sockets


def _production(name: str, alternatives: tuple[tuple[str, ...], ...]) -> GrammarProduction:
    return GrammarProduction(
        name,
        tuple(
            GrammarAlternative(
                tuple(GrammarSymbol(symbol) for symbol in symbols),
                100_000 + index,
            )
            for index, symbols in enumerate(alternatives)
        ),
    )


def _grammar_for(
    *,
    case_production: str | None = None,
    case_alternative: int | None = None,
    root_symbols: tuple[str, ...],
    relation_symbols: tuple[str, ...] = ("_table",),
    overrides: dict[str, GrammarProduction] | None = None,
) -> SelectGrammar:
    productions = dict(_CANONICAL_GRAMMAR.productions)
    if case_production is not None:
        assert case_alternative is not None
        source = productions[case_production].alternatives[case_alternative]
        productions["acceptance_case"] = GrammarProduction(
            "acceptance_case",
            (GrammarAlternative(source.symbols, 99_999),),
        )
        root_symbols = tuple(
            "acceptance_case" if symbol == "__CASE__" else symbol for symbol in root_symbols
        )
    productions["relation"] = _production("relation", (relation_symbols,))
    if overrides:
        productions.update(overrides)
    productions["acceptance_query"] = _production("acceptance_query", (root_symbols,))
    return SelectGrammar(
        productions,
        # Keep the evidence hash equal to the checked-in canonical grammar.
        # The temporary acceptance root only selects one exact production
        # alternative; it must not look like a different grammar version.
        source_text=_CANONICAL_SOURCE_TEXT,
        root="acceptance_query",
    )


def _generate(
    *,
    seed: int,
    case_production: str | None = None,
    case_alternative: int | None = None,
    root_symbols: tuple[str, ...],
    relation_symbols: tuple[str, ...] = ("_table",),
    overrides: dict[str, GrammarProduction] | None = None,
    grammar_config: GrammarQueryConfig | None = None,
) -> CandidateQuery:
    grammar = _grammar_for(
        case_production=case_production,
        case_alternative=case_alternative,
        root_symbols=root_symbols,
        relation_symbols=relation_symbols,
        overrides=overrides,
    )
    return GrammarQueryGenerator(grammar).generate(
        _manifest(),
        seed=seed,
        grammar_config=grammar_config,
    )


def _anti_cases() -> tuple[_Case, ...]:
    names = (
        "not_in_empty",
        "not_in_single",
        "not_in_multi",
        "not_in_outer_nullable",
        "not_in_inner_nullable",
        "not_in_both_nullable",
        "not_exists_empty",
        "not_exists_single",
        "not_exists_multi",
        "nested_not_exists_not_in",
        "nested_not_in_not_exists",
        "not_in_any_all_combination",
    )
    fragments = (
        ("NOT IN", "1 = 0"),
        ("NOT IN", "GROUP BY 1"),
        ("NOT IN", "SELECT 1"),
        ("NULL NOT IN", "GROUP BY 1"),
        ("NOT IN", "SELECT NULL"),
        ("NULL NOT IN", "SELECT NULL"),
        ("NOT EXISTS", "1 = 0"),
        ("NOT EXISTS", "LIMIT 1"),
        ("NOT EXISTS", "SELECT 1"),
        ("NOT EXISTS", "NOT IN", "SELECT NULL"),
        ("NOT IN", "NOT EXISTS", "1 = 0"),
        ("<> ALL", "NOT EXISTS"),
    )
    return tuple(
        _Case(
            name,
            _generate(
                seed=70_000 + index,
                case_production="anti_membership_predicate",
                case_alternative=index,
                root_symbols=(
                    "_scope_begin",
                    "_prepare_relation",
                    "SELECT",
                    "1",
                    "AS",
                    "_projection_alias",
                    "FROM",
                    "_emit_relation",
                    "WHERE",
                    "__CASE__",
                    "_scope_end",
                ),
            ),
            fragments[index],
        )
        for index, name in enumerate(names)
    )


def _frame_cases() -> tuple[_Case, ...]:
    cases: list[_Case] = []
    frame_count = len(_CANONICAL_GRAMMAR.productions["frame_clause"].alternatives)
    for index in range(frame_count):
        cases.append(
            _Case(
                f"rows_frame_{index:02d}",
                _generate(
                    seed=71_000 + index,
                    case_production="frame_clause",
                    case_alternative=index,
                    root_symbols=(
                        "_scope_begin",
                        "_prepare_relation",
                        "SELECT",
                        "SUM",
                        "(",
                        "_strict_numeric_column",
                        ")",
                        "OVER",
                        "(",
                        "ORDER",
                        "BY",
                        "_window_numeric_order",
                        "frame_case",
                        ")",
                        "AS",
                        "_projection_alias",
                        "FROM",
                        "_emit_relation",
                        "_scope_end",
                    ),
                    overrides={
                        "frame_case": GrammarProduction(
                            "frame_case",
                            (
                                GrammarAlternative(
                                    _CANONICAL_GRAMMAR.productions["frame_clause"]
                                    .alternatives[index]
                                    .symbols,
                                    99_998,
                                ),
                            ),
                        )
                    },
                ),
                ("ROWS",) if index < 14 else ("RANGE",),
            )
        )
    for production_name, prefix, count in (
        ("numeric_range_frame_clause", "numeric_range", 4),
        ("temporal_range_frame_clause", "temporal_range", 4),
    ):
        order_symbol = (
            "_window_numeric_order"
            if production_name.startswith("numeric")
            else "_window_temporal_order"
        )
        for index in range(count):
            cases.append(
                _Case(
                    f"{prefix}_{index:02d}",
                    _generate(
                        seed=72_000 + len(cases),
                        case_production=production_name,
                        case_alternative=index,
                        root_symbols=(
                            "_scope_begin",
                            "_prepare_relation",
                            "SELECT",
                            "SUM",
                            "(",
                            "_strict_numeric_column",
                            ")",
                            "OVER",
                            "(",
                            "ORDER",
                            "BY",
                            order_symbol,
                            "frame_case",
                            ")",
                            "AS",
                            "_projection_alias",
                            "FROM",
                            "_emit_relation",
                            "_scope_end",
                        ),
                        overrides={
                            "frame_case": GrammarProduction(
                                "frame_case",
                                (
                                    GrammarAlternative(
                                        _CANONICAL_GRAMMAR.productions[production_name]
                                        .alternatives[index]
                                        .symbols,
                                        99_997,
                                    ),
                                ),
                            )
                        },
                    ),
                    ("RANGE",),
                )
            )
    return tuple(cases)


def _hint_cases() -> tuple[_Case, ...]:
    normal_root = (
        "_scope_begin",
        "_prepare_base_relation",
        "SELECT",
        "__HINT__",
        "1",
        "AS",
        "_projection_alias",
        "FROM",
        "_emit_relation",
        "_scope_end",
    )
    cases: list[_Case] = []
    for index, (name, hint, fragments) in enumerate(
        (
            ("index_primary", "_optimizer_hint_index_primary", ("INDEX", "PRIMARY")),
            ("index_secondary", "_optimizer_hint_index_secondary", ("INDEX", "idx_tenant")),
            ("no_range_optimization", "_optimizer_hint_no_range", ("NO_RANGE_OPTIMIZATION",)),
            ("no_icp_fallback", "_optimizer_hint_no_icp", ("NO_ICP",)),
        )
    ):
        cases.append(
            _Case(
                name,
                _generate(
                    seed=73_000 + index,
                    root_symbols=tuple(
                        hint if symbol == "__HINT__" else symbol for symbol in normal_root
                    ),
                ),
                fragments,
            )
        )
    cases.append(
        _Case(
            "index_secondary_join_relation",
            _generate(
                seed=73_004,
                root_symbols=tuple(
                    "_optimizer_hint_index_secondary"
                    if symbol == "__HINT__"
                    else "_prepare_relation"
                    if symbol == "_prepare_base_relation"
                    else symbol
                    for symbol in normal_root
                ),
                relation_symbols=("_table", "JOIN", "_table"),
            ),
            ("INDEX", "idx_tenant"),
        )
    )
    for width in (2, 3, 4):
        relation = tuple(
            symbol
            for index in range(width)
            for symbol in (("_table",) if index == 0 else ("JOIN", "_table"))
        )
        cases.append(
            _Case(
                f"join_order_{width}",
                _generate(
                    seed=73_010 + width,
                    root_symbols=tuple(
                        "_optimizer_hint_join_order"
                        if symbol == "__HINT__"
                        else "_prepare_relation"
                        if symbol == "_prepare_base_relation"
                        else symbol
                        for symbol in normal_root
                    ),
                    relation_symbols=relation,
                ),
                ("JOIN_ORDER", *(f"`r{index}`" for index in range(1, width + 1))),
            )
        )
    derived_select = _production(
        "derived_select",
        (
            (
                "_scope_begin",
                "_prepare_base_relation",
                "SELECT",
                "_strict_numeric_column",
                "AS",
                "_projection_alias",
                "FROM",
                "_emit_relation",
                "_scope_end",
            ),
        ),
    )
    for index, (name, hint_symbol, expected_hint) in enumerate(
        (
            ("derived_merge", "_optimizer_hint_merge", "MERGE"),
            ("derived_no_merge", "_optimizer_hint_no_merge", "NO_MERGE"),
            (
                "derived_condition_pushdown",
                "_optimizer_hint_derived_condition_pushdown",
                "DERIVED_CONDITION_PUSHDOWN",
            ),
        )
    ):
        cases.append(
            _Case(
                name,
                _generate(
                    seed=73_020 + index,
                    root_symbols=tuple(
                        hint_symbol
                        if symbol == "__HINT__"
                        else "_prepare_relation"
                        if symbol == "_prepare_base_relation"
                        else symbol
                        for symbol in normal_root
                    ),
                    relation_symbols=("_derived_relation",),
                    overrides={"derived_select": derived_select},
                ),
                (expected_hint,),
            )
        )
    return tuple(cases)


def _normalized_rows(rows: list[tuple[object, ...]]) -> tuple[str, ...]:
    return tuple(sorted(repr(tuple(row)) for row in rows))


def _artifact_path(default_name: str = "latest-grammar-matrix-20260716") -> Path:
    configured = os.environ.get("SELECT_FUZZ_MATRIX_ARTIFACT_DIR")
    if configured:
        return Path(configured)
    return _ROOT / "artifacts" / default_name


def _write_artifact(path: Path, records: list[dict[str, object]], cases: tuple[_Case, ...]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "manifest.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "grammar_hash": _CANONICAL_GRAMMAR.sha256,
                "case_count": len(cases),
                "cases": [case.name for case in cases],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (path / "events.jsonl").write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def _run_cases(
    cases: tuple[_Case, ...],
    *,
    artifact_name: str = "latest-grammar-matrix-20260716",
) -> None:
    manifest = _manifest()
    setup = SetupBundleBuilder().build(
        manifest,
        seed=8_041,
        rows_per_table=3,
        scenario=DataScenario.MIXED_NULL,
    )
    sockets = _sockets()
    database = f"sf_grammar_matrix_{time.time_ns():x}"[-64:]
    artifact = _artifact_path(artifact_name)
    records: list[dict[str, object]] = []
    connections = [
        mysql.connector.connect(unix_socket=socket, user="root", autocommit=True)
        for socket in sockets
    ]
    try:
        for connection in connections:
            cursor = connection.cursor()
            cursor.execute("SELECT VERSION()")
            assert cursor.fetchone()[0].startswith("8.0.41")
            cursor.execute(f"CREATE DATABASE `{database}`")
            cursor.execute(f"USE `{database}`")
            for statement in setup.statements:
                cursor.execute(statement)
            cursor.close()

        for case in cases:
            for fragment in case.expected_fragments:
                assert fragment.casefold() in case.candidate.sql.casefold(), (
                    case.name,
                    fragment,
                    case.candidate.sql,
                )
            outcomes: list[tuple[str, ...]] = []
            warning_codes: list[tuple[int, ...]] = []
            metadata: list[tuple[tuple[object, ...], ...]] = []
            try:
                for connection in connections:
                    cursor = connection.cursor()
                    cursor.execute(f"EXPLAIN {case.candidate.sql}")
                    explain_rows = cursor.fetchall()
                    assert explain_rows, case.name
                    cursor.execute("SHOW WARNINGS")
                    explain_warnings = tuple(cursor.fetchall())
                    assert not any(
                        "hint" in str(warning[2]).casefold()
                        for warning in explain_warnings
                    ), (case.name, explain_warnings, case.candidate.sql)
                    cursor.execute(case.candidate.sql)
                    outcomes.append(_normalized_rows(cursor.fetchall()))
                    metadata.append(
                        tuple(
                            (description[0], description[1], description[7])
                            for description in cursor.description or ()
                        )
                    )
                    cursor.execute("SHOW WARNINGS")
                    warnings = tuple(cursor.fetchall())
                    warning_codes.append(tuple(int(warning[1]) for warning in warnings))
                    cursor.close()
            except Exception as error:
                records.append(
                    {
                        "case": case.name,
                        "grammar_hash": case.candidate.grammar_hash,
                        "seed": case.candidate.seed,
                        "sql": case.candidate.sql,
                        "production_trace": case.candidate.production_trace,
                        "status": "error",
                        "error": repr(error),
                    }
                )
                _write_artifact(artifact, records, cases)
                raise
            assert outcomes[0] == outcomes[1] == outcomes[2], case.name
            assert warning_codes[0] == warning_codes[1] == warning_codes[2], case.name
            assert metadata[0] == metadata[1] == metadata[2], case.name
            if case.expected_warning_codes is not None:
                assert warning_codes == [case.expected_warning_codes] * 3, (
                    case.name,
                    warning_codes,
                )
            records.append(
                {
                    "case": case.name,
                    "grammar_hash": case.candidate.grammar_hash,
                    "seed": case.candidate.seed,
                    "sql": case.candidate.sql,
                    "production_trace": case.candidate.production_trace,
                    "status": "passed",
                    "rows": outcomes,
                    "warning_codes": warning_codes,
                    "metadata": metadata,
                }
            )
            _write_artifact(artifact, records, cases)
    finally:
        for connection in connections:
            cursor = connection.cursor()
            cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
            cursor.close()
            connection.close()


@pytest.mark.mysql
@pytest.mark.timeout(600)
def test_anti_subquery_cardinality_null_and_nested_matrix_on_three_exact_8041_sockets() -> None:
    _run_cases(_anti_cases(), artifact_name="latest-grammar-matrix-20260716")


@pytest.mark.mysql
@pytest.mark.timeout(600)
def test_all_legal_frame_bounds_on_three_exact_8041_sockets() -> None:
    _run_cases(_frame_cases(), artifact_name="latest-grammar-frame-matrix-20260716")


@pytest.mark.mysql
@pytest.mark.timeout(600)
def test_optimizer_hint_positive_matrix_on_three_exact_8041_sockets() -> None:
    _run_cases(_hint_cases(), artifact_name="latest-grammar-hint-matrix-20260716")


def _function_cases(
    profile: FunctionValueProfile = FunctionValueProfile.NORMAL,
) -> tuple[_Case, ...]:
    cases: list[_Case] = []
    ordinal = 0
    for signature in DETERMINISTIC_FUNCTION_SIGNATURES:
        variants = (
            signature.signature_id,
            *(f"{signature.signature_id}_null_{position}" for position in sorted(signature.null_argument_positions)),
        )
        for variant in variants:
            null_position = (
                None
                if variant == signature.signature_id
                else int(variant.rsplit("_", maxsplit=1)[-1])
            )
            cases.append(
                _Case(
                    f"function_{variant}",
                    _generate(
                        seed=74_000 + ordinal,
                        root_symbols=(
                            "_scope_begin",
                            "SELECT",
                            "function_case",
                            "AS",
                            "_projection_alias",
                            "_scope_end",
                        ),
                        overrides={
                            "function_case": _production("function_case", ((f"_fn_{variant}",),))
                        },
                        grammar_config=GrammarQueryConfig(function_value_profile=profile),
                    ),
                    (signature.sql_name,),
                    signature.expected_warning_codes(null_position),
                )
            )
            ordinal += 1
    return tuple(cases)
