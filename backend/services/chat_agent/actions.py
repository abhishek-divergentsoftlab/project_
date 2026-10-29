"""RFQ actions the agent runs after the user confirmed a recap: create, publish, save an edit, close; plus the formatters of their results and the match payloads."""


from __future__ import annotations

import logging
import re
from typing import Any, Optional
import uuid

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.conversation import Conversation
from models.enums import RFQStatus
from models.user import User
from services.chat_agent import draft_assist, rfq_draft, rfq_edit



logger = logging.getLogger(__name__)


def format_rfq_summary_text(draft: dict[str, Any], readiness: dict[str, Any]) -> str:
    """Generate an intelligent, accurate acknowledgment reflecting all captured and missing fields."""
    if not readiness.get("has_role"):
        return "Are you looking to **BUY** or **SELL** goods?"

    role_str = str(draft.get("role") or "buyer").upper()

    if not readiness.get("has_product"):
        return f"Understood, registered as **{role_str}**. What specific **product or item** are you looking to buy or sell?"

    # Required fields are complete! Check all captured details
    prod_name = (draft.get("product_details") or {}).get("name") or draft.get("category") or "Product"
    lines = [f"📋 **RFQ Draft Updated** ({role_str}):"]
    lines.append(f"- **Product**: {prod_name.title() if isinstance(prod_name, str) else prod_name}")
    if draft.get("category"):
        lines.append(f"- **Category**: {draft['category']}")

    if draft.get("quantity") and draft["quantity"].get("value") is not None:
        q = draft["quantity"]
        val = q.get("value")
        val_str = f"{val:,.0f}" if isinstance(val, (int, float)) and val.is_integer() else f"{val}"
        lines.append(f"- **Quantity**: {val_str} {q.get('unit', '')}".strip())

    if draft.get("price_target") and draft["price_target"].get("amount") is not None:
        p = draft["price_target"]
        cur = p.get("currency", "INR")
        amt = p.get("amount")
        per_u = f" / {p.get('per_unit')}" if p.get("per_unit") else ""
        lines.append(f"- **Target Price**: {cur} {amt}{per_u}")

    if draft.get("location"):
        loc = draft["location"]
        loc_str = ", ".join(loc.get(k) for k in ("city", "state", "country") if loc.get(k))
        if loc_str:
            lines.append(f"- **Delivery Location**: {loc_str}")

    if draft.get("deadline"):
        dl = draft["deadline"]
        if dl.get("date"):
            lines.append(f"- **Deadline**: {dl.get('date')}")
        elif dl.get("in_days"):
            lines.append(f"- **Deadline**: within {dl.get('in_days')} days")

    # Missing optional fields
    missing_opts = []
    if not draft.get("quantity"):
        missing_opts.append("quantity")
    if not draft.get("price_target"):
        missing_opts.append("target budget/price")
    if not draft.get("location") or not (draft.get("location") or {}).get("city"):
        missing_opts.append("delivery location")
    if not draft.get("deadline"):
        missing_opts.append("delivery deadline")

    if missing_opts:
        opts_prompt = f"Would you like to specify **{', '.join(missing_opts)}**, or proceed to create this RFQ?"
    else:
        opts_prompt = "All key commercial terms are recorded! Would you like to add any technical specifications, or proceed to create this RFQ?"

    lines.append("")
    lines.append(opts_prompt)
    return "\n".join(lines)


async def execute_create_rfq_call(
    draft: dict[str, Any],
    tool_args: dict[str, Any],
    db: Optional[AsyncSession] = None,
    user: Optional[User] = None,
    assist: Optional[draft_assist.DraftAssist] = None,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Execute create_rfq tool call, validating role and submitting to database."""
    readiness = rfq_draft.evaluate_rfq_readiness(draft)
    if not readiness["has_role"] or not readiness["has_product"]:
        return None, "Role (buyer/seller) and Product name are required before creating an RFQ."

    if assist is not None:
        # The form blocks submit on an unsafe listing; so does chat (rfq_service still moderates server-side).
        blocked = await assist.block_reason(draft)
        if blocked:
            return None, blocked

    title = tool_args.get("title")
    status = tool_args.get("status", "active")
    notes = tool_args.get("notes")

    try:
        payload = rfq_draft.build_rfq_create_payload(
            draft=draft,
            title=title,
            status=status,
            notes=notes,
        )
    except rfq_draft.RFQDraftInvalid as exc:
        problems = "; ".join(f"{f}: {m}" for f, m in exc.errors)
        return None, f"I couldn't create the RFQ yet — please fix: {problems}"

    if db is not None and user is not None:
        if not user.can_post_as(payload.role):
            return None, (
                f"Your account is registered as '{user.role.value}' and cannot post a '{payload.role.value}' RFQ. "
                f"Please update your account role or post as a {user.role.value}."
            )
        try:
            from services import rfq_service
            rfq = await rfq_service.create_rfq(db, user, payload)
            return {
                "id": str(rfq.id),
                "title": rfq.title,
                "role": rfq.role.value,
                "status": rfq.status.value,
                "category": rfq.category,
            }, None
        except rfq_service.DuplicateRFQError as exc:
            # Never claim a new listing: point at the one that already exists.
            return None, f"You already have this RFQ: {exc.existing_title} (id {exc.existing_id}). No new RFQ was created."
        except Exception as exc:
            logger.exception("Failed to create RFQ in database: %s", exc)
            return None, f"Failed to create RFQ: {str(exc)}"
    else:
        # Fallback simulated creation (for tests without db session)
        simulated_id = str(uuid.uuid4())
        return {
            "id": simulated_id,
            "title": payload.title,
            "role": payload.role.value,
            "status": payload.status.value,
            "category": payload.category,
        }, None


async def _resolve_user_rfq(
    db: AsyncSession, user: User, ref: Any, statuses: tuple[Any, ...]
) -> Optional[Any]:
    """The RFQ a user's command names: by id (any owner, so callers can refuse with a clear message), else
    the newest of the user's own RFQs in ``statuses`` whose title, category or product name contains ``ref``."""
    from models.rfq import RFQ

    search_term = str(ref or "").strip()
    if not search_term:
        return None
    try:
        found = await db.get(RFQ, uuid.UUID(search_term))
    except Exception:
        found = None
    if found is not None:
        return found
    if re.fullmatch(r"[0-9a-fA-F]{8}", search_term):
        # The short id the assistant shows next to a listing's title: the user's own listings only.
        by_short = (
            await db.execute(
                select(RFQ)
                .where(
                    RFQ.user_id == user.id,
                    RFQ.status.in_(list(statuses)),
                    text("CAST(rfqs.id AS text) LIKE :short_id"),
                )
                .params(short_id=f"{search_term.lower()}%")
                .order_by(RFQ.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
        if by_short is not None:
            return by_short
    stmt = (
        select(RFQ)
        .where(
            RFQ.user_id == user.id,
            RFQ.status.in_(list(statuses)),
            or_(
                RFQ.title.ilike(f"%{search_term}%"),
                RFQ.category.ilike(f"%{search_term}%"),
                text("rfqs.product_details->>'name' ILIKE :search_term"),
            ),
        )
        .params(search_term=f"%{search_term}%")
        .order_by(RFQ.created_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


async def execute_close_rfq_call(
    rfq_id_str: Optional[str] = None,
    conversation: Optional[Conversation] = None,
    db: Optional[AsyncSession] = None,
    user: Optional[User] = None,
    require_unambiguous: bool = False,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Close one named active RFQ of the user's.

    The listing must be named (id, short id or title keyword): nothing is ever chosen for the user, not the
    conversation's own RFQ, not the newest one. Callers reach this only after the user confirmed that exact
    listing (the stored pending close action). ``conversation`` and ``require_unambiguous`` are kept for callers.
    """
    if db is None or user is None:
        return None, "Database session not available."

    from schemas.rfq import RFQUpdate
    from services import rfq_service

    if not (rfq_id_str or "").strip():
        return None, "Tell me which listing to close, for example \"close my cotton RFQ\"."
    target_rfq = await _resolve_user_rfq(db, user, rfq_id_str, (RFQStatus.ACTIVE,))
    if not target_rfq:
        return None, f"No active RFQ matching '{rfq_id_str}' was found in your listings."
    if target_rfq.user_id != user.id:
        return None, "You do not have permission to close this RFQ."
    if target_rfq.status != RFQStatus.ACTIVE:
        return None, f"'{target_rfq.title}' is {target_rfq.status.value}, so there is nothing to close."

    try:
        closed = await rfq_service.update_rfq(db, target_rfq, RFQUpdate(status=RFQStatus.CLOSED))
        if conversation:
            state = dict(conversation.state or {})
            if "last_created_rfq" in state:
                state["last_created_rfq"]["status"] = "closed"
            conversation.state = state
            await db.commit()

        return {
            "id": str(closed.id),
            "title": closed.title,
            "role": closed.role.value,
            "status": "closed",
            "category": closed.category,
        }, None
    except Exception as exc:
        logger.exception("Failed to close RFQ from chat: %s", exc)
        return None, f"Failed to close RFQ: {str(exc)}"


async def execute_publish_rfq_call(
    rfq_id_str: Optional[str] = None,
    conversation: Optional[Conversation] = None,
    db: Optional[AsyncSession] = None,
    user: Optional[User] = None,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Publish one of the user's draft RFQs (the form's "Publish now").

    Which RFQ: the one named by id or keyword; else the draft this conversation created last;
    else the user's only draft; otherwise nothing is published and the user is asked which one.
    """
    if db is None or user is None:
        return None, "Database session not available."

    from models.rfq import RFQ
    from services import rfq_service

    target = None
    if rfq_id_str:
        target = await _resolve_user_rfq(db, user, rfq_id_str, (RFQStatus.DRAFT,))
        if target is None:
            return None, f"No draft RFQ matching '{rfq_id_str}' was found in your listings."
    else:
        last_id = (conversation.state or {}).get("last_created_rfq_id") if conversation else None
        if last_id:
            try:
                last = await db.get(RFQ, uuid.UUID(str(last_id)))
            except Exception:
                last = None
            if last is not None and last.user_id == user.id and last.status == RFQStatus.DRAFT:
                target = last
        if target is None:
            drafts = (
                await db.execute(
                    select(RFQ)
                    .where(RFQ.user_id == user.id, RFQ.status == RFQStatus.DRAFT)
                    .order_by(RFQ.created_at.desc())
                    .limit(5)
                )
            ).scalars().all()
            if not drafts:
                return None, "You have no draft RFQs to publish."
            if len(drafts) > 1:
                titles = "; ".join(f"'{d.title}'" for d in drafts)
                return None, f"You have several draft RFQs ({titles}). Tell me which one to publish, for example \"publish my rice RFQ\"."
            target = drafts[0]

    if target.user_id != user.id:
        return None, "You do not have permission to publish this RFQ."
    if target.status == RFQStatus.ACTIVE:
        return None, f"'{target.title}' is already published."
    try:
        published = await rfq_service.publish_rfq(db, target)
    except rfq_service.RFQError as exc:
        return None, f"Could not publish '{target.title}': {exc}"
    except Exception as exc:
        logger.exception("Failed to publish RFQ from chat: %s", exc)
        return None, f"Failed to publish RFQ: {str(exc)}"
    return {
        "id": str(published.id),
        "title": published.title,
        "role": published.role.value,
        "status": published.status.value,
        "category": published.category,
    }, None


async def execute_save_rfq_edit_call(
    draft: dict[str, Any],
    snapshot: dict[str, Any],
    rfq_id_str: str,
    db: Optional[AsyncSession] = None,
    user: Optional[User] = None,
    assist: Optional[draft_assist.DraftAssist] = None,
) -> tuple[Optional[dict[str, Any]], Optional[str], bool]:
    """Save the chat's edits to the RFQ being edited: ``(rfq, error, changed)``.

    Only fields that differ from the loaded RFQ are sent, as the form's PATCH does. Nothing is
    sent (``changed`` False) when the draft equals what was loaded.
    """
    if db is None or user is None:
        return None, "Database session not available.", False

    from models.rfq import RFQ
    from services import rfq_service

    try:
        target = await db.get(RFQ, uuid.UUID(str(rfq_id_str)))
    except Exception:
        target = None
    if target is None:
        return None, "The RFQ you were editing could not be found.", False
    if target.user_id != user.id:
        return None, "You do not have permission to edit this RFQ.", False

    try:
        patch = rfq_edit.build_rfq_patch(draft, snapshot)
    except rfq_draft.RFQDraftInvalid as exc:
        problems = "; ".join(f"{f}: {m}" for f, m in exc.errors)
        return None, f"I couldn't save the changes yet — please fix: {problems}", True
    if patch is None:
        return None, None, False
    if assist is not None:
        blocked = await assist.block_reason(draft)
        if blocked:
            return None, blocked, True
    try:
        updated = await rfq_service.update_rfq(db, target, patch)
    except rfq_service.RFQError as exc:
        return None, f"Could not save the changes to '{target.title}': {exc}", True
    except Exception as exc:
        logger.exception("Failed to update RFQ from chat: %s", exc)
        return None, f"Failed to update RFQ: {str(exc)}", True
    return {
        "id": str(updated.id),
        "title": updated.title,
        "role": updated.role.value,
        "status": updated.status.value,
        "category": updated.category,
    }, None, True


def _draft_location(city: str, stated_country: Optional[str] = None) -> dict[str, Any]:
    """City/state/country from the gazetteer when it knows the city; never an invented country."""
    from services import locations

    loc = locations.find_location(city) if city else None
    if loc is not None and loc.is_city:
        out: dict[str, Any] = {"city": loc.city or city}
        if loc.state:
            out["state"] = loc.state
        out["country"] = loc.country
        return out
    out = {"city": city}
    if stated_country:
        out["country"] = stated_country
    return out


def _published_text(published: dict[str, Any]) -> str:
    rfq_id = published.get("id", "")
    return (
        f"🎉 **RFQ Published!** '{published.get('title', 'RFQ')}' (ID: `{rfq_id}`) is now live in the Marketplace.\n\n"
        f"[View RFQ in Marketplace](/marketplace?rfq={rfq_id})"
    )


def format_created_rfq_text(created_rfq: dict[str, Any]) -> str:
    """Format celebratory message when an RFQ is created."""
    rfq_id = created_rfq.get("id", "")
    title = created_rfq.get("title", "RFQ")
    role = (created_rfq.get("role") or "BUYER").upper()
    status = (created_rfq.get("status") or "ACTIVE").upper()
    category = created_rfq.get("category", "General Goods")

    if status == "DRAFT":
        return (
            f"📝 **RFQ saved as a draft (not published).**\n\n"
            f"- **Title**: {title}\n"
            f"- **RFQ ID**: `{rfq_id}`\n"
            f"- **Market Side**: {role}\n"
            f"- **Category**: {category}\n"
            f"- **Status**: DRAFT (not visible in the Marketplace)\n\n"
            f"You can review and publish it later from your RFQs."
        )

    rfq_link_md = f"[View RFQ in Marketplace](/marketplace?rfq={rfq_id})" if rfq_id else "[View Marketplace](/marketplace)"

    return (
        f"🎉 **RFQ Successfully Created & Published!**\n\n"
        f"- **Title**: {title}\n"
        f"- **RFQ ID**: `{rfq_id}`\n"
        f"- **Market Side**: {role}\n"
        f"- **Category**: {category}\n"
        f"- **Status**: {status} (Active & Live in Marketplace)\n"
        f"- **Listing Link**: {rfq_link_md}\n\n"
        f"Your listing has been submitted to the marketplace and is active in the matching index. "
        f"Click the link above to view your listing, or click 'Show matching' below to find counterparties!"
    )


async def _match_payloads(
    db: Optional[AsyncSession], user: Optional[User], tool_results: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Frontend ``MatchCandidate`` objects for the listings the read tools returned this turn.

    They come from the same serializer as the match API (``match_service._to_candidate``),
    loaded from the database in tool order, at most 10. A find_matches row carries the
    matcher's real score breakdown (recomputed with the same call the tool made). A
    search_listings row was never scored, so its ``score`` is null, which the UI renders
    without a percentage; no score is ever invented.
    """
    if db is None or user is None:
        return []
    from types import SimpleNamespace

    from sqlalchemy.orm import selectinload

    from models.rfq import RFQ
    from models.user import UserProfile
    from schemas.match import MatchScore
    from services import match_service

    # (rfq id, requester rfq id for find_matches rows) in tool order, de-duplicated
    ordered: list[tuple[uuid.UUID, Optional[uuid.UUID]]] = []
    seen: set[uuid.UUID] = set()
    for res in tool_results or []:
        tool = res.get("tool")
        if tool not in ("find_matches", "search_listings") or not res.get("ok", True):
            continue
        data = res.get("data") or {}
        rows = data.get("matches") if tool == "find_matches" else data.get("samples")
        requester = res.get("args", {}).get("rfq_id") if tool == "find_matches" else None
        for row in rows or []:
            try:
                rid = uuid.UUID(str(row.get("id")))
                req_id = uuid.UUID(str(requester)) if requester else None
            except (ValueError, TypeError):
                continue
            if rid not in seen:
                seen.add(rid)
                ordered.append((rid, req_id))
    ordered = ordered[:10]
    if not ordered:
        return []

    try:
        rfqs = {
            r.id: r
            for r in (
                await db.scalars(
                    select(RFQ)
                    .options(selectinload(RFQ.user).selectinload(User.profile))
                    .where(RFQ.id.in_([rid for rid, _ in ordered]))
                )
            ).all()
        }
        connections = await match_service._connections_for(db, user.id, list(rfqs))
        scores: dict[uuid.UUID, dict[uuid.UUID, Any]] = {}
        requesters: dict[uuid.UUID, Any] = {}
        for req_id in {req for _, req in ordered if req}:
            requester = await db.get(RFQ, req_id)
            if requester is None:
                continue
            requesters[req_id] = requester
            prefs = (
                await db.execute(select(UserProfile.matching_preferences).where(UserProfile.user_id == user.id))
            ).scalar_one_or_none() or None
            pairs, _path = await match_service._collect(db, requester, user.id, prefs)
            scores[req_id] = {cand.id: score for cand, score in pairs}

        blank = SimpleNamespace(latitude=None, longitude=None, location_country=None)
        out: list[dict[str, Any]] = []
        for rid, req_id in ordered:
            cand = rfqs.get(rid)
            if cand is None:
                continue
            score = (scores.get(req_id) or {}).get(rid) if req_id else None
            card = match_service._to_candidate(
                requesters.get(req_id, blank) if req_id else blank,
                cand,
                score or MatchScore(total=0),
                len(out) + 1,
                connections.get(rid),
            ).model_dump(mode="json")
            if score is None:
                card["score"] = None  # unscored listing: never show a made-up percentage
            out.append(card)
        return out
    except Exception as exc:
        logger.warning("Could not build match cards: %s", exc)
        return []
