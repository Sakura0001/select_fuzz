"""Adapter exposing the canonical grammar through the common generator contract."""

from __future__ import annotations

from select_fuzz.generation.query import (
    GeneratedQuery,
    QueryGenerationContext,
)
from select_fuzz.generation.query_grammar import GrammarQueryGenerator


class RandomGrammarQueryGenerator:
    name = "grammar"

    def __init__(
        self,
        generator: GrammarQueryGenerator | None = None,
        *,
        excluded_families: frozenset[str] = frozenset(),
    ) -> None:
        self._generator = generator or GrammarQueryGenerator()
        self._excluded_families = excluded_families

    def generate(
        self,
        context: QueryGenerationContext,
        *,
        seed: int,
    ) -> GeneratedQuery:
        if context.schema is None:
            raise ValueError("grammar query generation requires a schema")
        candidate = self._generator.generate(
            context.schema,
            seed=seed,
            excluded_families=self._excluded_families,
        )
        return GeneratedQuery(
            candidate.sql,
            seed,
            self.name,
            frozenset({"grammar_random"}),
        )


__all__ = ["RandomGrammarQueryGenerator"]
