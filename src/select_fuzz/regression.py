"""Versioned deterministic seed corpus for release regression gates."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from select_fuzz.domain import SeedTree
from select_fuzz.generation.query_grammar import SelectGrammar
from select_fuzz.generation.schema import SchemaProfile


def build_seed_corpus(seed: int) -> dict[str, object]:
    """Describe stable generator inputs without freezing executable web SQL."""

    if not isinstance(seed, int) or isinstance(seed, bool):
        raise TypeError("seed must be an integer")
    tree = SeedTree(seed)
    grammar = SelectGrammar.default()
    return {
        "grammar_alternatives": [
            {
                "alternative_ordinal": ordinal,
                "expected_tag": (
                    "grammar_alt:"
                    + grammar.stable_alternative_id(
                        f"{production_name}@{alternative.source_line}"
                    )
                ),
                "production": production_name,
                "seed": tree.derive(
                    "regression",
                    "grammar_alternative",
                    production_name,
                    ordinal,
                ),
            }
            for production_name, production in sorted(grammar.productions.items())
            for ordinal, alternative in enumerate(production.alternatives)
        ],
        "grammar_productions": [
            {
                "expected_tag": f"grammar:{production_name}",
                "production": production_name,
                "seed": tree.derive(
                    "regression", "grammar_production", production_name
                ),
            }
            for production_name in sorted(grammar.productions)
        ],
        "grammar_sha256": grammar.sha256,
        "mysql_version": "8.0.22",
        "root_seed": seed,
        "schema_profiles": [
            {
                "profile": profile.value,
                "seed": tree.derive("regression", "schema", profile.value),
            }
            for profile in SchemaProfile
        ],
        "schema_version": 1,
    }


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:  # pragma: no cover - OS contract defense
            raise OSError("regression seed write returned no progress")
        offset += written


def write_seed_corpus(path: str | Path, *, seed: int) -> Path:
    """Atomically publish canonical strict JSON for the controlled seed corpus."""

    destination = Path(path)
    payload = (
        json.dumps(
            build_seed_corpus(seed),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.tmp-{uuid4().hex}"
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    try:
        os.replace(temporary, destination)
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination


__all__ = ["build_seed_corpus", "write_seed_corpus"]
