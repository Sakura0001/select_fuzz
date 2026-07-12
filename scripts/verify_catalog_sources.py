#!/usr/bin/env python3
"""Verify that catalog evidence sources still match their locked bytes and locators."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys

from select_fuzz.generation.catalog_schema import (
    SourceLockError,
    inspect_catalog_source_locks,
    verify_catalog_source_lock,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch every official catalog source read-only and verify its SHA-256 "
            "and evidence locator manifest."
        )
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help=(
            "fetch stable scopes and print review candidates without modifying the catalog"
        ),
    )
    parser.add_argument("catalog", type=Path, help="path to a schema_version=2 catalog YAML")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.refresh:
            candidates = inspect_catalog_source_locks(args.catalog)
            for candidate in candidates:
                print(
                    f"{candidate.source_id}: {candidate.content_sha256} "
                    f"({candidate.locators_checked} locator(s))"
                )
            return 0
        report = verify_catalog_source_lock(args.catalog)
    except (OSError, SourceLockError, ValueError) as error:
        print(f"source-lock verification failed: {error}", file=sys.stderr)
        return 1

    print(
        f"verified {report.sources_checked} source(s) and "
        f"{report.locators_checked} locator(s): {args.catalog}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
