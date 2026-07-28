"""Adapter exposing the canonical grammar through the common generator contract."""

from __future__ import annotations

from select_fuzz.generation.query import (
    GeneratedQuery,
    QueryGenerationContext,
)
from select_fuzz.generation.query_grammar import GrammarQueryGenerator


class RandomGrammarQueryGenerator:
    name = "grammar"

    def __init__(self, generator: GrammarQueryGenerator | None = None) -> None:
        self._generator = generator or GrammarQueryGenerator()

    def generate(
        self,
        context: QueryGenerationContext,
        *,
        seed: int,
    ) -> GeneratedQuery:
        if context.schema is None:
            raise ValueError("grammar query generation requires a schema")
        candidate = self._generator.generate(context.schema, seed=seed)
        return GeneratedQuery(
            candidate.sql,
            seed,
            self.name,
            frozenset({"grammar_random"}),
        )


__all__ = ["RandomGrammarQueryGenerator"]
