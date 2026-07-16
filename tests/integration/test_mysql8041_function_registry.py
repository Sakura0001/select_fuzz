from __future__ import annotations

import pytest


@pytest.mark.mysql
@pytest.mark.timeout(600)
def test_every_deterministic_function_and_null_witness_on_exact_8041_triad() -> None:
    from select_fuzz.generation.query_grammar import FunctionValueProfile
    from test_mysql8041_grammar_matrix import _function_cases, _run_cases

    for profile in FunctionValueProfile:
        cases = _function_cases(profile)
        assert len(cases) == 335
        _run_cases(
            cases,
            artifact_name=f"latest-grammar-function-{profile.value}-20260716",
        )
