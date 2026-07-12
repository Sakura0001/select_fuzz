"""Process-stable hashes, identifiers, and hierarchical seeds."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import blake2b, sha256
import math
import re
from typing import TypeAlias


SeedPart: TypeAlias = str | int | bytes
_NAMESPACE = re.compile(r"^[a-z][a-z0-9_]*$")


def _part_bytes(part: SeedPart) -> bytes:
    if isinstance(part, str):
        tag, payload = b"s", part.encode("utf-8")
    elif isinstance(part, int):
        tag, payload = b"i", str(part).encode("ascii")
    elif isinstance(part, bytes):
        tag, payload = b"b", part
    else:  # pragma: no cover - protected by static typing, retained for runtime callers
        raise TypeError(f"unsupported deterministic hash part: {type(part).__name__}")
    return tag + len(payload).to_bytes(8, "big") + payload


def _digest(namespace: str, parts: tuple[SeedPart, ...], *, size: int) -> bytes:
    if not _NAMESPACE.fullmatch(namespace):
        raise ValueError("namespace must match [a-z][a-z0-9_]*")
    digest = blake2b(digest_size=size)
    digest.update(b"select-fuzz\0")
    digest.update(_part_bytes(namespace))
    for part in parts:
        digest.update(_part_bytes(part))
    return digest.digest()


@dataclass(frozen=True, slots=True)
class SeedTree:
    """Derive independent deterministic seeds without shared RNG state."""

    root: int

    def derive(self, *path: SeedPart) -> int:
        return int.from_bytes(_digest("seed", (self.root, *path), size=16), "big")


def deterministic_id(namespace: str, *parts: SeedPart) -> str:
    """Return a readable namespace plus a collision-resistant stable digest."""

    return f"{namespace}_{_digest(namespace, parts, size=16).hex()}"


def _frame(tag: bytes, payload: bytes = b"") -> bytes:
    return tag + len(payload).to_bytes(8, "big") + payload


def _value_bytes(value: object) -> bytes:
    if value is None:
        return _frame(b"n")
    if isinstance(value, bool):
        return _frame(b"B", b"1" if value else b"0")
    if isinstance(value, int):
        return _frame(b"i", str(value).encode("ascii"))
    if isinstance(value, str):
        return _frame(b"s", value.encode("utf-8"))
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite floats cannot be fingerprinted")
        return _frame(b"f", value.hex().encode("ascii"))
    if isinstance(value, bytes):
        return _frame(b"b", value)
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("fingerprint mapping keys must be strings")
        items = b"".join(
            _value_bytes(key) + _value_bytes(value[key]) for key in sorted(value)
        )
        return _frame(b"m", len(value).to_bytes(8, "big") + items)
    if isinstance(value, list):
        items = b"".join(_value_bytes(item) for item in value)
        return _frame(b"l", len(value).to_bytes(8, "big") + items)
    if isinstance(value, tuple):
        items = b"".join(_value_bytes(item) for item in value)
        return _frame(b"t", len(value).to_bytes(8, "big") + items)
    raise TypeError(f"unsupported fingerprint value: {type(value).__name__}")


def stable_fingerprint(value: object) -> str:
    """Hash an injective typed encoding for the supported logical value domain."""

    return sha256(_value_bytes(value)).hexdigest()
