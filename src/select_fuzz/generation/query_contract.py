"""Stable query execution contracts shared by generation, replay, and artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re


_SQLSTATE = re.compile(r"^[0-9A-Z]{5}$")


class QueryLane(StrEnum):
    """Execution classification retained for artifact and replay compatibility."""

    VALID = "valid"
    FREE_RANDOM = "free_random"
    NEGATIVE = "negative"


class ExpectedErrorKind(StrEnum):
    UNKNOWN_COLUMN = "unknown_column"
    SET_ARITY_MISMATCH = "set_arity_mismatch"
    INVALID_FUNCTION_ARITY = "invalid_function_arity"


@dataclass(frozen=True, slots=True)
class ExpectedError:
    kind: ExpectedErrorKind
    expected_errno: int
    expected_sqlstate: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ExpectedErrorKind):
            raise TypeError("kind must be an ExpectedErrorKind")
        if (
            not isinstance(self.expected_errno, int)
            or isinstance(self.expected_errno, bool)
            or not 0 <= self.expected_errno <= 0xFFFF
        ):
            raise ValueError("expected_errno must be an unsigned 16-bit integer")
        if (
            not isinstance(self.expected_sqlstate, str)
            or _SQLSTATE.fullmatch(self.expected_sqlstate) is None
        ):
            raise ValueError(
                "expected_sqlstate must contain five uppercase alphanumeric characters"
            )


__all__ = ["ExpectedError", "ExpectedErrorKind", "QueryLane"]
