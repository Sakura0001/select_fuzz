"""Persisted monotonic events and atomically resumable bounded SSE fan-out."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import AsyncIterator


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    sequence: int
    kind: str
    payload: dict[str, object]


class EventHistoryExpired(ValueError):
    pass


@dataclass(eq=False, slots=True)
class _Subscriber:
    queue: asyncio.Queue[EventEnvelope]
    loop: asyncio.AbstractEventLoop
    dropped: bool = False


class EventSubscription:
    def __init__(
        self,
        broker: EventBroker,
        subscriber: _Subscriber,
        replay: tuple[EventEnvelope, ...],
    ) -> None:
        self._broker = broker
        self._subscriber = subscriber
        self._replay = iter(replay)
        self._closed = False

    def __aiter__(self) -> EventSubscription:
        return self

    async def __anext__(self) -> EventEnvelope | None:
        if self._closed:
            raise StopAsyncIteration
        try:
            return next(self._replay)
        except StopIteration:
            pass
        if self._subscriber.dropped and self._subscriber.queue.empty():
            await self.aclose()
            raise StopAsyncIteration
        try:
            event = await asyncio.wait_for(
                self._subscriber.queue.get(), timeout=self._broker.heartbeat_seconds
            )
        except TimeoutError:
            return None
        if self._subscriber.dropped:
            await self.aclose()
        return event

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            self._broker._remove(self._subscriber)


class EventBroker:
    def __init__(
        self,
        state_path: str | Path | None = None,
        *,
        history_limit: int = 1000,
        queue_size: int = 128,
        heartbeat_seconds: float = 15,
    ) -> None:
        if history_limit < 1 or queue_size < 1 or heartbeat_seconds <= 0:
            raise ValueError("event broker limits must be positive")
        self._path = None if state_path is None else Path(state_path)
        if self._path is None:
            self._connection = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(self._path, check_same_thread=False)
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS api_event (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload_json TEXT NOT NULL)"""
        )
        self._connection.commit()
        self._history_limit = history_limit
        self._queue_size = queue_size
        self.heartbeat_seconds = heartbeat_seconds
        self._subscribers: set[_Subscriber] = set()
        self._lock = RLock()

    @property
    def sequence(self) -> int:
        with self._lock:
            row = self._connection.execute("SELECT COALESCE(MAX(sequence),0) FROM api_event").fetchone()
            assert row is not None
            return int(row[0])

    def publish(self, kind: str, payload: dict[str, object]) -> EventEnvelope:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self._lock:
            cursor = self._connection.execute(
                "INSERT INTO api_event(kind,payload_json) VALUES (?,?)", (kind, encoded)
            )
            if cursor.lastrowid is None:  # pragma: no cover - SQLite INSERT contract
                raise RuntimeError("event sequence was not allocated")
            sequence = int(cursor.lastrowid)
            cutoff = max(0, sequence - self._history_limit)
            self._connection.execute("DELETE FROM api_event WHERE sequence<=?", (cutoff,))
            self._connection.commit()
            event = EventEnvelope(sequence, kind, dict(payload))
            subscribers = tuple(self._subscribers)
            for subscriber in subscribers:
                def deliver(target: _Subscriber = subscriber) -> None:
                    if target.dropped:
                        return
                    try:
                        target.queue.put_nowait(event)
                    except asyncio.QueueFull:
                        target.dropped = True

                subscriber.loop.call_soon_threadsafe(deliver)
            return event

    def replay(self, after: int) -> tuple[EventEnvelope, ...]:
        with self._lock:
            first = self._connection.execute("SELECT MIN(sequence) FROM api_event").fetchone()
            if first is not None and first[0] is not None and after < int(first[0]) - 1:
                raise EventHistoryExpired("requested event history has expired")
            rows = self._connection.execute(
                "SELECT sequence,kind,payload_json FROM api_event WHERE sequence>? ORDER BY sequence",
                (after,),
            ).fetchall()
            return tuple(
                EventEnvelope(int(row[0]), str(row[1]), json.loads(row[2])) for row in rows
            )

    def open_subscription(self, after: int) -> EventSubscription:
        """Register before releasing the replay lock, closing replay/live race windows."""
        loop = asyncio.get_running_loop()
        with self._lock:
            replay = self.replay(after)
            subscriber = _Subscriber(asyncio.Queue(self._queue_size), loop)
            self._subscribers.add(subscriber)
        return EventSubscription(self, subscriber, replay)

    async def subscribe(self, after: int) -> AsyncIterator[EventEnvelope | None]:
        subscription = self.open_subscription(after)
        try:
            async for event in subscription:
                yield event
        finally:
            await subscription.aclose()

    def _remove(self, subscriber: _Subscriber) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)


def encode_sse(event: EventEnvelope) -> bytes:
    data = json.dumps(event.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"id: {event.sequence}\nevent: {event.kind}\ndata: {data}\n\n".encode()
