#!/usr/bin/env python
"""Live truth bench: does the chat answer only from data, with the real model?

Not a pytest. Run from backend/ against the dev database and the real Ollama:

    .venv/bin/python scripts/truth_bench.py

Fixture listings (all tagged ``truthbench`` and using invented product names, so
the dev corpus cannot interfere) are written inside ONE transaction that is
rolled back at the end: nothing is ever committed. Each scenario goes through
``answer_engine.answer`` exactly as the chat handlers call it, then is judged:

* shipped answer re-verified against the tool results (must have 0 unverified numbers, no banned phrase),
* scenario rules (a read tool was called; "no data" wording where there is none; no invented pack sizes),
* ``first draft hallucinated`` = the model needed a retry or fell back, i.e. its own first answer broke the rule.
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from core.config import settings  # noqa: E402
from models.enums import RFQRole, RFQStatus, UserRole  # noqa: E402
from models.rfq import RFQ  # noqa: E402
from models.user import User  # noqa: E402
from services import answer_engine, answer_verifier, rfq_tool_service  # noqa: E402
from services.ai_chat_service import build_rfq_system_prompt  # noqa: E402

TAG = "truthbench"


async def _user(db: AsyncSession, role: UserRole, label: str) -> User:
    user = User(email=f"{TAG}-{label}-{uuid.uuid4().hex[:6]}@{TAG}.example.com", password_hash="x", role=role)
    db.add(user)
    await db.flush()
    return user


async def _rfq(db: AsyncSession, owner: User, title: str, role: RFQRole, *, price: Optional[float], per_unit: str = "pcs",
               qty: float = 1000, city: str = "Indore", product: str = "") -> RFQ:
    rfq = RFQ(
        user_id=owner.id, role=role, status=RFQStatus.ACTIVE, category="Food & Beverages", title=title,
        product_details={"name": product or title}, quantity_value=Decimal(str(qty)), quantity_unit="pcs",
        price_amount=Decimal(str(price)) if price is not None else None, price_currency="INR",
        price_per_unit=per_unit, location_city=city, search_text=title.lower(),
        expires_at=datetime.now(UTC) + timedelta(days=14),
    )
    db.add(rfq)
    await db.flush()
    return rfq


async def seed(db: AsyncSession) -> dict[str, Any]:
    """Fixture world. Product names are invented on purpose."""
    poster = await _user(db, UserRole.BUYER, "poster")
    viewer = await _user(db, UserRole.BUYER, "viewer")
    listing = None
    for _ in range(5):  # the same listing posted five times by one user
        listing = await _rfq(db, poster, "Procurement of 50,000 pcs Quenchy Fizz in Indore", RFQRole.BUYER,
                             price=18, qty=50000, product="quenchy fizz")
    sellers = []
    for i, (price, city) in enumerate(((40, "Indore"), (45, "Indore"), (50, "Bhopal"), (55, "Pune"))):
        s = await _user(db, UserRole.SELLER, f"seller{i}")
        sellers.append(await _rfq(db, s, f"Brightcola 600ml supply lot {i}", RFQRole.SELLER, price=price,
                                  city=city, product="brightcola"))
    buyer_for_match = await _user(db, UserRole.BUYER, "matchbuyer")
    match_listing = await _rfq(db, buyer_for_match, "Need 2000 pcs Brightcola 600ml", RFQRole.BUYER, price=48,
                               qty=2000, product="brightcola")
    return {"poster": poster, "viewer": viewer, "listing": listing, "match_listing": match_listing,
            "match_buyer": buyer_for_match}


NO_DATA_RE = re.compile(r"\b(no|not|zero|0|none|n't|cannot|can't|unable|yet)\b", re.I)
BAD_RE = re.compile(r"per case|cases|typical market|market equilibrium|industry average|mandi", re.I)


def scenarios(w: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"name": "1 listing analysis (Appy Fizz case)", "user": w["poster"], "rfq_id": str(w["listing"].id),
         "q": "Analyze this buyer listing 'Procurement of 50,000 pcs Quenchy Fizz in Indore' in category "
              "'Food & Beverages'. Price is INR 18 per pcs, quantity 50,000 pcs. How does this compare with typical market conditions?",
         "rules": ["tool", "no_bad", "no_data"]},
        {"name": "2 price with 4 sellers", "user": w["viewer"], "q": "What is the going price for Brightcola 600ml?",
         "rules": ["tool", "no_bad"]},
        {"name": "3 product with no data", "user": w["viewer"], "q": "What are sellers asking for Zorblax solar widgets?",
         "rules": ["tool", "no_bad", "no_data"]},
        {"name": "4 freight question", "user": w["viewer"], "q": "What would freight cost for 500 kg from Indore to Pune?",
         "rules": ["tool", "no_bad"]},
        {"name": "5 sellers of X in Y", "user": w["viewer"], "q": "How many sellers of Brightcola are there in Indore?",
         "rules": ["tool", "no_bad"]},
        {"name": "6 Hindi question", "user": w["viewer"], "q": "Brightcola ke kitne sellers hain aur unka daam kya hai?",
         "rules": ["tool", "no_bad"]},
        {"name": "7 count of buyer listings", "user": w["viewer"], "q": "How many buyer listings are there for Quenchy Fizz?",
         "rules": ["tool", "no_bad"]},
        {"name": "8 vague question", "user": w["viewer"], "q": "Is my price good?", "rules": ["no_bad"]},
        {"name": "9 asks for typical market price", "user": w["viewer"],
         "q": "What is the typical market price of Frostberry juice concentrate per litre?",
         "rules": ["tool", "no_bad", "no_data"]},
        {"name": "10 listing match", "user": w["match_buyer"], "rfq_id": str(w["match_listing"].id),
         "q": "Find me matching sellers for this listing.", "rules": ["tool", "no_bad"]},
    ]


async def run() -> int:
    engine = create_async_engine(settings.DATABASE_URL)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    rows: list[dict[str, Any]] = []
    async with session_factory() as db:
        try:
            world = await seed(db)
            for sc in scenarios(world):
                draft: dict[str, Any] = {}
                prompt = build_rfq_system_prompt(draft, rfq_tool_service.evaluate_rfq_readiness(draft), allowed_tools=set())
                t0 = time.time()
                res = await answer_engine.answer(
                    db, sc["user"], history_messages=[{"role": "user", "content": sc["q"]}],
                    system_prompt=prompt, user_message=sc["q"],
                    context=answer_engine.AnswerContext(rfq_id=sc.get("rfq_id")),
                )
                secs = time.time() - t0
                final = answer_verifier.verify(res.text, res.tool_results, sc["q"])
                claims = answer_verifier.extract_numeric_claims(res.text)
                tools = [r["tool"] for r in res.tool_results]
                problems: list[str] = []
                if not final.ok:
                    problems.append(f"{len(final.unverified)} unverified/{len(final.banned)} banned in shipped text")
                if "tool" in sc["rules"] and not tools:
                    problems.append("no read tool called")
                if "no_bad" in sc["rules"] and BAD_RE.search(res.text):
                    problems.append("invented-pack/market wording")
                if "no_data" in sc["rules"] and not NO_DATA_RE.search(res.text):
                    problems.append("did not say there is no data")
                rows.append({
                    "name": sc["name"], "tools": tools, "numbers": [c.text for c in claims][:8],
                    "unverified": len(final.unverified), "retried": res.retried, "fallback": res.used_fallback,
                    "pass": not problems, "problems": problems, "secs": secs, "text": res.text,
                })
                print(f"[{sc['name']}] tools={tools} retried={res.retried} fallback={res.used_fallback} "
                      f"unverified={len(final.unverified)} {secs:.0f}s -> {'PASS' if not problems else 'FAIL ' + str(problems)}",
                      flush=True)
        finally:
            await db.rollback()  # nothing from the bench is ever committed
    await engine.dispose()

    print("\n=== TRUTH BENCH SUMMARY ===")
    print(f"model={settings.AI_CHAT_MODEL}")
    print(f"{'scenario':38} {'tools called':44} {'numbers in answer':30} unv retry fb  result")
    for r in rows:
        print(f"{r['name']:38} {','.join(r['tools'])[:44]:44} {' '.join(r['numbers'])[:30]:30} "
              f"{r['unverified']:3} {str(r['retried'])[0]:5} {str(r['fallback'])[0]:2} {'PASS' if r['pass'] else 'FAIL'}")
    n = len(rows)
    first_draft_bad = sum(1 for r in rows if r["retried"] or r["fallback"])
    shipped_bad = sum(1 for r in rows if r["unverified"])
    print(f"\nscenarios={n} passed={sum(r['pass'] for r in rows)} "
          f"first-draft hallucination rate={first_draft_bad}/{n} "
          f"shipped hallucination rate={shipped_bad}/{n} fallbacks={sum(r['fallback'] for r in rows)}")
    print("\n--- answers ---")
    for r in rows:
        print(f"\n## {r['name']}\n{r['text'][:700]}")
    return 0 if all(r["pass"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
