"""Durable replay jobs over the production ReplayService boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from select_fuzz.api.contracts import ReplayView
from select_fuzz.api.run_state import RunStore


class ReplayExecutor(Protocol):
    async def execute(self, case_id: str) -> dict[str, object]: ...


def _json_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return type(value).__name__


class ProductionReplayExecutor:
    """Run ReplayService.replay off the API event loop."""

    def __init__(self, service: object) -> None:
        replay = getattr(service, "replay", None)
        if not callable(replay):
            raise TypeError("replay service must provide replay(reference)")
        self._service = service

    async def execute(self, case_id: str) -> dict[str, object]:
        result = await asyncio.to_thread(self._service.replay, case_id)  # type: ignore[attr-defined]
        payload = _json_value(result)
        if not isinstance(payload, dict):
            raise TypeError("ReplayService returned a non-object result")
        return payload


class ReplayJobRunner:
    def __init__(self, store: RunStore, executor: ReplayExecutor) -> None:
        self._store = store
        self._executor = executor
        self._locks: dict[str, asyncio.Lock] = {}

    async def run(self, replay_id: str) -> ReplayView:
        lock = self._locks.setdefault(replay_id, asyncio.Lock())
        async with lock:
            current = self._store.get_replay(replay_id)
            if current is None:
                raise KeyError(replay_id)
            if current.state != "queued":
                return current
            self._store.set_replay(replay_id, "running")
            try:
                result = await self._executor.execute(current.case_id)
                raw_status = result.get("status")
                state = raw_status if raw_status in {"reproduced", "not_reproduced"} else "failed"
                finished = self._store.set_replay(replay_id, str(state), result)
            except Exception:
                finished = self._store.set_replay(
                    replay_id, "failed", {"error_type": "ReplayExecutionError"}
                )
            assert finished is not None
            return finished
