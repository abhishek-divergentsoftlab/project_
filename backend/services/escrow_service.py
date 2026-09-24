"""Service layer for Escrow Accounts, Milestone Payments, and Formal Deal Disputes."""

from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Optional
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.connection import Connection, ConnectionMessage
from models.enums import QuotationStatus, RFQRole
from models.escrow import DealDispute, EscrowAccount, EscrowMilestone
from models.quotation import Quotation
from models.rfq import RFQ
from schemas.escrow import (
    DealDisputeCreatePayload,
    DealDisputeResolvePayload,
    EscrowDepositPayload,
    MilestoneReleaseApprovePayload,
    MilestoneReleaseRequestPayload,
)
from services.websocket_manager import ws_manager

logger = logging.getLogger(__name__)


async def get_or_create_escrow_for_connection(
    db: AsyncSession,
    connection_id: uuid.UUID,
    user_id: uuid.UUID,
) -> EscrowAccount:
    """Fetch or initialize the Escrow Account and milestone tranches for an accepted quotation."""
    conn = await db.get(Connection, connection_id)
    if not conn:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deal Room connection not found",
        )

    if user_id not in (conn.sender_id, conn.receiver_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not an authorized participant in this Deal Room",
        )

    # 1. Check if escrow already exists for this connection
    stmt = (
        select(EscrowAccount)
        .where(EscrowAccount.connection_id == connection_id)
        .options(
            selectinload(EscrowAccount.milestones),
            selectinload(EscrowAccount.disputes),
        )
        .order_by(EscrowAccount.created_at.desc())
    )
    res = await db.execute(stmt)
    existing_escrow = res.scalars().first()
    if existing_escrow:
        return existing_escrow

    # 2. Find latest accepted quotation for this connection
    q_stmt = (
        select(Quotation)
        .where(
            Quotation.connection_id == connection_id,
            Quotation.status.in_([
                QuotationStatus.ACCEPTED,
                QuotationStatus.DELIVERED,
                QuotationStatus.RECEIVED,
                QuotationStatus.COMPLETED,
            ]),
        )
        .order_by(Quotation.created_at.desc())
    )
    q_res = await db.execute(q_stmt)
    quote = q_res.scalars().first()
    if not quote:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No accepted quotation found in this Deal Room. Accept a quotation first to establish escrow.",
        )

    # 3. Resolve Buyer vs Seller roles
    rfq = await db.get(RFQ, quote.rfq_id)
    if not rfq:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Referenced RFQ listing not found",
        )

    if rfq.role == RFQRole.BUYER:
        buyer_id = rfq.user_id
        seller_id = conn.receiver_id if conn.sender_id == buyer_id else conn.sender_id
    else:
        seller_id = rfq.user_id
        buyer_id = conn.receiver_id if conn.sender_id == seller_id else conn.sender_id

    total = Decimal(str(quote.total_amount))
    m1_amt = Decimal(str(round(total * Decimal("0.30"), 2)))
    m2_amt = Decimal(str(round(total * Decimal("0.40"), 2)))
    m3_amt = total - (m1_amt + m2_amt)

    escrow = EscrowAccount(
        connection_id=connection_id,
        quotation_id=quote.id,
        buyer_id=buyer_id,
        seller_id=seller_id,
        currency=quote.currency,
        total_amount=total,
        funded_amount=Decimal("0.00"),
        released_amount=Decimal("0.00"),
        refunded_amount=Decimal("0.00"),
        status="pending_deposit",
    )
    db.add(escrow)
    await db.flush()

    # Create 3 default milestones
    milestones = [
        EscrowMilestone(
            escrow_account_id=escrow.id,
            title="30% Production Advance",
            percentage=Decimal("30.00"),
            amount=m1_amt,
            order_index=0,
            status="pending",
        ),
        EscrowMilestone(
            escrow_account_id=escrow.id,
            title="40% Goods Dispatched & BL Proof",
            percentage=Decimal("40.00"),
            amount=m2_amt,
            order_index=1,
            status="pending",
        ),
        EscrowMilestone(
            escrow_account_id=escrow.id,
            title="30% Final Delivery & Inspection Acceptance",
            percentage=Decimal("30.00"),
            amount=m3_amt,
            order_index=2,
            status="pending",
        ),
    ]
    for m in milestones:
        db.add(m)

    await db.commit()
    await db.refresh(escrow)
    return escrow


async def fund_escrow(
    db: AsyncSession,
    connection_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: EscrowDepositPayload,
) -> EscrowAccount:
    """Deposit and lock total quotation amount in the escrow vault."""
    escrow = await get_or_create_escrow_for_connection(db, connection_id, user_id)

    if user_id != escrow.buyer_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the registered Buyer can deposit funds into the escrow vault",
        )

    if escrow.status == "disputed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot deposit funds while the Deal Room is in formal dispute",
        )

    if escrow.status in ("funded", "partially_released", "completed"):
        return escrow

    escrow.funded_amount = escrow.total_amount
    escrow.status = "funded"

    for m in escrow.milestones:
        if m.status == "pending":
            m.status = "funded"

    # Insert confirmation message into chat thread
    notice_msg = ConnectionMessage(
        connection_id=connection_id,
        sender_id=user_id,
        content=f"🔒 [ESCROW VAULT FUNDED] Buyer deposited {escrow.currency} {escrow.total_amount:,.2f} into Escrow Vault. All milestone tranches are now secured.",
    )
    db.add(notice_msg)

    await db.commit()
    await db.refresh(escrow)

    # Broadcast real-time WebSocket update
    await ws_manager.broadcast(
        connection_id,
        "escrow_funded",
        {"escrow_id": str(escrow.id), "total_amount": float(escrow.total_amount)},
    )

    return escrow


async def request_milestone_release(
    db: AsyncSession,
    connection_id: uuid.UUID,
    milestone_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: MilestoneReleaseRequestPayload,
) -> EscrowAccount:
    """Supplier requests buyer to inspect proof and release a funded milestone."""
    escrow = await get_or_create_escrow_for_connection(db, connection_id, user_id)

    if user_id != escrow.seller_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the Supplier can request milestone payment release",
        )

    if escrow.status == "disputed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Escrow releases are locked while a formal dispute is active",
        )

    milestone = await db.get(EscrowMilestone, milestone_id)
    if not milestone or milestone.escrow_account_id != escrow.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Escrow milestone not found",
        )

    if milestone.status != "funded":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Milestone cannot be requested for release from status '{milestone.status}'",
        )

    milestone.status = "release_requested"
    milestone.release_note = payload.proof_note

    notice_msg = ConnectionMessage(
        connection_id=connection_id,
        sender_id=user_id,
        content=f"📋 [MILESTONE RELEASE REQUESTED] Supplier completed milestone '{milestone.title}' ({escrow.currency} {milestone.amount:,.2f}). Proof: {payload.proof_note or 'Completed as agreed'}.",
    )
    db.add(notice_msg)

    await db.commit()
    await db.refresh(escrow)

    await ws_manager.broadcast(
        connection_id,
        "milestone_release_requested",
        {"milestone_id": str(milestone.id), "title": milestone.title},
    )

    return escrow


async def release_milestone_funds(
    db: AsyncSession,
    connection_id: uuid.UUID,
    milestone_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: MilestoneReleaseApprovePayload,
) -> EscrowAccount:
    """Buyer approves and releases funds for a milestone to the seller."""
    escrow = await get_or_create_escrow_for_connection(db, connection_id, user_id)

    if user_id != escrow.buyer_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the Buyer can approve and release escrow milestone funds",
        )

    if escrow.status == "disputed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Escrow releases are locked while a formal dispute is active",
        )

    milestone = await db.get(EscrowMilestone, milestone_id)
    if not milestone or milestone.escrow_account_id != escrow.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Escrow milestone not found",
        )

    if milestone.status not in ("funded", "release_requested"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Milestone cannot be released from status '{milestone.status}'",
        )

    now = datetime.now(timezone.utc)
    milestone.status = "released"
    milestone.released_at = now
    if payload.note:
        milestone.release_note = f"{milestone.release_note or ''} | Approved: {payload.note}".strip(" |")

    escrow.released_amount += milestone.amount

    # Check if all milestones are released
    all_released = all(
        (m.status == "released" or m.id == milestone.id)
        for m in escrow.milestones
    )

    if all_released:
        escrow.status = "completed"
    else:
        escrow.status = "partially_released"

    notice_msg = ConnectionMessage(
        connection_id=connection_id,
        sender_id=user_id,
        content=f"✅ [MILESTONE FUNDS RELEASED] Buyer approved release of {escrow.currency} {milestone.amount:,.2f} for '{milestone.title}'. Payout sent to Supplier.",
    )
    db.add(notice_msg)

    await db.commit()
    await db.refresh(escrow)

    await ws_manager.broadcast(
        connection_id,
        "milestone_released",
        {"milestone_id": str(milestone.id), "amount": float(milestone.amount), "completed": all_released},
    )

    return escrow


async def raise_dispute(
    db: AsyncSession,
    connection_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: DealDisputeCreatePayload,
) -> DealDispute:
    """Raise a formal dispute, lock escrow vault, and initiate mediation record."""
    conn = await db.get(Connection, connection_id)
    if not conn or user_id not in (conn.sender_id, conn.receiver_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not authorized to raise disputes on this connection",
        )

    # Get escrow if present
    e_stmt = select(EscrowAccount).where(EscrowAccount.connection_id == connection_id)
    e_res = await db.execute(e_stmt)
    escrow = e_res.scalars().first()

    dispute = DealDispute(
        connection_id=connection_id,
        escrow_account_id=escrow.id if escrow else None,
        raised_by_id=user_id,
        title=payload.title,
        category=payload.category,
        reason=payload.reason,
        severity=payload.severity,
        status="open",
        suggested_resolution=payload.suggested_resolution,
    )
    db.add(dispute)

    # Freeze escrow if active
    if escrow:
        escrow.status = "disputed"
        for m in escrow.milestones:
            if m.status in ("funded", "release_requested"):
                m.status = "disputed"

    # Post dispute alert banner in chat
    notice_msg = ConnectionMessage(
        connection_id=connection_id,
        sender_id=user_id,
        content=f"⚠️ [FORMAL DISPUTE OPENED] '{payload.title}' ({payload.category.upper()}). Severity: {payload.severity.upper()}. Reason: {payload.reason}. Escrow fund releases are FROZEN pending resolution.",
    )
    db.add(notice_msg)

    await db.commit()
    await db.refresh(dispute)

    await ws_manager.broadcast(
        connection_id,
        "dispute_raised",
        {"dispute_id": str(dispute.id), "title": dispute.title},
    )

    return dispute


async def resolve_dispute(
    db: AsyncSession,
    connection_id: uuid.UUID,
    dispute_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: DealDisputeResolvePayload,
) -> DealDispute:
    """Settle an active dispute with mutual agreement or escrow payout/refund."""
    conn = await db.get(Connection, connection_id)
    if not conn or user_id not in (conn.sender_id, conn.receiver_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not authorized to resolve disputes on this connection",
        )

    dispute = await db.get(DealDispute, dispute_id)
    if not dispute or dispute.connection_id != connection_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dispute not found",
        )

    now = datetime.now(timezone.utc)
    dispute.status = f"resolved_{payload.resolution}"
    dispute.resolution_notes = payload.resolution_notes
    dispute.resolved_at = now

    # Settle escrow funds based on resolution
    if dispute.escrow_account_id:
        escrow = await db.get(
            EscrowAccount,
            dispute.escrow_account_id,
            options=[selectinload(EscrowAccount.milestones)],
        )
        if escrow:
            if payload.resolution == "refund_buyer":
                remaining = escrow.funded_amount - escrow.released_amount
                escrow.refunded_amount = max(Decimal("0.00"), remaining)
                escrow.status = "refunded"
                for m in escrow.milestones:
                    if m.status in ("funded", "disputed", "release_requested"):
                        m.status = "refunded"
            elif payload.resolution == "release_funds":
                remaining = escrow.funded_amount - escrow.released_amount
                escrow.released_amount = escrow.funded_amount
                escrow.status = "completed"
                for m in escrow.milestones:
                    if m.status in ("funded", "disputed", "release_requested"):
                        m.status = "released"
                        m.released_at = now
            else:  # mutual_settlement: restore to funded/partially_released
                escrow.status = "partially_released" if escrow.released_amount > 0 else "funded"
                for m in escrow.milestones:
                    if m.status == "disputed":
                        m.status = "funded"

    notice_msg = ConnectionMessage(
        connection_id=connection_id,
        sender_id=user_id,
        content=f"✓ [DISPUTE RESOLVED] Dispute '{dispute.title}' resolved: {payload.resolution.upper()}. Notes: {payload.resolution_notes}.",
    )
    db.add(notice_msg)

    await db.commit()
    await db.refresh(dispute)

    await ws_manager.broadcast(
        connection_id,
        "dispute_resolved",
        {"dispute_id": str(dispute.id), "resolution": payload.resolution},
    )

    return dispute
