"""Service layer for Escrow Accounts, Milestone Payments, and Formal Deal Disputes.

Money invariants enforced here:

* every mutation (fund, request release, release, raise / resolve dispute)
  locks the escrow row with ``SELECT ... FOR UPDATE`` inside its transaction
  and re-reads the milestones after the lock, so two requests can never both
  act on the same stale state;
* ``released_amount + refunded_amount <= funded_amount`` always holds;
* a dispute is resolved once, only while ``open``, and under the concession
  rule: ``refund_buyer`` only by the seller, ``release_funds`` only by the
  buyer (the side giving up money), ``mutual_settlement`` needs both;
* the vault stays frozen until every open dispute on it is resolved;
* one vault per ORDER (quotation), created explicitly, never as a side effect
  of a read or of dispatch;
* a buyer's deposit is a REPORT: only an operator moves ``deposit_reported`` to
  ``funded``, and every release requires ``funded``;
* ``mutual_settlement`` is two-step (one party proposes, the other accepts).
"""

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
import logging
from typing import Optional
import uuid

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.connection import Connection, ConnectionMessage
from models.enums import QuotationStatus
from models.escrow import DealDispute, EscrowAccount, EscrowMilestone
from models.quotation import Quotation
from schemas.escrow import (
    DealDisputeCreatePayload,
    DealDisputeResolvePayload,
    EscrowDepositPayload,
    MilestoneReleaseApprovePayload,
    MilestoneReleaseRequestPayload,
)
from services import notification_service
from services.deal_parties import resolve_buyer_seller
from services.websocket_manager import ws_manager

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")

# Quote statuses from which an escrow vault may be opened. DISPATCHED is here
# because a seller can ship before the buyer opens the vault.
ESCROW_ELIGIBLE_QUOTE_STATUSES = (
    QuotationStatus.ACCEPTED,
    QuotationStatus.DISPATCHED,
    QuotationStatus.DELIVERED,
    QuotationStatus.RECEIVED,
    QuotationStatus.COMPLETED,
)

# Order stages in which the buyer may still fund the vault.
LIVE_ORDER_STATUSES = (
    QuotationStatus.ACCEPTED,
    QuotationStatus.DISPATCHED,
    QuotationStatus.DELIVERED,
)

# Escrow statuses in which a new dispute may be raised.
DISPUTABLE_ESCROW_STATUSES = ("funded", "partially_released", "disputed")

# Chat banners posted by this module. ConnectionMessage has no message_type
# column, so these tags are how system notices are told apart from messages a
# person typed (the dashboard excludes them from "messages sent").
SYSTEM_BANNER_TAGS: tuple[str, ...] = (
    "[ESCROW VAULT FUNDED]",
    "[ESCROW DEPOSIT REPORTED]",
    "[ESCROW DEPOSIT REJECTED]",
    "[MILESTONE RELEASE REQUESTED]",
    "[MILESTONE FUNDS RELEASED]",
    "[FORMAL DISPUTE OPENED]",
    "[DISPUTE RESOLVED]",
    "[DISPUTE SETTLEMENT PROPOSED]",
    "[DISPUTE WITHDRAWN]",
)


def _banner(connection_id: uuid.UUID, user_id: uuid.UUID, content: str) -> ConnectionMessage:
    return ConnectionMessage(connection_id=connection_id, sender_id=user_id, content=content)


def _assert_ledger(escrow: EscrowAccount) -> None:
    """Never pay out more than was deposited."""
    paid_out = (escrow.released_amount or ZERO) + (escrow.refunded_amount or ZERO)
    if paid_out > (escrow.funded_amount or ZERO) or paid_out < ZERO:
        logger.error(
            "Escrow ledger invariant violated for %s: released=%s refunded=%s funded=%s",
            escrow.id, escrow.released_amount, escrow.refunded_amount, escrow.funded_amount,
        )
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Escrow ledger conflict: payouts would exceed the funded amount",
        )


async def _load_participant_connection(
    db: AsyncSession, connection_id: uuid.UUID, user_id: uuid.UUID
) -> Connection:
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
    return conn


async def current_order_quote(db: AsyncSession, connection_id: uuid.UUID) -> Optional[Quotation]:
    """The deal room's live order: its newest quote that is accepted or later."""
    return (
        await db.execute(
            select(Quotation)
            .where(
                Quotation.connection_id == connection_id,
                Quotation.status.in_(ESCROW_ELIGIBLE_QUOTE_STATUSES),
            )
            .order_by(Quotation.created_at.desc(), Quotation.version.desc())
            .limit(1)
        )
    ).scalars().first()


def _escrow_query(quotation_id: uuid.UUID):
    return (
        select(EscrowAccount)
        .where(EscrowAccount.quotation_id == quotation_id)
        .options(
            selectinload(EscrowAccount.milestones),
            selectinload(EscrowAccount.disputes),
        )
    )


async def find_escrow_for_quote(
    db: AsyncSession, quotation_id: uuid.UUID, *, lock: bool = False
) -> Optional[EscrowAccount]:
    """The vault of one order, or None. Never creates one."""
    stmt = _escrow_query(quotation_id).execution_options(populate_existing=True)
    if lock:
        stmt = stmt.with_for_update(of=EscrowAccount)
    return (await db.execute(stmt)).scalars().first()


async def _find_escrow(
    db: AsyncSession, connection_id: uuid.UUID, *, lock: bool = False
) -> Optional[EscrowAccount]:
    """The vault of the connection's live order, or None."""
    quote = await current_order_quote(db, connection_id)
    if quote is None:
        return None
    return await find_escrow_for_quote(db, quote.id, lock=lock)


async def find_escrow_for_connection(
    db: AsyncSession, connection_id: uuid.UUID
) -> Optional[EscrowAccount]:
    """The vault of the connection's live order, or None. Never creates one."""
    return await _find_escrow(db, connection_id)


async def _lock_escrow(db: AsyncSession, escrow_id: uuid.UUID) -> EscrowAccount:
    """Take the row lock on an escrow and refresh it and its milestones."""
    escrow = (
        await db.execute(
            select(EscrowAccount)
            .where(EscrowAccount.id == escrow_id)
            .options(
                selectinload(EscrowAccount.milestones),
                selectinload(EscrowAccount.disputes),
            )
            .with_for_update(of=EscrowAccount)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    # Re-read the milestone rows after the lock was granted, so a status
    # written by the transaction we waited on is what we check against.
    await db.execute(
        select(EscrowMilestone)
        .where(EscrowMilestone.escrow_account_id == escrow_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return escrow


async def _locked_vault(
    db: AsyncSession, connection_id: uuid.UUID, user_id: uuid.UUID
) -> EscrowAccount:
    """Participant check, then the live order's vault locked FOR UPDATE. 404 if none was opened."""
    await _load_participant_connection(db, connection_id, user_id)
    existing = await _find_escrow(db, connection_id)
    if existing is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No escrow vault has been opened for this order yet",
        )
    return await _lock_escrow(db, existing.id)


async def _create_escrow(
    db: AsyncSession, conn: Connection, quote: Quotation
) -> EscrowAccount:
    """Create the vault of one order. Caller holds the Connection row lock."""
    buyer_id, seller_id = await resolve_buyer_seller(db, conn, quote.rfq_id)

    total = Decimal(str(quote.total_amount))
    m1_amt = (total * Decimal("0.30")).quantize(Decimal("0.01"))
    m2_amt = (total * Decimal("0.40")).quantize(Decimal("0.01"))
    m3_amt = total - (m1_amt + m2_amt)

    escrow = EscrowAccount(
        connection_id=conn.id,
        quotation_id=quote.id,
        buyer_id=buyer_id,
        seller_id=seller_id,
        currency=quote.currency,
        total_amount=total,
        funded_amount=ZERO,
        released_amount=ZERO,
        refunded_amount=ZERO,
        status="pending_deposit",
    )
    db.add(escrow)
    await db.flush()

    for idx, (title, pct, amt) in enumerate((
        ("30% Production Advance", Decimal("30.00"), m1_amt),
        ("40% Goods Dispatched & BL Proof", Decimal("40.00"), m2_amt),
        ("30% Final Delivery & Inspection Acceptance", Decimal("30.00"), m3_amt),
    )):
        db.add(EscrowMilestone(
            escrow_account_id=escrow.id,
            title=title,
            percentage=pct,
            amount=amt,
            order_index=idx,
            status="pending",
        ))
    await db.flush()
    return escrow


async def get_escrow_for_connection(
    db: AsyncSession, connection_id: uuid.UUID, user_id: uuid.UUID
) -> Optional[EscrowAccount]:
    """Read-only: the vault of the connection's live order, or None. Creates nothing."""
    await _load_participant_connection(db, connection_id, user_id)
    return await _find_escrow(db, connection_id)


async def open_escrow_for_connection(
    db: AsyncSession, connection_id: uuid.UUID, user_id: uuid.UUID
) -> EscrowAccount:
    """Explicitly open (or fetch) the vault for the connection's live order.

    One vault per ORDER: the UNIQUE index on ``quotation_id`` is the backstop.
    Creation is serialized on the Connection row so concurrent calls cannot
    open two; an IntegrityError is turned into a re-select.
    """
    await _load_participant_connection(db, connection_id, user_id)

    existing = await _find_escrow(db, connection_id)
    if existing is not None:
        return existing

    conn = (
        await db.execute(
            select(Connection)
            .where(Connection.id == connection_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    quote = await current_order_quote(db, connection_id)
    if quote is None:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No accepted quotation found in this Deal Room. Accept a quotation first to establish escrow.",
        )
    existing = await find_escrow_for_quote(db, quote.id)
    if existing is not None:
        await db.commit()
        return existing
    try:
        await _create_escrow(db, conn, quote)
        await db.commit()
    except IntegrityError:
        await db.rollback()
    quote = await current_order_quote(db, connection_id)
    existing = await find_escrow_for_quote(db, quote.id) if quote else None
    if existing is None:  # pragma: no cover
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Escrow creation failed")
    return existing


async def _notify(db: AsyncSession, user_id: uuid.UUID, connection_id: uuid.UUID, type_: str, title: str, body: str) -> None:
    await notification_service.create_notification(
        db, user_id=user_id, type=type_, title=title, body=body,
        link=f"/messages?connection={connection_id}",
    )


async def fund_escrow(
    db: AsyncSession,
    connection_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: EscrowDepositPayload,
) -> EscrowAccount:
    """The buyer REPORTS a deposit (method + reference). This is not proof of payment.

    There is no payment gateway: the vault moves to ``deposit_reported`` and
    only an operator (``/ops/payments/escrow/{id}/confirm-deposit``) moves it
    to ``funded``. The vault funded is always the connection's live order's;
    a vault opened for an earlier order is never reused.
    """
    conn = await _load_participant_connection(db, connection_id, user_id)
    buyer_id, _seller_id = await resolve_buyer_seller(db, conn)
    if user_id != buyer_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the registered Buyer can deposit funds into the escrow vault",
        )

    quote = await current_order_quote(db, connection_id)
    if quote is not None and quote.status not in LIVE_ORDER_STATUSES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Quotation {quote.quote_number} is already {quote.status.value}; "
            "escrow can only be funded for a live order",
        )
    opened = await open_escrow_for_connection(db, connection_id, user_id)
    quote = await current_order_quote(db, connection_id)
    if quote is None or opened.quotation_id != quote.id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This escrow vault belongs to an earlier order, not the live one on this deal",
        )
    escrow = await _lock_escrow(db, opened.id)

    if escrow.status == "disputed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot deposit funds while the Deal Room is in formal dispute",
        )

    if escrow.status == "refunded":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This escrow vault has been refunded and is closed",
        )

    if escrow.status != "pending_deposit":
        # deposit_reported / funded / partially_released / completed:
        # idempotent, a double-clicked "fund" must not report twice.
        await db.commit()
        return escrow

    now = datetime.now(timezone.utc)
    escrow.status = "deposit_reported"
    escrow.deposit_method = payload.payment_method
    escrow.deposit_reference = payload.payment_reference
    escrow.deposit_reported_at = now
    escrow.deposit_rejection_reason = None

    method_label = payload.payment_method.replace("_", " ")
    db.add(_banner(
        connection_id,
        user_id,
        f"⚠️ [ESCROW DEPOSIT REPORTED — DO NOT DISPATCH GOODS YET] Buyer claimed payment of {escrow.currency} {escrow.total_amount:,.2f} "
        f"via {method_label} (Ref: {payload.payment_reference}). Funds are PENDING bank/admin verification. "
        f"SUPPLIER WARNING: DO NOT SHIP GOODS until you see [ESCROW VAULT FUNDED — SAFE TO DISPATCH].",
    ))

    await db.commit()
    await db.refresh(escrow)

    await notification_service.notify_safely(
        db,
        _notify,
        user_id=escrow.seller_id,
        connection_id=connection_id,
        type_="escrow_deposit_reported",
        title="Buyer Reported a Payment (Do Not Ship Yet)",
        body=(
            f"Buyer claimed payment of {escrow.currency} {escrow.total_amount:,.2f} "
            f"(Ref: {payload.payment_reference}); awaiting bank verification. DO NOT SHIP GOODS YET."
        ),
    )

    await ws_manager.broadcast(
        connection_id,
        "escrow_deposit_reported",
        {"escrow_id": str(escrow.id), "total_amount": float(escrow.total_amount), "status": escrow.status},
    )
    return escrow


async def confirm_deposit(db: AsyncSession, escrow_id: uuid.UUID) -> EscrowAccount:
    """Operator: the reported payment really arrived. deposit_reported -> funded."""
    if await db.get(EscrowAccount, escrow_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Escrow vault not found")
    escrow = await _lock_escrow(db, escrow_id)
    if escrow.status != "deposit_reported":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"No reported deposit to confirm (escrow status: {escrow.status})",
        )
    escrow.funded_amount = escrow.total_amount
    escrow.status = "funded"
    escrow.deposit_confirmed_at = datetime.now(timezone.utc)
    for m in escrow.milestones:
        if m.status == "pending":
            m.status = "funded"
    _assert_ledger(escrow)

    db.add(_banner(
        escrow.connection_id,
        escrow.buyer_id,
        f"✅ [ESCROW VAULT FUNDED — SAFE TO DISPATCH] Payment of {escrow.currency} {escrow.total_amount:,.2f} "
        f"(Ref: {escrow.deposit_reference}) is fully verified and locked in platform escrow. "
        f"Supplier may now safely proceed with order production and shipment.",
    ))
    await db.commit()
    await db.refresh(escrow)

    await notification_service.notify_safely(
        db,
        notification_service.notify_escrow_funded,
        seller_id=escrow.seller_id,
        connection_id=escrow.connection_id,
        currency=escrow.currency,
        amount=escrow.total_amount,
    )
    await ws_manager.broadcast(
        escrow.connection_id,
        "escrow_funded",
        {"escrow_id": str(escrow.id), "total_amount": float(escrow.total_amount)},
    )
    return escrow


async def reject_deposit(db: AsyncSession, escrow_id: uuid.UUID, reason: str) -> EscrowAccount:
    """Operator: the reported payment was not found. deposit_reported -> pending_deposit."""
    if await db.get(EscrowAccount, escrow_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Escrow vault not found")
    escrow = await _lock_escrow(db, escrow_id)
    if escrow.status != "deposit_reported":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"No reported deposit to reject (escrow status: {escrow.status})",
        )
    rejected_ref = escrow.deposit_reference
    escrow.status = "pending_deposit"
    escrow.deposit_rejection_reason = reason
    escrow.deposit_method = None
    escrow.deposit_reference = None
    escrow.deposit_reported_at = None

    db.add(_banner(
        escrow.connection_id,
        escrow.buyer_id,
        f"⛔ [ESCROW DEPOSIT REJECTED] The reported payment (ref {rejected_ref}) could not be confirmed: "
        f"{reason}. The vault is still awaiting a deposit.",
    ))
    await db.commit()
    await db.refresh(escrow)

    await notification_service.notify_safely(
        db,
        _notify,
        user_id=escrow.buyer_id,
        connection_id=escrow.connection_id,
        type_="escrow_deposit_rejected",
        title="Escrow Deposit Not Confirmed",
        body=f"Your reported payment (ref {rejected_ref}) could not be confirmed: {reason}",
    )
    await ws_manager.broadcast(
        escrow.connection_id,
        "escrow_deposit_rejected",
        {"escrow_id": str(escrow.id), "reason": reason},
    )
    return escrow


async def list_escrows_by_status(db: AsyncSession, status_value: str, limit: int = 100) -> list[EscrowAccount]:
    rows = await db.scalars(
        select(EscrowAccount)
        .where(EscrowAccount.status == status_value)
        .options(selectinload(EscrowAccount.milestones), selectinload(EscrowAccount.disputes))
        .order_by(EscrowAccount.deposit_reported_at.asc().nulls_last(), EscrowAccount.created_at.asc())
        .limit(limit)
    )
    return list(rows.all())


async def request_milestone_release(
    db: AsyncSession,
    connection_id: uuid.UUID,
    milestone_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: MilestoneReleaseRequestPayload,
) -> EscrowAccount:
    """Supplier requests buyer to inspect proof and release a funded milestone."""
    escrow = await _locked_vault(db, connection_id, user_id)

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

    if escrow.status not in ("funded", "partially_released"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Milestone payments need a funded vault; the deposit has not been confirmed "
                f"(escrow status: {escrow.status})"
            ),
        )

    milestone = next((m for m in escrow.milestones if m.id == milestone_id), None)
    if milestone is None:
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

    db.add(_banner(
        connection_id,
        user_id,
        f"📋 [MILESTONE RELEASE REQUESTED] Supplier completed milestone '{milestone.title}' "
        f"({escrow.currency} {milestone.amount:,.2f}). Proof: {payload.proof_note or 'Completed as agreed'}.",
    ))

    await db.commit()
    await db.refresh(escrow)

    await notification_service.notify_safely(
        db,
        notification_service.notify_milestone_release_requested,
        buyer_id=escrow.buyer_id,
        connection_id=connection_id,
        milestone_title=milestone.title,
    )

    await ws_manager.broadcast(
        connection_id,
        "milestone_release_requested",
        {"milestone_id": str(milestone.id), "title": milestone.title},
    )
    return escrow


async def request_dispatch_milestone_release(
    db: AsyncSession,
    connection_id: uuid.UUID,
    seller_id: uuid.UUID,
    proof_note: str,
) -> Optional[EscrowAccount]:
    """Logistics hook: ask for the 40% dispatch tranche when goods ship.

    Only acts on an existing, funded vault whose dispatch milestone is still
    ``funded``; returns None (and changes nothing) otherwise. Never creates a
    vault as a side effect of dispatch.
    """
    escrow = await _find_escrow(db, connection_id)
    if escrow is None or escrow.status not in ("funded", "partially_released"):
        return None
    dispatch_milestone = next((m for m in escrow.milestones if m.order_index == 1), None)
    if dispatch_milestone is None or dispatch_milestone.status != "funded":
        return None
    return await request_milestone_release(
        db,
        connection_id,
        dispatch_milestone.id,
        seller_id,
        MilestoneReleaseRequestPayload(proof_note=proof_note[:1000]),
    )


async def release_milestone_funds(
    db: AsyncSession,
    connection_id: uuid.UUID,
    milestone_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: MilestoneReleaseApprovePayload,
) -> EscrowAccount:
    """Buyer approves and releases funds for a milestone to the seller."""
    escrow = await _locked_vault(db, connection_id, user_id)

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

    if escrow.status not in ("funded", "partially_released"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Milestone payments need a funded vault; the deposit has not been confirmed "
                f"(escrow status: {escrow.status})"
            ),
        )

    milestone = next((m for m in escrow.milestones if m.id == milestone_id), None)
    if milestone is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Escrow milestone not found",
        )

    # Checked after the row lock and a fresh read, so a concurrent release of
    # the same milestone sees "released" here and is refused.
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

    escrow.released_amount = (escrow.released_amount or ZERO) + milestone.amount
    _assert_ledger(escrow)

    all_released = all(m.status == "released" for m in escrow.milestones)
    escrow.status = "completed" if all_released else "partially_released"

    db.add(_banner(
        connection_id,
        user_id,
        f"✅ [MILESTONE FUNDS RELEASED] Buyer approved release of {escrow.currency} "
        f"{milestone.amount:,.2f} for '{milestone.title}'. Payout sent to Supplier.",
    ))

    await db.commit()
    await db.refresh(escrow)

    await notification_service.notify_safely(
        db,
        notification_service.notify_escrow_released,
        seller_id=escrow.seller_id,
        connection_id=connection_id,
        currency=escrow.currency,
        amount=milestone.amount,
        milestone_title=milestone.title,
    )

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
    """Raise a formal dispute, freeze the escrow vault, and open a mediation record.

    Only a funded (or partly released, or already disputed) vault can be
    disputed: an unfunded vault holds nothing to freeze, and a completed or
    refunded one is closed.
    """
    conn = await db.get(Connection, connection_id)
    if not conn or user_id not in (conn.sender_id, conn.receiver_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not authorized to raise disputes on this connection",
        )

    escrow = await _find_escrow(db, connection_id)
    if escrow is not None:
        escrow = await _lock_escrow(db, escrow.id)
    if escrow is None or escrow.status not in DISPUTABLE_ESCROW_STATUSES:
        current = escrow.status if escrow is not None else "not opened"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "A formal dispute can only be raised on a funded escrow vault "
                f"(current escrow status: {current})"
            ),
        )

    dispute = DealDispute(
        connection_id=connection_id,
        escrow_account_id=escrow.id,
        raised_by_id=user_id,
        title=payload.title,
        category=payload.category,
        reason=payload.reason,
        severity=payload.severity,
        status="open",
        suggested_resolution=payload.suggested_resolution,
    )
    db.add(dispute)

    escrow.status = "disputed"
    for m in escrow.milestones:
        if m.status in ("funded", "release_requested"):
            m.status = "disputed"

    db.add(_banner(
        connection_id,
        user_id,
        f"⚠️ [FORMAL DISPUTE OPENED] '{payload.title}' ({payload.category.upper()}). "
        f"Severity: {payload.severity.upper()}. Reason: {payload.reason}. "
        f"Escrow fund releases are FROZEN pending resolution.",
    ))

    await db.commit()
    await db.refresh(dispute)

    counterparty_id = conn.receiver_id if conn.sender_id == user_id else conn.sender_id
    await notification_service.notify_safely(
        db,
        notification_service.notify_dispute_raised,
        user_id=counterparty_id,
        connection_id=connection_id,
        dispute_title=dispute.title,
    )

    await ws_manager.broadcast(
        connection_id,
        "dispute_raised",
        {"dispute_id": str(dispute.id), "title": dispute.title},
    )
    return dispute


def _settle_refund(escrow: EscrowAccount) -> None:
    remaining = escrow.funded_amount - escrow.released_amount - escrow.refunded_amount
    if remaining > ZERO:
        escrow.refunded_amount = escrow.refunded_amount + remaining
    for m in escrow.milestones:
        if m.status in ("funded", "disputed", "release_requested"):
            m.status = "refunded"
    escrow.status = "refunded"


def _settle_release(escrow: EscrowAccount, now: datetime) -> None:
    remaining = escrow.funded_amount - escrow.released_amount - escrow.refunded_amount
    if remaining > ZERO:
        escrow.released_amount = escrow.released_amount + remaining
    for m in escrow.milestones:
        if m.status in ("funded", "disputed", "release_requested"):
            m.status = "released"
            m.released_at = now
    escrow.status = "completed"


def _settle_split(escrow: EscrowAccount, release_percent: Decimal, now: datetime) -> None:
    """Pay the seller ``release_percent`` of what is left in the vault and refund the rest."""
    remaining = escrow.funded_amount - escrow.released_amount - escrow.refunded_amount
    to_seller = (remaining * release_percent / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    to_seller = min(max(to_seller, ZERO), max(remaining, ZERO))
    to_buyer = max(remaining, ZERO) - to_seller
    escrow.released_amount = escrow.released_amount + to_seller
    escrow.refunded_amount = escrow.refunded_amount + to_buyer
    for m in escrow.milestones:
        if m.status in ("funded", "disputed", "release_requested"):
            m.status = "released" if to_seller > ZERO else "refunded"
            if to_seller > ZERO:
                m.released_at = now
    escrow.status = "completed" if escrow.released_amount > ZERO else "refunded"


def _unfreeze(escrow: EscrowAccount) -> None:
    """Resume the vault after the last open dispute is settled by agreement."""
    if escrow.status != "disputed":
        return  # already terminal (refunded / completed): nothing to restore
    if (escrow.funded_amount or ZERO) <= ZERO:
        # Never report "funded" for a vault that holds no money.
        escrow.status = "pending_deposit"
        for m in escrow.milestones:
            if m.status == "disputed":
                m.status = "pending"
        return
    for m in escrow.milestones:
        if m.status == "disputed":
            m.status = "funded"
    if all(m.status == "released" for m in escrow.milestones):
        escrow.status = "completed"
    elif escrow.released_amount > ZERO:
        escrow.status = "partially_released"
    else:
        escrow.status = "funded"


async def resolve_dispute(
    db: AsyncSession,
    connection_id: uuid.UUID,
    dispute_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: DealDisputeResolvePayload,
) -> DealDispute:
    """Settle an open dispute by concession or mutual agreement.

    ``mutual_settlement`` takes two calls from two different parties: the first
    records the proposal (and optional split), the second -- by the OTHER party
    -- accepts it. A counterparty who sends a different ``release_percent``
    makes a counter-proposal instead of accepting.
    """
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

    escrow: Optional[EscrowAccount] = None
    if dispute.escrow_account_id:
        escrow = await _lock_escrow(db, dispute.escrow_account_id)

    # Re-read the dispute under lock: a concurrent resolve that committed
    # while we waited must be seen here.
    dispute = (
        await db.execute(
            select(DealDispute)
            .where(DealDispute.id == dispute_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    if dispute.status != "open":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This dispute is already {dispute.status}",
        )

    if escrow is not None:
        buyer_id, seller_id = escrow.buyer_id, escrow.seller_id
    else:
        buyer_id, seller_id = await resolve_buyer_seller(db, conn)

    # Concession rule: only the side that gives up money may choose an
    # outcome that moves money to the other side.
    if payload.resolution == "refund_buyer" and user_id != seller_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only the seller can agree to refund the buyer",
        )
    if payload.resolution == "release_funds" and user_id != buyer_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only the buyer can agree to release the held funds to the seller",
        )

    now = datetime.now(timezone.utc)
    counterparty_id = conn.receiver_id if conn.sender_id == user_id else conn.sender_id

    other_open = 0
    if escrow is not None:
        other_open = await db.scalar(
            select(func.count())
            .select_from(DealDispute)
            .where(
                DealDispute.escrow_account_id == escrow.id,
                DealDispute.id != dispute.id,
                DealDispute.status == "open",
            )
        ) or 0

    if payload.resolution == "mutual_settlement":
        proposer_id = dispute.settlement_proposed_by_id
        new_pct = payload.release_percent
        is_counter = proposer_id is not None and proposer_id != user_id and (
            new_pct is not None and new_pct != dispute.settlement_release_percent
        )
        if proposer_id is None or proposer_id == user_id or is_counter:
            # Step one: record (or revise) the proposal; the OTHER party must accept.
            dispute.settlement_proposed_by_id = user_id
            dispute.settlement_release_percent = new_pct
            dispute.settlement_proposed_at = now
            dispute.suggested_resolution = payload.resolution_notes
            split = (
                f"seller receives {new_pct}% of the remaining balance, buyer is refunded the rest"
                if new_pct is not None else "resume the deal as agreed"
            )
            db.add(_banner(
                connection_id,
                user_id,
                f"🤝 [DISPUTE SETTLEMENT PROPOSED] Settlement proposed for '{dispute.title}': {split}. "
                f"Notes: {payload.resolution_notes}. Awaiting the other party's acceptance; "
                f"the dispute stays open and releases stay frozen.",
            ))
            await db.commit()
            await db.refresh(dispute)
            await notification_service.notify_safely(
                db,
                _notify,
                user_id=counterparty_id,
                connection_id=connection_id,
                type_="dispute_settlement_proposed",
                title="Settlement Proposed",
                body=f"A settlement was proposed on dispute '{dispute.title}': {split}.",
            )
            await ws_manager.broadcast(
                connection_id,
                "dispute_settlement_proposed",
                {"dispute_id": str(dispute.id), "proposed_by_id": str(user_id)},
            )
            return dispute
        # Step two: the counterparty accepts the stored proposal.
        if dispute.settlement_release_percent is not None and other_open:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Resolve the other open disputes on this vault before accepting a split settlement",
            )

    dispute.status = f"resolved_{payload.resolution}"
    dispute.resolution_notes = payload.resolution_notes
    dispute.resolved_at = now

    if escrow is not None:
        if payload.resolution == "refund_buyer":
            _settle_refund(escrow)
        elif payload.resolution == "release_funds":
            _settle_release(escrow, now)
        elif dispute.settlement_release_percent is not None:
            _settle_split(escrow, dispute.settlement_release_percent, now)
        elif other_open == 0:
            _unfreeze(escrow)
        # else: mutual settlement of one dispute while others remain open --
        # the vault stays frozen.
        _assert_ledger(escrow)

    db.add(_banner(
        connection_id,
        user_id,
        f"✓ [DISPUTE RESOLVED] Dispute '{dispute.title}' resolved: {payload.resolution.upper()}. "
        f"Notes: {payload.resolution_notes}.",
    ))

    await db.commit()
    await db.refresh(dispute)

    await notification_service.notify_safely(
        db,
        notification_service.notify_dispute_resolved,
        user_id=counterparty_id,
        connection_id=connection_id,
        dispute_title=dispute.title,
        resolution=payload.resolution,
    )

    await ws_manager.broadcast(
        connection_id,
        "dispute_resolved",
        {"dispute_id": str(dispute.id), "resolution": payload.resolution},
    )
    return dispute


async def withdraw_dispute(
    db: AsyncSession,
    connection_id: uuid.UUID,
    dispute_id: uuid.UUID,
    user_id: uuid.UUID,
) -> DealDispute:
    """Withdraw an open dispute. Only the party who raised it may do so."""
    conn = await db.get(Connection, connection_id)
    if not conn or user_id not in (conn.sender_id, conn.receiver_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not authorized to withdraw disputes on this connection",
        )
    dispute = await db.get(DealDispute, dispute_id)
    if not dispute or dispute.connection_id != connection_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dispute not found")

    escrow: Optional[EscrowAccount] = None
    if dispute.escrow_account_id:
        escrow = await _lock_escrow(db, dispute.escrow_account_id)
    dispute = (
        await db.execute(
            select(DealDispute)
            .where(DealDispute.id == dispute_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().one()

    if user_id != dispute.raised_by_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only the party who raised the dispute can withdraw it",
        )
    if dispute.status != "open":
        raise HTTPException(status.HTTP_409_CONFLICT, f"This dispute is already {dispute.status}")

    now = datetime.now(timezone.utc)
    dispute.status = "cancelled"
    dispute.resolved_at = now
    dispute.resolution_notes = "Withdrawn by the party who raised it"
    dispute.settlement_proposed_by_id = None
    dispute.settlement_release_percent = None
    dispute.settlement_proposed_at = None

    if escrow is not None:
        other_open = await db.scalar(
            select(func.count())
            .select_from(DealDispute)
            .where(
                DealDispute.escrow_account_id == escrow.id,
                DealDispute.id != dispute.id,
                DealDispute.status == "open",
            )
        ) or 0
        if other_open == 0:
            _unfreeze(escrow)
        _assert_ledger(escrow)

    db.add(_banner(
        connection_id,
        user_id,
        f"↩️ [DISPUTE WITHDRAWN] Dispute '{dispute.title}' was withdrawn by the party who raised it.",
    ))
    await db.commit()
    await db.refresh(dispute)

    counterparty_id = conn.receiver_id if conn.sender_id == user_id else conn.sender_id
    await notification_service.notify_safely(
        db,
        notification_service.notify_dispute_resolved,
        user_id=counterparty_id,
        connection_id=connection_id,
        dispute_title=dispute.title,
        resolution="withdrawn",
    )
    await ws_manager.broadcast(
        connection_id,
        "dispute_resolved",
        {"dispute_id": str(dispute.id), "resolution": "withdrawn"},
    )
    return dispute
