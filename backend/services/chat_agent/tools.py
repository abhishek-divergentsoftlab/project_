"""What the chat agent's model may call. Each executor VALIDATES and acts; it never decides what the user meant.

Every executor returns ``{"ok": bool, "result" | "error": ..., "ui": {...optional}}``. The model sees ``ok`` and
``result`` / ``error``; ``ui`` (rfq_draft, created_rfq, quick_replies, matched_candidates, step) goes to the wire.

Binding actions are two-phase: ``propose_action`` validates and stores a pending action plus a deterministic recap,
``confirm_action`` runs it only if the draft is unchanged since the recap. The model decides when the user's message
is a "yes"; code only enforces that a recap was shown for exactly this draft.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
import uuid
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from models.enums import RFQStatus
from models.rfq import RFQ
from services import categories, market_data_tools, platform_facts, spec_catalog
from services.chat_agent import actions, draft_assist, guards, negotiation, recaps, rfq_draft, rfq_edit
from services.chat_agent.state import AgentState, PendingAction
from services.untrusted_text import as_data

logger = logging.getLogger(__name__)

MAX_QUICK_REPLIES = 6
ALL_STATUSES = (RFQStatus.DRAFT, RFQStatus.ACTIVE, RFQStatus.EXPIRED, RFQStatus.CLOSED)
NO_ACCOUNT = "This needs a signed-in account; it is not available in this session."


@dataclass
class ToolContext:
    """Everything the executors of one turn share."""

    state: AgentState
    assist: draft_assist.DraftAssist
    user_texts: list[str]
    db: Any = None
    user: Any = None
    conversation: Any = None
    lang: str = "en"
    active_connection_id: Optional[str] = None  # the negotiation the chat was opened on
    persist: Optional[Callable[[], Awaitable[None]]] = None  # saves the state before a binding action runs
    # -- what the turn produced (read by the engine) --
    quick_replies: Optional[list[str]] = None
    created: Optional[dict[str, Any]] = None  # the RFQ a create / publish / save / close just returned
    matched: Optional[list[dict[str, Any]]] = None
    steps: list[dict[str, Any]] = field(default_factory=list)  # wire tool_steps, in order
    recap: Optional[str] = None  # the recap a propose_action showed this turn
    facts: list[tuple[str, str]] = field(default_factory=list)  # (needle, text): text must reach the reply if needle is absent
    forced_reply: Optional[str] = None  # a deterministic reply that replaces the model's (prohibited goods)
    new_shown: list[str] = field(default_factory=list)  # offered this turn: groundable from the NEXT turn on
    mutated: bool = False  # the draft or an RFQ changed this turn
    refresh_task: Optional["asyncio.Task[None]"] = None  # HSN / spec suggestions / moderation, running while the model works
    answered_specs: set[str] = field(default_factory=set)  # spec keys the user gave a value for this turn
    counterparty_message: Optional[dict[str, Any]] = None  # the message a send_message just delivered (wire field)

    @property
    def can_act(self) -> bool:
        return self.db is not None and self.user is not None

    def sources(self) -> list[str]:
        """Texts a ``said`` may come from: the user's messages and what earlier turns showed. Never this turn's own output."""
        return [*self.user_texts, *self.state.shown]


Executor = Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Tool:
    name: str
    schema: dict[str, Any]
    run: Executor
    title: str = ""  # the wire tool_step title; empty = a status line only
    concurrent_ok: bool = False  # touches neither the database session nor the draft


def _err(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, **extra}


def _ok(result: Any, **ui: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": True, "result": result}
    if ui:
        out["ui"] = ui
    return out


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def postable_roles(user: Any) -> list[str]:
    """The RFQ sides an account may post: both for a BOTH account, else its own."""
    role = getattr(getattr(user, "role", None), "value", getattr(user, "role", None))
    if role in ("buyer", "seller"):
        return [str(role)]
    return ["buyer", "seller"] if user is not None else []


def _draft_name(draft: dict[str, Any]) -> str:
    return draft_assist.product_name(draft)


def _short(rfq_id: Any) -> str:
    return str(rfq_id)[:8]


def open_specs(draft: dict[str, Any], assist: draft_assist.DraftAssist, limit: int = 4) -> list[dict[str, Any]]:
    """The next specs worth asking, with their options, from the stored suggestions."""
    out = []
    for spec in recaps.unfilled_specs(draft, assist.suggestions)[:limit]:
        out.append({
            "key": spec.get("key"), "label": spec.get("label"), "options": list(spec.get("options") or [])[:8],
            "unit": spec.get("unit"),
        })
    return out


def hsn_hint(draft: dict[str, Any], assist: draft_assist.DraftAssist) -> Optional[dict[str, Any]]:
    """The model's HSN suggestion for the product while none is applied."""
    sug = assist.pending_hsn
    if not sug or draft_assist.user_hsn(draft):
        return None
    return {"code": sug.get("code"), "description": sug.get("description")}


def draft_status(ctx: ToolContext) -> dict[str, Any]:
    """The draft, what is still missing and the next specs: what the model needs after every change."""
    draft = ctx.state.draft
    readiness = rfq_draft.evaluate_rfq_readiness(draft)
    return {
        "draft": draft,
        "missing": [*readiness["missing_required"], *[f for f in readiness["missing_optional"] if f != "description"]],
        "ready_to_create": readiness["required_complete"],
        "open_specs": open_specs(draft, ctx.assist),
        "hsn_suggestion": hsn_hint(draft, ctx.assist),
    }


def _changed_paths(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in sorted({*old, *new} - {"product_details"}):
        if old.get(key) != new.get(key):
            paths.append(key)
    od, nd = old.get("product_details") or {}, new.get("product_details") or {}
    for key in sorted({*od, *nd}):
        if od.get(key) != nd.get(key):
            paths.append("product_name" if key == "name" else f"specifications.{key}" if key not in guards.CORE_DETAIL_KEYS else key)
    return paths


def _drop_product_dependents(draft: dict[str, Any], evidence: dict[str, str], assist: draft_assist.DraftAssist, keep_category: bool) -> None:
    """A different product: its category, HSN code and specs belonged to the old one (the commercial terms stay)."""
    details = dict(draft.get("product_details") or {})
    draft["product_details"] = {k: v for k, v in details.items() if k == "name"}
    if not keep_category:
        draft.pop("category", None)
    for key in list(evidence):
        if key.startswith("specifications.") or key in ("hsn_code", "must_match") or (key == "category" and not keep_category):
            evidence.pop(key)
    assist.reset()


async def _resolve_category(draft: dict[str, Any]) -> None:
    """Fill a missing category from the product (cached deterministic resolve, else one cached classification)."""
    name = _draft_name(draft)
    if not name or draft.get("category"):
        return
    try:
        draft["category"] = await categories.resolve_category(None, title=name, description=draft.get("description"))
    except categories.CategoryError:
        return  # left empty: creation falls back to "General Goods"
    except Exception as exc:  # the model is down: the draft stays valid without a category
        logger.debug("category resolve skipped: %s", exc)


def _enforce_account_side(ctx: ToolContext, draft: dict[str, Any], rejected: list[dict[str, str]]) -> None:
    """The side must be one the account can post; an account with one side gets it without being asked."""
    sides = postable_roles(ctx.user)
    if not sides:
        return
    role = draft.get("role")
    if role and role not in sides:
        rejected.append({"field": "role", "said": "", "reason": (
            f"this account can only post as a {sides[0]}, so a {role} RFQ cannot be created from it: "
            f"tell the user and offer to draft it as a {sides[0]} instead")})
        draft.pop("role", None)
        ctx.state.evidence.pop("role", None)
        role = None
    if not role and len(sides) == 1 and _draft_name(draft):
        draft["role"] = sides[0]
        ctx.state.evidence["role"] = "account"


# ---------------------------------------------------------------------------
# update_rfq_draft
# ---------------------------------------------------------------------------
def _entry(value: Optional[dict[str, Any]] = None, **props: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            **({"value": value} if value else {}), **props,
            "said": {"type": "string", "description": "the user's exact words this came from"},
        },
        "required": ["said"],
    }


_STR = {"type": "string"}
_NUM = {"type": "number"}
UPDATE_FIELDS_SCHEMA = {
    "type": "object",
    "description": (
        "Only the fields the user gave in this conversation; omit or null everything else. Shape, e.g. "
        "{\"quantity\": {\"value\": 500, \"unit\": \"kg\", \"said\": \"500 kg\"}, "
        "\"location\": {\"city\": \"Indore\", \"said\": \"Indore\"}, "
        "\"price_target\": {\"amount\": 80, \"currency\": \"INR\", \"per_unit\": \"kg\", \"said\": \"80 rs per kg\"}, "
        "\"specifications\": {\"grain_length\": {\"value\": \"8 mm\", \"said\": \"8mm\"}}}. "
        "Other fields: role (buyer|seller), product_name, category, description, hsn_code, minimum_order, "
        "deadline {days|date}, must_match [spec keys]. `value`, `city`, `unit` and `per_unit` are always the standard "
        "English (Latin letters) name, whatever language the user wrote: product 'Tomato', city 'Indore', unit 'tonne'; "
        "`said` keeps the user's own words."
    ),
    "properties": {
        "role": {"description": "buyer|seller, a bare string or {value, said?}; no quote needed", "anyOf": [{"type": "string", "enum": ["buyer", "seller"]}, {"type": "object", "properties": {"value": {"type": "string", "enum": ["buyer", "seller"]}, "said": _STR}, "required": ["value"]}]},
        "product_name": _entry(_STR),
        "category": _entry(_STR),
        "description": _entry(_STR),
        "hsn_code": _entry(_STR),
        "quantity": _entry(_NUM, unit=_STR),
        "minimum_order": _entry(_NUM, unit=_STR),
        "price_target": _entry(amount=_NUM, currency=_STR, per_unit=_STR),
        "location": _entry(city=_STR, state=_STR, country=_STR),
        "deadline": _entry(days={"type": "integer"}, date={"type": "string", "description": "YYYY-MM-DD"}),
        "specifications": {
            "type": "object",
            "description": "spec key -> {value, said}, e.g. {\"grain_length\": {\"value\": \"8 mm\", \"said\": \"8mm\"}}",
            "additionalProperties": _entry({"type": ["string", "number", "array"]}),
        },
        "must_match": _entry({"type": "array", "items": _STR}),
    },
}


REJECTED_NOTE = "These fields were NOT saved: re-send them with the user's exact words in `said`, or ask the user."


def _remember_offer(ctx: ToolContext) -> None:
    hint = hsn_hint(ctx.state.draft, ctx.assist)
    if hint:  # shown to the user this turn: a "yes" next turn may quote the code
        ctx.new_shown.extend(str(x) for x in (hint["code"], hint["description"]) if x)


def _ground_location(kwargs: dict[str, Any]) -> None:
    """A city the gazetteer knows gets its standard spelling, state and country (what the user named stays as said)."""
    city = str(kwargs.get("city") or "").strip()
    if not city:
        return

    try:
        found = actions._draft_location(city, kwargs.get("country"))
    except Exception as exc:  # the gazetteer is a help, never a reason to lose the city
        logger.debug("location grounding skipped: %s", exc)
        return
    for key in ("city", "state", "country"):
        if found.get(key) and (key == "city" or not kwargs.get(key)):
            kwargs[key] = found[key]


async def update_rfq_draft(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    state, assist = ctx.state, ctx.assist
    old = copy.deepcopy(state.draft)
    draft = copy.deepcopy(state.draft)
    cleaned = guards.clean_update(args.get("fields"), args.get("remove"), ctx.sources(), draft)
    if cleaned.empty:
        return _err("nothing was applied", rejected=cleaned.rejected or [{"field": "fields", "said": "", "reason": "no values given"}])

    _ground_location(cleaned.kwargs)
    removed = guards.apply_removals(draft, cleaned.remove)
    evidence = guards.evidence_after_removal(state.evidence, removed)
    new_name = cleaned.kwargs.get("product_name")
    if new_name and _draft_name(draft) and not guards.same_product(_draft_name(draft), new_name):
        _drop_product_dependents(draft, evidence, assist, keep_category="category" in cleaned.kwargs)

    kwargs: dict[str, Any] = {**cleaned.kwargs}
    notes: list[str] = []
    kwargs["_notes"] = notes
    if cleaned.specs:
        kwargs["product_details"] = dict(cleaned.specs)
    new = rfq_draft.update_rfq_draft(draft, **kwargs)
    state.evidence = {**evidence, **cleaned.evidence}
    rejected = list(cleaned.rejected)

    if state.editing_snapshot and rfq_edit.enforce_locked_role(new, state.editing_snapshot) and "role" in cleaned.kwargs:
        rejected.append({"field": "role", "said": "", "reason": "the side of a saved RFQ cannot be changed; create a new RFQ for the other side"})
    state.draft = new
    _enforce_account_side(ctx, new, rejected)
    await _resolve_category(new)
    ctx.answered_specs |= set(cleaned.specs)
    draft_assist.apply_spec_suggestions(new, assist.suggestions.get("specs"), answered=ctx.answered_specs)
    await settle(ctx)  # an earlier refresh still running must finish before the next one starts
    if _draft_name(new):
        ctx.refresh_task = asyncio.create_task(assist.refresh(copy.deepcopy(new)))  # overlaps with the model's next step
    changed = _changed_paths(old, new)
    ctx.mutated = ctx.mutated or bool(changed)
    _remember_offer(ctx)
    if not changed and rejected:
        return _err("nothing was applied", rejected=rejected, rejected_note=REJECTED_NOTE, **draft_status(ctx))
    ctx.steps.append({
        "name": "update_rfq_draft", "title": "Updating RFQ Preferences & Draft", "status": "completed", "args": {"applied": changed},
    })
    result: dict[str, Any] = {"applied": changed}
    if rejected:  # first and loud: the model must fix these in this turn, they are NOT in the draft
        result["rejected"] = rejected
        result["rejected_note"] = REJECTED_NOTE
    return _ok({**result, "notes": notes, **draft_status(ctx)}, rfq_draft=new)


async def settle(ctx: ToolContext) -> None:
    """Wait for the background refresh, put the specs in catalogue form and drop a draft flagged as prohibited goods."""
    task, ctx.refresh_task = ctx.refresh_task, None
    if task is not None:
        await task  # refresh never raises
    draft = ctx.state.draft
    if not _draft_name(draft):
        return
    draft_assist.apply_spec_suggestions(draft, ctx.assist.suggestions.get("specs"), answered=ctx.answered_specs)
    blocked = ctx.assist.blocked_category(draft)
    if blocked:
        _prohibited(ctx, draft, blocked)


def _prohibited(ctx: ToolContext, draft: dict[str, Any], category: str) -> dict[str, Any]:
    """Prohibited goods are never drafted: the draft is dropped and the user is told why, in fixed words."""
    from services.moderation_service import CATEGORY_DESCRIPTIONS

    what = _draft_name(draft) or "that"
    ctx.forced_reply = recaps.blocked_listing_reply(what, CATEGORY_DESCRIPTIONS.get(category, category), ctx.lang)
    ctx.state.reset_draft()
    ctx.assist.reset()
    ctx.mutated = True
    return _err("prohibited goods: nothing was saved. Tell the user this cannot be listed and offer to start again with another product.")


# ---------------------------------------------------------------------------
# Lookups: suggest_specs, lookup_hsn, platform_info, set_quick_replies
# ---------------------------------------------------------------------------
async def suggest_specs(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    name = " ".join(str(args.get("product_name") or "").split()) or _draft_name(ctx.state.draft)
    if not name:
        return _err("product_name is required")
    draft = ctx.state.draft
    if name.casefold() == _draft_name(draft).casefold() and ctx.assist.suggestions.get("specs"):
        specs = ctx.assist.suggestions["specs"]
    else:
        specs = spec_catalog.family_from_suggestions(name, draft.get("category") if _draft_name(draft) else None, None).suggestions()
    rows = [
        {"key": s.get("key"), "label": s.get("label"), "options": list(s.get("options") or [])[:8], "unit": s.get("unit")}
        for s in specs[:10]
    ]
    return _ok({"product": name, "specs": rows})


async def lookup_hsn(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = " ".join(str(args.get("query") or "").split()) or _draft_name(ctx.state.draft)
    if not query:
        return _err("query is required")
    from api.v1 import hsn as hsn_api

    try:
        items = await asyncio.to_thread(hsn_api.search_hsn, query[:100], 6)
        if not items:
            best = await asyncio.wait_for(hsn_api.suggest_hsn(query), timeout=12)
            items = [best] if best else []
    except Exception as exc:
        logger.debug("HSN lookup failed: %s", exc)
        return _err("the HSN lookup is not available right now")
    rows = [{"code": i.code, "description": i.description, "category": i.category} for i in items[:6]]
    ctx.new_shown.extend(str(x) for r in rows for x in (r["code"], r["description"]))
    return _ok({"query": query, "codes": rows}) if rows else _ok({"query": query, "codes": [], "note": "no matching HSN code"})


async def market_prices(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if not ctx.can_act:
        return _err(NO_ACCOUNT)
    product = " ".join(str(args.get("product") or "").split())[:120]
    if not product:
        return _err("product is required")
    role = str(args.get("role") or "").strip().lower()
    place = " ".join(str(args.get("location") or "").split())[:80] or None
    try:
        async with negotiation.savepoint(ctx.db):
            res = await market_data_tools.market_price_stats(
                ctx.db, user=ctx.user, product=product, role=role if role in ("buyer", "seller") else None, city=place,
            )
    except Exception as exc:
        logger.debug("market price stats failed: %s", exc)
        return _err("the price data is not available right now")
    if not res.get("ok"):
        return _err("; ".join(res.get("notes") or []) or "the price data is not available right now")
    data = res.get("data") or {}
    base = {"product": product, **({"location": place} if place else {})}
    if not data.get("n_listings"):
        return _ok({**base, "listings": 0, "answer": "no data on the platform for this product: say so, give no price",
                    "notes": res.get("notes") or []})
    per = f"{data.get('currency')}/{data.get('unit_basis')}"
    stats = {k: data.get(k) for k in ("median", "p25", "p75", "min", "max")}
    return _ok({
        **base, "listings": data["n_listings"], "distinct_users": data.get("n_distinct_users"), "price_per": per, **stats,
        "by_role": {r: {"listings": v.get("n"), "median": v.get("median")} for r, v in (data.get("by_role") or {}).items()},
        "notes": res.get("notes") or [], "answer": f"quote these figures with their unit ({per}) and the number of listings",
    })


async def platform_info(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return _ok({"facts": platform_facts.FACTS_TEXT})


_OPTION_KEYS = ("title", "label", "text", "value", "name")


def _option_text(item: Any) -> str:
    """One chip's text: a string, or an object carrying title / label / text."""
    if isinstance(item, dict):
        item = next((item[k] for k in _OPTION_KEYS if isinstance(item.get(k), (str, int, float)) and str(item[k]).strip()), "")
    return " ".join(str(item or "").split())[:60]


def _clean_options(raw: Any) -> list[str]:
    options: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        text = _option_text(item)
        if text and text not in options:
            options.append(text)
    return options[:MAX_QUICK_REPLIES]


def note_language(args: dict[str, Any], ctx: ToolContext) -> None:
    """The model says which language it is answering in: the texts the code writes (recap, chips) follow it."""
    chosen = args.get("reply_language")
    if isinstance(chosen, str) and chosen.strip().lower() in recaps._ALIASES:
        ctx.lang = recaps.pick_lang(ctx.user_texts[-1] if ctx.user_texts else "", None, chosen)
        ctx.state.lang_hint = recaps.latin_lang(ctx.lang)


async def set_quick_replies(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    note_language(args, ctx)
    options = _clean_options(args.get("options"))
    if len(options) < 2:
        return _err("give 2 to 6 short options")
    ctx.quick_replies = options
    ctx.new_shown.extend(options)
    return _ok({"shown": options, "note": "the chips appear under your message by themselves: do not list them in your text"},
               quick_replies=options)


def _bare(line: str) -> str:
    """A line without list bullets / numbering / bold markers, comparable with canon()."""
    return guards.canon(re.sub(r"^\s*(?:[-*\u2022>]+|\d{1,2}[.)])\s*", "", line))


def _is_heading(line: str) -> bool:
    """A short label line ("**Quick replies:**"): at most four words, ending with a colon or wholly bold."""
    head = line.strip()
    label = head.strip("*_# ").rstrip(":").strip("*_ ").strip()
    if not label or len(label.split()) > 4:
        return False
    return head.rstrip("*_ ").endswith(":") or (head.startswith("**") and head.endswith("**"))


def strip_chip_block(text: str, options: list[str]) -> str:
    """Take a trailing block that only repeats the chips (optionally under a short heading line) out of the model's text.

    Mechanical, on the model's own output: lines are compared with the options by canon(), no word lists.
    """
    wanted = {guards.canon(o) for o in options if guards.canon(o)}
    lines = text.rstrip().split("\n")
    i, hits = len(lines), 0
    while i > 0:
        line = lines[i - 1]
        if not line.strip():
            i -= 1
        elif _bare(line) in wanted:
            hits += 1
            i -= 1
        else:
            break
    if hits < 2:
        return text
    k = i
    while k > 0 and not lines[k - 1].strip():
        k -= 1
    j = k - 1 if k > 0 and _is_heading(lines[k - 1]) else i
    return "\n".join(lines[:j]).rstrip()


def _balanced(text: str, start: int, open_ch: str, close_ch: str) -> int:
    """Index after the bracket group that opens at ``start`` (quotes respected), or -1."""
    depth, quote, i = 0, "", start
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _options_in(chunk: str) -> list[str]:
    """The option strings a written-out set_quick_replies call carries: the first JSON list in it."""
    at = chunk.find("[")
    end = _balanced(chunk, at, "[", "]") if at >= 0 else -1
    if end < 0:
        return []
    body = chunk[at:end]
    try:
        return _clean_options(json.loads(body))
    except ValueError:
        return _clean_options([a or b for a, b in re.findall(r"\"([^\"]+)\"|'([^']+)'", body)])


def strip_tool_markup(text: str) -> tuple[str, list[str], list[str]]:
    """Take a tool call the model wrote into its ANSWER as text out of it: (clean text, quick-reply options, tool names).

    ``<set_quick_replies options=[...]/>``, ``<tool>...</tool>`` and ``tool({...})`` of a known tool name. This reads the
    model's own output markup, never the user's message.
    """
    options: list[str] = []
    found: list[str] = []
    for name in TOOLS:
        if name not in text:
            continue
        n = re.escape(name)
        for pattern in (rf"<{n}\b[^>]*/>", rf"<{n}\b[^>]*>.*?</{n}\s*>", rf"<{n}\b[^>]*>", rf"</{n}\s*>"):
            def cut(m: "re.Match[str]") -> str:
                found.append(name)
                if name == "set_quick_replies" and not options:
                    options.extend(_options_in(m.group(0)))
                return ""
            text = re.sub(pattern, cut, text, flags=re.DOTALL)
        while True:  # tool_name({...}) / tool_name(options=[...])
            m = re.search(rf"(?<![\w.]){n}\s*\(", text)
            if not m:
                break
            end = _balanced(text, m.end() - 1, "(", ")")
            if end < 0:
                break
            found.append(name)
            if name == "set_quick_replies" and not options:
                options.extend(_options_in(text[m.start():end]))
            text = text[:m.start()] + text[end:]
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip(), options, found


# ---------------------------------------------------------------------------
# Reading the user's RFQs
# ---------------------------------------------------------------------------
OWN_RFQS_BRIEF = 6


async def own_rfqs_brief(ctx: ToolContext) -> list[dict[str, Any]]:
    """The user's own RFQs for the system prompt, most recently updated first (one cheap query, inside a savepoint)."""
    if not ctx.can_act:
        return []
    try:
        async with negotiation.savepoint(ctx.db):
            res = await market_data_tools.get_my_rfqs(ctx.db, user=ctx.user, limit=OWN_RFQS_BRIEF)
    except Exception as exc:
        logger.debug("own RFQs brief skipped: %s", exc)
        return []
    if not res.get("ok"):
        return []
    rows = [_brief_row(r) for r in (res.get("data") or {}).get("rfqs", [])]
    for row in rows:  # the list is most recently updated first: "this request" means the first live one
        if row["status"] == RFQStatus.ACTIVE.value:
            row["most_recent_active"] = True
            break
    return rows


def _brief_row(r: dict[str, Any]) -> dict[str, Any]:
    qty = f"{_num_text(r.get('quantity'))} {r.get('quantity_unit') or ''}".strip() if r.get("quantity") is not None else None
    price = None
    if r.get("price") is not None:
        price = f"{_num_text(r['price'])} {r.get('currency') or ''}".strip() + (f"/{r['price_per_unit']}" if r.get("price_per_unit") else "")
    row = {"id": r["id"], "short_id": _short(r["id"]), "title": str(r.get("title") or "")[:80], "status": r.get("status"),
           "side": r.get("role"), "qty": qty, "price": price, "place": r.get("city") or None}
    return {k: v for k, v in row.items() if v is not None}


def _num_text(value: Any) -> str:
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except (InvalidOperation, ValueError):
        return str(value)


async def _own_rfq(ctx: ToolContext, ref: Any, statuses: tuple[RFQStatus, ...]) -> tuple[Any, Optional[str]]:
    """One of the user's RFQs by id, short id or title keyword; (None, reason) otherwise."""

    ref = str(ref or ctx.state.last_rfq_id or "").strip()
    if not ref:
        return None, "which RFQ? give rfq_id (or list_my_rfqs first)"
    if negotiation.is_id_prefix(ref):
        rfq, why = await negotiation.resolve_short_id(
            ctx.db, RFQ, ref, RFQ.user_id == ctx.user.id, RFQ.status.in_(list(statuses)), noun="RFQ", title=lambda r: r.title,
        )
        if rfq is not None or why and "several" in why:
            return rfq, why
    rfq = await actions._resolve_user_rfq(ctx.db, ctx.user, ref, statuses)
    if rfq is None:
        return None, f"no RFQ of yours matches '{ref}' in the status needed"
    if rfq.user_id != ctx.user.id:
        return None, "that RFQ is not yours"
    return rfq, None


async def list_my_rfqs(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if not ctx.can_act:
        return _err(NO_ACCOUNT)
    res = await market_data_tools.get_my_rfqs(ctx.db, user=ctx.user, status=args.get("status") or None, limit=10)
    if not res.get("ok"):
        return _err("; ".join(res.get("notes") or []) or "could not list RFQs")
    rows = [
        {"id": r["id"], "short_id": _short(r["id"]), "title": r["title"], "side": r["role"], "status": r["status"],
         "quantity": r.get("quantity"), "unit": r.get("quantity_unit"), "city": r.get("city")}
        for r in (res.get("data") or {}).get("rfqs", [])
    ]
    return _ok({"rfqs": rows, "note": None if rows else "no RFQs yet"})


async def get_rfq(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if not ctx.can_act:
        return _err(NO_ACCOUNT)
    rfq, why = await _own_rfq(ctx, args.get("rfq_id"), ALL_STATUSES)
    if rfq is None:
        return _err(why or "not found")
    return _ok({"id": str(rfq.id), "status": rfq.status.value, "rfq": rfq_edit.rfq_to_draft(rfq)})


async def start_edit_rfq(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if not ctx.can_act:
        return _err(NO_ACCOUNT)
    rfq, why = await _own_rfq(ctx, args.get("rfq_id"), ALL_STATUSES)
    if rfq is None:
        return _err(why or "not found")
    if rfq.status not in (RFQStatus.DRAFT, RFQStatus.ACTIVE):
        return _err(f"'{rfq.title}' is {rfq.status.value} and can no longer be edited; a new RFQ can be created instead")
    loaded = rfq_edit.rfq_to_draft(rfq)
    state = ctx.state
    state.draft, state.evidence, state.pending = copy.deepcopy(loaded), {}, None
    state.editing_rfq_id, state.editing_snapshot = str(rfq.id), copy.deepcopy(loaded)
    ctx.assist.reset()
    ctx.mutated = True
    ctx.steps.append({
        "name": "edit_rfq", "title": "Loading RFQ for Editing", "status": "completed",
        "args": {"title": rfq.title, "id": str(rfq.id)},
    })
    return _ok(
        {"editing": str(rfq.id), "status": rfq.status.value, **draft_status(ctx),
         "note": "its side cannot change; when the user is done call propose_action save_rfq_edit"},
        rfq_draft=state.draft,
    )


async def find_matches(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if not ctx.can_act:
        return _err(NO_ACCOUNT)
    from models.rfq import RFQ

    ref = str(args.get("rfq_id") or ctx.state.last_rfq_id or ctx.state.editing_rfq_id or "").strip()
    try:
        rfq = await ctx.db.get(RFQ, uuid.UUID(ref)) if ref else None
    except ValueError:
        rfq, _ = await _own_rfq(ctx, ref, ALL_STATUSES)
    if rfq is None or rfq.user_id != ctx.user.id:
        return _err("no RFQ of yours to match yet: create one first or give rfq_id")
    res = await market_data_tools.find_matches(ctx.db, user=ctx.user, rfq_id=rfq.id, limit=5)
    if not res.get("ok"):
        return _err("; ".join(res.get("notes") or []) or "matching failed")
    data = res.get("data") or {}
    ctx.matched = await actions._match_payloads(ctx.db, ctx.user, [res]) or None
    rows = [
        {"id": m["id"], "title": as_data(str(m.get("title") or "")[:120], "listing_title"), "side": m.get("role"),
         "price": m.get("price"), "currency": m.get("currency"), "per": m.get("price_per_unit"),
         "quantity": m.get("quantity"), "unit": m.get("quantity_unit"), "city": m.get("city"),
         "match_pct": m.get("match_score_pct")}
        for m in data.get("matches", [])
    ]
    return _ok(
        {"rfq": str(rfq.id), "total_matches": data.get("total_matches", len(rows)), "top": rows, "notes": res.get("notes") or []},
        matched_candidates=ctx.matched,
    )


async def search_listings(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Browse what other users have on the market: sellers or buyers of a product. Read only; never touches the draft."""
    if not ctx.can_act:
        return _err(NO_ACCOUNT)

    product = " ".join(str(args.get("product") or "").split())[:120]
    if not product:
        return _err("product is required")
    side = str(args.get("side") or "").strip().lower()
    place = " ".join(str(args.get("location") or "").split())[:80] or None
    try:
        async with negotiation.savepoint(ctx.db):
            res = await market_data_tools.search_listings(
                ctx.db, user=ctx.user, product=product, role=side if side in ("buyer", "seller") else None, city=place, limit=8,
            )
    except Exception as exc:
        logger.debug("listing search failed: %s", exc)
        return _err("the listings are not available right now")
    if not res.get("ok"):
        return _err("; ".join(res.get("notes") or []) or "the listings are not available right now")
    rows = (res.get("data") or {}).get("samples") or []
    base = {"product": product, **({"side": side} if side in ("buyer", "seller") else {}), **({"location": place} if place else {})}
    if not rows:
        return _ok({**base, "found": 0, "answer": f"nothing is listed for '{product}' right now: say so plainly, naming the product"})
    ctx.matched = await actions._match_payloads(ctx.db, ctx.user, [res]) or None
    top = [
        {"id": m["id"], "title": as_data(str(m.get("title") or "")[:120], "listing_title"), "side": m.get("role"),
         "price": m.get("price"), "currency": m.get("currency"), "per": m.get("price_per_unit"),
         "quantity": m.get("quantity"), "unit": m.get("quantity_unit"), "city": m.get("city")}
        for m in rows
    ]
    return _ok({**base, "found": len(top), "top": top, "notes": res.get("notes") or [],
                "answer": "the cards are shown to the user; mention the best one or two in a sentence"},
               matched_candidates=ctx.matched)


# ---------------------------------------------------------------------------
# Negotiations (reads; the binding steps are in ``negotiation`` and go through propose / confirm)
# ---------------------------------------------------------------------------
async def list_negotiations(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if not ctx.can_act:
        return _err(NO_ACCOUNT)
    rfq_id = None
    if args.get("rfq_id"):
        rfq, why = await _own_rfq(ctx, args.get("rfq_id"), ALL_STATUSES)
        if rfq is None:
            return _err(why or "not found")
        rfq_id = rfq.id
    return await negotiation.list_threads(ctx, rfq_id)


async def get_negotiation(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return await negotiation.get_thread(ctx, args.get("connection_id"))


# ---------------------------------------------------------------------------
# Binding actions: propose -> confirm
# ---------------------------------------------------------------------------
ACTIONS = ("create_rfq", "publish_rfq", "save_rfq_edit", "close_rfq", *negotiation.NEGOTIATION_ACTIONS)


async def _target_hash(action: str, ctx: ToolContext, args: dict[str, Any]) -> Optional[str]:
    """What the recap was about: the chat draft (create / edit), the stored RFQ (publish / close) or the exact args (negotiation)."""
    if action in negotiation.NEGOTIATION_ACTIONS:
        return await negotiation.target_hash(action, args, ctx) if ctx.can_act else None
    if action in ("create_rfq", "save_rfq_edit"):
        return recaps.draft_hash(ctx.state.draft)
    if not ctx.can_act:
        return None
    rfq, _why = await _own_rfq(ctx, args.get("rfq_id"), ALL_STATUSES)
    if rfq is None:
        return None
    payload = {"d": recaps.draft_hash(rfq_edit.rfq_to_draft(rfq)), "s": rfq.status.value}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:32]


def _labels(ctx: ToolContext) -> dict[str, str]:
    return recaps.spec_labels(ctx.assist.suggestions.get("specs"))


async def _create_checks(ctx: ToolContext, publish: bool) -> Optional[str]:
    """Why the draft cannot become an RFQ yet (None when it can), checked exactly as creation will."""
    state = ctx.state
    if state.editing_rfq_id:
        return "you are editing a saved RFQ: use save_rfq_edit, or cancel_action with discard_draft to start a new one"
    readiness = rfq_draft.evaluate_rfq_readiness(state.draft)
    if not readiness["required_complete"]:
        return f"cannot create yet, missing: {', '.join(readiness['missing_required'])}. Ask the user."
    blocked = await ctx.assist.block_reason(state.draft)
    if blocked:
        return blocked
    try:
        payload = rfq_draft.build_rfq_create_payload(state.draft, status="active" if publish else "draft")
    except rfq_draft.RFQDraftInvalid as exc:
        return "fix first: " + "; ".join(f"{f}: {m}" for f, m in exc.errors)
    if ctx.user is not None and hasattr(ctx.user, "can_post_as") and not ctx.user.can_post_as(payload.role):
        return f"this account can only post as a {postable_roles(ctx.user)[0]}; a {payload.role.value} RFQ cannot be created from it"
    return None


def _plain_number(value: Any) -> Optional[str]:
    try:
        return format(Decimal(str(value).replace(",", "").strip()).normalize(), "f")
    except (InvalidOperation, ValueError):
        return None


def draft_facts(draft: dict[str, Any], *sections: str) -> list[str]:
    """The numbers of a draft's recap (quantity, minimum order, price, days to deadline): a reply in any language must carry them."""
    out: list[str] = []
    for key, sub in (("quantity", "value"), ("minimum_order", "value"), ("price_target", "amount"), ("deadline", "in_days")):
        if sections and key not in sections:
            continue
        section = draft.get(key)
        number = _plain_number(section.get(sub)) if isinstance(section, dict) and section.get(sub) not in (None, "") else None
        if number and number not in out:
            out.append(number)
    return out


_CHIPS = {
    "create_rfq": {"en": list(recaps.RECAP_REPLIES), "hinglish": ["Abhi publish karo", "Draft mein save karo", "Kuch badlo"]},
    "publish_rfq": {"en": list(recaps.PUBLISH_CONFIRM_REPLIES), "hinglish": ["Haan, publish karo", "Nahi, draft rehne do"]},
    "close_rfq": {"en": list(recaps.CLOSE_CONFIRM_REPLIES), "hinglish": ["Haan, band karo", "Nahi, khula rehne do"]},
    "save_rfq_edit": {"en": ["Save changes", "Change something"], "hinglish": ["Changes save karo", "Kuch aur badlo"]},
}


def _chips(action: str, lang: str) -> list[str]:
    return list(_CHIPS[action][recaps.latin_lang(lang)])


async def _propose_create(args: dict[str, Any], ctx: ToolContext) -> tuple[Optional[PendingAction], Optional[str], list[str], Optional[str]]:
    publish = args.get("publish") is not False
    why = await _create_checks(ctx, publish)
    if why:
        return None, None, [], why
    note = "account" if ctx.state.evidence.get("role") == "account" else None
    recap = recaps.build_recap(ctx.state.draft, ctx.lang, role_note=note, labels=_labels(ctx))
    pending = PendingAction(
        "create_rfq", {"publish": publish}, recaps.draft_hash(ctx.state.draft), recap,
        facts=draft_facts(ctx.state.draft),
    )
    return pending, recap, _chips("create_rfq", ctx.lang), None


async def _propose_saved(action: str, args: dict[str, Any], ctx: ToolContext) -> tuple[Optional[PendingAction], Optional[str], list[str], Optional[str]]:
    """publish_rfq / close_rfq: a stored RFQ of the user's."""
    if not ctx.can_act:
        return None, None, [], NO_ACCOUNT
    wanted = RFQStatus.DRAFT if action == "publish_rfq" else RFQStatus.ACTIVE
    rfq, why = await _own_rfq(ctx, args.get("rfq_id"), (wanted,))
    if rfq is None:
        return None, None, [], why
    if rfq.status != wanted:
        verb = "published" if action == "publish_rfq" else "closed"
        return None, None, [], f"'{rfq.title}' is {rfq.status.value}: only {'a draft' if wanted == RFQStatus.DRAFT else 'a live RFQ'} can be {verb}"
    rid = {"rfq_id": str(rfq.id)}
    if action == "publish_rfq":
        stored = rfq_edit.rfq_to_draft(rfq)
        readiness = rfq_draft.evaluate_rfq_readiness(stored)
        if not readiness["required_complete"]:
            return None, None, [], f"cannot publish '{rfq.title}' yet, missing: {', '.join(readiness['missing_required'])}. Ask the user."
        recap = recaps.build_publish_recap(stored, rfq.title, _short(rfq.id), ctx.lang)
        facts = [str(rfq.title), _short(rfq.id), *draft_facts(stored)]
    else:
        recap = recaps.build_close_ask(rfq.title, _short(rfq.id), ctx.lang)
        facts = [str(rfq.title), _short(rfq.id)]
    pending = PendingAction(action, rid, await _target_hash(action, ctx, rid) or "", recap, facts=facts)
    return pending, recap, _chips(action, ctx.lang), None


async def _propose_edit(ctx: ToolContext) -> tuple[Optional[PendingAction], Optional[str], list[str], Optional[str]]:
    state = ctx.state
    if not (state.editing_rfq_id and state.editing_snapshot):
        return None, None, [], "no RFQ is being edited: call start_edit_rfq first"
    changed = rfq_edit.changed_fields(state.draft, state.editing_snapshot)
    if not changed:
        return None, None, [], "nothing has changed yet: ask the user what to change"
    try:
        rfq_edit.build_rfq_patch(state.draft, state.editing_snapshot)
    except rfq_draft.RFQDraftInvalid as exc:
        return None, None, [], "fix first: " + "; ".join(f"{f}: {m}" for f, m in exc.errors)
    blocked = await ctx.assist.block_reason(state.draft)
    if blocked:
        return None, None, [], blocked
    recap = recaps.build_edit_recap(state.draft, state.editing_snapshot, list(changed), ctx.lang, labels=_labels(ctx))
    pending = PendingAction(
        "save_rfq_edit", {"rfq_id": state.editing_rfq_id}, recaps.draft_hash(state.draft), recap,
        facts=draft_facts({k: state.draft.get(k) for k in changed}),
    )
    return pending, recap, _chips("save_rfq_edit", ctx.lang), None


def _negotiation_args(args: dict[str, Any]) -> dict[str, Any]:
    """The model's flat propose_action arguments without the action name."""
    return {k: v for k, v in args.items() if k != "action"}


async def propose_action(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    action = str(args.get("action") or "")
    note_language(args, ctx)
    await settle(ctx)
    if action not in ACTIONS:
        return _err(f"action must be one of {', '.join(ACTIONS)}")
    if action == "create_rfq":
        pending, recap, chips, why = await _propose_create(args, ctx)
    elif action == "save_rfq_edit":
        pending, recap, chips, why = await _propose_edit(ctx)
    elif action in negotiation.NEGOTIATION_ACTIONS:
        pending, recap, chips, why = await negotiation.propose(action, _negotiation_args(args), ctx)
    else:
        pending, recap, chips, why = await _propose_saved(action, args, ctx)
    if why or pending is None or recap is None:
        return _err(why or "cannot propose this action")
    earlier = ctx.state.pending_valid()
    if (
        earlier is not None and earlier.turn < ctx.state.turn and earlier.action == pending.action
        and earlier.draft_hash == pending.draft_hash
        and {k: v for k, v in earlier.args.items() if k != "publish"} == {k: v for k, v in pending.args.items() if k != "publish"}
    ):  # the same thing the user has already seen: only the publish choice may differ; the answer is theirs to give now
        if args.get("publish") is not None and action == "create_rfq":
            earlier.args["publish"] = bool(args["publish"])
        ctx.quick_replies = ctx.quick_replies or chips  # the answer is still theirs to give: keep the chips under the message
        ctx.new_shown.extend(ctx.quick_replies or [])
        return _ok({"note": "already proposed; the user has seen the recap — if their message agreed, call confirm_action now"})
    pending.turn = ctx.state.turn
    ctx.state.pending = pending
    ctx.recap = recap
    ctx.quick_replies = ctx.quick_replies or chips
    ctx.new_shown.extend(ctx.quick_replies or [])
    return _ok(
        {"recap": recap, "instruction": "Show this recap to the user word for word and wait for their answer. "
         "Do not call confirm_action in this turn."},
        quick_replies=ctx.quick_replies,
    )


def _step(name: str, title: str, done: Optional[dict[str, Any]], err: Optional[str]) -> dict[str, Any]:
    return {
        "name": name, "title": title, "status": "completed" if done else "failed",
        "args": {"title": done["title"], "id": done["id"]} if done else {"error": err},
    }


async def _run_pending(pending: PendingAction, args: dict[str, Any], ctx: ToolContext) -> tuple[Optional[dict[str, Any]], Optional[str], dict[str, Any]]:
    """Execute the stored action with the existing executors. Returns (rfq, error, wire step)."""

    action, state = pending.action, ctx.state
    if action == "create_rfq":
        publish = pending.args.get("publish", True) if args.get("publish") is None else bool(args.get("publish"))
        created, err = await actions.execute_create_rfq_call(
            state.draft, {"status": "active" if publish else "draft"}, db=ctx.db, user=ctx.user, assist=ctx.assist
        )
        return created, err, _step("create_rfq", "Creating Marketplace RFQ", created, err)
    if action == "publish_rfq":
        done, err = await actions.execute_publish_rfq_call(
            rfq_id_str=pending.args.get("rfq_id"), conversation=ctx.conversation, db=ctx.db, user=ctx.user
        )
        return done, err, _step("publish_rfq", "Publishing Marketplace RFQ", done, err)
    if action == "close_rfq":
        done, err = await actions.execute_close_rfq_call(
            rfq_id_str=pending.args.get("rfq_id"), conversation=ctx.conversation, db=ctx.db, user=ctx.user
        )
        return done, err, _step("close_rfq", "Closing Marketplace RFQ", done, err)
    done, err, _changed = await actions.execute_save_rfq_edit_call(
        state.draft, state.editing_snapshot or {}, str(pending.args.get("rfq_id")), db=ctx.db, user=ctx.user, assist=ctx.assist
    )
    if done is None and err is None:
        err = "nothing had changed, so nothing was saved"
    return done, err, _step("save_rfq_edit", "Saving RFQ Changes", done, err)


def _facts_for(action: str, done: dict[str, Any]) -> tuple[str, str]:
    """(needle, text) the reply must contain once the action went through."""

    needle = str(done.get("id") or "")
    if action == "create_rfq":
        return needle, actions.format_created_rfq_text(done)
    if action == "publish_rfq":
        return needle, actions._published_text(done)
    if action == "close_rfq":
        return needle, f"✅ **RFQ Closed:** '{done.get('title')}' (ID: `{needle}`) has been closed. It is no longer active in the marketplace."
    return needle, f"✅ **RFQ updated:** '{done.get('title')}' (ID: `{needle}`) now has your changes."


async def _confirm_negotiation(pending: PendingAction, ctx: ToolContext) -> dict[str, Any]:
    """Run a negotiation action with exactly the arguments the recap showed."""
    done, err, step, fact = await negotiation.execute(pending, ctx)
    ctx.steps.append(step)
    if done is None:
        return _err(err or "the action failed")
    ctx.mutated = True
    if fact:
        ctx.facts.append(fact)
    return _ok({"done": pending.action, **done, "next": "tell the user it went through; do not repeat it"})


async def confirm_action(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    state = ctx.state
    await settle(ctx)
    pending = state.pending_valid()
    if pending is None:
        state.pending = None
        return _err("no action is waiting for confirmation: call propose_action first")
    if pending.turn >= state.turn:
        return _err("the user has not answered the recap yet: wait for their next message")
    current = await _target_hash(pending.action, ctx, pending.args)
    if current != pending.draft_hash:
        state.pending = None
        return _err("the draft changed since the recap was shown: propose the action again and show the new recap")
    if args.get("args") is not None:
        logger.info("confirm_action: ignoring model-supplied args; running the proposed arguments")
    # Consumed BEFORE it runs and stored right away: a repeated "yes", a second tab or a crash can never run it twice.
    state.pending = None
    if ctx.persist is not None:
        await ctx.persist()
    if pending.action in negotiation.NEGOTIATION_ACTIONS:
        return await _confirm_negotiation(pending, ctx)
    done, err, step = await _run_pending(pending, args, ctx)
    ctx.steps.append(step)
    if done is None:
        return _err(err or "the action failed")
    ctx.created = done
    ctx.mutated = True
    ctx.facts.append(_facts_for(pending.action, done))
    if pending.action != "close_rfq":
        state.last_rfq_id = str(done["id"])
    if pending.action in ("create_rfq", "save_rfq_edit"):
        state.reset_draft()
        ctx.assist.reset()
    return _ok(
        {"done": pending.action, "rfq": {k: done.get(k) for k in ("id", "title", "role", "status", "category")},
         "next": "tell the user it went through (status and id above); offer to find matching counterparties"},
        created_rfq=done, rfq_draft=state.draft,
    )


def _discard_fact(ctx: ToolContext) -> tuple[str, str]:
    """(needle, one-line confirmation) in the language and script of the user's latest message."""
    latest = ctx.user_texts[-1] if ctx.user_texts else ""
    if any("\u0900" <= ch <= "\u097f" for ch in latest):
        return "हटा", "ड्राफ्ट हटा दिया गया।"
    if recaps.latin_lang(ctx.lang) == "hinglish":
        return "discard", "Draft discard kar diya."
    return "discard", "Draft discarded."


async def cancel_action(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    note_language(args, ctx)
    state = ctx.state
    had = state.pending is not None
    state.pending = None
    if args.get("discard_draft") is True:
        state.reset_draft()
        ctx.assist.reset()
        ctx.mutated = True
        ctx.facts.append(_discard_fact(ctx))  # "draft discarded" is a fact of this action: the reply must say it
        return _ok({"cleared_pending": had, "draft_discarded": True, "status": "draft discarded"}, rfq_draft=state.draft)
    return _ok({"cleared_pending": had})


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
def _spec(name: str, description: str, properties: dict[str, Any], required: Optional[list[str]] = None) -> dict[str, Any]:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required or []},
    }}


_REPLY_LANGUAGE = {
    "type": "string", "enum": list(recaps.REPLY_LANGUAGES),
    "description": "The language AND SCRIPT of the user's latest message: english, hinglish (Hindi written in Latin letters), or "
                   "hindi_devanagari (only if the user wrote Devanagari letters). Latin-script Hindi is always hinglish. The platform "
                   "writes its own recap and fixed texts in it.",
}
_RFQ_ID = {"type": "string", "description": "RFQ id, 8-character short id or a title keyword of the user's own RFQ"}
_CONN_ID = {"type": "string", "description": "negotiation (connection) id or its 8-character short id; default: the chat's focus"}

TOOLS: dict[str, Tool] = {t.name: t for t in (
    Tool("update_rfq_draft", _spec(
        "update_rfq_draft",
        "Save what the user said into the RFQ draft. Every value needs `said` (their exact words). Fields they did not "
        "mention must be omitted or null: that leaves them unchanged. `remove` clears a field explicitly "
        "(e.g. \"price_target\", \"location.city\", \"specifications.grade\").",
        {"fields": UPDATE_FIELDS_SCHEMA, "remove": {"type": "array", "items": _STR}},
    ), update_rfq_draft),
    Tool("suggest_specs", _spec(
        "suggest_specs", "Specification keys worth asking for a product, with their allowed options.",
        {"product_name": _STR},
    ), suggest_specs, concurrent_ok=True),
    Tool("lookup_hsn", _spec(
        "lookup_hsn", "Search HSN codes for a product description; returns candidate codes.", {"query": _STR}, ["query"],
    ), lookup_hsn, concurrent_ok=True),
    Tool("set_quick_replies", _spec(
        "set_quick_replies", "Chips shown under your reply (2 to 6 short options the user can tap to answer).",
        {"options": {"type": "array", "items": _STR}, "reply_language": _REPLY_LANGUAGE}, ["options"],
    ), set_quick_replies),
    Tool("propose_action", _spec(
        "propose_action",
        "Validate a binding action and get the recap to show the user. create_rfq: publish=true (default) posts it live, "
        "false saves a draft. publish_rfq / close_rfq: needs rfq_id (or the last RFQ). save_rfq_edit: for the RFQ being "
        "edited. Negotiation: connect {rfq_id: the counterparty's listing}; send_message {connection_id, text}; "
        "send_quote {connection_id, unit_price, quantity, quantity_unit?, currency?, lead_time_days?, incoterms?, "
        "payment_terms?, valid_days?, notes?, price_said | suggested=true} (a counter-offer is a new quote); "
        "accept_quote / reject_quote {connection_id, quote_id?, reason?}. Nothing happens until confirm_action.",
        {"action": {"type": "string", "enum": list(ACTIONS)}, "rfq_id": _RFQ_ID, "publish": {"type": "boolean"},
         "connection_id": _CONN_ID, "text": {"type": "string", "description": "send_message: the exact message"},
         "unit_price": _NUM, "quantity": _NUM, "quantity_unit": _STR, "currency": {"type": "string", "description": "3-letter code"},
         "lead_time_days": {"type": "integer"}, "incoterms": _STR, "payment_terms": _STR,
         "valid_days": {"type": "integer"}, "notes": _STR,
         "price_said": {"type": "string", "description": "send_quote: the user's exact words that contain the price"},
         "suggested": {"type": "boolean", "description": "send_quote: true when the price is your own proposal, not the user's"},
         "quote_id": {"type": "string", "description": "accept_quote / reject_quote: the quote (default: the one pending for the user)"},
         "reason": _STR, "reply_language": _REPLY_LANGUAGE},
        ["action"],
    ), propose_action),
    Tool("confirm_action", _spec(
        "confirm_action",
        "Run the proposed action exactly as the recap showed it; takes no action arguments. Only after the user's message "
        "AFTER the recap clearly agrees. For create_rfq, `publish` may override publish-now vs draft if the user chose.",
        {"publish": {"type": "boolean"}, "reply_language": _REPLY_LANGUAGE},
    ), confirm_action),
    Tool("cancel_action", _spec(
        "cancel_action", "Drop the proposed action; discard_draft=true also empties the draft / stops editing.",
        {"discard_draft": {"type": "boolean"}, "reply_language": _REPLY_LANGUAGE},
    ), cancel_action),
    Tool("list_my_rfqs", _spec(
        "list_my_rfqs", "The user's own RFQs (id, title, status).",
        {"status": {"type": "string", "enum": ["draft", "active", "expired", "closed"]}},
    ), list_my_rfqs),
    Tool("get_rfq", _spec("get_rfq", "One of the user's own RFQs in full.", {"rfq_id": _RFQ_ID}, ["rfq_id"]), get_rfq),
    Tool("start_edit_rfq", _spec(
        "start_edit_rfq", "Load one of the user's draft or live RFQs into the draft so it can be changed.", {"rfq_id": _RFQ_ID}, ["rfq_id"],
    ), start_edit_rfq),
    Tool("find_matches", _spec(
        "find_matches", "Best matching counterparties for one of the user's RFQs (default: the last one created).",
        {"rfq_id": _RFQ_ID},
    ), find_matches),
    Tool("search_listings", _spec(
        "search_listings",
        "Browse what is on the market: active listings of other users for a product. side='seller' for 'show me sellers "
        "of X / who sells X', side='buyer' for buyers of X. Read only: it never starts or changes a draft.",
        {"product": _STR, "side": {"type": "string", "enum": ["buyer", "seller"]}, "location": _STR}, ["product"],
    ), search_listings),
    Tool("list_negotiations", _spec(
        "list_negotiations", "The user's negotiations (connections): counterparty, listing, status, latest quote, what waits for the user. "
        "Optionally only those on one of the user's RFQs.", {"rfq_id": _RFQ_ID},
    ), list_negotiations),
    Tool("get_negotiation", _spec(
        "get_negotiation", "One negotiation in full: the last messages, every quote version and the listing's target price and quantity.",
        {"connection_id": _CONN_ID},
    ), get_negotiation),
    Tool("market_prices", _spec(
        "market_prices",
        "Current prices of a product on this platform: median, range, currency, unit and number of listings, or an explicit "
        "'no data'. The only source for price questions. Call it immediately with just the product (standard English "
        "name); location and role are optional filters, never ask the user for them first.",
        {"product": _STR, "location": _STR, "role": {"type": "string", "enum": ["buyer", "seller"]}}, ["product"],
    ), market_prices),
    Tool("platform_info", _spec(
        "platform_info", "What this platform really does (payments, escrow, KYC, fees, safety). Answer only from it.",
        {"topic": _STR},
    ), platform_info, concurrent_ok=True),
)}

READ_ONLY_STATUS = {
    "suggest_specs": "Checking the specifications for this product...\n",
    "lookup_hsn": "Looking up HSN codes...\n",
    "list_my_rfqs": "Looking up your RFQs...\n",
    "get_rfq": "Opening the RFQ...\n",
    "find_matches": "Finding matching counterparties...\n",
    "list_negotiations": "Looking up your negotiations...\n",
    "get_negotiation": "Reading the negotiation...\n",
    "market_prices": "Checking prices on the platform...\n",
    "search_listings": "Searching the listings...\n",
    "platform_info": "Checking platform facts...\n",
}


def tool_specs() -> list[dict[str, Any]]:
    """The tool schemas offered to the model, in a fixed order."""
    return [t.schema for t in TOOLS.values()]


async def run_tool(name: str, args: Any, ctx: ToolContext) -> dict[str, Any]:
    """Run one tool call; never raises (an executor failure comes back to the model as an error)."""
    tool = TOOLS.get(name)
    if tool is None:
        return _err(f"unknown tool '{name}'")
    if not isinstance(args, dict):
        return _err("arguments must be a JSON object")
    try:
        return await tool.run(args, ctx)
    except Exception as exc:  # the model gets to recover; the turn goes on
        logger.exception("chat tool %s failed: %s", name, exc)
        return _err(f"{name} failed unexpectedly; tell the user and carry on without it")
