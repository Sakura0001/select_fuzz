"""Shared production query-generator wiring for fuzz mode."""

from __future__ import annotations

from select_fuzz.generation.query import WeightedQueryGenerator
from select_fuzz.generation.query.grammar import RandomGrammarQueryGenerator
from select_fuzz.generation.query.load_shaped import LoadShapedQueryGenerator
from select_fuzz.generation.query_grammar import GrammarQueryConfig, GrammarQueryGenerator


FUZZ_EXCLUDED_GRAMMAR_FAMILIES = frozenset({"json", "fulltext", "spatial"})


def build_fuzz_query_generator(
    max_tables_per_query_block: int,
) -> WeightedQueryGenerator:
    grammar = RandomGrammarQueryGenerator(
        GrammarQueryGenerator(
            config=GrammarQueryConfig(
                max_tables_per_query_block=max_tables_per_query_block
            )
        ),
        excluded_families=FUZZ_EXCLUDED_GRAMMAR_FAMILIES,
    )
    return WeightedQueryGenerator(
        (
            ("grammar", grammar, 50),
            ("load_shaped", LoadShapedQueryGenerator(), 50),
        )
    )


__all__ = ["FUZZ_EXCLUDED_GRAMMAR_FAMILIES", "build_fuzz_query_generator"]
