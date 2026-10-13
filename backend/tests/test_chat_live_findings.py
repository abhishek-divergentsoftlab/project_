"""Findings from replaying two real chat sessions (apples, bananas) against the live model.

Every test stubs the model; everything asserted is deterministic:

1. an answer to the spec question is a drafting turn (a), is filed without the model (b) and is acknowledged from the
   draft diff, never with the model's "RFQ draft saved/updated" (c);
2. the spec list is shown once (the model only acknowledges);
3. the suggested specs and their options are facts the verifier accepts;
4. the suggested HSN is offered in one line after the spec question (twice at most) and "fresh" accepts it;
5. "yes publish it" after the RFQ was created says it exists;
6. the helpers run while the model works.
"""

import asyncio
import copy
import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from services import (
    ai_chat_service as acs,
    answer_engine,
    answer_verifier,
    chat_draft_assist as cda,
    chat_permissions as perms,
    chat_reply,
    chat_spec_filing as csf,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.filterwarnings("ignore::pytest.PytestWarning")]

U1 = "I want to buy 500kg apples in Indore, in 20dollar par kg, before 20 december 2026"
U3 = (
    "product type is fuji, quality is minimum Brix lael and packaging in carton size box of 20kg each, "
    "certification must of organic and fssai"
)
B1 = "I want to buy 500kg bananas in Indore, in 20dollar par kg, before 20 december 2026"
SAFE = {"is_safe": True, "reason": None, "category": None}
RECAP_Q = "Shall I publish it now, or save it as a draft?"
BRIX_Q = "What minimum Brix Sugar Content (°Bx) do you need? e.g. 12, 14"

APPLE8 = [
    {"key": "apple_variety", "label": "Apple Variety", "placeholder": "Fuji", "input_type": "select",
     "options": ["Fuji", "Honeycrisp", "Gala", "Granny Smith", "Red Delicious"], "must_match": True},
    {"key": "size_diameter_mm", "label": "Size Diameter (mm)", "placeholder": "70", "input_type": "numeric",
     "options": ["65", "70", "75", "80", "85"]},
    {"key": "firmness_kpa", "label": "Firmness (kPa)", "placeholder": "80", "input_type": "numeric",
     "options": ["80", "100", "120", "140", "160"]},
    {"key": "blemish_pct", "label": "Blemish Percentage (%)", "placeholder": "2", "input_type": "numeric",
     "options": ["0", "2", "5", "10", "15"]},
    {"key": "packaging_type", "label": "Packaging Type", "placeholder": "Carton Box", "input_type": "select",
     "options": ["Carton Box", "Plastic Tray", "Net Bag", "Bulk Bin", "Palletized"]},
    {"key": "brix_sugar_content_bx", "label": "Brix Sugar Content (°Bx)", "placeholder": "14", "input_type": "numeric",
     "options": ["12", "14", "16", "18", "20"]},
    {"key": "moisture_pct", "label": "Moisture Content (%)", "placeholder": "85", "input_type": "numeric",
     "options": ["80", "85", "90", "92", "95"]},
    {"key": "storage_days", "label": "Storage Duration (days)", "placeholder": "90", "input_type": "numeric",
     "options": ["30", "60", "90", "120", "150"]},
]
HSN_0803 = {
    "code": "0803", "description": "Bananas, Including Plantains, Fresh Or Dried",
    "formatted_title": "Bananas, Including Plantains, Fresh Or Dried", "category": "Agriculture", "confidence": 0.9,
}
HSN_RICE = {"code": "100630", "description": "Rice", "formatted_title": "Rice", "category": "Agriculture", "confidence": 0.9}
BANANA_SPECS = [
    {"key": "variety", "label": "Variety", "placeholder": "Cavendish", "input_type": "select",
     "options": ["Cavendish", "Red Banana", "Lady Finger"]},
    {"key": "ripeness", "label": "Ripeness Level (0-10 Scale)", "placeholder": "5", "input_type": "numeric", "options": ["3", "5", "7", "9"]},
]
COMPLETE = {
    "role": "buyer", "category": "Agriculture", "product_details": {"name": "Basmati Rice"},
    "quantity": {"value": 500.0, "unit": "kg"}, "price_target": {"amount": 50.0, "currency": "INR", "per_unit": "kg"},
    "location": {"city": "Pune", "state": "Maharashtra", "country": "India"}, "deadline": {"in_days": 7},
}


class _Helpers:
    def __init__(self, specs=None, hsn=None, spec_delay=0.0):
        self.specs, self.hsn, self.spec_delay = specs or [], hsn, spec_delay
        self.events: list[str] = []

    def __enter__(self):
        async def specs(*a, **k):
            self.events.append("specs-start")
            if self.spec_delay:
                await asyncio.sleep(self.spec_delay)
            return self.specs

        self.ps = [
            patch.object(cda, "_fetch_hsn_suggestion", AsyncMock(return_value=self.hsn)),
            patch.object(cda, "_fetch_spec_suggestions", specs),
            patch.object(cda, "_fetch_moderation", AsyncMock(return_value=SAFE)),
        ]
        for p in self.ps:
            p.__enter__()
        return self

    def __exit__(self, *a):
        for p in self.ps:
            p.__exit__(*a)


class Model:
    """A scripted model: a reply (or tool call) per user message; every call is recorded."""

    def __init__(self, default="Noted."):
        self.script: dict[str, list] = {}
        self.default = default
        self.calls: list[dict] = []
        self.events: list[str] = []

    def on(self, user_text, *responses):
        self.script[user_text] = [r if isinstance(r, dict) else {"role": "assistant", "content": r} for r in responses]
        return self

    def __enter__(self):
        async def chat(messages, tools):
            self.events.append("model")
            user_msg = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
            self.calls.append({
                "user": user_msg, "system": messages[0]["content"],
                "tools": {((t or {}).get("function") or {}).get("name") for t in (tools or [])},
            })
            seq = self.script.get(user_msg)
            if not seq:
                return {"role": "assistant", "content": self.default}
            n = sum(1 for c in self.calls if c["user"] == user_msg) - 1
            return copy.deepcopy(seq[min(n, len(seq) - 1)])

        self.p = patch("services.answer_engine._chat", chat)
        self.p.__enter__()
        return self

    def __exit__(self, *a):
        self.p.__exit__(*a)


NO_MODEL = patch("services.answer_engine._chat", side_effect=AssertionError("model must not be called"))


def _tool(args):
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "update_rfq_draft", "arguments": args}}]}


async def _msg(user, content, draft=None, cid=None, prev=None):
    """One turn. ``prev`` is the assistant text the client sends back as history (a bare "yes" is read against it)."""
    body = {"messages": [*([{"role": "assistant", "content": prev}] if prev else []), {"role": "user", "content": content}]}
    if draft is not None:
        body["current_rfq"] = draft
    if cid:
        body["conversation_id"] = cid
    res = await user.post("/ai-chat/message", json=body)
    assert res.status_code == 200, res.text
    return res.json()


async def _state(db):
    await db.rollback()
    return (await db.execute(text("select state from conversations order by created_at desc limit 1"))).scalar_one()


async def _rfqs(db):
    await db.rollback()
    return (await db.execute(text("select role, status, title, product_details from rfqs"))).all()


# =====================================================================================================================
# 1a. the answer to the question just asked is a drafting turn, whatever the parser made of it
# =====================================================================================================================
def test_the_apples_answer_reads_as_a_verification_lookup_to_the_permission_gate():
    draft = {"role": "buyer", "product_details": {"name": "apples"}}
    assert perms.effective_intent(U3) == "verification"
    assert perms.allowed_action_tools(U3, draft, {"rfq_draft": draft}, "verification") == set()  # why the service must decide


@pytest.mark.parametrize("message,asks", [
    ("which certifications do I need?", True), ("what is the brix of fuji", True), ("kya organic zaroori hai", True),
    ("kaun sa variety best hai", True), ("is fssai mandatory", True), ("certification must of organic and fssai", False),
    ("product type is fuji", False), ("carton box of 20 kg", False), ("skip", False),
])
def test_a_question_is_not_an_answer(message, asks):
    assert acs._asks_something(message) is asks


async def test_the_spec_answer_gets_the_update_tool_and_a_drafting_turn_even_though_it_reads_as_verification(client, db, make_actor):
    user = await make_actor("both")
    seen: dict = {}
    real = answer_engine.answer

    async def spy(*a, **k):
        seen["drafting"] = k["context"].drafting
        seen["round_only"] = k["context"].tool_round_only
        return await real(*a, **k)

    with _Helpers(specs=APPLE8), Model() as model:
        d1 = await _msg(user, U1)
        with patch("services.answer_engine.answer", spy):
            d2 = await _msg(user, U3, cid=d1["conversation_id"])
    assert seen == {"drafting": True, "round_only": True}
    assert "update_rfq_draft" in model.calls[-1]["tools"] and "Do NOT ask any question" in model.calls[-1]["system"]
    assert d2["rfq_draft"]["product_details"]["variety"] == "Fuji"


async def test_a_question_about_the_specs_is_not_a_drafting_turn(client, db, make_actor):
    user = await make_actor("both")
    seen: dict = {}
    real = answer_engine.answer

    async def spy(*a, **k):
        seen["drafting"] = k["context"].drafting
        return await real(*a, **k)

    with _Helpers(specs=APPLE8), Model() as model:
        d1 = await _msg(user, U1)
        with patch("services.answer_engine.answer", spy):
            d2 = await _msg(user, "which certification is needed for export?", cid=d1["conversation_id"])
    assert not seen.get("drafting") and "update_rfq_draft" not in model.calls[-1]["tools"]
    assert "Added:" not in d2["reply"] and "Certifications" not in d2["rfq_draft"]["product_details"]


# =====================================================================================================================
# 1b. deterministic spec filing
# =====================================================================================================================
def _file(message, specs=APPLE8, details=None, pending=None):
    draft = {"role": "buyer", "product_details": {"name": "apples", **(details or {})}}
    filing = csf.file_answer(draft, message, specs, pending_value=pending)
    return draft["product_details"], filing


def test_the_apples_answer_is_filed_under_the_catalogue_keys():
    details, filing = _file(U3)
    assert details["variety"] == "Fuji" and not details.get("variety__must_match")  # "must" is about certification
    assert details["packaging_type"] == "Carton"  # from the full list of 8, not only the 3 that were asked
    assert details["pack_size"] == "20 kg"
    assert details["certifications"] == ["Organic", "FSSAI"] and details["certifications__must_match"] is True
    assert not any("brix" in k.lower() or k.lower() in ("quality", "grade") for k in details)  # "minimum Brix lael" is garbled
    assert "size_diameter" not in details  # "carton size box of 20kg" is not a size in mm
    assert not any(k.endswith("__must_match") and v is not True for k, v in details.items())  # never a False flag
    assert filing.ask_value == {
        "label": "Brix Sugar Content (°Bx)", "key": "brix_sugar_content", "qualifier": "minimum",
        "examples": ["12", "14"], "unit": "°Bx",
    }
    assert filing.must == ["certifications"]


@pytest.mark.parametrize("message,key,value", [
    ("fuji", "variety", "Fuji"),
    ("variety is gala or fuji", "variety", "Fuji, Gala"),
    ("type is honeycrisp", "variety", "Honeycrisp"),
    ("carton", "packaging_type", "Carton"),
    ("net bag packing", "packaging_type", "Bag"),
    ("minimum brix 14", "brix_sugar_content", "≥14 °Bx"),
    ("min 14 brix", "brix_sugar_content", "≥14 °Bx"),
    ("brix: 12-14", "brix_sugar_content", "12-14 °Bx"),
    ("size 70 mm", "size_diameter", "70 mm"),
    ("firmness at least 100 kpa", "firmness", "≥100 kPa"),
    ("pack size of 25 kg per bag", "pack_size", "25 kg"),
    ("20 kg per box", "pack_size", "20 kg"),
    ("pack size 20000 g", "pack_size", "20 kg"),
    ("colour is red, texture: crisp", "color", "Red"),
    ("texture: crisp", "texture", "Crisp"),
    ("certified organic, haccp", "certifications", ["Organic", "HACCP"]),
])
def test_filing_examples(message, key, value):
    details, _ = _file(message)
    assert details[key] == value, details


def test_must_attaches_to_the_spec_it_is_written_next_to():
    details, filing = _file("variety must be fuji, packaging carton, certification organic aur fssai zaroori hai")
    assert details["variety__must_match"] is True and details["certifications__must_match"] is True
    assert not details.get("packaging_type__must_match")
    assert set(filing.must) == {"variety", "certifications"}


@pytest.mark.parametrize("message", ["size 20kg", "size is carton", "firmness is soft", "brix lael", "brix"])
def test_a_numeric_spec_needs_a_grounded_number_with_a_compatible_unit(message):
    details, filing = _file(message)
    assert not any(k in details for k in ("size_diameter", "firmness", "brix_sugar_content")), details
    if "brix" in message or "firmness" in message:
        assert filing.ask_value is not None


def test_a_bare_number_answers_the_value_question_asked_last_turn():
    pending = {"label": "Brix Sugar Content (°Bx)", "key": "brix_sugar_content", "unit": "°Bx", "qualifier": "minimum"}
    for message, expected in (("14", "≥14 °Bx"), ("12-14", "12-14 °Bx"), ("max 16", "≤16 °Bx"), ("14 brix", "≥14 °Bx")):
        details, _ = _file(message, pending=pending)
        assert details["brix_sugar_content"] == expected, (message, details)
    details, _ = _file("lets see", pending=pending)
    assert "brix_sugar_content" not in details


def test_filing_never_touches_the_form_fields_or_a_question():
    details, filing = _file("price is 25 and quantity is 600 kg, location: Pune")
    assert set(details) == {"name"} and not filing.touched
    details, _ = _file("fuji", details={"Apple Variety": "Gala"})
    assert details["variety"] == "Fuji" and "Apple Variety" not in details  # the newest answer wins, under the catalogue key


async def test_the_spec_answer_is_filed_before_the_model_runs_and_the_model_cannot_undo_or_duplicate_it(client, db, make_actor):
    user = await make_actor("both")
    invented = _tool({"product_details": {
        "name": "fuji apples", "organic": True, "quality": "minimum Brix lael", "min_brix": 14, "pack_weight": {"unit": "kg", "value": 20},
        "certification": ["organic", "fssai"], "packaging": "carton size box of 20kg each", "variety": "fuji",
    }})
    seen: dict = {}
    real = answer_engine.answer

    async def spy(*a, **k):
        seen["draft_at_start"] = copy.deepcopy(k["context"].draft())
        return await real(*a, **k)

    with _Helpers(specs=APPLE8), Model().on(U3, invented, "Great, Brix 14 it is.") as model:
        d1 = await _msg(user, U1)
        with patch("services.answer_engine.answer", spy):
            d2 = await _msg(user, U3, cid=d1["conversation_id"])
    details = d2["rfq_draft"]["product_details"]
    # the draft the model started from already held the filed answer
    start = seen["draft_at_start"]["product_details"]
    assert start["pack_size"] == "20 kg" and start["packaging_type"] == "Carton" and start["variety"] == "Fuji"
    assert {k for k in details if not k.endswith("__must_match")} == {
        "name", "variety", "packaging_type", "pack_size", "certifications"}, details
    assert details["certifications"] == ["Organic", "FSSAI"] and details["certifications__must_match"] is True
    assert d2["reply"].rstrip().endswith(BRIX_Q)
    # the follow-up answer completes it; then the recap
    d3 = await _msg(user, "14", cid=d1["conversation_id"])
    assert d3["rfq_draft"]["product_details"]["brix_sugar_content"] == "≥14 °Bx"
    assert d3["reply"].startswith("Added: Brix Sugar Content ≥14 °Bx") and d3["reply"].rstrip().endswith(RECAP_Q)


# =====================================================================================================================
# 1c. honest acknowledgement
# =====================================================================================================================
@pytest.mark.parametrize("claim", [
    "RFQ draft saved: 500 kg apples, $20/kg, Indore, delivery by 20 Dec 2026.",
    "RFQ draft updated with: Product: Fuji apples, Packaging: 20 kg carton boxes, Certifications: Organic + FSSAI",
    "RFQ draft created: apples.", "RFQ draft captured: 500 kg bananas, $20/kg.", "Updated RFQ draft with: Fuji.",
    "Your RFQ draft has been updated with the new specs.", "I've updated your RFQ draft.", "RFQ draft saved ho gaya.",
    "Added: Apple Variety Fuji.",
])
def test_the_models_draft_claims_are_removed(claim):
    cleaned, removed = chat_reply.strip_draft_claims(f"{claim}\n\nGreat choice.")
    assert removed and cleaned == "Great choice."


def test_a_claim_with_a_bullet_block_loses_the_block_but_not_the_rest():
    text_in = "RFQ draft finalized:  \n- **Product:** Bananas  \n- **Type:** Buying\n\nHere is your RFQ draft:\n- **Product:** Bananas"
    cleaned, removed = chat_reply.strip_draft_claims(text_in)
    assert removed and cleaned == "Here is your RFQ draft:\n- **Product:** Bananas"


def test_the_acknowledgement_comes_from_the_draft_diff():
    before = {"role": "buyer", "product_details": {"name": "apples"}}
    after = {
        "role": "buyer", "quantity": {"value": 500.0, "unit": "kg"},
        "product_details": {"name": "apples", "variety": "Fuji", "packaging_type": "Carton", "pack_size": "20 kg",
                            "certifications": ["Organic", "FSSAI"], "certifications__must_match": True},
    }
    assert chat_reply.build_ack(before, after) == (
        "Added: Quantity 500 kg, Variety Fuji, Packaging Carton, Pack size 20 kg, Certifications Organic, FSSAI (must match)."
    )
    assert chat_reply.build_ack(before, after, "hinglish").startswith("Add kiya: Quantity 500 kg, Variety Fuji")
    labelled = chat_reply.build_ack(before, after, labels={"variety": "Apple Variety"})
    assert "Apple Variety Fuji" in labelled
    assert chat_reply.build_ack(after, after) == ""  # nothing changed: no claim at all
    changed = {**after, "quantity": {"value": 600, "unit": "kg"}}
    assert chat_reply.build_ack(after, changed) == "Updated: Quantity 600 kg."
    # the same fields written another way are not news: in_days -> date, case, unit spelling, location detail
    a = {"role": "buyer", "product_details": {"name": "bananas"}, "deadline": {"in_days": 68}, "location": {"city": "Indore"},
         "price_target": {"amount": 20.0, "currency": "USD", "per_unit": "kg"}, "quantity": {"value": 500.0, "unit": "kg"}}
    b = {"role": "buyer", "product_details": {"name": "Bananas"}, "deadline": {"date": "2026-12-20"},
         "location": {"city": "Indore", "state": "MP"}, "price_target": {"amount": 20, "currency": "USD", "per_unit": "per kg"},
         "quantity": {"value": 500, "unit": "kg"}}
    assert chat_reply.build_ack(a, b) == ""


async def test_the_reply_lists_what_was_really_added_and_never_says_the_draft_was_saved(client, db, make_actor):
    user = await make_actor("both")
    claim = (
        "RFQ draft updated with: Product: Fuji apples, Packaging: 20 kg carton boxes, Certifications: Organic + FSSAI\n\n"
        "Let me know if you'd like to adjust the Brix level."
    )
    with _Helpers(specs=APPLE8), Model().on(U3, claim) as model:
        d1 = await _msg(user, U1)
        d2 = await _msg(user, U3, cid=d1["conversation_id"])
    reply = d2["reply"]
    assert reply.startswith(
        "Added: Apple Variety Fuji, Packaging Type Carton, Certifications Organic, FSSAI (must match), Pack size 20 kg."
    )
    assert "RFQ draft updated" not in reply and "Let me know" not in reply and "Brix level" not in reply
    assert d1["reply"].startswith("Added: Product Apples, Quantity 500 kg, Target price USD 20 per kg, Location Indore")


async def test_when_nothing_changed_there_is_no_updated_claim_at_all(client, db, make_actor):
    user = await make_actor("both")
    with _Helpers(specs=APPLE8), Model().on("hmm, let me think about it", "RFQ draft saved: everything is updated.") as model:
        d1 = await _msg(user, U1)
        d2 = await _msg(user, "hmm, let me think about it", cid=d1["conversation_id"])
    low = d2["reply"].lower()
    assert "saved" not in low and "updated" not in low and "added:" not in low and "created" not in low
    assert d2["created_rfq"] is None and await _rfqs(db) == []


# =====================================================================================================================
# 2. one spec list per reply
# =====================================================================================================================
async def test_the_models_own_spec_list_is_dropped_when_the_flow_appends_the_spec_question(client, db, make_actor):
    user = await make_actor("both")
    echo = (
        "RFQ draft saved: 500 kg apples, $20/kg, Indore.\n\nTo match you with the right sellers, tell me:\n"
        "• Apple Variety  \n• Size Diameter (mm)  \n• Firmness (kPa)  \n\n"
        "(No size or firmness specs found in current listings — provide your requirements directly.)"
    )
    with _Helpers(specs=APPLE8), Model().on(U1, echo) as model:
        d = await _msg(user, U1)
    reply = d["reply"]
    assert reply.count("• Apple Variety") == 1 and reply.count("• Size Diameter (mm)") == 1
    assert "No size or firmness" not in reply and reply.count("tell me") == 1
    assert "• Apple Variety — Fuji, Honeycrisp, Gala, Granny Smith…" in reply
    prompt = model.calls[0]["system"]
    assert "Do NOT ask any question" in prompt and "do NOT list or suggest specs" in prompt


def test_the_no_specs_remark_goes_even_when_no_spec_question_follows():
    out = chat_reply.strip_spec_echo("Noted. (No size or firmness specs found in current listings.)", APPLE8, drop_lists=False)
    assert out == "Noted."
    keep = chat_reply.strip_spec_echo("Fuji is a sweet variety.", APPLE8, drop_lists=True)
    assert keep == "Fuji is a sweet variety."


def test_the_models_text_on_a_drafting_turn_is_short_and_invites_nothing():
    out = chat_reply.limit_model_text("Great.\n- a\n- b\nFuji it is. Let me know if you want more. One. Two.")
    assert out == "Great. Fuji it is."
    # "Reply 'yes' ..." is the flow's own wording (the HSN offer): the model must not add a second yes-question
    assert chat_reply.limit_model_text("Noted. Reply 'yes' to lock specs or adjust any detail.") == "Noted."


# =====================================================================================================================
# 3. suggested specs are allowed facts
# =====================================================================================================================
def test_the_verifier_accepts_the_suggested_spec_options():
    assist = cda.DraftAssist({"draft_suggestions": {"specs": APPLE8, "specs_for": "apples"}})
    draft = {"role": "buyer", "product_details": {"name": "apples"}}
    reply = "Size Diameter (mm) — 65, 70, 75, 80, 85. Firmness (kPa) — 80, 100. Brix Sugar Content (°Bx) 12, 14."
    assert not answer_verifier.verify(reply, [], "", draft, "").ok
    check = answer_verifier.verify(reply, [], "", draft, assist.extra_allowed_text(draft))
    assert check.ok, check.problems()


async def test_a_reply_quoting_the_suggestions_is_not_replaced_by_the_facts_only_answer(client, db, make_actor):
    user = await make_actor("both")
    draft = {"role": "buyer", "category": "Agriculture", "product_details": {"name": "apples"},
             "quantity": {"value": 500.0, "unit": "kg"}}
    quoting = "Sizes of 65, 70 and 75 mm are common for apples."
    with _Helpers(specs=APPLE8), Model().on("what sizes are common?", quoting) as model:
        d = await _msg(user, "what sizes are common?", draft)
    assert d["reply"].startswith(quoting)


# =====================================================================================================================
# 4. the HSN offer
# =====================================================================================================================
def test_the_hsn_line_with_and_without_qualifiers():
    assert chat_reply.hsn_line(HSN_0803) == (
        "Suggested HSN 0803 – Bananas, Including Plantains, Fresh Or Dried. Reply 'fresh' or 'dried' (or 'yes') to use it."
    )
    assert chat_reply.hsn_line(HSN_RICE) == "Suggested HSN 100630 – Rice. Reply 'yes' to use it."
    assert chat_reply.hsn_line(HSN_0803, "hinglish") == (
        "Suggested HSN 0803 – Bananas, Including Plantains, Fresh Or Dried. Use karne ke liye 'fresh' ya 'dried' (ya 'haan') likhiye."
    )
    assert chat_reply.hsn_line(HSN_RICE, "hinglish").endswith("Use karne ke liye 'haan' likhiye.")


async def test_the_hsn_is_offered_after_the_spec_question_and_fresh_accepts_it(client, db, make_actor):
    user = await make_actor("both")
    with _Helpers(specs=BANANA_SPECS, hsn=HSN_0803), Model():
        d1 = await _msg(user, B1)
        cid = d1["conversation_id"]
        reply = d1["reply"]
        assert "tell me a few specs" in reply and "• Variety — Cavendish, Red Banana, Lady Finger" in reply
        offer = "Suggested HSN 0803 – Bananas, Including Plantains, Fresh Or Dried. Reply 'fresh' or 'dried' (or 'yes') to use it."
        assert reply.rstrip().endswith(offer) and reply.index("• Variety") < reply.index("Suggested HSN")
        assert reply.count("Suggested HSN") == 1
        d2 = await _msg(user, "fresh", cid=cid)
    details = d2["rfq_draft"]["product_details"]
    assert details["hsn_code"] == "0803" and details["hsn_source"] == "manual" and details["condition"] == "Fresh"
    assert d2["reply"].startswith("Added: HSN 0803, Condition Fresh.")
    # the HSN answer is not an answer to the spec question: it is asked again (with label chips), not the recap
    assert "tell me a few specs" in d2["reply"] and RECAP_Q not in d2["reply"] and d2["quick_replies"] == ["Variety", "Ripeness Level", "Skip"]


async def test_yes_accepts_the_offered_hsn_and_the_offer_is_in_hinglish_for_hinglish_users(client, db, make_actor):
    user = await make_actor("both")
    with _Helpers(specs=[], hsn=HSN_RICE), Model():
        d1 = await _msg(user, "I want to buy basmati rice 500 kg in Pune, 50 rupees per kg, within 7 days")
        assert d1["reply"].rstrip().endswith(
            "Shall I use it?"
        ) or "Suggested HSN 100630" in d1["reply"] or "HSN 100630" in d1["reply"]
        d2 = await _msg(user, "haan", cid=d1["conversation_id"], prev=d1["reply"])
    assert d2["rfq_draft"]["product_details"]["hsn_code"] == "100630"
    user2 = await make_actor("both")
    bananas = {**COMPLETE, "product_details": {"name": "bananas"}}
    with _Helpers(specs=BANANA_SPECS, hsn=HSN_0803), Model():
        h = await _msg(user2, "mujhe 600 kg bananas chahiye", bananas)
    assert "Use karne ke liye 'fresh' ya 'dried' (ya 'haan') likhiye." in h["reply"] and "specs bataiye" in h["reply"]


async def test_an_ignored_hsn_offer_is_made_twice_at_most(client, db, make_actor):
    user = await make_actor("both")
    with _Helpers(specs=APPLE8, hsn={**HSN_RICE, "code": "080810", "description": "Apples", "formatted_title": "Apples"}), Model():
        d1 = await _msg(user, U1)
        cid = d1["conversation_id"]
        assert d1["reply"].count("Suggested HSN 080810") == 1
        d2 = await _msg(user, "brix lael", cid=cid)  # ignores the HSN, names a spec without a value: asked again
        assert d2["reply"].count("Suggested HSN 080810") == 1 and "Brix Sugar Content" in d2["reply"]
        d3 = await _msg(user, "skip", cid=cid)  # ignores it again: the recap follows, with no third offer
        assert "Suggested HSN" not in d3["reply"] and d3["reply"].rstrip().endswith(RECAP_Q)
        st = await _state(db)
        assert st["draft_suggestions"]["hsn_offers"] == 2
        assert "hsn_code" not in d3["rfq_draft"]["product_details"]


# =====================================================================================================================
# 5. already published
# =====================================================================================================================
async def test_publish_after_the_rfq_was_created_says_it_exists(client, db, make_actor):
    user = await make_actor("both")
    with _Helpers(), Model():
        d = await _msg(user, "create the rfq", COMPLETE)
        cid = d["conversation_id"]
        assert d["created_rfq"] is None and d["reply"].rstrip().endswith(RECAP_Q)
        d = await _msg(user, "yes", cid=cid)
        assert d["created_rfq"] is not None
        rfq_id = d["created_rfq"]["id"]
    with _Helpers(), NO_MODEL:
        again = await _msg(user, "yes publish it", cid=cid)
        assert again["created_rfq"] is None
        assert again["reply"] == f"Your RFQ 'Basmati Rice' is already published (id {rfq_id}). Want to start a new one?"
        assert "we still need Role" not in again["reply"]
        hi = await _msg(user, "mujhe ye rfq ko publish kar do", cid=cid)
        assert hi["reply"] == f"Aapka RFQ 'Basmati Rice' pehle hi publish ho chuka hai (id {rfq_id}). Kya aap naya RFQ shuru karna chahte hain?"
    assert len(await _rfqs(db)) == 1


async def test_without_a_created_rfq_publish_still_asks_for_the_missing_details(client, db, make_actor):
    user = await make_actor("both")
    with _Helpers(), NO_MODEL:
        d = await _msg(user, "yes publish it")
    assert "we still need" in d["reply"] and "already published" not in d["reply"]


# =====================================================================================================================
# 6. the helpers run while the model works
# =====================================================================================================================
async def test_the_suggestions_are_fetched_while_the_model_works_not_before_it(client, db, make_actor):
    user = await make_actor("both")
    helpers = _Helpers(specs=APPLE8, spec_delay=0.3)

    async def slow_chat(messages, tools):
        helpers.events.append("model-start")
        await asyncio.sleep(0.3)
        helpers.events.append("model-end")
        return {"role": "assistant", "content": "Noted."}

    started = asyncio.get_event_loop().time()
    with helpers, patch("services.answer_engine._chat", slow_chat):
        d = await _msg(user, U1)
    elapsed = asyncio.get_event_loop().time() - started
    # both are under way at once: the spec fetch starts while the model call is still running
    assert helpers.events.index("model-start") < helpers.events.index("model-end")
    assert helpers.events.index("specs-start") < helpers.events.index("model-end")
    assert "tell me a few specs" in d["reply"] and "• Apple Variety" in d["reply"]  # the reply still waits for them
    assert elapsed < 0.55 + 0.45  # 0.3 s model and 0.3 s fetch overlap: well under their 0.6 s sum plus overhead


async def test_a_drafting_turn_makes_one_model_call_and_a_reply_with_the_tool_call_only_is_still_worded_by_the_flow(client, db, make_actor):
    user = await make_actor("both")
    tool = _tool({"role": "buyer", "product_name": "apples", "quantity_value": 500, "quantity_unit": "kg"})
    with _Helpers(specs=APPLE8), Model().on(U1, tool, "RFQ draft saved: this second call is never made.") as model:
        d = await _msg(user, U1)
    assert len(model.calls) == 1
    assert d["reply"].startswith("Added: Product Apples, Quantity 500 kg") and "second call" not in d["reply"]
    assert "tell me a few specs" in d["reply"]
