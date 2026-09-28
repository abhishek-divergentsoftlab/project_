"""WebSocket endpoints for real-time messaging, typing indicators, and deal room events.

Authentication (in order of preference)
---------------------------------------
1. ``Sec-WebSocket-Protocol: bearer, <access token>`` -- browsers send this
   with ``new WebSocket(url, ["bearer", token])``. The server answers with the
   ``bearer`` subprotocol. Keeps the token out of URLs and access logs.
2. First message ``{"type": "auth", "token": "<access token>"}`` sent within
   ``AUTH_MESSAGE_TIMEOUT`` seconds of opening a socket that had no token.
3. ``?token=<access token>`` query parameter -- still accepted because the
   current frontend uses it, but it ends up in proxy/server logs; migrate off.

A socket is only registered for broadcasts after the token is valid, the user
is active and a party of the connection, and the connection is not REJECTED.
"""

import asyncio
import logging
import uuid
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from core.security import TokenError, decode_token
from db.session import SessionLocal
from models.connection import Connection
from models.enums import ConnectionStatus
from models.user import User
from services.websocket_manager import ws_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websockets"])

AUTH_MESSAGE_TIMEOUT = 10.0
BEARER_SUBPROTOCOL = "bearer"


class _AcceptOnce:
    """Wraps a WebSocket so ``accept()`` is idempotent and uses our subprotocol.

    ``ws_manager.connect`` always calls ``accept()``; the first-message flow
    has to accept earlier, and the subprotocol flow has to echo ``bearer``.
    Everything else is delegated, and the same wrapper object is used for
    connect / broadcast(exclude=...) / disconnect so identity checks hold.
    """

    def __init__(self, websocket: Any, subprotocol: Optional[str] = None) -> None:
        self._ws = websocket
        self._subprotocol = subprotocol
        self._accepted = False

    async def accept(self) -> None:
        if self._accepted:
            return
        self._accepted = True
        if self._subprotocol:
            await self._ws.accept(subprotocol=self._subprotocol)
        else:
            await self._ws.accept()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ws, name)

    def __hash__(self) -> int:
        return id(self)


def _token_from_subprotocol(websocket: Any) -> Optional[str]:
    headers = getattr(websocket, "headers", None)
    if not headers:
        return None
    raw = headers.get("sec-websocket-protocol")
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) >= 2 and parts[0].lower() == BEARER_SUBPROTOCOL:
        return parts[1]
    return None


def _user_id_from(token: Optional[str]) -> Optional[uuid.UUID]:
    if not token:
        return None
    try:
        payload = decode_token(token, expected_type="access")
        return uuid.UUID(payload["sub"])
    except (TokenError, ValueError, KeyError):
        return None


@router.websocket("/ws/connections/{connection_id}")
async def connection_websocket(
    websocket: WebSocket,
    connection_id: uuid.UUID,
    token: Annotated[Optional[str], Query()] = None,
) -> None:
    """Real-time bi-directional channel for an active connection thread."""
    protocol_token = _token_from_subprotocol(websocket)
    socket = _AcceptOnce(websocket, BEARER_SUBPROTOCOL if protocol_token else None)

    supplied = protocol_token or token
    if not supplied:
        # Token-in-first-message flow: open the socket, then wait for {"type":"auth"}.
        await socket.accept()
        try:
            first = await asyncio.wait_for(websocket.receive_json(), timeout=AUTH_MESSAGE_TIMEOUT)
        except (asyncio.TimeoutError, WebSocketDisconnect, ValueError, KeyError, RuntimeError):
            first = None
        if isinstance(first, dict) and first.get("type") == "auth":
            supplied = first.get("token")
        if not isinstance(supplied, str):
            supplied = None

    user_id = _user_id_from(supplied)
    if user_id is None:
        logger.warning("WebSocket rejected: missing, invalid or expired access token")
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

        if conn.status is ConnectionStatus.REJECTED:
            logger.info("WebSocket rejected: connection %s was rejected", connection_id)
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

    await ws_manager.connect(connection_id, socket)

    try:
        while True:
            data = await websocket.receive_json()
            event_type = data.get("type") if isinstance(data, dict) else None

            if event_type == "ping":
                await websocket.send_json({"type": "pong"})
            elif event_type == "auth":
                continue  # a client that always sends auth-first is harmless
            elif event_type == "typing":
                # Broadcast typing indicator to counterparty only
                await ws_manager.broadcast(
                    connection_id,
                    "typing",
                    {"sender_id": str(user_id)},
                    exclude=socket,
                )
    except WebSocketDisconnect:
        await ws_manager.disconnect(connection_id, socket)
    except Exception as exc:  # noqa: BLE001
        logger.warning("WebSocket error for connection %s: %s", connection_id, exc)
        await ws_manager.disconnect(connection_id, socket)
