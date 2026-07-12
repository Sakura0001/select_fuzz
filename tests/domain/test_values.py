from __future__ import annotations

import json
import subprocess
import sys

from select_fuzz.domain.values import SeedTree, deterministic_id, stable_fingerprint


def test_seed_tree_is_deterministic_worker_safe_and_length_prefixed() -> None:
    tree = SeedTree(root=42)
    path = ("worker", 3, "round", 7, "query", 9)

    assert tree.derive(*path) == tree.derive(*path)
    assert tree.derive("worker", 3) != tree.derive("worker", 4)
    assert tree.derive("ab", "c") != tree.derive("a", "bc")


def test_seed_and_id_are_stable_across_python_processes() -> None:
    script = (
        "import json; "
        "from select_fuzz.domain.values import SeedTree, deterministic_id; "
        "print(json.dumps([SeedTree(42).derive('worker', 3, 'round', 7), "
        "deterministic_id('case', 42, 'join.inner')]))"
    )
    first = subprocess.check_output([sys.executable, "-c", script], text=True).strip()
    second = subprocess.check_output([sys.executable, "-c", script], text=True).strip()

    assert json.loads(first) == json.loads(second)


def test_deterministic_id_has_namespace_and_canonical_fingerprint() -> None:
    assert deterministic_id("case", 1, "x").startswith("case_")
    assert deterministic_id("case", 1, "x") != deterministic_id("round", 1, "x")
    assert stable_fingerprint({"b": [2, 1], "a": 1}) == stable_fingerprint(
        {"a": 1, "b": [2, 1]}
    )


def test_fingerprint_type_encoding_cannot_collide_with_user_mappings() -> None:
    assert stable_fingerprint(1.5) != stable_fingerprint({"$float": 1.5.hex()})
    assert stable_fingerprint(b"abc") != stable_fingerprint({"$bytes": "616263"})
    assert stable_fingerprint([1, 2]) != stable_fingerprint((1, 2))
    assert stable_fingerprint(1) != stable_fingerprint("1")


def test_time_or_database_labels_do_not_change_case_payload_identity() -> None:
    logical_payload = {"seed": 9, "feature": "cte.recursive", "query_index": 4}

    case_id = deterministic_id("case", stable_fingerprint(logical_payload))
    later_case_id = deterministic_id("case", stable_fingerprint(logical_payload))

    assert case_id == later_case_id
