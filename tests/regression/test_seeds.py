from __future__ import annotations

import json
from pathlib import Path

from select_fuzz.generation.query import SUPPORTED_VARIANT_IDS
from select_fuzz.generation.schema import SchemaProfile
from select_fuzz.regression import build_seed_corpus, write_seed_corpus


def test_seed_corpus_covers_every_schema_query_lane_and_error_family() -> None:
    corpus = build_seed_corpus(20260712)

    assert {item["profile"] for item in corpus["schema_profiles"]} == {
        profile.value for profile in SchemaProfile
    }
    assert {item["feature_id"] for item in corpus["query_variants"]} == set(
        SUPPORTED_VARIANT_IDS
    )
    assert {item["lane"] for item in corpus["query_lanes"]} == {
        "valid",
        "free_random",
        "negative",
    }
    assert {item["family"] for item in corpus["negative_error_families"]} == {
        "unknown_column",
        "set_arity",
        "function_arity",
    }


def test_seed_corpus_is_deterministic_and_contains_no_sql_or_credentials() -> None:
    first = build_seed_corpus(20260712)
    second = build_seed_corpus(20260712)
    payload = json.dumps(first, sort_keys=True).casefold()

    assert first == second
    assert "query_sql" not in payload
    assert "setup_sql" not in payload
    assert "password" not in payload
    assert "credential" not in payload


def test_seed_corpus_writer_publishes_strict_json_atomically(tmp_path: Path) -> None:
    destination = tmp_path / "regression" / "seeds.json"

    written = write_seed_corpus(destination, seed=20260712)

    assert written == destination
    assert json.loads(destination.read_text(encoding="utf-8")) == build_seed_corpus(
        20260712
    )
    assert not list(destination.parent.glob(".*.tmp-*"))


def test_checked_in_seed_corpus_matches_the_generator_registry() -> None:
    project_root = Path(__file__).resolve().parents[2]

    checked_in = json.loads(
        (project_root / "tests" / "regression" / "seeds.json").read_text(
            encoding="utf-8"
        )
    )

    assert checked_in == build_seed_corpus(20260712)
