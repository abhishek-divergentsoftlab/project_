"""Live replays of two real chat sessions against the REAL Ollama model. Skipped unless ``LIVE_LLM=1``.

    LIVE_LLM=1 .venv/bin/python -m pytest -p no:cacheprovider tests/live/test_live_chat_sessions.py -q -s

They use the main conftest's fixtures (test database, ``client``, ``make_actor``), so nothing touches the development
data. Run with ``-s`` to print both transcripts with the time of every turn. Everything asserted is carried by the
deterministic layer (spec filing, the spec question, the HSN offer, the honest acknowledgement), never by what the
model happens to write, so the assertions hold whatever the model says.
"""

import os
import re
import time

import pytest
from sqlalchemy import text

from core.config import settings

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(os.environ.get("LIVE_LLM") != "1", reason="set LIVE_LLM=1 to run against the real Ollama model"),
]

SESSIONS = {
    "apples": [
        "I want to buy 500kg apples in Indore, in 20dollar par kg, before 20 december 2026",
        "what attributes or spesication i can add in this more",
        "product type is fuji, quality is minimum Brix lael and packaging in carton size box of 20kg each, certification must of organic and fssai",
        "ok proceed",
        "yes publish it",
        "yes publish it",
    ],
    "bananas": [
        "I want to buy 500kg bananas in Indore, in 20dollar par kg, before 20 december 2026",
        "fresh",
        "continue refinig it",
        "skip",
        "yes publish it",
    ],
}


@pytest.fixture(autouse=True)
def real_helpers(monkeypatch):
    """The HSN suggestion uses the model as it does in the app (the test conftest switches it off)."""
    monkeypatch.setattr(settings, "HSN_AI_ENABLED", True)


async def _replay(name, client, db, make_actor):
    user = await make_actor("both")
    cid, history, turns = None, [], []
    print(f"\n\n========== {name} ==========")
    for msg in SESSIONS[name]:
        history.append({"role": "user", "content": msg})
        body = {"messages": history}
        if cid:
            body["conversation_id"] = cid
        started = time.time()
        res = await user.post("/ai-chat/message", json=body)
        assert res.status_code == 200, res.text
        data = res.json()
        cid = data.get("conversation_id") or cid
        reply = data["reply"]
        history.append({"role": "assistant", "content": reply})
        turns.append((msg, reply, data))
        print(f"\nUSER: {msg}\nBOT ({time.time() - started:.1f}s): {reply}")
    await db.rollback()
    rows = (await db.execute(text("select role, status, title, product_details from rfqs"))).all()
    print(f"\nRFQS CREATED: {len(rows)}")
    for r in rows:
        print(dict(r._mapping))
    return turns, rows


def _assert_clean_replies(turns):
    for _msg, reply, _data in turns:
        assert "Say 'create RFQ'" not in reply and "create RFQ'" not in reply
        labels = [re.split(r"\s+[—-]\s+", l.strip().lstrip("•-* ").strip(), maxsplit=1)[0].strip().lower()
                  for l in reply.splitlines() if l.strip().startswith("•")]
        assert len(labels) == len(set(labels)), f"a spec list appears twice in one reply: {labels}"
        assert not re.search(r"RFQ draft (saved|created|updated)", reply, re.IGNORECASE), reply


def _find(details, *words):
    return next((v for k, v in details.items() if all(w in k.lower() for w in words) and not k.endswith("__must_match")), None)


async def test_live_apples_session(client, db, make_actor):
    turns, rows = await _replay("apples", client, db, make_actor)
    _assert_clean_replies(turns)
    assert len(rows) == 1, "exactly one RFQ is created per session"
    rfq = rows[0]
    assert rfq.role == "buyer" and rfq.status == "active"
    details = rfq.product_details
    assert "fuji" in str(_find(details, "variety")).lower()
    assert "carton" in str(_find(details, "packag")).lower()
    certs = str(_find(details, "certif")).lower()
    assert "organic" in certs and "fssai" in certs
    assert details.get("Certifications__must_match") is True
    assert not any("brix" in k.lower() for k in details), "the garbled 'minimum Brix lael' must not be stored"
    # turn 1 asks the spec question once; turn 3 files the answer and asks for the Brix value
    assert "tell me a few specs" in turns[0][1]
    assert "Brix" in turns[2][1] and "?" in turns[2][1] and turns[2][1].lower().count("do you need") == 1
    # turn 6: the RFQ already exists
    assert "already published" in turns[5][1]
    assert "we still need Role" not in turns[5][1]


async def test_live_bananas_session(client, db, make_actor):
    turns, rows = await _replay("bananas", client, db, make_actor)
    _assert_clean_replies(turns)
    assert len(rows) == 1, "exactly one RFQ is created per session"
    rfq = rows[0]
    assert rfq.role == "buyer" and rfq.status == "active"
    assert str(rfq.product_details.get("hsn_code", "")).startswith("0803"), rfq.product_details
    assert "Suggested HSN 0803" in turns[0][1], "the HSN is offered with the first spec question"
