"""Conservative admission checks for row-limited differential queries."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DeterministicOrderProof:
    """Generator-owned proof that every effective row limit has stable ordering."""

    covers_all_row_limits: bool


@dataclass(frozen=True, slots=True)
class QueryDeterminism:
    admissible: bool
    reason: str | None = None
    row_limits: tuple[int | None, ...] = ()


def _tokens(sql: str) -> tuple[str, ...]:
    tokens: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        character = sql[index]
        if character.isspace():
            index += 1
            continue
        if character == "#":
            newline = sql.find("\n", index + 1)
            index = length if newline < 0 else newline + 1
            continue
        if (
            character == "-"
            and index + 1 < length
            and sql[index + 1] == "-"
            and (index + 2 == length or sql[index + 2].isspace())
        ):
            newline = sql.find("\n", index + 2)
            index = length if newline < 0 else newline + 1
            continue
        if character == "/" and index + 1 < length and sql[index + 1] == "*":
            end = sql.find("*/", index + 2)
            index = length if end < 0 else end + 2
            continue
        if character in {"'", '"', "`"}:
            quote = character
            index += 1
            while index < length:
                if sql[index] == "\\" and quote != "`":
                    index += 2
                    continue
                if sql[index] == quote:
                    if index + 1 < length and sql[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue
        if character.isdigit():
            end = index + 1
            while end < length and sql[end].isdigit():
                end += 1
            tokens.append(sql[index:end])
            index = end
            continue
        if character.isalpha() or character == "_":
            end = index + 1
            while end < length and (sql[end].isalnum() or sql[end] in {"_", "$"}):
                end += 1
            tokens.append(sql[index:end].upper())
            index = end
            continue
        if character == ",":
            tokens.append(character)
        index += 1
    return tuple(tokens)


def _row_limits(sql: str) -> tuple[int | None, ...]:
    tokens = _tokens(sql)
    limits: list[int | None] = []
    for index, token in enumerate(tokens):
        if token != "LIMIT":
            continue
        if index + 1 >= len(tokens) or not tokens[index + 1].isdigit():
            limits.append(None)
            continue
        first = int(tokens[index + 1])
        if index + 2 < len(tokens) and tokens[index + 2] == ",":
            if index + 3 >= len(tokens) or not tokens[index + 3].isdigit():
                limits.append(None)
            else:
                limits.append(int(tokens[index + 3]))
        else:
            limits.append(first)
    return tuple(limits)


def assess_query_determinism(
    sql: str,
    proof: DeterministicOrderProof | None = None,
) -> QueryDeterminism:
    """Reject nonzero/unknown LIMIT unless the generator supplies a full proof."""

    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("sql must be a nonempty string")
    limits = _row_limits(sql)
    if not limits or all(limit == 0 for limit in limits):
        return QueryDeterminism(True, row_limits=limits)
    if proof is not None and proof.covers_all_row_limits:
        return QueryDeterminism(True, row_limits=limits)
    return QueryDeterminism(
        False,
        reason="nondeterministic_row_limit",
        row_limits=limits,
    )


__all__ = [
    "DeterministicOrderProof",
    "QueryDeterminism",
    "assess_query_determinism",
]
