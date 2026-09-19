"""Connection requests: the consent step before two accounts can talk.

Every transition here is guarded, because a connection is what unlocks contact
details. The rules are:

* you may only request a listing that is live and not your own
* only the receiver may accept or reject, and only while it is pending
* only an accepted connection carries messages or contact details
"""

import uuid
from typing import Optional, Sequence

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.connection import Connection, ConnectionMessage
from models.enums import ConnectionStatus, RFQStatus
from models.rfq import RFQ
from models.user import User
from schemas.connection import ConnectionMessageCreate, ConnectionOut
from schemas.match import Counterparty

# Everything a connection row needs in order to be rendered without a second
# round trip: the listing, and both parties' profiles.
_FULL = (
    selectinload(Connection.rfq).selectinload(RFQ.user).selectinload(User.profile),
    selectinload(Connection.sender).selectinload(User.profile),
    selectinload(Connection.receiver).selectinload(User.profile),
)


async def _reload(db: AsyncSession, connection_id: uuid.UUID) -> Connection:
    """Re-read one connection with all its relationships loaded."""
    row = await db.scalar(
        select(Connection).options(*_FULL).where(Connection.id == connection_id)
    )
    if row is None:  # pragma: no cover -- only reachable if deleted mid-request
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found")
    return row


def to_out(connection: Connection, viewer_id: uuid.UUID) -> ConnectionOut:
    """Render a connection from one participant's point of view."""
    sent = connection.sender_id == viewer_id
    other = connection.receiver if sent else connection.sender
    profile = other.profile if other else None
    accepted = connection.status is ConnectionStatus.ACCEPTED

    counterparty = Counterparty(
        company_name=profile.company_name if profile else None,
        city=profile.city if profile else None,
        state=profile.state if profile else None,
        country=profile.country if profile else None,
        member_since=other.created_at if other else None,
        connection_id=connection.id,
        connection_status=connection.status,
        contact_name=(profile.name if profile else None) if accepted else None,
        email=(other.email if other else None) if accepted else None,
        phone=(profile.phone if profile else None) if accepted else None,
        address=(profile.address if profile else None) if accepted else None,
    )

    messages = connection.messages or []
    return ConnectionOut(
        id=connection.id,
        rfq_id=connection.rfq_id,
        sender_id=connection.sender_id,
        receiver_id=connection.receiver_id,
        status=connection.status,
        created_at=connection.created_at,
        updated_at=connection.updated_at,
        direction="sent" if sent else "received",
        rfq_title=connection.rfq.title if connection.rfq else None,
        rfq_role=connection.rfq.role if connection.rfq else None,
        counterparty=counterparty,
        message_count=len(messages),
        last_message_at=messages[-1].created_at if messages else None,
    )


async def create_connection(
    db: AsyncSession, sender_id: uuid.UUID, rfq_id: uuid.UUID
) -> Connection:
    rfq = await db.get(RFQ, rfq_id)
    if rfq is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "RFQ not found")

    if rfq.user_id == sender_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Cannot connect to your own RFQ"
        )

    # A closed, draft or expired listing is not open for business. Without this
    # check a stale match page could raise a request against a listing its owner
    # had already withdrawn.
    if not rfq.is_matchable:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This listing is no longer accepting connection requests",
        )

    existing = await db.scalar(
        select(Connection).where(
            Connection.sender_id == sender_id, Connection.rfq_id == rfq_id
        )
    )
    if existing is not None:
        if existing.status is ConnectionStatus.REJECTED:
            # Silently handing back the rejected row made the button look like
            # it had worked. Say what happened instead.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This request was declined; you cannot ask again",
            )
        return await _reload(db, existing.id)

    connection = Connection(
        sender_id=sender_id,
        receiver_id=rfq.user_id,
        rfq_id=rfq_id,
        status=ConnectionStatus.PENDING,
    )
    db.add(connection)
    try:
        await db.commit()
    except IntegrityError:
        # Two clicks in flight at once. The unique index is the real guard; this
        # turns the race into the same answer the second caller would have got.
        await db.rollback()
        duplicate = await db.scalar(
            select(Connection).where(
                Connection.sender_id == sender_id, Connection.rfq_id == rfq_id
            )
        )
        if duplicate is None:  # pragma: no cover -- not a uniqueness violation
            raise
        return await _reload(db, duplicate.id)

    return await _reload(db, connection.id)


async def get_connection(
    db: AsyncSession, connection_id: uuid.UUID, user_id: uuid.UUID
) -> Connection:
    connection = await db.scalar(
        select(Connection).options(*_FULL).where(Connection.id == connection_id)
    )
    if connection is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found")
    if user_id not in (connection.sender_id, connection.receiver_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not authorized")
    return connection


async def _decide(
    db: AsyncSession,
    connection_id: uuid.UUID,
    user_id: uuid.UUID,
    outcome: ConnectionStatus,
) -> Connection:
    connection = await get_connection(db, connection_id, user_id)

    if connection.receiver_id != user_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only the person who received the request can answer it",
        )
    if connection.status is not ConnectionStatus.PENDING:
        # Accepting an already-rejected request used to be allowed, which let a
        # receiver undo their own refusal and unlock contact details after the
        # fact.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This request has already been {connection.status.value}",
        )

    connection.status = outcome
    await db.commit()
    return await _reload(db, connection_id)


async def accept_connection(
    db: AsyncSession, connection_id: uuid.UUID, user_id: uuid.UUID
) -> Connection:
    return await _decide(db, connection_id, user_id, ConnectionStatus.ACCEPTED)


async def reject_connection(
    db: AsyncSession, connection_id: uuid.UUID, user_id: uuid.UUID
) -> Connection:
    return await _decide(db, connection_id, user_id, ConnectionStatus.REJECTED)


async def send_message(
    db: AsyncSession,
    connection_id: uuid.UUID,
    sender_id: uuid.UUID,
    payload: ConnectionMessageCreate,
) -> ConnectionMessage:
    connection = await get_connection(db, connection_id, sender_id)
    if connection.status is not ConnectionStatus.ACCEPTED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The connection must be accepted before either side can send messages",
        )

    message = ConnectionMessage(
        connection_id=connection_id, sender_id=sender_id, content=payload.content
    )
    db.add(message)
    await db.commit()
    await db.refresh(message)
    return message


async def list_messages(
    db: AsyncSession, connection_id: uuid.UUID, user_id: uuid.UUID
) -> Sequence[ConnectionMessage]:
    await get_connection(db, connection_id, user_id)
    return (
        await db.scalars(
            select(ConnectionMessage)
            .where(ConnectionMessage.connection_id == connection_id)
            .order_by(ConnectionMessage.created_at.asc())
        )
    ).all()


async def list_connections_for_rfq(
    db: AsyncSession, rfq_id: uuid.UUID, user_id: uuid.UUID
) -> Sequence[Connection]:
    rfq = await db.get(RFQ, rfq_id)
    if rfq is None or rfq.user_id != user_id:
        # 404 rather than 403: confirming the id exists tells a stranger that
        # somebody else owns it.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "RFQ not found")

    return (
        await db.scalars(
            select(Connection)
            .options(*_FULL, selectinload(Connection.messages))
            .where(Connection.rfq_id == rfq_id)
            .order_by(Connection.created_at.desc())
        )
    ).all()


async def list_user_connections(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    status_filter: Optional[ConnectionStatus] = None,
    limit: int = 100,
    offset: int = 0,
) -> Sequence[Connection]:
    """Every thread this account is a party to, newest activity first."""
    query = (
        select(Connection)
        .options(*_FULL, selectinload(Connection.messages))
        .where(or_(Connection.sender_id == user_id, Connection.receiver_id == user_id))
    )
    if status_filter is not None:
        query = query.where(Connection.status == status_filter)

    return (
        await db.scalars(
            query.order_by(Connection.updated_at.desc()).limit(limit).offset(offset)
        )
    ).all()


async def get_rfq_pending_counts(
    db: AsyncSession, user_id: uuid.UUID
) -> dict[uuid.UUID, int]:
    """Pending requests per RFQ, for the badge on the listing card."""
    rows = await db.execute(
        select(Connection.rfq_id, func.count(Connection.id))
        .where(
            Connection.receiver_id == user_id,
            Connection.status == ConnectionStatus.PENDING,
        )
        .group_by(Connection.rfq_id)
    )
    return {rfq_id: count for rfq_id, count in rows.all()}
