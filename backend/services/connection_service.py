"""Connection requests: the consent step before two accounts can talk.

Every transition here is guarded, because a connection is what unlocks contact
details. The rules are:

* you may only request a listing that is live and not your own
* only the receiver may accept or reject, and only while it is pending
* only an accepted connection carries messages or contact details
"""

import logging
import uuid
from pathlib import Path
from typing import Optional, Sequence

from fastapi import HTTPException, status
from core.config import settings
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
from services.image_moderator import inspect_live_image
from services.websocket_manager import ws_manager


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

    conn_loaded = await _reload(db, connection.id)

    # --- Notification: tell the RFQ owner ---
    try:
        from services import notification_service
        sender_user = await db.get(User, sender_id)
        sender_profile = getattr(sender_user, "profile", None) if sender_user else None
        sender_name = (
            (getattr(sender_profile, "company_name", None) or getattr(sender_profile, "name", None))
            if sender_profile else "Someone"
        ) or "Someone"
        await notification_service.notify_connection_request(
            db,
            receiver_id=rfq.user_id,
            sender_name=sender_name,
            rfq_title=rfq.title,
            connection_id=connection.id,
        )
        await db.commit()
    except Exception:
        logging.getLogger(__name__).debug("Notification create failed (non-fatal)", exc_info=True)

    return conn_loaded


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
    conn = await _decide(db, connection_id, user_id, ConnectionStatus.ACCEPTED)
    # --- Notification: tell the sender their request was accepted ---
    try:
        from services import notification_service
        accepter = await db.get(User, user_id)
        accepter_profile = getattr(accepter, "profile", None) if accepter else None
        accepter_name = (
            (getattr(accepter_profile, "company_name", None) or getattr(accepter_profile, "name", None))
            if accepter_profile else "Someone"
        ) or "Someone"
        await notification_service.notify_connection_accepted(
            db, sender_id=conn.sender_id, accepter_name=accepter_name, connection_id=connection_id
        )
        await db.commit()
    except Exception:
        logging.getLogger(__name__).debug("Notification accept failed (non-fatal)", exc_info=True)
    return conn


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
        connection_id=connection_id,
        sender_id=sender_id,
        content=payload.content,
        image_url=payload.image_url,
        is_live_capture=payload.is_live_capture,
    )
    db.add(message)
    await db.commit()
    await db.refresh(message)

    await ws_manager.broadcast(
        connection_id,
        "chat_message",
        {
            "id": str(message.id),
            "connection_id": str(message.connection_id),
            "sender_id": str(message.sender_id),
            "content": message.content,
            "image_url": message.image_url,
            "is_live_capture": message.is_live_capture,
            "created_at": message.created_at.isoformat(),
        },
    )

    # --- Notification: tell the counterparty ---
    try:
        from services import notification_service
        receiver_id = (
            connection.receiver_id if connection.sender_id == sender_id else connection.sender_id
        )
        sender_user = await db.get(User, sender_id)
        sender_profile = getattr(sender_user, "profile", None) if sender_user else None
        sender_name = (
            (getattr(sender_profile, "company_name", None) or getattr(sender_profile, "name", None))
            if sender_profile else "Someone"
        ) or "Someone"
        await notification_service.notify_new_message(
            db,
            receiver_id=receiver_id,
            sender_name=sender_name,
            connection_id=connection_id,
            preview=payload.content[:80] if payload.content else None,
        )
        await db.commit()
    except Exception:
        logging.getLogger(__name__).debug("Notification message failed (non-fatal)", exc_info=True)

    return message


async def send_live_capture_message(
    db: AsyncSession,
    connection_id: uuid.UUID,
    sender_id: uuid.UUID,
    file_bytes: bytes,
    content_type: str,
    caption: Optional[str] = None,
) -> ConnectionMessage:
    connection = await get_connection(db, connection_id, sender_id)
    if connection.status is not ConnectionStatus.ACCEPTED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The connection must be accepted before either side can send messages",
        )

    # Validate mime type
    allowed_types = {"image/jpeg", "image/png", "image/webp", "image/jpg"}
    if content_type.lower() not in allowed_types:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Invalid image format '{content_type}'. Must be JPEG, PNG, or WebP.",
        )

    # Validate file size (max 10MB)
    if len(file_bytes) > 10 * 1024 * 1024:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Image size exceeds maximum limit of 10MB.",
        )

    # AI Content Safety Moderation using Ollama qwen3-vl:8b
    is_safe, warning_msg, _ = await inspect_live_image(file_bytes, content_type)
    if not is_safe:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            warning_msg
            or "⚠️ Content Warning: Image flagged for vulgar, violent, or sexual content. Upload rejected.",
        )

    # Save to disk under UPLOAD_DIR/live_captures
    upload_dir = Path(settings.UPLOAD_DIR) / "live_captures"
    upload_dir.mkdir(parents=True, exist_ok=True)

    ext = ".jpg"
    if "png" in content_type.lower():
        ext = ".png"
    elif "webp" in content_type.lower():
        ext = ".webp"

    file_id = f"live_{uuid.uuid4().hex}{ext}"
    dest_path = upload_dir / file_id
    dest_path.write_bytes(file_bytes)

    image_url = f"/api/v1/media/live_captures/{file_id}"
    message_content = caption.strip() if caption and caption.strip() else "📸 Live Camera Snapshot"

    message = ConnectionMessage(
        connection_id=connection_id,
        sender_id=sender_id,
        content=message_content,
        image_url=image_url,
        is_live_capture=True,
    )
    db.add(message)
    await db.commit()
    await db.refresh(message)

    await ws_manager.broadcast(
        connection_id,
        "chat_message",
        {
            "id": str(message.id),
            "connection_id": str(message.connection_id),
            "sender_id": str(message.sender_id),
            "content": message.content,
            "image_url": message.image_url,
            "is_live_capture": True,
            "created_at": message.created_at.isoformat(),
        },
    )

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
