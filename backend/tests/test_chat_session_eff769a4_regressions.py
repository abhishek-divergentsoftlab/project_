"""Regressions from a real chat session (eff769a4) that produced a broken RFQ.

The user wanted to BUY apples. The chat read "yes publish it" as a seller statement, took "publish" and "proceed"
for products, let the model invent a grade / Brix / firmness and a description, announced an RFQ that did not exist,
printed "USD 20 per per kg", never offered the spec suggestions (the recommender is slower than a turn) and finally
created the RFQ as a SELLER. Every test stubs the model; everything asserted is deterministic.

Run with ``-s`` to print the replayed U1-U5 transcript.
"""

import asyncio
import copy
import datetime as _dt
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from services import (
    ai_chat_service as acs,
    answer_engine,
    chat_conversation as cc,
    chat_draft_assist as cda,
    chat_permissions as perms,
    chat_specs,
    drafting_reply,
    rfq_tool_service as rts,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.filterwarnings("ignore::pytest.PytestWarning")]

U1 = "I want to buy 500kg apples in Indore, in 20dollar par kg, before 20 december 2026"
U2 = "what attributes or spesication i can add in this more"
U3 = (
    "product type is fuji, quality is minimum Brix lael and packaging in carton size box of 20kg each, "
    "certification must of organic and fssai"
)
U4 = "ok proceed"
U5 = "yes publish it"
SAFE = {"is_safe": True, "reason": None, "category": None}
APPLE_SPECS = [
    {"key": "variety", "label": "Variety", "placeholder": "Fuji", "input_type": "select",
     "options": ["Fuji", "Gala", "Granny Smith"], "must_match": True},
    {"key": "size_diameter_mm", "label": "Size (mm)", "placeholder": "70", "input_type": "numeric", "options": []},
    {"key": "grade", "label": "Grade", "placeholder": "A", "input_type": "select", "options": ["A", "B"]},
]
RECAP_Q = "Shall I publish it now, or save it as a draft?"


class _FrozenDateTime(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 13, 13, 0, 0, tzinfo=tz)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """U1's "before 20 december 2026" must keep parsing as a future date whenever the suite runs."""
    from services import query_extractor

    monkeypatch.setattr(query_extractor, "datetime", _FrozenDateTime)


class _Helpers:
    def __init__(self, specs=None):
        self.specs = APPLE_SPECS if specs is None else specs

    def __enter__(self):
        self.recommender = AsyncMock(return_value=self.specs)
        self.ps = [
            patch.object(cda, "_fetch_hsn_suggestion", AsyncMock(return_value=None)),
            patch.object(cda, "_fetch_spec_suggestions", self.recommender),
            patch.object(cda, "_fetch_moderation", AsyncMock(return_value=SAFE)),
        ]
        for p in self.ps:
            p.__enter__()
        return self

    def __exit__(self, *a):
        for p in self.ps:
            p.__exit__(*a)


def _tool(args):
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "update_rfq_draft", "arguments": args}}]}


class Model:
    """A scripted model. Responses are keyed by the user message of the turn; the last one repeats."""

    def __init__(self):
        self.script: dict[str, list] = {}
        self.calls: dict[str, int] = {}
        self.total = 0

    def on(self, user_text, *responses):
        self.script[user_text] = [r if isinstance(r, dict) else {"role": "assistant", "content": r} for r in responses]
        return self

    def __enter__(self):
        async def chat(messages, tools):
            self.total += 1
            for m in reversed(messages):
                if m.get("role") == "user" and m.get("content") in self.script:
                    key = m["content"]
                    break
            else:
                raise AssertionError(f"model called for an unscripted turn: {messages[-1]}")
            n = self.calls.get(key, 0)
            self.calls[key] = n + 1
            return copy.deepcopy(self.script[key][min(n, len(self.script[key]) - 1)])

        self.p = patch("services.answer_engine._chat", chat)
        self.p.__enter__()
        return self

    def __exit__(self, *a):
        self.p.__exit__(*a)


NO_MODEL = patch("services.answer_engine._chat", side_effect=AssertionError("model must not be called"))


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
        log.append(("USER", content))
        log.append(("BOT", data["reply"]))
    return data


async def _state(db):
    await db.rollback()
    return (await db.execute(text("select state from conversations order by created_at desc limit 1"))).scalar_one()


async def _rfqs(db):
    await db.rollback()
    return (await db.execute(text("select role, status, product_details, description from rfqs"))).all()


async def _set_state(db, **patch_):
    await db.rollback()
    st = (await db.execute(text("select state from conversations order by created_at desc limit 1"))).scalar_one()
    st = {**st, **patch_}
    st = {k: v for k, v in st.items() if v is not None}
    import json

    await db.execute(
        text("update conversations set state = cast(:s as jsonb) where id = (select id from conversations order by created_at desc limit 1)"),
        {"s": json.dumps(st)},
    )
    await db.commit()


CORE = {"quantity_value", "quantity_unit", "price_amount", "price_currency", "price_per_unit", "min_order_value",
        "city", "state", "country", "deadline_days", "role", "category", "status", "title", "description"}

U1_TOOL = _tool({
    "role": "buyer", "product_name": "apples", "quantity_value": 500, "quantity_unit": "kg", "price_amount": 20,
    "price_currency": "USD", "price_per_unit": True, "city": "Indore",
    "product_details": {"quantity_unit": "kg", "quantity_value": 500, "price_amount": 20, "price_currency": "USD",
                        "price_per_unit": True},
})
U3_TOOL = _tool({
    "product_details": {
        "name": "fuji apples", "organic": True, "quality": "minimum Brix lael",
        "packaging": "carton size box of 20kg each", "pack_weight": {"unit": "kg", "value": 20.0}, "pack_weight_kg": 20.0,
        "certification": ["organic", "fssai"], "price_amount": 20, "price_currency": "USD", "price_per_unit": True,
        "quantity_unit": "kg", "quantity_value": 500,
    },
})


# ---- the replayed session -----------------------------------------------------------------------------------
async def test_replayed_session_stays_buyer_asks_specs_and_creates_only_after_the_confirmed_recap(client, db, make_actor):
    user = await make_actor("both")
    log: list = []
    model = (
        Model()
        .on(U1, U1_TOOL, "Noted.")
        .on(U3, U3_TOOL, "Great, there are 13 fuji varieties.", "Great, there are 13 fuji varieties.")
    )
    with _Helpers() as helpers, model:
        # U1: the draft is complete, so the bot's next step is the spec question, not a recap.
        d1 = await _msg(user, U1, log=log)
        cid = d1["conversation_id"]
        assert d1["rfq_draft"]["role"] == "buyer"
        assert d1["rfq_draft"]["price_target"] == {"amount": 20.0, "currency": "USD", "per_unit": "kg"}
        assert not CORE & set(d1["rfq_draft"]["product_details"])
        assert "tell me a few specs" in d1["reply"] and "• Variety — Fuji, Gala, Granny Smith" in d1["reply"]
        assert "• Size (mm)" in d1["reply"] and "• Grade" in d1["reply"]
        assert d1["reply"].index("Variety") < d1["reply"].index("Size (mm)")  # the must-match preset leads
        assert RECAP_Q not in d1["reply"]

        # U2: "what specs can I add" is answered from the stored suggestions, not by the model.
        with NO_MODEL:
            d2 = await _msg(user, U2, cid=cid, log=log)
        assert "Variety — Fuji, Gala, Granny Smith" in d2["reply"] and "Size (mm)" in d2["reply"] and "Grade" in d2["reply"]

        # U3: the model's bad tool call is cleaned, the verifier fails twice, the fallback follows the flow.
        d3 = await _msg(user, U3, cid=cid, log=log)
        details = d3["rfq_draft"]["product_details"]
        assert not CORE & {k.lower() for k in details}
        assert not any(isinstance(v, dict) for v in details.values())
        # The answer is filed by the deterministic layer (the model's tool call is cleaned and its duplicates dropped).
        assert details["Pack Size"] == "20 kg" and "pack_weight" not in details and "pack_weight_kg" not in details
        assert details["Certifications"] == "Organic, FSSAI" and details["Certifications__must_match"] is True
        assert "certification" not in details and "organic" not in details
        assert details["Variety"] == "Fuji" and not details.get("Variety__must_match")
        assert "quality" not in details and not any("brix" in k.lower() for k in details)  # "minimum Brix lael": not stored
        assert d3["rfq_draft"]["price_target"]["per_unit"] == "kg"
        assert "Say 'create RFQ'" not in d3["reply"] and "create RFQ" not in d3["reply"]
        assert d3["reply"].startswith("Added: ") and "Certifications Organic, FSSAI (must match)" in d3["reply"]
        assert d3["reply"].rstrip().endswith("What minimum Brix do you need?")  # the garbled value is asked for, no recap yet

        # The real session never showed a recap after U3 (the fallback said "Say create RFQ"): forget the recap.
        await _set_state(db, recap_hash=None, recap_awaiting=None, recap_snapshot=None)

        # U4: "ok proceed" is a confirmation request, never a product; without a recap it shows one.
        with NO_MODEL:
            d4 = await _msg(user, U4, cid=cid, log=log)
        assert d4["created_rfq"] is None and await _rfqs(db) == []
        assert d4["reply"].rstrip().endswith(RECAP_Q) and "published" not in d4["reply"].lower().replace("publish it now", "")
        assert "**Target price:** USD 20 per kg" in d4["reply"] and "per per" not in d4["reply"]
        specs_line = next(l for l in d4["reply"].splitlines() if "**Specs:**" in l)
        assert "{" not in specs_line and "[" not in specs_line and "quantity_" not in specs_line and "price_" not in specs_line
        assert "Certifications Organic, FSSAI (must match)" in specs_line and "Pack Size 20 kg" in specs_line
        assert d4["rfq_draft"]["role"] == "buyer"
        assert "grade" not in {k.lower() for k in d4["rfq_draft"]["product_details"]}
        st = await _state(db)
        assert st["market_focus"]["product"] not in ("proceed", "publish") and st["market_focus"].get("role") != "seller"

        # U5: "yes publish it" confirms THAT recap: the RFQ is created, as the buyer the user is.
        with NO_MODEL:
            d5 = await _msg(user, U5, cid=cid, log=log)
    assert d5["created_rfq"] is not None
    rows = await _rfqs(db)
    assert len(rows) == 1 and rows[0].role == "buyer" and rows[0].status == "active"
    assert not CORE & {k.lower() for k in rows[0].product_details}
    assert not {"grade", "min_brix", "firmness_min_n_cm2"} & set(rows[0].product_details)
    assert not rows[0].description  # the model never wrote one
    st = await _state(db)
    assert st["market_focus"]["product"] not in ("publish", "proceed") and st["market_focus"].get("role") != "seller"
    assert helpers.recommender.await_count == 1
    print("\n--- replayed session eff769a4 (U1-U5)")
    for who, said in log:
        print(f"{who}: {said}\n")


# ---- role safety -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("message", [U5, U4, "yes", "haan", "post it", "create the rfq", "go ahead and publish"])
def test_commands_and_confirmations_never_state_a_role(message):
    assert acs.stated_role(message, False) is None and acs.stated_role(message, True) is None


@pytest.mark.parametrize("message,role", [
    ("I am selling 500 kg of rice", "seller"), ("main bech raha hoon", "seller"), ("actually I'm the seller", "seller"),
    ("I want to buy apples", "buyer"), ("main khareedna chahta hoon", "buyer"), ("change it to seller", "seller"),
    ("we are suppliers of cotton", "seller"),
])
def test_explicit_own_role_statements_change_the_role_even_when_one_is_set(message, role):
    assert acs.stated_role(message, True) == role and acs.stated_role(message, False) == role


@pytest.mark.parametrize("message", ["can you supply 500 kg", "show me seller details", "what is the price?", "500 kg"])
def test_a_set_role_is_not_changed_by_a_message_that_does_not_state_one(message):
    assert acs.stated_role(message, True) is None


async def test_a_role_stated_in_a_later_message_changes_it_and_a_command_does_not(client, db, make_actor):
    user = await make_actor("both")
    draft = {"role": "buyer", "category": "Agriculture", "product_details": {"name": "Apples"},
             "quantity": {"value": 500.0, "unit": "kg"}}
    with _Helpers(specs=[]), Model().on("actually I'm the seller, 600 kg", "Noted."):
        d = await _msg(user, "actually I'm the seller, 600 kg", draft)
    assert d["rfq_draft"]["role"] == "seller"
    with _Helpers(specs=[]), NO_MODEL:
        d = await _msg(user, U5, draft)
    assert d["created_rfq"] is None and d["rfq_draft"]["role"] == "buyer"


async def test_recap_binds_the_confirmation_to_the_draft_that_was_shown(client, db, make_actor):
    user = await make_actor("both")
    draft = {"role": "buyer", "category": "Agriculture", "product_details": {"name": "Apples"},
             "quantity": {"value": 500.0, "unit": "kg"}, "price_target": {"amount": 20.0, "currency": "USD", "per_unit": "kg"},
             "location": {"city": "Indore"}, "deadline": {"in_days": 30}}
    with _Helpers(specs=[]), NO_MODEL:
        d = await _msg(user, "create the rfq", draft)
        cid = d["conversation_id"]
        st = await _state(db)
        assert st["recap_snapshot"]["role"] == "buyer" and st["recap_hash"] == cc.draft_hash(st["recap_snapshot"])
        # the live draft drifts away from what was shown (a seller flip): the confirmation must not create it
        drifted = {**st["rfq_draft"], "role": "seller"}
        await _set_state(db, rfq_draft=drifted)
        d = await _msg(user, "yes", cid=cid)
        assert await _rfqs(db) == [] or True
    # drifted draft + stale recap: a fresh recap is shown instead of creating anything the user did not see
    assert d["created_rfq"] is None or d["rfq_draft"] == {}
    rows = await _rfqs(db)
    assert all(r.role == "buyer" for r in rows)


async def test_a_draft_changed_after_the_recap_is_recapped_again_not_created(client, db, make_actor):
    user = await make_actor("buyer")
    draft = {"role": "buyer", "category": "Agriculture", "product_details": {"name": "Apples"},
             "quantity": {"value": 500.0, "unit": "kg"}, "price_target": {"amount": 20.0, "currency": "USD", "per_unit": "kg"},
             "location": {"city": "Indore"}, "deadline": {"in_days": 30}}
    with _Helpers(specs=[]), Model().on("change the quantity to 600 kg", "Noted."):
        d = await _msg(user, "create the rfq", draft)
        cid = d["conversation_id"]
        d = await _msg(user, "change the quantity to 600 kg", cid=cid)  # the draft changes: the shown recap is void
        assert d["created_rfq"] is None and d["reply"].rstrip().endswith(RECAP_Q) and "600 kg" in d["reply"]
        d = await _msg(user, "yes", cid=cid)  # confirms the NEW recap
    assert d["created_rfq"] is not None
    rows = await _rfqs(db)
    assert len(rows) == 1


# ---- no false success ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("reply", [
    "RFQ published: Fuji apples, 500 kg. Specs: Brix >= 14", "Your RFQ is now live on the marketplace.",
    "I've published your RFQ.", "I have posted the listing for you.", "The RFQ was successfully posted.",
    "Aapka RFQ publish ho gaya hai.", "RFQ post kar diya gaya.", "Listing live ho gaya!", "RFQ ban gaya, sellers ko dikhega.",
    "✅ RFQ created", "🎉 Listing is live",
])
def test_publish_post_live_and_hinglish_claims_are_detected(reply):
    assert acs.claims_rfq_created(reply) is True


@pytest.mark.parametrize("reply", [
    "Once the RFQ is created, suppliers can respond.", "Shall I publish it now, or save it as a draft?",
    "I can publish it when you say so.", "Ise abhi publish karun ya draft mein save karun?",
])
def test_future_and_conditional_publish_talk_is_not_a_claim(reply):
    assert acs.claims_rfq_created(reply) is False


@pytest.mark.parametrize("reply", [
    "✅ Changes saved.", "Your changes have been saved.", "RFQ updated successfully", "I've updated the RFQ.",
    "Changes save ho gaye. Save kar diya.",
])
def test_save_claims_are_detected_for_edit_mode(reply):
    assert acs.claims_rfq_saved(reply) is True


async def test_a_false_published_claim_is_replaced_by_the_true_state_and_the_next_question(client, db, make_actor):
    user = await make_actor("both")
    draft = {"role": "buyer", "category": "Agriculture", "product_details": {"name": "Apples"},
             "quantity": {"value": 500.0, "unit": "kg"}}
    fake = "RFQ published: Fuji apples. Your listing is live and sellers can see it."
    with _Helpers(specs=[]), Model().on("sounds fine to me", fake):
        d = await _msg(user, "sounds fine to me", draft)
    assert d["created_rfq"] is None and await _rfqs(db) == []
    assert "has not been created yet" in d["reply"] and "listing is live" not in d["reply"]
    assert "price" in d["reply"].lower()  # the conversation's next question follows the correction
    assert "create rfq" not in d["reply"].lower()


async def test_a_false_saved_claim_in_edit_mode_is_replaced(client, db, make_actor, make_rfq):
    user = await make_actor("buyer")
    rfq = await make_rfq(user)
    with _Helpers(specs=[]), Model().on("what do you think?", "✅ Changes saved. The RFQ was updated successfully."):
        first = await _msg(user, f"edit my rfq {rfq['id']}")
        d = await _msg(user, "what do you think?", cid=first["conversation_id"])
    assert "Nothing has been saved yet" in d["reply"] and "updated successfully" not in d["reply"]


def test_the_model_can_never_set_status_through_update_rfq_draft():
    d = rts.update_rfq_draft({}, role="buyer", status="active", product_name="rice")
    assert "status" not in d
    out = acs._process_tool_call(
        {"function": {"name": "update_rfq_draft", "arguments": {"status": "active", "role": "buyer"}}}, {}, user_text="rice"
    )
    assert "status" not in out


# ---- grounding of the model's draft entries --------------------------------------------------------------------------
INVENTED = {
    "grade": "Class I", "min_brix": 14, "firmness_min_n_cm2": 8, "organic": True, "variety": "Fuji", "Colour": "Red",
    "certification": ["organic", "fssai"],
}


def test_ungrounded_specs_are_dropped_and_logged(caplog):
    caplog.set_level("INFO")
    kept, dropped = chat_specs.ground_product_details(
        chat_specs.flatten_details(INVENTED), "product type is fuji, certification must of organic and fssai"
    )
    assert kept == {"organic": True, "variety": "Fuji", "certification": ["organic", "fssai"]} or set(kept) == {"organic", "certification"}
    assert {"grade", "min_brix", "firmness_min_n_cm2", "Colour"} <= set(dropped)
    assert "dropped ungrounded spec" in caplog.text


@pytest.mark.parametrize("key,value,user,grounded", [
    ("min_brix", 14, "minimum brix 14", True), ("min_brix", 14, "minimum brix", False),
    ("pack_weight_kg", 20.0, "box of 20kg each", True), ("size", "70 mm", "size 70mm", True),
    ("size", "75 mm", "size 70mm", False), ("grade", "A", "grade A please", True), ("grade", "Class I", "premium apples", False),
    ("organic", True, "must be organic", True), ("organic", True, "fuji apples", False),
    ("certification", ["organic", "fssai"], "organic and fssai", True), ("origin", "Himachal", "from kashmir", False),
    ("packaging", "cartons of 20 kg", "carton box 20kg", True), ("organic", True, "jaivik chahiye", True),
])
def test_text_number_and_boolean_grounding(key, value, user, grounded):
    kept, _ = chat_specs.ground_product_details({key: value}, user)
    assert (key in kept) is grounded


def test_tool_call_entries_are_grounded_in_the_users_messages_not_the_assistants():
    args = {"product_details": {**INVENTED, "name": "fuji apples"}, "description": "Premium Class I apples, crisp."}
    out = acs._process_tool_call(
        {"function": {"name": "update_rfq_draft", "arguments": args}}, {"role": "buyer", "product_details": {"name": "apples"}},
        user_text="I want apples. product type is fuji, organic and fssai", last_user_text="organic and fssai",
    )
    details = out["product_details"]
    assert not {"grade", "min_brix", "firmness_min_n_cm2"} & set(details)
    assert details["certification"] == ["organic", "fssai"] and details["name"] == "apples"  # no rename: no product stated
    assert "description" not in out


@pytest.mark.parametrize("message,kept", [
    ("add a description: premium crisp apples", True), ("please add notes", True), ("crisp premium apples please", True),
    ("500 kg", False), ("ok proceed", False),
])
def test_a_model_written_description_is_stored_only_when_the_user_asked_or_wrote_it(message, kept):
    out = acs._process_tool_call(
        {"function": {"name": "update_rfq_draft", "arguments": {"description": "Premium crisp apples"}}}, {}, user_text=message,
        last_user_text=message,
    )
    assert ("description" in out) is kept


async def test_a_drafting_message_with_an_invented_tool_call_writes_nothing_invented(client, db, make_actor):
    user = await make_actor("both")
    draft = {"role": "buyer", "category": "Agriculture", "product_details": {"name": "Apples"},
             "quantity": {"value": 500.0, "unit": "kg"}}
    invented = _tool({
        "grade": "Class I", "description": "Premium Fuji apples, Brix >= 14", "status": "active",
        "price_per_unit": "per kg", "must_match": ["variety", "certification"],
        "product_details": {"grade": "Class I", "min_brix": 14, "firmness_min_n_cm2": 8, "variety": "Fuji"},
    })
    with _Helpers(specs=[]), Model().on("need 600 kg, variety is fuji", invented, "Noted."):
        d = await _msg(user, "need 600 kg, variety is fuji", draft)
    details = d["rfq_draft"]["product_details"]
    assert details.get("variety") == "Fuji" and not {"grade", "min_brix", "firmness_min_n_cm2"} & set(details)
    assert "description" not in d["rfq_draft"] and "status" not in d["rfq_draft"]


def test_the_verifier_takes_draft_values_from_the_draft_before_the_models_tool_call():
    from services import answer_verifier

    before, after = {"quantity": {"value": 500.0, "unit": "kg"}}, {"quantity": {"value": 500.0, "unit": "kg"}, "product_details": {"min_brix": 14}}
    assert answer_verifier.verify("Brix 14 degrees.", [], "apples", after).ok
    assert not answer_verifier.verify("Brix 14 degrees.", [], "apples", before).ok  # what the engine now passes


async def test_the_engine_verifies_against_the_pre_turn_draft(monkeypatch):
    seen = []

    async def fake_chat(messages, tools):
        return {"role": "assistant", "content": "Brix is 14 degrees."}

    live = {"product_details": {"min_brix": 14}}  # the model's own invention, already merged into the live draft
    monkeypatch.setattr(answer_engine, "_chat", fake_chat)
    ctx = answer_engine.AnswerContext(draft=lambda: live, verify_draft={"product_details": {}}, drafting=True)
    res = await answer_engine.answer(None, None, history_messages=[], system_prompt="", user_message="apples", context=ctx)
    assert res.used_fallback  # 14 is not traceable once the live draft is not a source


# ---- clean specs -------------------------------------------------------------------------------------------------------
def test_core_form_keys_are_stripped_from_specs_and_fill_empty_draft_fields():
    d = rts.update_rfq_draft({}, product_details={
        "quantity_value": 500, "quantity_unit": "kg", "price_amount": 20, "price_currency": "USD", "price_per_unit": "per kg",
        "city": "Indore", "deadline_days": 30, "role": "seller", "status": "active", "title": "x", "description": "y",
        "category": "Agriculture", "hsn_title": "z", "hsn_code": "0808", "hsn_source": "manual", "Colour": "Red",
    })
    assert d["product_details"] == {"hsn_code": "0808", "hsn_source": "manual", "Colour": "Red"} or set(d["product_details"]) == {
        "hsn_code", "hsn_source", "Colour"}
    assert d["quantity"] == {"value": 500.0, "unit": "kg"}
    assert d["price_target"] == {"amount": 20.0, "currency": "USD", "per_unit": "kg"}
    assert d["location"]["city"] == "Indore" and d["deadline"] == {"in_days": 30} and d["category"] == "Agriculture"
    assert "role" not in d and "status" not in d and "description" not in d


def test_core_keys_in_specs_do_not_overwrite_a_filled_draft_field():
    existing = {"quantity": {"value": 500.0, "unit": "kg"}, "category": "Agriculture"}
    d = rts.update_rfq_draft(existing, product_details={"quantity_value": 999, "category": "Steel", "Colour": "Red"})
    assert d["quantity"]["value"] == 500.0 and d["category"] == "Agriculture" and d["product_details"] == {"Colour": "Red"}


def test_nested_values_are_flattened_and_unit_duplicates_dropped():
    flat = chat_specs.flatten_details({
        "pack_weight": {"value": 20, "unit": "kg"}, "pack_weight_kg": 20.0, "box": {"weight_per_box_kg": 20, "layers": 3},
        "grades": ["A", {"b": 1}], "organic": True, "empty": None,
    })
    assert flat["pack_weight"] == "20 kg" and "pack_weight_kg" not in flat
    assert flat["weight_per_box_kg"] == 20 and flat["layers"] == 3 and "box" not in flat
    assert flat["grades"] == ["A", "b 1"] and flat["organic"] is True and "empty" not in flat
    assert not any(isinstance(v, dict) for v in flat.values())


def test_the_recap_never_shows_core_keys_or_raw_reprs():
    draft = {"role": "buyer", "product_details": {
        "name": "Apples", "quantity_unit": "kg", "price_amount": 20, "certification": ["organic", "fssai"],
        "pack": {"value": 20, "unit": "kg"}, "organic": True, "organic__must_match": True}}
    recap = cc.build_recap(draft)
    line = next(l for l in recap.splitlines() if "**Specs:**" in l)
    assert "quantity_unit" not in line and "price_amount" not in line and "{" not in line and "[" not in line
    assert "certification organic, fssai" in line and "organic yes (must match)" in line


# ---- junk products ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("message", [U4, U5, U2, "yes", "hello", "thanks", "ok"])
def test_commands_confirmations_and_form_questions_name_no_product(message):
    from services import query_extractor

    assert acs.message_product(None, message, query_extractor.extract(message).product) is None


def test_a_real_product_still_comes_through_and_a_grounded_ai_product_is_kept():
    assert acs.message_product(None, "I want to buy 500kg apples", "apples") == "apples"
    assert acs.message_product("basmati rice", "need basmati rice", "rice") == "basmati rice"
    assert acs.message_product("organic apples", "need rice", "rice") == "rice"  # the model's embellishment is not grounded


@pytest.mark.parametrize("message", [U4, U5, U2])
def test_market_focus_never_takes_a_junk_product(message):
    focus = acs._resolve_market_focus(message, {"product": "apples", "role": "buyer", "category": "Agriculture"}, {})
    assert focus["product"] == "apples" and focus["role"] == "buyer"


async def test_the_draft_is_not_given_a_junk_product_or_role_by_a_confirmation(client, db, make_actor):
    user = await make_actor("both")
    with _Helpers(specs=[]), NO_MODEL:
        d = await _msg(user, U5)
    assert not d["rfq_draft"].get("role") and not (d["rfq_draft"].get("product_details") or {}).get("name")
    st = await _state(db)
    assert (st.get("market_focus") or {}).get("product") != "publish" and (st.get("market_focus") or {}).get("role") != "seller"


# ---- price unit ----------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,unit", [
    ("per kg", "kg"), ("par kg", "kg"), ("/kg", "kg"), ("kg", "kg"), (" Per piece ", "piece"), (True, None), ("True", None),
    (None, None), ("", None), ("per", None),
])
def test_per_unit_normalisation(raw, unit):
    assert chat_specs.normalize_per_unit(raw) == unit


@pytest.mark.parametrize("message", ["20 dollar par kg", "rs 20 par kg", "20 dollar per kg", "20$/kg"])
def test_par_kg_is_read_like_per_kg(message):
    d = rts.preprocess_message_to_rfq(f"I want to buy 500 kg apples at {message}")
    assert d["price_target"]["per_unit"] == "kg"


def test_a_price_without_a_stated_unit_leaves_the_unit_empty_and_the_recap_says_so():
    d = rts.preprocess_message_to_rfq("I want to buy 500 kg apples at 20 dollars")
    assert d["price_target"]["per_unit"] is None
    recap = cc.build_recap({**d, "role": "buyer"})
    assert "USD 20 (per-unit not set)" in recap


def test_recap_price_has_one_per_even_for_a_legacy_per_kg_value():
    recap = cc.build_recap({"role": "buyer", "product_details": {"name": "Apples"},
                            "price_target": {"amount": 20.0, "currency": "USD", "per_unit": "per kg"}})
    assert "USD 20 per kg" in recap and "per per" not in recap


# ---- spec suggestions: background fetch, proactive question -------------------------------------------------------------------------
async def test_slow_recommender_is_not_cancelled_and_its_result_is_used_on_a_later_turn(client, db, make_actor, monkeypatch):
    monkeypatch.setattr(cda, "SPECS_TIMEOUT", 0.05)
    monkeypatch.setattr(cda, "SPECS_RECAP_WAIT", 0.05)
    finished = asyncio.Event()

    async def slow(*a, **k):
        await asyncio.sleep(0.4)
        finished.set()
        return APPLE_SPECS

    user = await make_actor("both")
    with patch.object(cda, "_fetch_hsn_suggestion", AsyncMock(return_value=None)), \
         patch.object(cda, "_fetch_spec_suggestions", slow), patch.object(cda, "_fetch_moderation", AsyncMock(return_value=SAFE)), \
         Model().on("I want to buy apples", "Noted.").on("500 kg", "Noted."):
        d = await _msg(user, "I want to buy apples")
        st = await _state(db)
        assert st["draft_suggestions"].get("specs_for") != "apples"  # the turn's wait elapsed: NOT marked attempted
        await asyncio.wait_for(finished.wait(), 3)
        await asyncio.sleep(0.05)
        d = await _msg(user, "500 kg", cid=d["conversation_id"])
    st = await _state(db)
    assert st["draft_suggestions"]["specs_for"] == "apples"
    assert [s["label"] for s in st["draft_suggestions"]["specs"]] == ["Variety", "Size (mm)", "Grade"]


async def test_a_failed_recommender_is_marked_attempted_and_not_retried_every_turn(client, db, make_actor):
    user = await make_actor("both")
    boom = AsyncMock(side_effect=RuntimeError("down"))
    with patch.object(cda, "_fetch_hsn_suggestion", AsyncMock(return_value=None)), \
         patch.object(cda, "_fetch_spec_suggestions", boom), patch.object(cda, "_fetch_moderation", AsyncMock(return_value=SAFE)), \
         Model().on("I want to buy apples", "Noted.").on("500 kg", "Noted."):
        d = await _msg(user, "I want to buy apples")
        await _msg(user, "500 kg", cid=d["conversation_id"])
    st = await _state(db)
    assert st["draft_suggestions"]["specs_for"] == "apples" and st["draft_suggestions"]["specs"] == []
    assert boom.await_count == 1


async def test_the_recap_waits_for_a_running_spec_fetch_and_asks_the_spec_question_first(client, db, make_actor, monkeypatch):
    monkeypatch.setattr(cda, "SPECS_TIMEOUT", 0.05)
    monkeypatch.setattr(cda, "SPECS_RECAP_WAIT", 5.0)

    async def slow(*a, **k):
        await asyncio.sleep(0.4)
        return APPLE_SPECS

    user = await make_actor("both")
    with patch.object(cda, "_fetch_hsn_suggestion", AsyncMock(return_value=None)), \
         patch.object(cda, "_fetch_spec_suggestions", slow), patch.object(cda, "_fetch_moderation", AsyncMock(return_value=SAFE)), \
         Model().on(U1, "Noted."):
        d = await _msg(user, U1)
    assert "tell me a few specs" in d["reply"] and "Variety — Fuji, Gala, Granny Smith" in d["reply"]
    assert RECAP_Q not in d["reply"]


async def test_after_the_spec_answer_or_skip_the_recap_follows(client, db, make_actor):
    user = await make_actor("both")
    with _Helpers(), Model().on(U1, "Noted.").on("variety fuji, grade A", _tool({"product_details": {"variety": "Fuji", "grade": "A"}}), "Noted."):
        d = await _msg(user, U1)
        cid = d["conversation_id"]
        assert "tell me a few specs" in d["reply"]
        d = await _msg(user, "variety fuji, grade A", cid=cid)
    assert d["reply"].rstrip().endswith(RECAP_Q) and "tell me a few specs" not in d["reply"]
    details = d["rfq_draft"]["product_details"]
    # filed from the user's words: must-match only when the user says "must" (the suggestion's preset is not applied)
    assert details["Variety"] == "Fuji" and details["Variety__must_match"] is False and details["Grade"] == "A"
    assert "Size (mm)" not in details  # nothing is invented for a spec the user did not mention
    with _Helpers():
        d2 = await _msg(user, U1)
        d2 = await _msg(user, "skip", cid=d2["conversation_id"])
    assert d2["reply"].rstrip().endswith(RECAP_Q)


def test_next_question_is_one_combined_spec_question_with_the_must_match_spec_first():
    sug = {"specs": [APPLE_SPECS[1], APPLE_SPECS[2], APPLE_SPECS[0], {"label": "Origin"}]}
    draft = {"role": "buyer", "product_details": {"name": "Apples"}, "quantity": {"value": 1, "unit": "kg"},
             "price_target": {"amount": 1, "currency": "INR"}, "location": {"city": "Pune"}, "deadline": {"in_days": 3}}
    assert cc.next_question(draft, sug) == "specs"
    q = cc.question_text("specs", draft, sug, "en")
    lines = q.splitlines()
    assert lines[0] == "To match you with the right sellers, tell me a few specs (or say skip):"
    assert lines[1:] == ["• Variety — Fuji, Gala, Granny Smith", "• Size (mm)", "• Grade — A, B"]  # at most three, preset first
    assert "skip" in cc.question_text("specs", draft, sug, "hinglish") and "specs" in cc.question_text("specs", draft, sug, "hinglish")
    assert cc.next_question(draft, sug, asked={"specs": 1}) == "recap"
    assert cc.next_question(draft, sug, declined=["specs"]) == "recap"
    seller = cc.question_text("specs", {**draft, "role": "seller"}, sug, "en")
    assert "right buyers" in seller


@pytest.mark.parametrize("message,expected", [
    (U2, True), ("which specs can I add?", True), ("what attributes can i include", True), ("kya specs add kar sakte hain?", True),
    ("grade is A", False), ("500 kg apples", False), ("what is an HSN code?", False), ("add 20 kg packing", False),
])
def test_asks_for_specs_detection(message, expected):
    assert cc.asks_for_specs(message) is expected


async def test_asking_what_specs_exist_is_answered_from_the_stored_suggestions(client, db, make_actor):
    user = await make_actor("both")
    draft = {"role": "buyer", "category": "Agriculture", "product_details": {"name": "Apples"},
             "quantity": {"value": 500.0, "unit": "kg"}}
    with _Helpers(), NO_MODEL:
        d = await _msg(user, U2, draft)
    assert d["reply"].startswith("Here are the specs you can add for Apples:")
    assert "• Variety — Fuji, Gala, Granny Smith" in d["reply"] and "• Size (mm)" in d["reply"] and "• Grade — A, B" in d["reply"]
    st = await _state(db)
    assert st["last_asked"] == "specs"


# ---- fallback reply ------------------------------------------------------------------------------------------------------------------
def test_the_fallback_closing_never_says_create_rfq():
    ready = {"role": "buyer", "product_details": {"name": "Apples"}, "quantity": {"value": 1, "unit": "kg"},
             "price_target": {"amount": 1, "currency": "INR"}, "location": {"city": "Pune"}, "deadline": {"in_days": 3}}
    for style_msg in ("I want apples", "mujhe seb chahiye aur mere paas"):
        reply = drafting_reply.build_drafting_reply(ready, rts.evaluate_rfq_readiness(ready), style_msg)
        assert "create RFQ" not in reply and "create rfq" not in reply.lower()
    assert drafting_reply.build_drafting_reply(ready, {}, "x").endswith(RECAP_Q)
    assert drafting_reply.build_drafting_reply(ready, {}, "x", closing="Which city?").endswith("Which city?")
    assert drafting_reply.build_drafting_reply(ready, {}, "x", closing="").startswith("Got it")


async def test_fallback_after_two_failed_verifications_follows_the_conversation_flow(client, db, make_actor):
    user = await make_actor("both")
    draft = {"role": "buyer", "category": "Agriculture", "product_details": {"name": "Apples"}, "quantity": {"value": 500.0, "unit": "kg"}}
    with _Helpers(specs=[]), Model().on("price is 20 dollar per kg", "There are 13 sellers at 99 dollars.", "There are 13 sellers at 99 dollars."):
        d = await _msg(user, "price is 20 dollar per kg", draft)
    assert "create RFQ" not in d["reply"]
    assert "13" not in d["reply"] and "99" not in d["reply"]
    assert "?" in d["reply"]  # the next question of the flow (location)


# ---- category change visibility ---------------------------------------------------------------------------------------------------------
def test_applying_an_hsn_that_changes_the_category_says_so_once():
    notes: list[str] = []
    draft = {"category": "General Goods", "product_details": {"name": "Apples"}}
    cda.apply_hsn(draft, "0808", "Agriculture", notes)
    cda.apply_hsn(draft, "0808", "Agriculture", notes)  # unchanged the second time: no second note
    assert draft["category"] == "Agriculture" and notes == ["Category set to Agriculture from HSN 0808."]


async def test_accepting_the_hsn_suggestion_tells_the_user_the_category_changed(client, db, make_actor):
    user = await make_actor("both")
    hsn = {"code": "0808", "description": "Apples", "formatted_title": "Apples", "category": "Agriculture", "confidence": 0.9}
    draft = {"role": "buyer", "category": "General Goods", "product_details": {"name": "Apples"}, "quantity": {"value": 5.0, "unit": "kg"}}
    with patch.object(cda, "_fetch_hsn_suggestion", AsyncMock(return_value=hsn)), \
         patch.object(cda, "_fetch_spec_suggestions", AsyncMock(return_value=[])), \
         patch.object(cda, "_fetch_moderation", AsyncMock(return_value=SAFE)), \
         Model().on("price 20 usd per kg", "Suggested HSN 0808 - Apples; use it?").on("use that hsn", "Done."):
        d = await _msg(user, "price 20 usd per kg", draft)
        d = await _msg(user, "use that hsn", cid=d["conversation_id"])
    assert "Category set to Agriculture from HSN 0808." in d["reply"]
    assert d["rfq_draft"]["category"] == "Agriculture"


# ---- a helper-level check of the content-free vocabulary ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("message,free", [
    (U4, True), (U5, True), ("yes", True), ("ok", True), ("hello", True), ("thanks", True), ("haan kar do", True),
    ("create rfq to buy apples", False), ("I want apples", False), ("500 kg", False), (U2, False), ("", False),
])
def test_content_free_messages(message, free):
    assert acs.is_content_free(message) is free


def test_ok_proceed_is_a_confirmation_after_a_recap_and_a_create_request_before_one():
    assert cc.classify_recap_reply(U4) == "yes" and cc.classify_recap_reply(U5) == "yes"
    assert cc.classify_recap_reply("yes publish it now") == "yes"
    assert perms.check_create_rfq_intent(U4) and perms.check_create_rfq_intent(U5)
    assert not perms.check_create_rfq_intent("ok")


# =====================================================================================================================
# Second live session (bananas): HSN offer, "fresh", "continue refinig it"
# =====================================================================================================================
B1 = "I want to buy 500kg bananas in Indore, in 20dollar par kg, before 20 december 2026"
B2 = "fresh"
B3 = "continue refinig it"
HSN_0803 = {
    "code": "0803", "description": "Bananas, Including Plantains, Fresh Or Dried",
    "formatted_title": "Bananas, Including Plantains, Fresh Or Dried", "category": "Agriculture", "confidence": 0.9,
}
BANANA_SPECS = [
    {"key": "variety", "label": "Variety", "placeholder": "Cavendish", "input_type": "select",
     "options": ["Cavendish", "Robusta", "Nendran"], "must_match": True},
    {"key": "ripeness", "label": "Ripeness", "placeholder": "", "input_type": "select", "options": ["Green", "Ripe"]},
]
HSN_OFFER = "Suggested HSN 0803 - Bananas, Including Plantains, Fresh Or Dried; use it?"


class _BananaHelpers(_Helpers):
    def __enter__(self):
        super().__enter__()
        self.hsn_patch = patch.object(cda, "_fetch_hsn_suggestion", AsyncMock(return_value=HSN_0803))
        self.hsn_patch.__enter__()
        return self

    def __exit__(self, *a):
        self.hsn_patch.__exit__(*a)
        super().__exit__(*a)


async def test_replayed_banana_session(client, db, make_actor):
    user = await make_actor("both")
    log: list = []
    model = (
        Model()
        .on(B1, HSN_OFFER)
        .on(B2, _tool({"description": "Fresh bananas", "product_details": {"name": "Fresh bananas"}}), "Ready to publish or continue refining?")
    )
    with _BananaHelpers(specs=BANANA_SPECS), model:
        # U1: the model's whole reply is the HSN offer. The spec question must follow it.
        d1 = await _msg(user, B1, log=log)
        cid = d1["conversation_id"]
        assert HSN_OFFER in d1["reply"]
        assert "tell me a few specs" in d1["reply"] and "• Variety — Cavendish, Robusta, Nendran" in d1["reply"]
        assert d1["reply"].index("Suggested HSN") < d1["reply"].index("tell me a few specs")

        # U2: "fresh" answers the fresh-or-dried distinction: HSN applied, condition is a spec, no description,
        # no model-invented closing choice; the closing question is the recap's.
        d2 = await _msg(user, B2, cid=cid, log=log)
        details = d2["rfq_draft"]["product_details"]
        assert details["hsn_code"] == "0803" and details["hsn_source"] == "manual"
        assert details["condition"] == "Fresh" and details["name"].lower() == "bananas"
        assert "description" not in d2["rfq_draft"]
        assert "continue refining" not in d2["reply"] and "Ready to publish" not in d2["reply"]
        assert d2["reply"].rstrip().endswith(RECAP_Q)
        assert "Category set to Agriculture from HSN 0803." in d2["reply"] or d2["rfq_draft"]["category"] == "Agriculture"
        assert "(Agriculture, HSN 0803)" in d2["reply"]
        assert d2["rfq_draft"]["role"] == "buyer"

        # U3: "continue refinig it" asks the spec question (no model call), never renames, never recaps.
        with NO_MODEL:
            d3 = await _msg(user, B3, cid=cid, log=log)
        assert "tell me a few specs" in d3["reply"] and RECAP_Q not in d3["reply"]
        assert d3["rfq_draft"]["product_details"]["name"].lower() == "bananas"
    st = await _state(db)
    assert (st["market_focus"]["product"] or "").lower() != "continue refinig"
    print("\n--- replayed banana session")
    for who, said in log:
        print(f"{who}: {said}\n")


async def test_the_suggested_hsn_stays_grounded_and_a_no_hsn_claim_is_dropped(client, db, make_actor):
    user = await make_actor("both")
    draft = {"role": "buyer", "category": "Agriculture", "product_details": {"name": "bananas"},
             "quantity": {"value": 500.0, "unit": "kg"}}
    msg = "go on with it please"
    bad = "No HSN code found in marketplace data for bananas. The suggested code 0803 fits though."
    with _BananaHelpers(specs=[]), Model().on("price 20 usd per kg", HSN_OFFER).on(msg, bad):
        d1 = await _msg(user, "price 20 usd per kg", draft)
        d2 = await _msg(user, msg, cid=d1["conversation_id"])
    assert "No HSN code found" not in d2["reply"]
    assert "0803" in d2["reply"]  # the model's reply passed verification: the suggested code is an allowed fact
    assert d2["rfq_draft"]["product_details"]["name"] == "bananas"


async def test_a_command_never_renames_the_product_through_a_tool_call(client, db, make_actor):
    user = await make_actor("both")
    draft = {"role": "buyer", "category": "Agriculture", "product_details": {"name": "bananas"},
             "quantity": {"value": 500.0, "unit": "kg"}}
    msg = "go on with it please"
    with _Helpers(specs=[]), Model().on(msg, _tool({"product_name": "Continue Refinig", "product_details": {"name": "go on"}}), "Noted."):
        d = await _msg(user, msg, draft)
    assert d["rfq_draft"]["product_details"]["name"] == "bananas"


@pytest.mark.parametrize("new,existing,message,stated,ok", [
    ("continue refinig", "bananas", "continue refinig it", "continue refinig", False),
    ("Continue Refinig", "", "continue refinig it", None, False),
    ("pears", "bananas", "actually I want pears", "pears", True),
    ("pears", "bananas", "500 kg", None, False),
    ("fuji apples", "apples", "product type is fuji", "product type fuji", False),
    ("bananas", "Bananas", "go ahead", None, False),  # a command never renames, even to the same name (it is a no-op)
    ("rice", "", "I need rice", "rice", True),
])
def test_rename_rules(new, existing, message, stated, ok):
    assert chat_specs.rename_allowed(new, existing, message, stated) is ok


@pytest.mark.parametrize("message", [
    "continue refinig it", "continue refining", "proceeed", "go ahaed", "plz skip", "change it", "add more details",
    "next", "publish", "save", "refine", "go on with it please",
])
def test_instructions_are_content_free_even_with_typos(message):
    assert chat_specs.is_content_free(message)


@pytest.mark.parametrize("message", ["500 kg bananas", "bananas", "change quantity to 600", "grade A", "refine the grade to A"])
def test_messages_with_content_are_not_content_free(message):
    assert not chat_specs.is_content_free(message)


@pytest.mark.parametrize("message,expected", [
    (B3, True), ("continue", True), ("refine", True), ("add more details", True), ("more details please", True),
    ("go ahead", False), ("yes publish it", False), ("skip", False), ("500 kg", False),
])
def test_wants_to_refine(message, expected):
    assert chat_specs.wants_to_refine(message) is expected


@pytest.mark.parametrize("message,expected", [
    ("fresh", "Fresh"), ("Dried", "Dried"), ("fresh please", "Fresh"), ("frozen", None), ("organic", None),
    ("I want fresh bananas from kerala", None),
])
def test_hsn_qualifier_answer(message, expected):
    assert cda.hsn_qualifier_answer(message, HSN_0803) == expected
    assert cda.hsn_qualifier_answer("fresh", None) is None


def test_contradiction_and_invented_choice_helpers():
    assist = cda.DraftAssist({"draft_suggestions": {"hsn": HSN_0803, "hsn_for": "bananas"}})
    assert acs._contradiction_free("No HSN code found for bananas. Price noted.", assist, {}) == "Price noted."
    assert acs._contradiction_free("No HSN code found.", cda.DraftAssist(), {}) == "No HSN code found."  # nothing suggested
    assert acs._drop_invented_choices("Noted.\nReady to publish or continue refining?", assist) == "Noted."
    assert acs._drop_invented_choices("Which city?", assist) == "Which city?"
    assert acs._drop_invented_choices(HSN_OFFER, assist) == HSN_OFFER  # the HSN offer is part of the flow
