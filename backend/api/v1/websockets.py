"""WebSocket endpoints for real-time messaging, typing indicators, and deal room events."""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select

from core.security import TokenError, decode_token
from db.session import SessionLocal
from models.connection import Connection

from models.user import User
from services.websocket_manager import ws_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websockets"])


@router.websocket("/ws/connections/{connection_id}")
async def connection_websocket(
    websocket: WebSocket,
    connection_id: uuid.UUID,
    token: Annotated[str, Query()],
) -> None:
    """Real-time bi-directional channel for an active connection thread.

    Authenticates via JWT token query param and ensures caller is an authorized
    participant of the connection before establishing the connection pool.
    """
    try:
        payload = decode_token(token, expected_type="access")
        user_id = uuid.UUID(payload["sub"])
    except (TokenError, ValueError, KeyError):
        logger.warning("WebSocket rejected: invalid or expired access token")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    async with SessionLocal() as db:
        user = await db.get(User, user_id)

        if user is None or not user.is_active:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        conn = await db.get(Connection, connection_id)
        if conn is None or user_id not in (conn.sender_id, conn.receiver_id):
            logger.warning("WebSocket rejected: user %s not in connection %s", user_id, connection_id)
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

    await ws_manager.connect(connection_id, websocket)

    try:
        while True:
            data = await websocket.receive_json()
            event_type = data.get("type")

            if event_type == "ping":
                await websocket.send_json({"type": "pong"})
            elif event_type == "typing":
                # Broadcast typing indicator to counterparty only
                await ws_manager.broadcast(
                    connection_id,
                    "typing",
                    {"sender_id": str(user_id)},
                    exclude=websocket,
                )
    except WebSocketDisconnect:
        await ws_manager.disconnect(connection_id, websocket)
    except Exception as exc:  # noqa: BLE001
        logger.warning("WebSocket error for connection %s: %s", connection_id, exc)
        await ws_manager.disconnect(connection_id, websocket)
