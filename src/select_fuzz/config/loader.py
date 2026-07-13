"""YAML loading, CLI precedence, and late environment credential resolution."""

from __future__ import annotations

import os
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from pydantic import SecretStr, ValidationError

from select_fuzz.config.models import AppConfig, NodeConfig, ResolvedCredentials, RunMode


class ConfigLoadError(ValueError):
    """A deliberately sanitized configuration error."""


_CORRECTNESS_FLAT_KEYS = {
    "workers",
    "queries_per_round",
    "timeout_seconds",
    "row_limit",
    "byte_limit",
    "min_rows_per_table",
    "max_rows_per_table",
    "free_random_rate",
    "negative_mutation_rate",
}
_PERFORMANCE_FLAT_KEYS = {
    "workers",
    "queries_per_round",
    "initial_table_rows",
    "max_table_rows",
    "max_calibration_rounds",
    "calibration_runs_per_reference",
    "calibration_min_seconds",
    "calibration_max_seconds",
    "formal_timeout_seconds",
    "regression_threshold",
    "max_start_skew_ms",
}


def _validation_summary(error: ValidationError) -> str:
    issues = []
    for item in error.errors(include_url=False, include_context=False, include_input=False):
        location = ".".join(str(part) for part in item["loc"]) or "configuration"
        issues.append(f"{location} [{item['type']}]")
    return "Invalid configuration: " + "; ".join(issues)


def _merge_mapping(target: dict[str, Any], source: Mapping[str, object]) -> None:
    for key, value in source.items():
        if value is None:
            continue
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            _merge_mapping(current, value)
        else:
            target[key] = deepcopy(value)


def _set_dotted(target: dict[str, Any], dotted_key: str, value: object) -> None:
    parts = dotted_key.split(".")
    if not parts or any(not part for part in parts) or parts[0] not in {
        "correctness",
        "performance",
    }:
        raise ConfigLoadError(f"Unsupported CLI override key: {dotted_key}")
    cursor = target
    for part in parts[:-1]:
        child = cursor.setdefault(part, {})
        if not isinstance(child, dict):
            raise ConfigLoadError(f"Unsupported CLI override path: {dotted_key}")
        cursor = child
    cursor[parts[-1]] = deepcopy(value)


def _apply_cli_overrides(raw: dict[str, Any], cli: Mapping[str, object]) -> None:
    cli_mode = cli.get("mode")
    mode_value = (
        raw.get("mode", RunMode.CORRECTNESS.value) if cli_mode is None else cli_mode
    )
    if not isinstance(mode_value, str):
        raise ConfigLoadError("Invalid CLI mode")
    try:
        mode: RunMode | None = RunMode(mode_value)
    except (TypeError, ValueError):
        mode = None
    if mode is None:
        raise ConfigLoadError("Invalid CLI mode")

    if cli.get("mode") is not None:
        raw["mode"] = mode.value

    for key, value in cli.items():
        if value is None or key == "mode":
            continue
        if key in {"correctness", "performance"}:
            if not isinstance(value, Mapping):
                raise ConfigLoadError(f"CLI override {key} must be a mapping")
            section = raw.setdefault(key, {})
            if not isinstance(section, dict):
                raise ConfigLoadError(f"Configuration section {key} must be a mapping")
            _merge_mapping(section, value)
        elif "." in key:
            _set_dotted(raw, key, value)
        elif mode is RunMode.CORRECTNESS and key in _CORRECTNESS_FLAT_KEYS:
            section = raw.setdefault("correctness", {})
            if not isinstance(section, dict):
                raise ConfigLoadError("Configuration section correctness must be a mapping")
            section[key] = deepcopy(value)
        elif mode is RunMode.PERFORMANCE and (
            key in _PERFORMANCE_FLAT_KEYS or key == "timeout_seconds"
        ):
            section = raw.setdefault("performance", {})
            if not isinstance(section, dict):
                raise ConfigLoadError("Configuration section performance must be a mapping")
            target_key = "formal_timeout_seconds" if key == "timeout_seconds" else key
            section[target_key] = deepcopy(value)
        else:
            raise ConfigLoadError(f"Unsupported CLI override key: {key}")


def load_config(
    path: str | Path, *, cli: Mapping[str, object] | None = None
) -> AppConfig:
    """Load YAML and apply CLI values with CLI taking precedence."""

    config_path = Path(path)
    invalid_yaml = False
    try:
        document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigLoadError(f"Unable to read configuration file: {config_path}") from error
    except yaml.YAMLError:
        document = None
        invalid_yaml = True

    if invalid_yaml:
        raise ConfigLoadError("Invalid YAML configuration")

    if not isinstance(document, Mapping):
        raise ConfigLoadError("Configuration root must be a mapping")

    raw: dict[str, Any] = deepcopy(dict(document))
    if cli:
        _apply_cli_overrides(raw, cli)
    validation_message: str | None = None
    try:
        config = AppConfig.model_validate(raw)
    except ValidationError as error:
        validation_message = _validation_summary(error)
    if validation_message is not None:
        raise ConfigLoadError(validation_message)
    return config


def resolve_credentials(
    node: NodeConfig, environ: Mapping[str, str] | None = None
) -> ResolvedCredentials:
    """Resolve environment references immediately before opening a connector."""

    source = os.environ if environ is None else environ
    missing = [name for name in (node.username_env, node.password_env) if not source.get(name)]
    if missing:
        raise ConfigLoadError(f"Missing required credential environment variable: {', '.join(missing)}")
    return ResolvedCredentials(
        username=SecretStr(source[node.username_env]),
        password=SecretStr(source[node.password_env]),
    )
