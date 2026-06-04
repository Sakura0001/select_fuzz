from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class SqlFragment:
    text: str

    def render(self) -> str:
        return self.text


def join_sql(parts: Iterable[str], separator: str = " ") -> str:
    return separator.join(part for part in parts if part)
