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


async def _warm_catalogue(product):
    """Generate the product's spec family up front (the real recommender), so the replay is not at the mercy of the model
    swapping between the attribute model and the chat model: the catalogue is stored once, as in production."""
    from services import spec_catalog

    fam = await spec_catalog.ensure_family(product, "Agriculture")
    assert fam.persisted, "the attribute recommender produced no family"
    print(f"\n[catalogue warmed: {fam.family_key}: {[a['key'] for a in fam.attributes]}]")


async def _replay(name, client, db, make_actor):
    await _warm_catalogue({"apples": "apples", "bananas": "bananas"}[name])
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


async def test_live_apples_session(client, db, make_actor):
    turns, rows = await _replay("apples", client, db, make_actor)
    _assert_clean_replies(turns)
    assert len(rows) == 1, "exactly one RFQ is created per session"
    rfq = rows[0]
    assert rfq.role == "buyer" and rfq.status == "active"
    details = rfq.product_details
    # canonical catalogue keys and values: the same names the RFQ form and matching use
    assert details.get("variety") == "Fuji", details
    assert details.get("packaging_type") == "Carton", details
    assert details.get("pack_size") == "20 kg", details
    assert details.get("certifications") == ["Organic", "FSSAI"], details
    assert details.get("certifications__must_match") is True
    assert not any(k.endswith("__must_match") and v is not True for k, v in details.items()), "no False must-match keys"
    assert not any(isinstance(v, dict) for v in details.values()), "specs are flat"
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
    # The HSN is offered in the first reply (after the spec question, or on its own when the first spec generation is still
    # running); answering it does not count as answering the spec question, which is asked again.
    assert "HSN 0803" in turns[0][1]
    assert "tell me a few specs" in turns[1][1] and "Shall I publish it now" not in turns[1][1]


async def test_live_bananas_attribute_chips_session(client, db, make_actor):
    """U1 -> spec question + HSN line + label chips; "fresh" -> asked again (not the recap); tap a label -> its options;
    tap an option -> remaining labels + Done; Done -> the recap with its chips. Chips are read from the replies."""
    await _warm_catalogue("bananas")
    user = await make_actor("both")
    history, cid = [], None

    async def say(msg):
        nonlocal cid
        history.append({"role": "user", "content": msg})
        res = await user.post("/ai-chat/message", json={"messages": history, **({"conversation_id": cid} if cid else {})})
        assert res.status_code == 200, res.text
        data = res.json()
        cid = data.get("conversation_id") or cid
        history.append({"role": "assistant", "content": data["reply"]})
        print(f"\nUSER: {msg}\nBOT: {data['reply']}\nCHIPS: {data.get('quick_replies')}")
        return data

    d1 = await say(SESSIONS["bananas"][0])
    labels = d1["quick_replies"]
    assert labels and labels[-1] == "Skip" and len(labels) >= 2 and "tell me a few specs" in d1["reply"]
    d2 = await say("fresh")
    assert "tell me a few specs" in d2["reply"] and "Shall I publish it now" not in d2["reply"]
    assert d2["quick_replies"][-1] == "Skip" and "Condition" not in d2["quick_replies"]
    d3 = await say(d2["quick_replies"][0])
    assert d3["quick_replies"][-2:] in (["Other", "Skip"], ["Other", "Done"]) or d3["quick_replies"][-1] == "Other"
    first = d3["quick_replies"][0]
    d4 = await say(first)
    assert d4["quick_replies"][-1] in ("Done", "Other")
    if d4["quick_replies"][-1] != "Done":  # a multi-choice stays open: Done closes it first
        d4 = await say("Done")
    d5 = await say("Done")
    assert d5["quick_replies"] == ["Publish now", "Save as draft", "Change something"], d5["reply"]
    assert "Shall I publish it now" in d5["reply"]
