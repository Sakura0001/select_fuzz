"""Composable SELECT generator contracts shared by execution modes."""

from __future__ import annotations

from dataclasses import dataclass
import random
from typing import Protocol

from select_fuzz.generation.query_grammar import GrammarSchema
from select_fuzz.generation.schema import SchemaManifest


@dataclass(frozen=True, slots=True)
class QueryGenerationContext:
    database: str
    schema: SchemaManifest | GrammarSchema | None

    def __post_init__(self) -> None:
        if not self.database:
            raise ValueError("database must not be empty")


@dataclass(frozen=True, slots=True)
class GeneratedQuery:
    sql: str
    seed: int
    generator: str
    tags: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.sql.strip():
            raise ValueError("sql must not be empty")
        if not self.generator:
            raise ValueError("generator must not be empty")


class QueryGenerator(Protocol):
    def generate(
        self,
        context: QueryGenerationContext,
        *,
        seed: int,
    ) -> GeneratedQuery: ...


class WeightedQueryGenerator:
    """Choose one generator for every query using only the supplied seed."""

    def __init__(
        self,
        generators: tuple[tuple[str, QueryGenerator, int], ...],
    ) -> None:
        if not generators:
            raise ValueError("at least one query generator is required")
        names = [name for name, _generator, _weight in generators]
        if len(names) != len(set(names)) or any(not name for name in names):
            raise ValueError("query generator names must be unique and nonempty")
        if any(
            not isinstance(weight, int) or isinstance(weight, bool) or weight <= 0
            for _name, _generator, weight in generators
        ):
            raise ValueError("query generator weights must be positive integers")
        self._generators = generators
        self._total_weight = sum(weight for _name, _generator, weight in generators)

    def generate(
        self,
        context: QueryGenerationContext,
        *,
        seed: int,
    ) -> GeneratedQuery:
        ticket = random.Random(seed).randrange(self._total_weight)
        cursor = 0
        for _name, generator, weight in self._generators:
            cursor += weight
            if ticket < cursor:
                return generator.generate(context, seed=seed)
        raise RuntimeError("weighted query selection exhausted")  # pragma: no cover


__all__ = [
    "GeneratedQuery",
    "QueryGenerationContext",
    "QueryGenerator",
    "WeightedQueryGenerator",
]
