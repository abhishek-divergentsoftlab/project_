"""Connection requests and the messages that follow an accepted one."""

import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Query, status

from api.deps import CurrentUser, DbSession
from models.enums import ConnectionStatus
from schemas.connection import (
    ConnectionCreate,
    ConnectionMessageCreate,
    ConnectionMessageOut,
    ConnectionOut,
)
from services import connection_service

router = APIRouter(prefix="/connections", tags=["connections"])


@router.post("", response_model=ConnectionOut, status_code=status.HTTP_201_CREATED)
async def create_connection(
    payload: ConnectionCreate, current_user: CurrentUser, db: DbSession
) -> ConnectionOut:
    """Ask the owner of a listing to be put in touch.

    Idempotent: asking twice returns the request you already have rather than
    creating a second one.
    """
    connection = await connection_service.create_connection(
        db, current_user.id, payload.rfq_id
    )
    return connection_service.to_out(connection, current_user.id)


@router.get("", response_model=list[ConnectionOut])
async def list_connections(
    current_user: CurrentUser,
    db: DbSession,
    status_filter: Annotated[Optional[ConnectionStatus], Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ConnectionOut]:
    """Every request this account has sent or received."""
    rows = await connection_service.list_user_connections(
        db, current_user.id, status_filter=status_filter, limit=limit, offset=offset
    )
    return [connection_service.to_out(row, current_user.id) for row in rows]


@router.get("/{connection_id}", response_model=ConnectionOut)
async def get_connection(
    connection_id: uuid.UUID, current_user: CurrentUser, db: DbSession
) -> ConnectionOut:
    connection = await connection_service.get_connection(
        db, connection_id, current_user.id
    )
    return connection_service.to_out(connection, current_user.id)


@router.post("/{connection_id}/accept", response_model=ConnectionOut)
async def accept_connection(
    connection_id: uuid.UUID, current_user: CurrentUser, db: DbSession
) -> ConnectionOut:
    """Agree to be contacted. This is what reveals your email and phone."""
    connection = await connection_service.accept_connection(
        db, connection_id, current_user.id
    )
    return connection_service.to_out(connection, current_user.id)


@router.post("/{connection_id}/reject", response_model=ConnectionOut)
async def reject_connection(
    connection_id: uuid.UUID, current_user: CurrentUser, db: DbSession
) -> ConnectionOut:
    connection = await connection_service.reject_connection(
        db, connection_id, current_user.id
    )
    return connection_service.to_out(connection, current_user.id)


@router.get("/{connection_id}/messages", response_model=list[ConnectionMessageOut])
async def list_messages(
    connection_id: uuid.UUID, current_user: CurrentUser, db: DbSession
) -> list[ConnectionMessageOut]:
    messages = await connection_service.list_messages(
        db, connection_id, current_user.id
    )
    return [ConnectionMessageOut.model_validate(m) for m in messages]


@router.post(
    "/{connection_id}/messages",
    response_model=ConnectionMessageOut,
    status_code=status.HTTP_201_CREATED,
)
async def send_message(
    connection_id: uuid.UUID,
    payload: ConnectionMessageCreate,
    current_user: CurrentUser,
    db: DbSession,
) -> ConnectionMessageOut:
    message = await connection_service.send_message(
        db, connection_id, current_user.id, payload
    )
    return ConnectionMessageOut.model_validate(message)
