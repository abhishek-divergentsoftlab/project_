"""The chat's conversation flow: one question at a time, a recap the user confirms, role handling, edit recaps.

The model is stubbed (it never decides what gets created); everything asserted here is deterministic.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from services import chat_conversation as cc, chat_draft_assist as cda, chat_permissions as perms

pytestmark = [pytest.mark.asyncio, pytest.mark.filterwarnings("ignore::pytest.PytestWarning")]

BASE = {
    "role": "buyer",
    "category": "Agriculture",
    "product_details": {"name": "Basmati Rice"},
    "quantity": {"value": 500.0, "unit": "kg"},
}
COMPLETE = {
    **BASE,
    "price_target": {"amount": 50.0, "currency": "INR", "per_unit": "kg"},
    "location": {"city": "Pune", "state": "Maharashtra", "country": "India"},
    "deadline": {"in_days": 7},
}
SAFE = {"is_safe": True, "reason": None, "category": None}
NO_MODEL = patch("services.answer_engine._chat", side_effect=AssertionError("model must not be called"))
RECAP_Q = "Shall I publish it now, or save it as a draft?"
RECAP_Q_HI = "Ise abhi publish karun ya draft mein save karun?"


class _Helpers:
    def __init__(self, specs=None, hsn=None):
        self.specs, self.hsn = specs or [], hsn

    def __enter__(self):
        self.ps = [
            patch.object(cda, "_fetch_hsn_suggestion", AsyncMock(return_value=self.hsn)),
            patch.object(cda, "_fetch_spec_suggestions", AsyncMock(return_value=self.specs)),
            patch.object(cda, "_fetch_moderation", AsyncMock(return_value=SAFE)),
        ]
        for p in self.ps:
            p.__enter__()
        return self

    def __exit__(self, *a):
        for p in self.ps:
            p.__exit__(*a)


def _script(content="Noted.", seen=None):
    async def chat(messages, tools):
        if seen is not None:
            seen.append(messages[0]["content"])
        return {"role": "assistant", "content": content}

    return patch("services.answer_engine._chat", chat)


def _tool_chat(args, reply="Noted."):
    calls = {"n": 0}

    async def chat(messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "update_rfq_draft", "arguments": args}}]}
        return {"role": "assistant", "content": reply}

    return patch("services.answer_engine._chat", chat)


async def _msg(user, content, draft=None, cid=None, log=None):
    body = {"messages": [{"role": "user", "content": content}]}
    if draft is not None:
        body["current_rfq"] = draft
    if cid:
        body["conversation_id"] = cid
    res = await user.post("/ai-chat/message", json=body)
    assert res.status_code == 200, res.text
    data = res.json()
    if log is not None:
        log.append(("user", content))
        log.append(("assistant", data["reply"]))
    return data


async def _state(db):
    await db.rollback()
    return (await db.execute(text("select state from conversations order by created_at desc limit 1"))).scalar_one()


async def _rfqs(db):
    await db.rollback()
    return (await db.execute(text("select status from rfqs"))).scalars().all()


def _show(title, log):
    print(f"\n--- {title}")
    for who, said in log:
        print(f"{who.upper()}: {said}")


# ---- A. recap + confirm ----------------------------------------------------------------
async def test_create_intent_shows_recap_and_does_not_create(client, db, make_actor):
    user = await make_actor("buyer")
    with _Helpers(), NO_MODEL:
        data = await _msg(user, "create the rfq", COMPLETE)
    assert data["created_rfq"] is None and await _rfqs(db) == []
    reply = data["reply"]
    assert reply.endswith(RECAP_Q)
    for part in ("**Product:** Basmati Rice (Agriculture)", "**Type:** Buying", "**Quantity:** 500 kg",
                 "**Target price:** INR 50 per kg", "**Location:** Pune, Maharashtra, India", "**Deadline:** within 7 days"):
        assert part in reply
    st = await _state(db)
    assert st["recap_hash"] == cc.draft_hash(COMPLETE) and st["recap_awaiting"] == "create"


async def test_yes_after_recap_creates_active_and_clears_recap(client, db, make_actor):
    user = await make_actor("buyer")
    log = []
    with _Helpers(), NO_MODEL:
        first = await _msg(user, "create the rfq", COMPLETE, log=log)
        data = await _msg(user, "yes", cid=first["conversation_id"], log=log)
    assert data["created_rfq"]["status"] == "active" and await _rfqs(db) == ["active"]
    st = await _state(db)
    assert "recap_hash" not in st and "recap_awaiting" not in st
    _show("English: recap then yes", log)


@pytest.mark.parametrize("answer", ["draft", "save as draft", "save it as a draft", "draft mein rakho"])
async def test_draft_after_recap_creates_a_draft(client, db, make_actor, answer):
    user = await make_actor("buyer")
    with _Helpers(), NO_MODEL:
        first = await _msg(user, "create the rfq", COMPLETE)
        data = await _msg(user, answer, cid=first["conversation_id"])
    assert data["created_rfq"]["status"] == "draft" and await _rfqs(db) == ["draft"]


@pytest.mark.parametrize("answer", ["ok", "theek hai", "go ahead", "publish", "haan"])
async def test_bare_affirmatives_after_recap_publish(client, db, make_actor, answer):
    user = await make_actor("buyer")
    with _Helpers(), NO_MODEL:
        first = await _msg(user, "create the rfq", COMPLETE)
        data = await _msg(user, answer, cid=first["conversation_id"])
    assert data["created_rfq"]["status"] == "active"


@pytest.mark.parametrize("answer", ["no", "wait", "ruko"])
async def test_no_after_recap_does_not_create_and_forgets_the_recap(client, db, make_actor, answer):
    user = await make_actor("buyer")
    with _Helpers(), NO_MODEL:
        first = await _msg(user, "create the rfq", COMPLETE)
        data = await _msg(user, answer, cid=first["conversation_id"])
        assert data["created_rfq"] is None and "What would you like to change" in data["reply"] or "badalna" in data["reply"]
        again = await _msg(user, "create it", cid=first["conversation_id"])
    assert again["created_rfq"] is None and RECAP_Q in again["reply"]  # a fresh recap, not a creation
    assert await _rfqs(db) == []


async def test_draft_change_after_recap_invalidates_it(client, db, make_actor):
    user = await make_actor("buyer")
    with _Helpers(), NO_MODEL:
        first = await _msg(user, "create the rfq", COMPLETE)
    cid = first["conversation_id"]
    with _Helpers(), _tool_chat({"quantity_value": 800, "quantity_unit": "kg"}):
        changed = await _msg(user, "make it 800 kg", cid=cid)
    assert changed["rfq_draft"]["quantity"]["value"] == 800
    assert "800 kg" in changed["reply"] and RECAP_Q in changed["reply"]  # the changed draft is recapped again
    with _Helpers(), NO_MODEL:
        data = await _msg(user, "create it", cid=cid)  # not seen the old recap's draft: confirm the new one first
    assert data["created_rfq"] is not None  # the user saw the 800 kg recap in the previous turn
    assert await _rfqs(db) == ["active"]


async def test_changing_the_draft_after_the_recap_needs_a_new_recap_before_creating(client, db, make_actor):
    user = await make_actor("buyer")
    with _Helpers(), NO_MODEL:
        first = await _msg(user, "create the rfq", COMPLETE)
    cid = first["conversation_id"]
    # an edit that does not go through a drafting turn (state changed behind the recap)
    st = await _state(db)
    st["rfq_draft"]["quantity"] = {"value": 900.0, "unit": "kg"}
    await db.execute(text("update conversations set state = cast(:s as jsonb)"), {"s": json.dumps(st)})
    await db.commit()
    with _Helpers(), NO_MODEL:
        data = await _msg(user, "yes", cid=cid)
    assert data["created_rfq"] is None and await _rfqs(db) == []  # a bare "yes" never confirms an unseen draft
    assert "900 kg" in data["reply"] or data["reply"]


async def test_recap_with_more_details_in_same_message_is_not_a_confirmation(client, db, make_actor):
    user = await make_actor("buyer")
    with _Helpers(), NO_MODEL:
        first = await _msg(user, "create the rfq", COMPLETE)
    with _Helpers(), _tool_chat({"quantity_value": 600, "quantity_unit": "kg"}):
        data = await _msg(user, "yes but make it 600 kg", cid=first["conversation_id"])
    assert data["created_rfq"] is None and await _rfqs(db) == []


async def test_hinglish_recap_and_haan(client, db, make_actor):
    user = await make_actor("buyer")
    log = []
    with _Helpers(), NO_MODEL:
        first = await _msg(user, "mujhe yeh rfq bana do aur jaldi", COMPLETE, log=log)
        assert first["created_rfq"] is None and first["reply"].endswith(RECAP_Q_HI)
        assert "**Khareedna" not in first["reply"] and "Khareedna" in first["reply"]
        data = await _msg(user, "haan", cid=first["conversation_id"], log=log)
    assert data["created_rfq"]["status"] == "active"
    _show("Hinglish: recap then haan", log)


async def test_explicit_draft_request_creates_a_draft_without_a_recap(client, db, make_actor):
    user = await make_actor("buyer")
    with _Helpers(), NO_MODEL:
        data = await _msg(user, "save it as a draft", COMPLETE)
    assert data["created_rfq"]["status"] == "draft"


def test_bare_affirmatives_follow_the_recap_question_in_both_languages():
    for prev in (RECAP_Q, RECAP_Q_HI, "...\n\n" + RECAP_Q):
        for yes in ("yes", "ok", "haan", "theek hai", "go ahead"):
            assert perms.check_create_rfq_intent(yes, previous_assistant=prev), (prev, yes)
    assert not perms.check_create_rfq_intent("yes", previous_assistant="Which city?")
    for prev in ("Save these changes?", "Ye changes save karun?"):
        assert perms.check_save_edit_intent("theek hai", previous_assistant=prev)


def test_recap_lists_hsn_specs_must_match_min_order_and_dispatch():
    draft = {
        "role": "seller", "category": "Agriculture", "description": "Export quality, double polished",
        "product_details": {"name": "Basmati Rice", "hsn_code": "100630", "Grain Length": "7mm", "Grain Length__must_match": True},
        "quantity": {"value": 5000, "unit": "kg"}, "minimum_order": {"value": 100, "unit": "kg"},
        "price_target": {"amount": 70, "currency": "INR", "per_unit": "kg"}, "location": {"city": "Karnal"},
        "deadline": {"in_days": 10},
    }
    out = cc.build_recap(draft, "en", role_note="seller")
    assert "(Agriculture, HSN 100630)" in out and "**Type:** Selling (your account can only post as a seller)" in out
    assert "**Minimum order:** 100 kg" in out and "**Offer price:** INR 70 per kg" in out
    assert "**Dispatch within:** 10 days" in out and "Grain Length: 7mm (must match)" in out
    assert "**Description:** Export quality" in out and out.endswith(RECAP_Q)
    assert "Ise abhi publish karun" in cc.build_recap(draft, "hinglish")


# ---- B. one question at a time -------------------------------------------------------------
def test_next_question_order():
    specs = {"specs": [{"label": "Grain Length"}, {"label": "Moisture"}, {"label": "Broken"}, {"label": "Age"}],
             "hsn": {"code": "100630", "description": "Rice"}}
    draft: dict = {}
    assert cc.next_question(draft, specs) == "role"
    draft["role"] = "seller"
    assert cc.next_question(draft, specs) == "product"
    draft["product_details"] = {"name": "Rice"}
    seen = []
    for field, key, value in (
        ("quantity", "quantity", {"value": 1, "unit": "kg"}),
        ("min_order", "minimum_order", {"value": 1, "unit": "kg"}),  # sellers only, right after quantity
        ("price", "price_target", {"amount": 1, "currency": "INR"}),
        ("location", "location", {"city": "Pune"}),
        ("dispatch", "deadline", {"in_days": 5}),
    ):
        assert cc.next_question(draft, specs) == field
        seen.append(field)
        draft[key] = value
    assert cc.next_question(draft, specs) == "specs"  # ONE combined question for the suggested specs
    assert cc.next_question(draft, specs, asked={"specs": 1}) == "hsn"
    assert cc.next_question(draft, specs, declined=["specs"]) == "hsn"
    draft["product_details"].update({"Grain Length": "7mm", "Moisture": "12%", "Broken": "2%", "Age": "1 year"})
    assert cc.next_question(draft, specs) == "hsn"  # every suggestion answered: nothing left to ask about
    assert cc.next_question(draft, {**specs, "hsn_offered": True}) == "recap"
    assert cc.next_question(draft, specs, declined=["hsn"]) == "recap"


def test_buyers_are_never_asked_for_a_minimum_order_and_declined_fields_are_skipped():
    draft = {"role": "buyer", "product_details": {"name": "Rice"}, "quantity": {"value": 1, "unit": "kg"}}
    assert cc.next_question(draft, {}) == "price"
    assert cc.next_question(draft, {}, declined=["price"]) == "location"
    assert cc.next_question(draft, {}, declined=["price", "location", "dispatch"]) == "recap"
    # a field asked twice without an answer is let go
    assert cc.next_question(draft, {}, asked={"price": 2}) == "location"


@pytest.mark.parametrize("message,expected", [
    ("skip", "one"), ("not needed", "one"), ("nahi chahiye", "one"), ("baad mein", "one"), ("no", "one"),
    ("skip the rest", "all"), ("that's all", "all"), ("bas itna hi", "all"),
    ("500 kg basmati", None), ("the price is 50 per kg, skip location", None),
])
def test_decline_detection(message, expected):
    assert cc.detect_decline(message) == expected


async def test_conversation_walks_the_questions_in_order_and_ends_with_the_recap(client, db, make_actor):
    user = await make_actor("buyer")
    log = []
    with _Helpers(), _script("Noted."):
        d = await _msg(user, "I want to buy basmati rice", log=log)
        cid = d["conversation_id"]
        assert d["rfq_draft"]["role"] == "buyer" and "how much" in d["reply"].lower()
        d = await _msg(user, "500 kg", cid=cid, log=log)
        assert d["rfq_draft"]["quantity"]["value"] == 500 and "price" in d["reply"].lower()
        d = await _msg(user, "skip", cid=cid, log=log)
        assert "city" in d["reply"].lower() and "price" not in d["reply"].lower()
        assert d["rfq_draft"]["product_details"]["name"].lower() == "basmati rice"  # "skip" is not a product
        d = await _msg(user, "Pune", cid=cid, log=log)
        assert d["rfq_draft"]["location"]["city"] == "Pune" and "days" in d["reply"].lower()
        d = await _msg(user, "within 7 days", cid=cid, log=log)
        assert RECAP_Q in d["reply"] and "**Quantity:** 500 kg" in d["reply"] and "Not set:** Price" in d["reply"]
        st = await _state(db)
        assert st["declined_fields"] == ["price"] and st["recap_awaiting"] == "create"
    with _Helpers(), NO_MODEL:
        d = await _msg(user, "yes", cid=cid, log=log)
    assert d["created_rfq"]["status"] == "active"
    _show("English: asked one question at a time, then recap and yes", log)


async def test_model_question_is_kept_and_only_one_question_is_appended_when_it_has_none(client, db, make_actor):
    user = await make_actor("buyer")
    with _Helpers(), _script("Great, basmati rice it is. Which city do you want it delivered to?"):
        asks = await _msg(user, "I want to buy basmati rice 500 kg")
    assert asks["reply"].count("?") == 1  # the model's own question stands: nothing is appended
    with _Helpers(), _script("Great, basmati rice it is."):
        silent = await _msg(user, "I want to buy basmati rice 500 kg")
    assert silent["reply"].count("?") == 1 and "price" in silent["reply"].lower()


async def test_prompt_directive_only_acknowledges_and_the_flow_asks_exactly_one_question(client, db, make_actor):
    user = await make_actor("buyer")
    seen: list[str] = []
    with _Helpers(), _script("Noted.", seen=seen):
        d = await _msg(user, "I want to buy basmati rice 500 kg")
    prompt = seen[0]
    assert "ONE short sentence" in prompt and "Do NOT ask any question" in prompt and "ask only this" not in prompt
    # the question is appended by the flow, once, and only that one
    assert d["reply"].count("?") == 1 and "target price per unit" in d["reply"] and "delivery location" not in d["reply"]


async def test_suggested_specs_are_asked_in_one_question_then_the_hsn_offer(client, db, make_actor):
    user = await make_actor("buyer")
    specs = [
        {"key": "grain_length", "label": "Grain Length", "placeholder": "", "input_type": "select", "options": ["Long", "Extra long"]},
        {"key": "moisture", "label": "Moisture", "placeholder": ""},
    ]
    hsn = {"code": "100630", "description": "Rice", "formatted_title": "Rice", "category": "Agriculture", "confidence": 0.9}
    with _Helpers(specs=specs, hsn=hsn), _script("Noted."):
        d = await _msg(user, "change the quantity to 500 kg", COMPLETE)
        cid = d["conversation_id"]
        assert "tell me a few specs" in d["reply"]
        assert "• Grain Length — Long, Extra long" in d["reply"] and "• Moisture" in d["reply"]
        # the HSN suggestion is offered in one line right after the spec question, in the same reply
        assert "Suggested HSN 100630 \u2013 Rice. Reply 'yes' to use it." in d["reply"]
        assert d["reply"].index("• Moisture") < d["reply"].index("Suggested HSN")
        d = await _msg(user, "skip", cid=cid)
        assert RECAP_Q in d["reply"] and "tell me a few specs" not in d["reply"]


async def test_non_drafting_questions_get_no_appended_question(client, db, make_actor):
    user = await make_actor("buyer")
    with _Helpers(), _script("HSN is the tariff code used for customs."):
        d = await _msg(user, "what is an HSN code?", BASE)
    assert d["reply"].startswith("HSN is the tariff code") and "?" not in d["reply"]
    assert RECAP_Q not in d["reply"]


# ---- C. role in the conversation -----------------------------------------------------------
async def test_both_role_account_is_asked_buy_or_sell(client, db, make_actor):
    user = await make_actor("both")
    with _Helpers(), _script("Noted."):
        d = await _msg(user, "basmati rice 500 kg")
    assert "Are you buying or selling basmati rice?" in d["reply"] and not d["rfq_draft"].get("role")


async def test_single_role_account_gets_its_role_without_being_asked_and_hears_it_once(client, db, make_actor):
    user = await make_actor("seller")
    log = []
    with _Helpers(), _script("Noted."):
        d = await _msg(user, "basmati rice 500 kg", log=log)
        cid = d["conversation_id"]
        assert d["rfq_draft"]["role"] == "seller" and "buying or selling" not in d["reply"]
        d = await _msg(user, "create the rfq", cid=cid, log=log)
    assert "your account can only post as a seller" in d["reply"] and d["created_rfq"] is None
    with _Helpers(), NO_MODEL:
        again = await _msg(user, "create the rfq", cid=cid)
        assert again["created_rfq"] is not None  # same draft, same recap: confirmed
    _show("Seller-only account: role set automatically, mentioned once", log)


async def test_role_the_account_cannot_post_is_refused_not_set(client, db, make_actor):
    user = await make_actor("buyer")
    with _Helpers(), _script("Noted."):
        d = await _msg(user, "I want to sell basmati rice 500 kg")
    assert "can only post as a buyer" in d["reply"]
    assert d["rfq_draft"].get("role") != "seller"


async def test_seller_is_asked_for_the_minimum_order_after_quantity(client, db, make_actor):
    user = await make_actor("both")
    draft = {"role": "seller", "category": "Agriculture", "product_details": {"name": "Basmati Rice"}}
    with _Helpers(), _script("Noted."):
        d = await _msg(user, "I have 5000 kg", draft)
        assert "minimum order" in d["reply"].lower()


# ---- D. "... and save" in edit mode ---------------------------------------------------------
async def _edit_conv(user, make_rfq):
    rfq = await make_rfq(user, role="buyer", title="Rice 500 kg", product="basmati rice", category="Agriculture",
                         quantity=(500, "kg"), price=(50, "INR", "kg"), status="draft")
    with _Helpers(), _script("ok"):
        first = await _msg(user, "edit my rice rfq")
    return rfq, first["conversation_id"]


async def _row(db, rfq_id):
    await db.rollback()
    return (await db.execute(text("select status, quantity_value, price_amount from rfqs where id = :i"), {"i": rfq_id})).one()


@pytest.mark.parametrize("phrase,column,expected", [
    ("set price to 75 INR per kg and save", "price_amount", 75),
    ("change qty to 600 and save", "quantity_value", 600),
    ("price 80 kar do aur save karo", "price_amount", 80),
])
async def test_change_and_save_in_one_message_saves_directly(client, db, make_actor, make_rfq, phrase, column, expected):
    user = await make_actor("buyer")
    rfq, cid = await _edit_conv(user, make_rfq)
    with _Helpers(), NO_MODEL:
        data = await _msg(user, phrase, cid=cid)
    assert "RFQ Updated" in data["reply"] and data["editing_rfq_id"] is None, data["reply"]
    assert float(getattr(await _row(db, rfq["id"]), column)) == expected


@pytest.mark.parametrize("phrase", ["set price to 50 and save", "change qty to 600 and save", "price 50 kar do aur save karo"])
def test_and_save_phrases_are_save_intents(phrase):
    assert perms.check_save_edit_intent(phrase) and perms.edit_command_has_change(phrase)


@pytest.mark.parametrize("phrase", ["set price to 50 and don't save", "should I set price to 50 and save?", "price 50 mat save karo"])
def test_and_save_negations_and_questions_do_not_save(phrase):
    assert not perms.check_save_edit_intent(phrase)


async def test_edit_save_shows_only_changed_fields_then_yes_saves(client, db, make_actor, make_rfq):
    user = await make_actor("buyer")
    rfq, cid = await _edit_conv(user, make_rfq)
    log = []
    with _Helpers(), _tool_chat({"price_amount": 65, "price_currency": "INR", "price_per_unit": "kg"}):
        await _msg(user, "make the price 65 INR per kg", cid=cid, log=log)
    with _Helpers(), NO_MODEL:
        recap = await _msg(user, "save changes", cid=cid, log=log)
        assert "INR 50 per kg -> INR 65 per kg" in recap["reply"] and "Quantity" not in recap["reply"]
        assert recap["reply"].endswith("Save these changes?") and recap["editing_rfq_id"] is not None
        assert float((await _row(db, rfq["id"])).price_amount) == 50
        data = await _msg(user, "yes", cid=cid, log=log)
    assert "RFQ Updated" in data["reply"] and float((await _row(db, rfq["id"])).price_amount) == 65
    _show("Edit mode: changed-fields recap, yes saves", log)


async def test_edit_save_hinglish_recap_and_no_keeps_editing(client, db, make_actor, make_rfq):
    user = await make_actor("buyer")
    rfq, cid = await _edit_conv(user, make_rfq)
    with _Helpers(), _tool_chat({"quantity_value": 700, "quantity_unit": "kg"}):
        await _msg(user, "mujhe quantity 700 kg kar do aur jaldi", cid=cid)
    with _Helpers(), NO_MODEL:
        recap = await _msg(user, "changes save karo", cid=cid)
        assert recap["reply"].endswith("Ye changes save karun?") and "500 kg -> 700 kg" in recap["reply"]
        no = await _msg(user, "ruko", cid=cid)
        assert no["editing_rfq_id"] is not None and float((await _row(db, rfq["id"])).quantity_value) == 500
        recap2 = await _msg(user, "save changes", cid=cid)  # the "no" forgot the recap: shown again
        assert recap2["reply"].endswith("Ye changes save karun?")
        done = await _msg(user, "haan", cid=cid)
    assert "RFQ Updated" in done["reply"] and float((await _row(db, rfq["id"])).quantity_value) == 700


async def test_edit_change_after_the_edit_recap_needs_a_new_recap(client, db, make_actor, make_rfq):
    user = await make_actor("buyer")
    rfq, cid = await _edit_conv(user, make_rfq)
    with _Helpers(), _tool_chat({"quantity_value": 700, "quantity_unit": "kg"}):
        await _msg(user, "make the quantity 700 kg", cid=cid)
    with _Helpers(), NO_MODEL:
        await _msg(user, "save changes", cid=cid)
    with _Helpers(), _tool_chat({"quantity_value": 750, "quantity_unit": "kg"}):
        await _msg(user, "actually make it 750 kg", cid=cid)
    with _Helpers(), NO_MODEL:
        again = await _msg(user, "save changes", cid=cid)
    assert "500 kg -> 750 kg" in again["reply"] and float((await _row(db, rfq["id"])).quantity_value) == 500
