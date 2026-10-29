"""Trading with counterparties through chat: read a negotiation, and the two-phase connect / message / quote /
accept / reject actions.

Everything here calls the SAME service functions the REST endpoints call (``connection_service`` /
``quotation_service``), which own validation, locking, websocket events and notifications. This module only
(1) reads, with counterparty text fenced as data, (2) validates a proposal early so the model can fix its arguments
before the user is asked, and (3) runs exactly the arguments the user saw in the recap.

It never reads the user's intent: the model decides, ``tools.propose_action`` / ``confirm_action`` gate.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import String, cast, or_, select

from models.enums import ConnectionStatus, QuotationStatus, RFQStatus
from models.rfq import RFQ
from schemas.connection import ConnectionMessageCreate
from schemas.quotation import QuotationCreate
from services import connection_service, quotation_service
from services.chat_agent import guards, negotiation_text
from services.chat_agent.state import PendingAction
from services.untrusted_text import as_data

logger = logging.getLogger(__name__)

NEGOTIATION_ACTIONS = ("connect", "send_message", "send_quote", "accept_quote", "reject_quote")
MAX_THREADS = 10
MAX_MESSAGES = 15
MAX_QUOTES = 12
BRIEF_THREADS = 5
QUOTE_FIELDS = ("unit_price", "quantity", "quantity_unit", "currency", "lead_time_days", "incoterms", "payment_terms", "valid_days", "notes")
SEND_REPLIES = ["Send it", "Change it"]


_HEX_PREFIX = re.compile(r"[0-9a-f]{8,31}")


def is_id_prefix(ref: Any) -> bool:
    """A short id: 8+ hex characters that are not a full UUID."""
    return bool(_HEX_PREFIX.fullmatch(str(ref or "").strip().lower()))


async def find_by_prefix(db: Any, model: Any, prefix: str, *conditions: Any) -> list[Any]:
    """Up to 4 rows of ``model`` (already scoped by ``conditions``) whose id starts with ``prefix``."""
    stmt = select(model).where(cast(model.id, String).like(f"{prefix.lower()}%"), *conditions).limit(4)
    return list((await db.execute(stmt)).scalars().all())


async def resolve_short_id(
    db: Any, model: Any, ref: Any, *conditions: Any, noun: str = "item", title: Any = None,
) -> tuple[Any, Optional[str]]:
    """The ONE row a short id (8+ hex prefix) names within the scope ``conditions``.

    (row, None) for exactly one match; (None, reason) for none or several, the reason listing up to 3 candidates as
    'title (short id)'. (None, None) when ``ref`` is not a short id at all, so the caller handles it its own way."""
    if not is_id_prefix(ref):
        return None, None
    prefix = str(ref).strip().lower()
    rows = await find_by_prefix(db, model, prefix, *conditions)
    if len(rows) == 1:
        return rows[0], None
    if not rows:
        return None, f"no {noun} of yours or open to you starts with '{prefix}': check the id"
    names = "; ".join(f"{_plain(title(r), 60) + ' ' if title else ''}({str(r.id)[:8]})" for r in rows[:3])
    return None, f"several {noun}s start with '{prefix}': {names}. Give more characters of the id"


def _err(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, **extra}


def _ok(result: Any, **ui: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": True, "result": result}
    if ui:
        out["ui"] = ui
    return out


def _val(x: Any) -> Any:
    return getattr(x, "value", x)


def _iso(x: Any) -> Optional[str]:
    return x.isoformat() if isinstance(x, datetime) else (str(x) if x else None)


def savepoint(db: Any) -> Any:
    """``async with savepoint(db):`` runs a read inside a SAVEPOINT (a stand-in session without one runs it plainly)."""
    begin = getattr(db, "begin_nested", None)
    return begin() if callable(begin) else contextlib.nullcontext()


def _plain(text: Any, limit: int = 80) -> str:
    """Another user's text inside a recap: one line, clipped, with the data-fence markers defanged."""
    return " ".join(str(text or "").split())[:limit].replace("<<", "‹‹").replace(">>", "››")


def _detail(exc: HTTPException) -> str:
    return str(exc.detail)


def _dec(value: Any) -> Optional[Decimal]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        return None


def _num(d: Any) -> str:
    """A number as plain text without exponent or trailing zeros: 52.5, 500, 1000000."""
    dec = _dec(d)
    if dec is None:
        return str(d)
    text = format(dec.normalize(), "f")
    return text


def _money(amount: Any, currency: str) -> str:
    dec = _dec(amount)
    return f"{currency} {dec:,.2f}" if dec is not None else f"{currency} {amount}"


async def _rollback(ctx: Any) -> None:
    try:
        await ctx.db.rollback()
    except Exception as exc:  # a failed rollback must not hide the real error
        logger.debug("rollback after a failed negotiation action: %s", exc)


# ---------------------------------------------------------------------------
# Who / what
# ---------------------------------------------------------------------------
def _other(conn: Any, user_id: Any) -> Any:
    return conn.receiver if conn.sender_id == user_id else conn.sender


def other_name(conn: Any, user_id: Any) -> str:
    """The counterparty's display name, the way the legacy chat resolves it (company, else contact name)."""
    other = _other(conn, user_id)
    prof = getattr(other, "profile", None)
    return str(getattr(prof, "company_name", None) or getattr(prof, "name", None) or "the counterparty")


def _listing_owner_is_me(conn: Any, user_id: Any) -> bool:
    rfq = getattr(conn, "rfq", None)
    return rfq is not None and getattr(rfq, "user_id", None) == user_id


def my_side(conn: Any, user_id: Any) -> Optional[str]:
    """buyer / seller: the side of the listing's owner when it is mine, the opposite otherwise."""
    rfq = getattr(conn, "rfq", None)
    role = _val(getattr(rfq, "role", None)) if rfq is not None else None
    if role not in ("buyer", "seller"):
        return None
    if _listing_owner_is_me(conn, user_id):
        return str(role)
    return "seller" if role == "buyer" else "buyer"


def quote_row(q: Any, user_id: Any) -> dict[str, Any]:
    """One quote for the model; text written by the counterparty is fenced."""
    mine = q.sender_id == user_id
    notes, terms = getattr(q, "notes", None), getattr(q, "payment_terms", None)
    return {
        "quote_id": str(q.id), "number": getattr(q, "quote_number", None), "version": getattr(q, "version", None),
        "status": _val(q.status), "by": "me" if mine else "them", "addressed_to_me": q.receiver_id == user_id,
        "unit_price": _num(q.unit_price), "currency": q.currency, "quantity": _num(q.quantity), "unit": q.quantity_unit,
        "total": _num(getattr(q, "total_amount", None) if getattr(q, "total_amount", None) is not None else (_dec(q.unit_price) or 0) * (_dec(q.quantity) or 0)),
        "lead_time_days": getattr(q, "lead_time_days", None), "incoterms": _val(getattr(q, "incoterms", None)),
        "payment_terms": terms if mine or not terms else as_data(str(terms)[:200], "quote_payment_terms"),
        "valid_until": _iso(getattr(q, "valid_until", None)),
        "notes": notes if mine or not notes else as_data(str(notes)[:600], "quote_notes"),
        "created_at": _iso(getattr(q, "created_at", None)),
    }


def _rfq_snapshot(conn: Any, user_id: Any) -> dict[str, Any]:
    rfq = getattr(conn, "rfq", None)
    if rfq is None:
        return {}
    owner_me = _listing_owner_is_me(conn, user_id)
    title = str(getattr(rfq, "title", "") or "")[:160]
    return {
        "rfq_id": str(rfq.id), "title": title if owner_me else as_data(title, "listing_title"),
        "listing_owner": "me" if owner_me else "them", "listing_side": _val(getattr(rfq, "role", None)),
        "my_side": my_side(conn, user_id),
        "target_price": None if getattr(rfq, "price_amount", None) is None else _num(rfq.price_amount),
        "currency": getattr(rfq, "price_currency", None), "per_unit": getattr(rfq, "price_per_unit", None),
        "quantity": None if getattr(rfq, "quantity_value", None) is None else _num(rfq.quantity_value),
        "unit": getattr(rfq, "quantity_unit", None), "status": _val(getattr(rfq, "status", None)),
    }


async def resolve_connection(ctx: Any, ref: Any) -> tuple[Any, Optional[str]]:
    """The connection by full id, short id (prefix) or the chat's focus; the user must be a party to it."""
    if not ctx.can_act:
        return None, "This needs a signed-in account; it is not available in this session."
    text = str(ref or getattr(ctx, "active_connection_id", None) or "").strip()
    if not text:
        return None, "which negotiation? give connection_id (call list_negotiations first)"
    try:
        wanted = uuid.UUID(text)
    except ValueError:
        wanted = None
    try:
        if wanted is not None:
            return await connection_service.get_connection(ctx.db, wanted, ctx.user.id), None
        if is_id_prefix(text):
            from models.connection import Connection

            uid = ctx.user.id
            row, why = await resolve_short_id(
                ctx.db, Connection, text, or_(Connection.sender_id == uid, Connection.receiver_id == uid),
                noun="negotiation", title=lambda c: f"negotiation {_val(c.status)}",
            )
            if row is not None:
                return await connection_service.get_connection(ctx.db, row.id, uid), None
            return None, why
    except HTTPException as exc:
        return None, _detail(exc)
    return None, f"no single negotiation of yours matches '{text[:40]}': call list_negotiations"


async def _quotes(ctx: Any, conn: Any) -> list[Any]:
    """Every quote on an accepted connection, newest version first ([] when not accepted yet)."""
    if _val(conn.status) != ConnectionStatus.ACCEPTED.value:
        return []
    rows = await quotation_service.list_quotations(ctx.db, conn.id, ctx.user.id)
    return sorted(rows, key=lambda q: getattr(q, "version", 0) or 0, reverse=True)


def _last_message_from(conn: Any, user_id: Any) -> Optional[str]:
    msgs = getattr(conn, "messages", None) or []
    return None if not msgs else ("me" if msgs[-1].sender_id == user_id else "them")


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------
def _thread_row(conn: Any, user_id: Any, quotes: list[Any]) -> dict[str, Any]:
    latest = quotes[0] if quotes else None
    status = _val(conn.status)
    return {
        "connection_id": str(conn.id), "short_id": str(conn.id)[:8],
        "counterparty": as_data(other_name(conn, user_id)[:100], "company_name"),
        "listing": _rfq_snapshot(conn, user_id).get("title"), "listing_owner": "me" if _listing_owner_is_me(conn, user_id) else "them",
        "status": status, "direction": "sent" if conn.sender_id == user_id else "received",
        "latest_quote": quote_row(latest, user_id) if latest is not None else None,
        "request_waiting_for_my_answer": status == "pending" and conn.receiver_id == user_id,
        "quote_waiting_for_my_answer": bool(latest is not None and _val(latest.status) == "pending" and latest.receiver_id == user_id),
        "messages": len(getattr(conn, "messages", None) or []), "last_message_from": _last_message_from(conn, user_id),
    }


async def list_threads(ctx: Any, rfq_id: Optional[uuid.UUID]) -> dict[str, Any]:
    user = ctx.user
    try:
        if rfq_id is not None:
            conns = list(await connection_service.list_connections_for_rfq(ctx.db, rfq_id, user.id))
        else:
            conns = list(await connection_service.list_user_connections(ctx.db, user.id, limit=MAX_THREADS))
    except HTTPException as exc:
        return _err(_detail(exc))
    rows = []
    for conn in conns[:MAX_THREADS]:
        try:
            quotes = await _quotes(ctx, conn)
        except HTTPException:
            quotes = []
        rows.append(_thread_row(conn, user.id, quotes))
    return _ok({"negotiations": rows, "total": len(conns), "note": None if rows else "no connections yet"})


async def get_thread(ctx: Any, ref: Any) -> dict[str, Any]:
    conn, why = await resolve_connection(ctx, ref)
    if conn is None:
        return _err(why or "negotiation not found")
    uid = ctx.user.id
    try:
        messages = list(await connection_service.list_messages(ctx.db, conn.id, uid))[-MAX_MESSAGES:]
        quotes = await _quotes(ctx, conn)
    except HTTPException as exc:
        return _err(_detail(exc))
    msgs = [
        {"from": "me" if m.sender_id == uid else "them",
         "text": str(m.content or "")[:1000] if m.sender_id == uid else as_data(str(m.content or "")[:1000], "counterparty_message"),
         "at": _iso(getattr(m, "created_at", None))}
        for m in messages
    ]
    return _ok({
        "connection_id": str(conn.id), "status": _val(conn.status),
        "counterparty": as_data(other_name(conn, uid)[:100], "company_name"),
        "listing": _rfq_snapshot(conn, uid), "messages": msgs,
        "quotes": [quote_row(q, uid) for q in quotes[:MAX_QUOTES]],
        "note": None if _val(conn.status) == "accepted" else "the connection is not accepted: no messages or quotes are possible yet",
    })


async def brief(ctx: Any) -> Optional[dict[str, Any]]:
    """A few lines for the system prompt: the user's active threads and the one the chat is focused on."""
    if not ctx.can_act:
        return None
    try:
        async with savepoint(ctx.db):  # a failing query must never leave the turn's session in an aborted transaction
            conns = list(await connection_service.list_user_connections(ctx.db, ctx.user.id, limit=BRIEF_THREADS))
            focus = None
            active = getattr(ctx, "active_connection_id", None)
            if active:
                try:
                    focus = await connection_service.get_connection(ctx.db, uuid.UUID(str(active)), ctx.user.id)
                except (HTTPException, ValueError):
                    focus = None
    except Exception as exc:
        logger.debug("negotiation brief skipped: %s", exc)
        return None
    uid = ctx.user.id

    def line(c: Any) -> dict[str, Any]:
        return {
            "id": str(c.id), "with": as_data(other_name(c, uid)[:60], "company_name"), "status": _val(c.status),
            "listing": _rfq_snapshot(c, uid).get("title"), "last_message_from": _last_message_from(c, uid),
        }

    if not conns and focus is None:
        return None
    return {"threads": [line(c) for c in conns], "focus": line(focus) if focus is not None else None}


# ---------------------------------------------------------------------------
# Propose: validate and build the recap
# ---------------------------------------------------------------------------
def _hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:32]


def _quote_marker(q: Any) -> dict[str, Any]:
    return {k: str(v) for k, v in (
        ("id", q.id), ("status", _val(q.status)), ("v", getattr(q, "version", "")), ("p", _num(q.unit_price)),
        ("q", _num(q.quantity)), ("c", q.currency), ("u", q.quantity_unit)
    )}


async def target_hash(action: str, args: dict[str, Any], ctx: Any) -> Optional[str]:
    """What the recap was about: the exact arguments, plus (for quotes) the quote state they were validated against."""
    base: dict[str, Any] = {"action": action, "args": args}
    if action in ("send_quote", "accept_quote", "reject_quote"):
        conn, _why = await resolve_connection(ctx, args.get("connection_id"))
        if conn is None:
            return None
        try:
            quotes = await _quotes(ctx, conn)
        except HTTPException:
            return None
        if action == "send_quote":
            base["latest"] = _quote_marker(quotes[0]) if quotes else None
        else:
            target = next((q for q in quotes if str(q.id) == str(args.get("quote_id"))), None)
            if target is None:
                return None
            base["quote"] = _quote_marker(target)
    return _hash(base)


def same_args(given: dict[str, Any], stored: dict[str, Any]) -> Optional[str]:
    """The first key whose value differs between what the model passes to confirm and what the user saw."""
    for key, value in given.items():
        if key not in stored:
            return key
        a, b = _dec(value), _dec(stored[key])
        if a is not None and b is not None:
            if a != b:
                return key
        elif " ".join(str(value).split()).casefold() != " ".join(str(stored[key]).split()).casefold():
            return key
    return None


Proposal = tuple[Optional[PendingAction], Optional[str], list[str], Optional[str]]


def _fail(why: str) -> Proposal:
    return None, None, [], why


async def _finish(
    action: str, args: dict[str, Any], recap: str, chips: list[str], ctx: Any, facts: Optional[list[str]] = None,
) -> Proposal:
    digest = await target_hash(action, args, ctx)
    if digest is None:
        return _fail("could not verify the negotiation state: try again")
    return PendingAction(action, args, digest, recap, facts=[str(f) for f in facts or [] if str(f).strip()]), recap, chips, None


async def propose(action: str, args: dict[str, Any], ctx: Any) -> Proposal:
    if not ctx.can_act:
        return _fail("This needs a signed-in account; it is not available in this session.")
    try:
        if action == "connect":
            return await _propose_connect(args, ctx)
        conn, why = await resolve_connection(ctx, args.get("connection_id"))
        if conn is None:
            return _fail(why or "negotiation not found")
        if _val(conn.status) != ConnectionStatus.ACCEPTED.value:
            return _fail(f"this connection is {_val(conn.status)}: messages and quotes need an accepted connection")
        if action == "send_message":
            return await _propose_message(conn, args, ctx)
        if action == "send_quote":
            return await _propose_quote(conn, args, ctx)
        return await _propose_answer(action, conn, args, ctx)
    except HTTPException as exc:
        return _fail(_detail(exc))


async def _propose_connect(args: dict[str, Any], ctx: Any) -> Proposal:
    ref = str(args.get("rfq_id") or "").strip()
    try:
        rfq = await ctx.db.get(RFQ, uuid.UUID(ref))
    except ValueError:
        rfq, why = await resolve_short_id(
            ctx.db, RFQ, ref, RFQ.user_id != ctx.user.id, RFQ.status == RFQStatus.ACTIVE, noun="active listing",
            title=lambda r: r.title,
        )
        if rfq is None:
            return _fail(why or "rfq_id must be the id (full, or the 8-character short id) of the counterparty's listing from find_matches")
    if rfq is None:
        return _fail("that listing was not found")
    if rfq.user_id == ctx.user.id:
        return _fail("that is the user's own listing: they cannot connect to it")
    if not rfq.is_matchable:
        return _fail("that listing is no longer accepting connection requests")
    for c in await connection_service.list_user_connections(ctx.db, ctx.user.id, limit=100):
        if c.rfq_id == rfq.id and c.sender_id == ctx.user.id:
            if _val(c.status) == "rejected":
                return _fail("this request was declined earlier; the user cannot ask again")
            return _fail(f"already connected: connection {c.id} is {_val(c.status)}. Use get_negotiation instead")
    clean = {"rfq_id": str(rfq.id)}
    t = _t(ctx)
    role = t["role"].get(str(_val(rfq.role)), str(_val(rfq.role)))
    recap = t["connect"].format(title=_plain(rfq.title, 100), role=role)
    return await _finish("connect", clean, recap, list(t["connect_chips"]), ctx, facts=[_plain(rfq.title, 100)])


async def _propose_message(conn: Any, args: dict[str, Any], ctx: Any) -> Proposal:
    text = str(args.get("text") or "").strip()
    try:
        ConnectionMessageCreate(content=text)
    except ValidationError as exc:
        return _fail("the message is not valid: " + _validation_text(exc))
    clean = {"connection_id": str(conn.id), "text": text}
    t = _t(ctx)
    recap = t["message"].format(who=_plain(other_name(conn, ctx.user.id), 60), text=text)
    return await _finish("send_message", clean, recap, list(t["send_chips"]), ctx, facts=[text])


def _t(ctx: Any) -> dict[str, Any]:
    """The texts in the language the code writes in this turn (english / hinglish / hindi_devanagari)."""
    return negotiation_text.texts(getattr(ctx, "lang", "en"))


def _validation_text(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'value'}: {e['msg']}" for e in exc.errors())[:400]


def _price_grounded(args: dict[str, Any], ctx: Any) -> Optional[str]:
    """The price is either the user's own words (grounded) or the model's suggestion (the recap is then the grounding)."""
    said, suggested = str(args.get("price_said") or "").strip(), args.get("suggested") is True
    if said:
        price = _dec(args.get("unit_price"))
        if not guards.is_grounded(said, ctx.sources()):
            return "price_said is not in the user's words: quote them exactly, or set suggested=true if the price is your own proposal"
        if price is None or price not in guards.numbers_in(said):
            return "the price is not in price_said: quote the user's words that contain it, or set suggested=true"
        return None
    if suggested:
        return None
    return "give price_said (the user's exact words with the price), or set suggested=true when the price is your own proposal"


async def _propose_quote(conn: Any, args: dict[str, Any], ctx: Any) -> Proposal:
    why = _price_grounded(args, ctx)
    if why:
        return _fail(why)
    raw = {k: args[k] for k in QUOTE_FIELDS if args.get(k) not in (None, "")}
    for key in ("unit_price", "quantity"):
        if key in raw:
            raw[key] = str(raw[key])
    try:
        payload = QuotationCreate(**raw)
    except ValidationError as exc:
        return _fail("the quote is not valid: " + _validation_text(exc))
    except TypeError:
        return _fail("the quote has unknown fields")
    rfq = await quotation_service._ensure_rfq_open(ctx.db, conn.rfq_id)
    await quotation_service._ensure_no_live_order(ctx.db, conn.id)
    rfq_unit = quotation_service.rfq_unit_of(rfq)
    currency = payload.currency or rfq.price_currency or "INR"
    unit = payload.quantity_unit or rfq_unit or "pcs"
    quotation_service._check_unit_compatible(unit, rfq_unit)
    clean: dict[str, Any] = {
        "connection_id": str(conn.id), "unit_price": _num(payload.unit_price), "quantity": _num(payload.quantity),
        "quantity_unit": unit, "currency": currency, "valid_days": payload.valid_days or 14,
        "suggested": args.get("suggested") is True and not str(args.get("price_said") or "").strip(),
    }
    for key in ("lead_time_days", "payment_terms", "notes"):
        if getattr(payload, key) not in (None, ""):
            clean[key] = getattr(payload, key)
    if payload.incoterms is not None:
        clean["incoterms"] = _val(payload.incoterms)
    uid = ctx.user.id
    quotes = await _quotes(ctx, conn)
    total = payload.unit_price * payload.quantity
    t = _t(ctx)
    lines = [
        t["quote_head"].format(who=_plain(other_name(conn, uid), 60), title=_plain(getattr(conn.rfq, 'title', ''), 80)),
        t["price"].format(price=_money(payload.unit_price, currency), unit=unit),
        t["quantity"].format(qty=_num(payload.quantity), unit=unit),
        t["total"].format(total=_money(total, currency)),
    ]
    if "lead_time_days" in clean:
        lines.append(t["lead"].format(n=clean["lead_time_days"]))
    if "incoterms" in clean:
        lines.append(t["incoterms"].format(v=clean["incoterms"]))
    if "payment_terms" in clean:
        lines.append(t["payment"].format(v=_plain(clean["payment_terms"], 120)))
    lines.append(t["valid"].format(n=clean["valid_days"]))
    if "notes" in clean:
        lines.append(t["notes"].format(v=_plain(clean["notes"], 300)))
    latest = quotes[0] if quotes else None
    if latest is not None and _val(latest.status) == "pending":
        old = f"v{latest.version} ({_money(latest.unit_price, latest.currency)} per {latest.quantity_unit})"
        lines.append(t["replaces" if latest.sender_id == uid else "counter"].format(old=old))
    if clean["suggested"]:
        lines.append(t["suggested"])
    lines.append(t["quote_ask"])
    facts = [clean["unit_price"], clean["quantity"], *(str(clean[k]) for k in ("lead_time_days", "valid_days") if k in clean)]
    return await _finish("send_quote", clean, "\n".join(lines), list(t["send_chips"]), ctx, facts=facts)


async def _propose_answer(action: str, conn: Any, args: dict[str, Any], ctx: Any) -> Proposal:
    uid = ctx.user.id
    quotes = await _quotes(ctx, conn)
    ref = str(args.get("quote_id") or "").strip().lower()
    if ref:
        matches = [q for q in quotes if str(q.id).lower().startswith(ref) or str(getattr(q, "quote_number", "")).lower() == ref]
    else:
        matches = [q for q in quotes if _val(q.status) == "pending" and q.receiver_id == uid]
    if len(matches) != 1:
        return _fail("which quote? give quote_id (get_negotiation lists them)" if not matches else "several quotes match (" + ", ".join(f"{q.quote_number} ({str(q.id)[:8]})" for q in matches[:3]) + "): give more of the quote_id")
    quote = matches[0]
    verb = "accept" if action == "accept_quote" else "decline"
    if quote.receiver_id != uid:
        return _fail(f"the user sent quote {quote.quote_number} themselves: only the other side can {verb} it")
    if _val(quote.status) != QuotationStatus.PENDING.value:
        return _fail(f"quote {quote.quote_number} is {_val(quote.status)}, not pending: it cannot be {verb}ed")
    valid_until = getattr(quote, "valid_until", None)
    if valid_until is not None and valid_until < datetime.now(timezone.utc):
        return _fail(f"quote {quote.quote_number} has expired")
    if action == "accept_quote":
        await quotation_service._ensure_rfq_open(ctx.db, quote.rfq_id)
        await quotation_service._ensure_no_live_order(ctx.db, conn.id, exclude_id=quote.id)
    clean: dict[str, Any] = {"connection_id": str(conn.id), "quote_id": str(quote.id)}
    reason = " ".join(str(args.get("reason") or "").split())[:500]
    if action == "reject_quote" and reason:
        clean["reason"] = reason
    row = quote_row(quote, uid)
    who = _plain(other_name(conn, uid), 60)
    total = f"{_money(quote.total_amount, quote.currency)}" if getattr(quote, "total_amount", None) is not None else ""
    t = _t(ctx)
    desc = t["desc"].format(
        number=quote.quote_number, who=who, price=_money(quote.unit_price, quote.currency), unit=quote.quantity_unit,
        qty=_num(quote.quantity), total=total)
    extras = [t["x_lead"].format(n=row["lead_time_days"])] if row["lead_time_days"] else []
    if row["incoterms"]:
        extras.append(t["x_incoterms"].format(v=row["incoterms"]))
    if getattr(quote, "payment_terms", None):
        extras.append(t["x_payment"].format(v=_plain(quote.payment_terms, 80)))
    if extras:
        desc += " (" + ", ".join(extras) + ")"
    if action == "accept_quote":
        recap, chips = t["accept"].format(desc=desc), list(t["accept_chips"])
    else:
        recap = t["reject"].format(desc=desc, reason=t["reason"].format(v=reason) if reason else "")
        chips = list(t["reject_chips"])
    return await _finish(action, clean, recap, chips, ctx, facts=[quote.quote_number, _num(quote.unit_price), _num(quote.quantity)])


# ---------------------------------------------------------------------------
# Confirm: run exactly the stored arguments
# ---------------------------------------------------------------------------
async def execute(pending: PendingAction, ctx: Any) -> tuple[Optional[dict[str, Any]], Optional[str], dict[str, Any], Optional[tuple[str, str]]]:
    """Returns (result, error, wire step, (needle, fact text the reply must contain))."""
    action, args, uid = pending.action, pending.args, ctx.user.id
    t = _t(ctx)
    titles = {
        "connect": "Sending Connection Request", "send_message": "Sending Message", "send_quote": "Sending Quotation",
        "accept_quote": "Accepting Quotation", "reject_quote": "Declining Quotation",
    }
    try:
        if action == "connect":
            conn = await connection_service.create_connection(ctx.db, uid, uuid.UUID(str(args["rfq_id"])))
            done = {"connection_id": str(conn.id), "status": _val(conn.status)}
            fact = (str(conn.id), t["done_connect"].format(id=conn.id))
        elif action == "send_message":
            peer, _why = await resolve_connection(ctx, args["connection_id"])
            msg = await connection_service.send_message(
                ctx.db, uuid.UUID(args["connection_id"]), uid, ConnectionMessageCreate(content=args["text"]))
            done = {"connection_id": args["connection_id"], "message_id": str(msg.id)}
            ctx.counterparty_message = {
                "connection_id": args["connection_id"], "counterparty_name": other_name(peer, uid) if peer is not None else "the counterparty",
                "message": args["text"], "status": "delivered", "message_id": str(msg.id),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            fact = (str(msg.id), t["done_message"].format(id=msg.id, text=args["text"]))
        elif action == "send_quote":
            fields = {k: args[k] for k in QUOTE_FIELDS if args.get(k) not in (None, "")}
            quote = await quotation_service.create_quotation(
                ctx.db, uuid.UUID(args["connection_id"]), uid, QuotationCreate(**fields))
            done = {"connection_id": args["connection_id"], "quote": quote_row(quote, uid)}
            fact = (str(quote.id), t["done_quote"].format(
                number=quote.quote_number, price=_money(quote.unit_price, quote.currency), unit=quote.quantity_unit,
                qty=_num(quote.quantity), total=_money(quote.total_amount, quote.currency), id=quote.id))
        elif action == "accept_quote":
            quote, award = await quotation_service.accept_quotation(
                ctx.db, uuid.UUID(args["connection_id"]), uuid.UUID(args["quote_id"]), uid)
            done = {"connection_id": args["connection_id"], "quote": quote_row(quote, uid), "award": {
                k: (str(v) if isinstance(v, Decimal) else v) for k, v in (award or {}).items()}}
            note = t["over_award"] if (award or {}).get("over_award") else ""
            fact = (str(quote.id), t["done_accept"].format(
                number=quote.quote_number, id=quote.id, po=getattr(quote, "purchase_order_reference", None) or t["po_created"], note=note))
        else:
            quote = await quotation_service.reject_quotation(
                ctx.db, uuid.UUID(args["connection_id"]), uuid.UUID(args["quote_id"]), uid, reason=args.get("reason"))
            done = {"connection_id": args["connection_id"], "quote": quote_row(quote, uid)}
            fact = (str(quote.id), t["done_reject"].format(number=quote.quote_number, id=quote.id))
    except HTTPException as exc:
        await _rollback(ctx)
        err = _detail(exc)
        return None, err, {"name": action, "title": titles[action], "status": "failed", "args": {"error": err}}, None
    except ValidationError as exc:
        await _rollback(ctx)
        err = _validation_text(exc)
        return None, err, {"name": action, "title": titles[action], "status": "failed", "args": {"error": err}}, None
    step = {"name": action, "title": titles[action], "status": "completed", "args": {"id": next(iter(done.values()))}}
    return done, None, step, fact
