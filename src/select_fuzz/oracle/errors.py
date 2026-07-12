"""Typed oracle input errors and conservative MySQL error normalization."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from select_fuzz.domain.models import ErrorInfo


class OracleInputError(ValueError):
    """Executions or wire values are unsafe or invalid for comparison."""


class CanonicalizationError(OracleInputError):
    """A wire value cannot be represented without losing type information."""


class OracleCapacityError(OracleInputError):
    """Exact comparison would exceed the oracle's reviewed work bound."""


@dataclass(frozen=True, slots=True)
class NormalizedError:
    errno: int
    sqlstate: str
    message: str
    raw_message: str = field(compare=False, hash=False)


_LEADING_CONNECTION_ID = re.compile(
    r"(?i)^(?P<label>\s*(?:connection|thread)\s+id)\s*[:=#]?\s*\d+\b"
)
_ACCOUNT_USER_HOST = re.compile(
    r"(?i)(?P<prefix>\bfor\s+user\s+'(?:[^'\\]|\\.)*'\s*@\s*)"
    r"'(?:[^'\\]|\\.)*'"
)
_HOST_CONNECTION = re.compile(
    r"(?i)(?<!['\"`])\b(?P<context>on|to\s+host)\s+"
    r"(?P<host>\[[0-9a-f:.%]+\]|'(?:[^'\\]|\\.)*'|"
    r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)*)\s+"
    r"(?P<label>connection|thread)(?:\s+id)?\s*[:=#]?\s*\d+\b"
)


def _is_word_apostrophe(text: str, index: int) -> bool:
    return (
        text[index] == "'"
        and index > 0
        and index + 1 < len(text)
        and text[index - 1].isalnum()
        and text[index + 1].isalnum()
    )


def _inside_quoted_fragment(text: str, position: int) -> bool:
    quote: str | None = None
    index = 0
    while index < position:
        character = text[index]
        if _is_word_apostrophe(text, index):
            index += 1
            continue
        if quote is None:
            if character in {"'", '"', "`"}:
                quote = character
            index += 1
            continue
        if character == "\\":
            index += 2
            continue
        if character == quote:
            if index + 1 < position and text[index + 1] == quote:
                index += 2
                continue
            quote = None
        index += 1
    return quote is not None


def _replace_unquoted(
    pattern: re.Pattern[str],
    text: str,
    replacement: Callable[[re.Match[str]], str],
) -> str:
    pieces: list[str] = []
    previous = 0
    for match in pattern.finditer(text):
        if _inside_quoted_fragment(text, match.start()):
            continue
        pieces.extend((text[previous : match.start()], replacement(match)))
        previous = match.end()
    pieces.append(text[previous:])
    return "".join(pieces)


def normalize_error(error: ErrorInfo) -> NormalizedError:
    """Remove only connection/host fragments while retaining the raw message."""

    message = _replace_unquoted(
        _ACCOUNT_USER_HOST,
        error.message,
        lambda match: f"{match.group('prefix')}'<host>'",
    )
    message = _replace_unquoted(
        _HOST_CONNECTION,
        message,
        lambda match: (
            f"{match.group('context').lower()} <host> "
            f"{match.group('label').lower()} <connection_id>"
        ),
    )
    message = _LEADING_CONNECTION_ID.sub(
        lambda match: (
            "thread id: <connection_id>"
            if "thread" in match.group("label").lower()
            else "connection id: <connection_id>"
        ),
        message,
    )
    return NormalizedError(
        errno=error.errno,
        sqlstate=error.sqlstate,
        message=message,
        raw_message=error.message,
    )
