#!/usr/bin/env python3
"""Enforce independent line and branch coverage thresholds from coverage.py JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from select_fuzz.coverage_gate import coverage_percentages


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--min-lines", type=float, default=90.0)
    parser.add_argument("--min-branches", type=float, default=85.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        lines, branches = coverage_percentages(report)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"coverage gate failed: {type(error).__name__}")
        return 2
    print(f"coverage: lines={lines:.2f}% branches={branches:.2f}%")
    if lines < args.min_lines or branches < args.min_branches:
        print(
            "coverage gate failed: "
            f"required lines>={args.min_lines:g}% branches>={args.min_branches:g}%"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
