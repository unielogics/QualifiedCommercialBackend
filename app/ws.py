"""WebSocket manager — per-deal channels for real-time updates."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from fastapi import WebSocket


class DealChannel:
    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)

    async def connect(self, deal_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self._connections[deal_id].add(ws)

    def disconnect(self, deal_id: str, ws: WebSocket) -> None:
        self._connections[deal_id].discard(ws)
        if not self._connections[deal_id]:
            del self._connections[deal_id]

    async def broadcast(self, deal_id: str, payload: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in list(self._connections.get(deal_id, set())):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(deal_id, ws)


channel = DealChannel()
