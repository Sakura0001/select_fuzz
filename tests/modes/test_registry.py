from select_fuzz.modes import MODE_REGISTRY, ModeDefinition


def test_registry_exposes_exactly_three_isolated_modes() -> None:
    assert set(MODE_REGISTRY) == {"correctness", "performance", "fuzz"}
    assert all(isinstance(definition, ModeDefinition) for definition in MODE_REGISTRY.values())
    assert MODE_REGISTRY["correctness"].label == "三库对比"
    assert MODE_REGISTRY["performance"].label == "性能对比"
    assert MODE_REGISTRY["fuzz"].label == "并发 Fuzz"
