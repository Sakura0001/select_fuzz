"""Canonical entry point for the three-topology correctness mode.

The implementation remains in :mod:`select_fuzz.correctness` for backwards
compatibility with existing callers.  Keeping this small adapter here makes
the mode boundary explicit and gives future modes the same package shape.
"""

from select_fuzz.correctness import build_correctness_runner

__all__ = ["build_correctness_runner"]
