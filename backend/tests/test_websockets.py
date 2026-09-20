"""Tests for WebSocket real-time connection manager and endpoints."""

import uuid
import pytest
from fastapi import WebSocketDisconnect

from api.v1.websockets import connection_websocket
from core.security import create_access_token
from services.websocket_manager import WebSocketManager, ws_manager


class FakeWebSocket:
    def __init__(self, incoming: list | None = None):
        self.accepted = False
        self.closed_code = None
        self.sent = []
        self.incoming = list(incoming or [])

    async def accept(self):
        self.accepted = True

    async def close(self, code: int = 1000):
        self.closed_code = code

    async def send_json(self, data):
        self.sent.append(data)

    async def receive_json(self):
        if self.incoming:
            return self.incoming.pop(0)
        raise WebSocketDisconnect()


def test_websocket_manager_broadcast():
    """Verify WebSocketManager handles message broadcasting and dead socket cleanup."""
    import asyncio

    manager = WebSocketManager()
    conn_id = uuid.uuid4()

    class FakeSocket:
        def __init__(self, fail: bool = False):
            self.fail = fail
            self.received = []

        async def send_json(self, data):
            if self.fail:
                raise RuntimeError("Connection closed")
            self.received.append(data)

    s1 = FakeSocket(fail=False)
    s2 = FakeSocket(fail=False)
    s3 = FakeSocket(fail=True)  # Should be cleanly pruned

    async def run_test():
        manager._active[conn_id] = {s1, s2, s3}

        # Broadcast event
        await manager.broadcast(conn_id, "test_event", {"message": "hello"})

        assert len(s1.received) == 1
        assert s1.received[0]["type"] == "test_event"
        assert len(s2.received) == 1
        # Dead socket was pruned
        assert s3 not in manager._active[conn_id]
        assert len(manager._active[conn_id]) == 2

    asyncio.run(run_test())


async def test_websocket_rejects_invalid_token():
    """WebSocket closes with 1008 policy violation when token is invalid."""
    ws = FakeWebSocket()
    fake_conn = uuid.uuid4()

    await connection_websocket(ws, fake_conn, token="invalid-token")
    assert ws.accepted is False
    assert ws.closed_code == 1008


async def test_websocket_rejects_stranger(accepted_pair, make_actor):
    """A user who is not a participant in the connection is rejected."""
    _buyer, _seller, conn_id, _listing = accepted_pair
    stranger = await make_actor("buyer")

    token = create_access_token(stranger.id)
    ws = FakeWebSocket()

    await connection_websocket(ws, uuid.UUID(conn_id), token=token)
    assert ws.accepted is False
    assert ws.closed_code == 1008


async def test_websocket_connected_ping_pong_and_typing(accepted_pair):
    """An authorized user connects, exchanges ping-pong, and handles typing events."""
    buyer, _seller, conn_id, _listing = accepted_pair
    token = create_access_token(buyer.id)

    # Prepare client sending a ping and typing event
    ws = FakeWebSocket(incoming=[{"type": "ping"}, {"type": "typing"}])

    await connection_websocket(ws, uuid.UUID(conn_id), token=token)

    assert ws.accepted is True
    assert len(ws.sent) == 1
    assert ws.sent[0] == {"type": "pong"}
