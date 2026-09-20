"""In-memory WebSocket connection manager for real-time messaging and deal updates."""

import asyncio
import logging
import uuid
from typing import Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WebSocketManager:
    """Manages active WebSockets keyed by connection_id.

    Designed for sub-millisecond local event distribution between counterparties.
    In a distributed multi-node production setup, this can be backed by Redis Pub/Sub.
    """

    def __init__(self) -> None:
        # connection_id -> set of active WebSockets
        self._active: dict[uuid.UUID, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, connection_id: uuid.UUID, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            if connection_id not in self._active:
                self._active[connection_id] = set()
            self._active[connection_id].add(websocket)
        logger.info("WebSocket connected for connection %s (active: %d)", connection_id, len(self._active[connection_id]))

    async def disconnect(self, connection_id: uuid.UUID, websocket: WebSocket) -> None:
        async with self._lock:
            if connection_id in self._active:
                self._active[connection_id].discard(websocket)
                if not self._active[connection_id]:
                    del self._active[connection_id]
        logger.info("WebSocket disconnected for connection %s", connection_id)

    async def broadcast(
        self,
        connection_id: uuid.UUID,
        event_type: str,
        data: dict,
        exclude: Optional[WebSocket] = None,
    ) -> None:
        payload = {"type": event_type, "data": data}
        async with self._lock:
            sockets = list(self._active.get(connection_id, set()))

        dead_sockets: list[WebSocket] = []
        for ws in sockets:
            if ws is exclude:
                continue
            try:
                await ws.send_json(payload)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to send to WebSocket in connection %s: %s", connection_id, exc)
                dead_sockets.append(ws)

        if dead_sockets:
            async with self._lock:
                if connection_id in self._active:
                    for dead in dead_sockets:
                        self._active[connection_id].discard(dead)


ws_manager = WebSocketManager()
