"""
Tiny in-memory async pub/sub bus.

The engine publishes state snapshots and discrete events (buys, sells, log lines);
the dashboard's WebSocket handler subscribes. Keeps the engine and the web server
fully decoupled — the engine doesn't know or care who's listening.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any


class EventBus:
    def __init__(self, history: int = 200):
        self._subscribers: set[asyncio.Queue] = set()
        self._log: deque = deque(maxlen=history)
        self._last_state: dict | None = None

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _emit(self, message: dict) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                # Slow consumer — drop the oldest by draining one.
                try:
                    q.get_nowait()
                    q.put_nowait(message)
                except Exception:  # noqa: BLE001
                    pass

    def publish_state(self, state: dict) -> None:
        self._last_state = state
        self._emit({"type": "state", "data": state})

    def publish_event(self, kind: str, payload: dict) -> None:
        payload = {**payload, "ts": time.time(), "kind": kind}
        self._emit({"type": "event", "data": payload})

    def log(self, level: str, text: str) -> None:
        entry = {"ts": time.time(), "level": level, "text": text}
        self._log.append(entry)
        self._emit({"type": "log", "data": entry})

    def recent_log(self) -> list:
        return list(self._log)

    def last_state(self) -> dict | None:
        return self._last_state


# Process-wide singleton.
bus = EventBus()
