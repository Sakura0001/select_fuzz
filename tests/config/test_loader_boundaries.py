from __future__ import annotations

from pathlib import Path

import pytest

from select_fuzz.config.loader import (
    ConfigLoadError,
    _apply_cli_overrides,
    _merge_mapping,
    _set_dotted,
    load_config,
)


def test_recursive_merge_skips_none_and_replaces_non_mapping_values() -> None:
    target = {"correctness": {"workers": 1, "row_limit": 2}, "mode": "correctness"}
    _merge_mapping(
        target,
        {"correctness": {"workers": 3, "row_limit": None}, "mode": "performance"},
    )
    assert target == {
        "correctness": {"workers": 3, "row_limit": 2},
        "mode": "performance",
    }


@pytest.mark.parametrize("key", ["", ".workers", "unknown.workers", "correctness."])
def test_dotted_overrides_reject_unsupported_keys(key: str) -> None:
    with pytest.raises(ConfigLoadError, match="Unsupported CLI override key"):
        _set_dotted({}, key, 1)


def test_dotted_overrides_reject_scalar_intermediate_sections() -> None:
    with pytest.raises(ConfigLoadError, match="override path"):
        _set_dotted({"correctness": 1}, "correctness.workers", 2)


@pytest.mark.parametrize(
    ("raw", "cli", "message"),
    [
        ({}, {"mode": 1}, "Invalid CLI mode"),
        ({}, {"mode": "unknown"}, "Invalid CLI mode"),
        ({}, {"correctness": 1}, "must be a mapping"),
        ({"correctness": 1}, {"correctness": {"workers": 2}}, "must be a mapping"),
        ({"correctness": 1}, {"workers": 2}, "must be a mapping"),
        (
            {"mode": "performance", "performance": 1},
            {"queries_per_round": 2},
            "must be a mapping",
        ),
        ({}, {"unknown": 1}, "Unsupported CLI override key"),
    ],
)
def test_cli_override_validation_paths(
    raw: dict[str, object], cli: dict[str, object], message: str
) -> None:
    with pytest.raises(ConfigLoadError, match=message):
        _apply_cli_overrides(raw, cli)


def test_cli_nested_dotted_and_performance_timeout_aliases() -> None:
    raw: dict[str, object] = {}
    _apply_cli_overrides(
        raw,
        {
            "mode": "performance",
            "performance": {"queries_per_round": 2},
            "performance.max_table_rows": 100,
            "timeout_seconds": 9,
            "workers": None,
        },
    )
    assert raw["mode"] == "performance"
    assert raw["performance"] == {
        "queries_per_round": 2,
        "max_table_rows": 100,
        "formal_timeout_seconds": 9,
    }


def test_load_config_sanitizes_io_yaml_root_and_validation_failures(tmp_path: Path) -> None:
    with pytest.raises(ConfigLoadError, match="Unable to read"):
        load_config(tmp_path / "missing.yaml")

    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("key: [", encoding="utf-8")
    with pytest.raises(ConfigLoadError, match="Invalid YAML"):
        load_config(malformed)

    scalar = tmp_path / "scalar.yaml"
    scalar.write_text("value", encoding="utf-8")
    with pytest.raises(ConfigLoadError, match="root must be a mapping"):
        load_config(scalar)

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("mode: correctness\nnodes: []\n", encoding="utf-8")
    with pytest.raises(ConfigLoadError, match=r"Invalid configuration: configuration \["):
        load_config(invalid)
