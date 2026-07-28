"""Stable contracts implemented by every independently packaged run mode."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Protocol

from select_fuzz.config import AppConfig
from select_fuzz.domain import RunRequest
from select_fuzz.service import RunSummary


class ModeRunner(Protocol):
    def run(self, request: RunRequest, stop_event: Event) -> RunSummary: ...


ModeFactory = Callable[[AppConfig, Path], ModeRunner]


@dataclass(frozen=True, slots=True)
class ModeDefinition:
    name: str
    label: str
    factory: ModeFactory

    def __post_init__(self) -> None:
        if not self.name or not self.label:
            raise ValueError("mode name and label must not be empty")


__all__ = ["ModeDefinition", "ModeFactory", "ModeRunner"]
