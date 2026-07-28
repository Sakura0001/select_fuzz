from dataclasses import dataclass

from select_fuzz.generation.query import (
    GeneratedQuery,
    QueryGenerationContext,
    WeightedQueryGenerator,
)


@dataclass
class _StubGenerator:
    name: str

    def generate(self, context: QueryGenerationContext, *, seed: int) -> GeneratedQuery:
        del context
        return GeneratedQuery(
            sql=f"SELECT '{self.name}'",
            seed=seed,
            generator=self.name,
            tags=frozenset({self.name}),
        )


def test_weighted_query_generator_selects_per_call_deterministically() -> None:
    generator = WeightedQueryGenerator(
        (("grammar", _StubGenerator("grammar"), 50), ("load_shaped", _StubGenerator("load"), 50))
    )
    context = QueryGenerationContext(database="sf_f_case", schema=None)

    first = [generator.generate(context, seed=seed).generator for seed in range(200)]
    second = [generator.generate(context, seed=seed).generator for seed in range(200)]

    assert first == second
    assert 70 <= first.count("grammar") <= 130
    assert 70 <= first.count("load") <= 130
