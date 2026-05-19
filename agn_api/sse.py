from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any
from uuid import uuid4


@dataclass
class SSEClient:
    client_id: str
    queue: asyncio.Queue[dict[str, Any]]


class SSEHub:
    def __init__(self) -> None:
        self._clients: dict[str, SSEClient] = {}
        self._lock = asyncio.Lock()
        self._event_seq = 0

    async def register(self) -> SSEClient:
        client = SSEClient(client_id=f"client-{uuid4().hex[:12]}", queue=asyncio.Queue(maxsize=256))
        async with self._lock:
            self._clients[client.client_id] = client
        return client

    async def unregister(self, client_id: str) -> None:
        async with self._lock:
            self._clients.pop(client_id, None)

    async def broadcast(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            self._event_seq += 1
            event_id = f"evt-{self._event_seq:08d}"
            event = {"event_id": event_id, **payload}
            clients = list(self._clients.values())

        for client in clients:
            self._safe_enqueue(client.queue, event)

        return event

    async def client_count(self) -> int:
        async with self._lock:
            return len(self._clients)

    @staticmethod
    def encode_event(*, event_name: str, data: dict[str, Any], event_id: str | None = None) -> str:
        parts: list[str] = []
        if event_id:
            parts.append(f"id: {event_id}")
        parts.append(f"event: {event_name}")
        parts.append(f"data: {json.dumps(data, ensure_ascii=True)}")
        return "\n".join(parts) + "\n\n"

    @staticmethod
    def ping_payload() -> dict[str, str]:
        return {"server_ts_utc": datetime.now(tz=timezone.utc).isoformat()}

    @staticmethod
    def _safe_enqueue(queue: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop if queue is still full.
                pass
