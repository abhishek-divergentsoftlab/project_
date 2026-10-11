"""Phase 4: the form's lifecycle actions in chat: publish a saved draft, edit a saved RFQ, save/cancel an edit."""

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from services import ai_chat_service, chat_draft_assist as cda, chat_permissions as perms, rfq_service

pytestmark = [pytest.mark.asyncio, pytest.mark.filterwarnings("ignore::pytest.PytestWarning")]

BASE = {
    "role": "buyer",
    "category": "Agriculture",
    "product_details": {"name": "Basmati Rice"},
    "quantity": {"value": 500.0, "unit": "kg"},
}
SAFE = {"is_safe": True, "reason": None, "category": None}
NO_MODEL = patch("services.answer_engine._chat", side_effect=AssertionError("model must not be called"))


class _Helpers:
    """The form's remote helpers, stubbed (no HSN/spec suggestion, content judged safe)."""

    def __enter__(self):
        self.ps = [
            patch.object(cda, "_fetch_hsn_suggestion", AsyncMock(return_value=None)),
            patch.object(cda, "_fetch_spec_suggestions", AsyncMock(return_value=[])),
            patch.object(cda, "_fetch_moderation", AsyncMock(return_value=SAFE)),
        ]
        for p in self.ps:
            p.__enter__()
        return self

    def __exit__(self, *a):
        for p in self.ps:
            p.__exit__(*a)


def _script(content="ok"):
    async def chat(messages, tools):
        return {"role": "assistant", "content": content}

    return patch("services.answer_engine._chat", chat)


def _tool_chat(args):
    calls = {"n": 0}

    async def chat(messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "update_rfq_draft", "arguments": args}}]}
        return {"role": "assistant", "content": "Noted."}

    return patch("services.answer_engine._chat", chat)


async def _msg(user, content, draft=None, cid=None, previous=None):
    history = [{"role": "assistant", "content": previous}] if previous else []
    body = {"messages": [*history, {"role": "user", "content": content}]}
    if draft is not None:
        body["current_rfq"] = draft
    if cid:
        body["conversation_id"] = cid
    res = await user_post(user, body)
    return res


async def user_post(user, body):
    res = await user.post("/ai-chat/message", json=body)
    assert res.status_code == 200, res.text
    return res.json()


async def _row(db, rfq_id, cols="status, quantity_value, title"):
    await db.rollback()  # fresh snapshot: the API committed in another session
    return (await db.execute(text(f"select {cols} from rfqs where id = :i"), {"i": rfq_id})).one()


async def _count(db, user_id=None):
    await db.rollback()
    return (await db.execute(text("select count(*) from rfqs"))).scalar_one()


async def _draft_rfq(make_rfq, user, title="Rice 500 kg", product="basmati rice", **kw):
    return await make_rfq(
        user, role="buyer", title=title, product=product, category="Agriculture", quantity=(500, "kg"),
        price=(50, "INR", "kg"), status="draft", **kw,
    )


# ---- permissions (pure) ---------------------------------------------------
@pytest.mark.parametrize("phrase", [
    "publish my rice rfq", "publish it", "post the draft rfq", "publish kar do", "meri rice rfq publish kar do",
    "go live with my cotton rfq", "can you publish my rice rfq?",
])
def test_publish_phrases(phrase):
    assert perms.check_publish_rfq_intent(phrase), phrase


@pytest.mark.parametrize("phrase", [
    "don't publish my rice rfq", "should I publish my rice rfq?", "how do I publish an rfq", "publish nahi karna hai",
    "not ready to publish it", "post a message to the seller",
])
def test_publish_negations_and_questions(phrase):
    assert not perms.check_publish_rfq_intent(phrase), phrase


def test_publish_target_and_bare_yes():
    assert perms.publish_rfq_target("publish my rice rfq") == "rice"
    assert perms.publish_rfq_target("publish the draft rfq") is None
    uid = str(uuid.uuid4())
    assert perms.publish_rfq_target(f"publish rfq {uid}") == uid
    assert perms.check_publish_rfq_intent("yes", "Want me to publish it?")
    assert not perms.check_publish_rfq_intent("yes", "Which city?")


@pytest.mark.parametrize("phrase,target", [
    ("edit my rice rfq", "rice"),
    ("change my cotton listing", "cotton"),
    ("change the quantity of my rice RFQ to 600 kg", "rice"),
    ("meri rice RFQ badlo", "rice"),
    ("update my rice rfq", "rice"),
])
def test_edit_phrases(phrase, target):
    assert perms.check_edit_rfq_intent(phrase), phrase
    assert perms.edit_rfq_target(phrase) == target


@pytest.mark.parametrize("phrase", [
    "don't edit my rice rfq", "how do I edit my rice rfq?", "should I change my rice rfq?",
    "update the rfq to include delivery to Pune", "change the quantity to 600 kg", "edit the message",
])
def test_edit_negations_questions_and_draft_updates(phrase):
    assert not perms.check_edit_rfq_intent(phrase), phrase


def test_save_and_cancel_edit_phrases():
    for t in ("save changes", "save", "done, save", "update it", "changes save karo", "create the rfq", "publish"):
        assert perms.check_save_edit_intent(t), t
    for t in ("don't save yet", "should I save the changes?", "make it 700 kg"):
        assert not perms.check_save_edit_intent(t), t
    for t in ("cancel edit", "discard changes", "edit cancel karo"):
        assert perms.check_cancel_edit_intent(t), t
    assert not perms.check_cancel_edit_intent("close my rice rfq")


def test_allowed_tools_for_lifecycle_commands():
    assert perms.allowed_action_tools("publish my rice rfq", {}, {}, "other") == {"publish_rfq"}
    assert perms.allowed_action_tools("edit my rice rfq", {}, {}, "other") == {"edit_rfq"}
    assert "create_rfq" in perms.allowed_action_tools("publish it", BASE, {}, "other")
    in_edit = perms.allowed_action_tools("create the rfq", BASE, {"editing_rfq_id": "x"}, "other")
    assert "create_rfq" not in in_edit
    assert "publish_rfq" not in perms.allowed_action_tools("should I publish my rice rfq?", {}, {}, "other")


# ---- publish --------------------------------------------------------------
async def test_publish_the_draft_created_in_this_conversation(client, db, make_actor):
    user = await make_actor("buyer")
    with _Helpers(), NO_MODEL:
        first = await _msg(user, "save it as a draft", BASE)
        rid = first["created_rfq"]["id"]
        assert first["created_rfq"]["status"] == "draft"
        data = await _msg(user, "publish it", cid=first["conversation_id"])
    assert "RFQ Published" in data["reply"]
    assert data["created_rfq"]["id"] == rid and data["created_rfq"]["status"] == "active"
    assert (await _row(db, rid)).status == "active"
    conv = (await db.execute(text("select state from conversations"))).scalar_one()
    assert conv["last_created_rfq_id"] == rid
    assert await _count(db) == 1


async def test_publish_the_only_draft(client, db, make_actor, make_rfq):
    user = await make_actor("buyer")
    rfq = await _draft_rfq(make_rfq, user)
    with _Helpers(), NO_MODEL:
        data = await _msg(user, "publish kar do")
    assert data["created_rfq"]["id"] == rfq["id"] and "RFQ Published" in data["reply"]
    assert (await _row(db, rfq["id"])).status == "active"


async def test_publish_by_keyword(client, db, make_actor, make_rfq):
    user = await make_actor("buyer")
    rice = await _draft_rfq(make_rfq, user)
    cotton = await _draft_rfq(make_rfq, user, title="Cotton 100 bales", product="cotton")
    with _Helpers(), NO_MODEL:
        data = await _msg(user, "publish my cotton rfq")
    assert data["created_rfq"]["id"] == cotton["id"]
    assert (await _row(db, rice["id"])).status == "draft"


async def test_publish_ambiguous_asks_with_titles_and_publishes_nothing(client, db, make_actor, make_rfq):
    user = await make_actor("buyer")
    a = await _draft_rfq(make_rfq, user)
    b = await _draft_rfq(make_rfq, user, title="Cotton 100 bales", product="cotton")
    with _Helpers(), NO_MODEL:
        data = await _msg(user, "publish it")
    assert data["created_rfq"] is None
    assert "'Rice 500 kg'" in data["reply"] and "'Cotton 100 bales'" in data["reply"]
    assert "RFQ Published" not in data["reply"]
    assert (await _row(db, a["id"])).status == "draft" and (await _row(db, b["id"])).status == "draft"


async def test_publish_failure_past_deadline_is_surfaced(client, db, make_actor, make_rfq):
    user = await make_actor("buyer")
    rfq = await _draft_rfq(make_rfq, user)
    await db.execute(text("update rfqs set deadline_at = now() - interval '2 days' where id = :i"), {"i": rfq["id"]})
    await db.commit()
    with _Helpers(), NO_MODEL:
        data = await _msg(user, "publish my rice rfq")
    assert data["created_rfq"] is None
    assert "deadline has already passed" in data["reply"] and "RFQ Published" not in data["reply"]
    assert (await _row(db, rfq["id"])).status == "draft"


async def test_publish_not_offered_for_others_rfqs(client, db, make_actor, make_rfq):
    owner = await make_actor("buyer")
    other = await make_actor("buyer")
    theirs = await _draft_rfq(make_rfq, owner, title="Zebra hides", product="zebra hides")
    with _Helpers(), NO_MODEL:
        by_id = await _msg(other, f"publish rfq {theirs['id']}")
        by_word = await _msg(other, "publish my zebra rfq")
        no_draft = await _msg(other, "publish it")
    assert "permission" in by_id["reply"]
    assert "No draft RFQ matching 'zebra'" in by_word["reply"]
    assert "no draft RFQs" in no_draft["reply"] or "need" in no_draft["reply"].lower()
    for d in (by_id, by_word, no_draft):
        assert d["created_rfq"] is None
    assert (await _row(db, theirs["id"])).status == "draft"


async def test_publish_with_chat_draft_creates_instead_of_publishing(client, db, make_actor, make_rfq):
    user = await make_actor("buyer")
    existing = await _draft_rfq(make_rfq, user, title="Old draft", product="wheat")
    with _Helpers(), NO_MODEL:
        data = await _msg(user, "publish it", BASE)
    assert data["created_rfq"]["id"] != existing["id"] and data["created_rfq"]["status"] == "active"
    assert (await _row(db, existing["id"])).status == "draft"
    assert await _count(db) == 2


async def test_publish_without_any_draft_keeps_create_behaviour(client, db, make_actor):
    user = await make_actor("buyer")
    with _Helpers(), NO_MODEL:
        data = await _msg(user, "publish it")
    assert data["created_rfq"] is None and "Buyer or Seller" in data["reply"]


async def test_question_does_not_publish(client, db, make_actor, make_rfq):
    user = await make_actor("buyer")
    rfq = await _draft_rfq(make_rfq, user)
    with _Helpers(), _script("It is a draft until you publish it."):
        data = await _msg(user, "should I publish my rice rfq?")
        neg = await _msg(user, "don't publish my rice rfq yet")
    assert data["created_rfq"] is None and neg["created_rfq"] is None
    assert (await _row(db, rfq["id"])).status == "draft"


# ---- edit -----------------------------------------------------------------
async def test_edit_loads_by_keyword_as_chat_draft(client, db, make_actor, make_rfq):
    user = await make_actor("buyer")
    rfq = await _draft_rfq(make_rfq, user)
    with _Helpers(), NO_MODEL:
        data = await _msg(user, "edit my rice rfq")
    assert data["editing_rfq_id"] == rfq["id"]
    d = data["rfq_draft"]
    assert d["role"] == "buyer" and d["title"] == "Rice 500 kg" and d["quantity"] == {"value": 500, "unit": "kg"}
    assert d["price_target"]["amount"] == 50 and d["location"]["city"] == "Indore"
    assert d["product_details"]["name"] == "basmati rice"
    assert "Editing 'Rice 500 kg'" in data["reply"]
    st = (await db.execute(text("select state from conversations"))).scalar_one()
    assert st["editing_rfq_id"] == rfq["id"] and st["editing_snapshot"]["quantity"]["value"] == 500


async def test_edit_loads_by_id_and_hinglish(client, db, make_actor, make_rfq):
    user = await make_actor("buyer")
    rfq = await _draft_rfq(make_rfq, user)
    with _Helpers(), NO_MODEL:
        by_id = await _msg(user, f"edit rfq {rfq['id']}")
        hing = await _msg(user, "meri rice RFQ badlo")
    assert by_id["editing_rfq_id"] == rfq["id"] and hing["editing_rfq_id"] == rfq["id"]


async def test_edit_closed_rfq_is_refused(client, db, make_actor, make_rfq):
    user = await make_actor("buyer")
    rfq = await make_rfq(user, role="buyer", title="Rice 500 kg", product="basmati rice")
    assert (await user.post(f"/rfqs/{rfq['id']}/close")).status_code == 200
    with _Helpers(), NO_MODEL:
        data = await _msg(user, "edit my rice rfq")
    assert "can no longer be edited" in data["reply"]
    assert data["editing_rfq_id"] is None


async def test_edit_someone_elses_rfq_is_refused(client, db, make_actor, make_rfq):
    owner = await make_actor("buyer")
    other = await make_actor("buyer")
    rfq = await _draft_rfq(make_rfq, owner)
    with _Helpers(), NO_MODEL:
        data = await _msg(other, f"edit rfq {rfq['id']}")
        word = await _msg(other, "edit my rice rfq")
    assert "permission" in data["reply"] and data["editing_rfq_id"] is None
    assert "No RFQ matching 'rice'" in word["reply"]


async def test_edit_with_change_in_same_message_then_save_sends_only_changed_fields(client, db, make_actor, make_rfq):
    user = await make_actor("buyer")
    rfq = await _draft_rfq(make_rfq, user)
    with _Helpers(), _tool_chat({"quantity_value": 600, "quantity_unit": "kg"}):
        data = await _msg(user, "change the quantity of my rice RFQ to 600 kg")
    assert data["editing_rfq_id"] == rfq["id"]
    assert data["rfq_draft"]["quantity"] == {"value": 600, "unit": "kg"}
    assert (await _row(db, rfq["id"])).quantity_value == 500  # not saved yet

    seen = []
    real = rfq_service.update_rfq

    async def spy(db_, rfq_, payload):
        seen.append(payload)
        return await real(db_, rfq_, payload)

    with _Helpers(), NO_MODEL, patch.object(rfq_service, "update_rfq", spy):
        saved = await _msg(user, "save changes", cid=data["conversation_id"])
    assert len(seen) == 1
    assert set(seen[0].model_dump(exclude_unset=True)) == {"quantity"}
    assert "RFQ Updated" in saved["reply"] and saved["created_rfq"]["id"] == rfq["id"]
    assert saved["editing_rfq_id"] is None and not saved["rfq_draft"]
    row = await _row(db, rfq["id"])
    assert float(row.quantity_value) == 600 and row.title == "Rice 500 kg" and row.status == "draft"
    st = (await db.execute(text("select state from conversations"))).scalar_one()
    assert "editing_rfq_id" not in st and "editing_snapshot" not in st


async def test_save_without_changes_says_so_and_does_not_update(client, db, make_actor, make_rfq):
    user = await make_actor("buyer")
    await _draft_rfq(make_rfq, user)
    with _Helpers(), NO_MODEL:
        first = await _msg(user, "edit my rice rfq")
        with patch.object(rfq_service, "update_rfq", AsyncMock(side_effect=AssertionError("no update"))):
            data = await _msg(user, "save changes", cid=first["conversation_id"])
    assert "nothing to save" in data["reply"]
    assert data["editing_rfq_id"] is not None


async def test_role_change_is_rejected_while_editing(client, db, make_actor, make_rfq):
    user = await make_actor("buyer")
    rfq = await _draft_rfq(make_rfq, user)
    with _Helpers(), NO_MODEL:
        first = await _msg(user, "edit my rice rfq")
    with _Helpers(), _tool_chat({"role": "seller"}):
        data = await _msg(user, "change the role to seller, I want to sell 500 kg", cid=first["conversation_id"])
    assert data["rfq_draft"]["role"] == "buyer"
    assert "role cannot be changed" in data["reply"]
    with _Helpers(), NO_MODEL:
        saved = await _msg(user, "save changes", cid=first["conversation_id"])
    assert "role" not in saved["reply"].lower().split("changed:")[-1]
    assert (await _row(db, rfq["id"], "role")).role == "buyer"


async def test_cancel_edit_discards_without_saving(client, db, make_actor, make_rfq):
    user = await make_actor("buyer")
    rfq = await _draft_rfq(make_rfq, user)
    with _Helpers(), _tool_chat({"quantity_value": 900, "quantity_unit": "kg"}):
        first = await _msg(user, "change the quantity of my rice RFQ to 900 kg")
    with _Helpers(), NO_MODEL:
        data = await _msg(user, "cancel edit", cid=first["conversation_id"])
    assert "Edit cancelled" in data["reply"] and data["editing_rfq_id"] is None and not data["rfq_draft"]
    assert float((await _row(db, rfq["id"])).quantity_value) == 500


async def test_create_intent_in_edit_mode_does_not_create_new_rfq(client, db, make_actor, make_rfq):
    user = await make_actor("buyer")
    await _draft_rfq(make_rfq, user)
    with _Helpers(), NO_MODEL:
        first = await _msg(user, "edit my rice rfq")
        data = await _msg(user, "create the rfq", cid=first["conversation_id"])
    assert await _count(db) == 1
    assert data["created_rfq"] is None and data["editing_rfq_id"] is not None


async def test_save_and_publish_an_edited_draft(client, db, make_actor, make_rfq):
    user = await make_actor("buyer")
    rfq = await _draft_rfq(make_rfq, user)
    with _Helpers(), _tool_chat({"quantity_value": 700, "quantity_unit": "kg"}):
        first = await _msg(user, "change the quantity of my rice RFQ to 700 kg")
    with _Helpers(), NO_MODEL:
        data = await _msg(user, "publish it", cid=first["conversation_id"])
    assert "RFQ Updated" in data["reply"] and "RFQ Published" in data["reply"]
    assert data["created_rfq"]["status"] == "active" and data["editing_rfq_id"] is None
    row = await _row(db, rfq["id"])
    assert row.status == "active" and float(row.quantity_value) == 700
    assert await _count(db) == 1


async def test_save_blocked_when_moderation_flags_the_edit(client, db, make_actor, make_rfq):
    user = await make_actor("buyer")
    rfq = await _draft_rfq(make_rfq, user)
    unsafe = {"is_safe": False, "reason": "prohibited items", "category": None}
    with patch.object(cda, "_fetch_hsn_suggestion", AsyncMock(return_value=None)), \
         patch.object(cda, "_fetch_spec_suggestions", AsyncMock(return_value=[])), \
         patch.object(cda, "_fetch_moderation", AsyncMock(return_value=unsafe)), \
         _tool_chat({"description": "illegal goods"}):
        first = await _msg(user, "change the description of my rice RFQ to illegal goods")
        with NO_MODEL:
            data = await _msg(user, "save changes", cid=first["conversation_id"])
    assert "content policy" in data["reply"] and data["editing_rfq_id"] is not None
    assert (await _row(db, rfq["id"], "description")).description is None


async def test_invalid_value_is_reported_not_saved(client, db, make_actor, make_rfq):
    user = await make_actor("buyer")
    rfq = await _draft_rfq(make_rfq, user)
    with _Helpers(), NO_MODEL:
        first = await _msg(user, "edit my rice rfq")
    cid = first["conversation_id"]
    st = (await db.execute(text("select state from conversations"))).scalar_one()
    st["rfq_draft"]["quantity"] = {"value": 0, "unit": "kg"}
    await db.execute(text("update conversations set state = cast(:s as jsonb)"), {"s": json.dumps(st)})
    await db.commit()
    with _Helpers(), NO_MODEL:
        data = await _msg(user, "save changes", cid=cid)
    assert "please fix" in data["reply"] and "quantity" in data["reply"]
    assert float((await _row(db, rfq["id"])).quantity_value) == 500


# ---- close still works --------------------------------------------------------
async def test_close_still_works_through_shared_resolver(client, db, make_actor, make_rfq):
    user = await make_actor("buyer")
    rfq = await make_rfq(user, role="buyer", title="Rice 500 kg", product="basmati rice")
    with _Helpers(), NO_MODEL:
        data = await _msg(user, "close my rice rfq")
    assert "RFQ Closed" in data["reply"]
    assert (await _row(db, rfq["id"])).status == "closed"
    # keyword never reaches another user's RFQ
    other = await make_actor("buyer")
    with _Helpers(), NO_MODEL:
        miss = await _msg(other, "close my rice rfq")
    assert "No active RFQ matching 'rice'" in miss["reply"]


async def test_stream_done_payload_carries_editing_rfq_id(client, db, make_actor, make_rfq):
    user = await make_actor("buyer")
    rfq = await _draft_rfq(make_rfq, user)
    with _Helpers(), NO_MODEL:
        res = await user.post("/ai-chat/stream", json={"messages": [{"role": "user", "content": "edit my rice rfq"}]})
    assert res.status_code == 200, res.text
    chunks = [json.loads(l[6:]) for l in res.text.splitlines() if l.startswith("data: ") and l[6:].strip().startswith("{")]
    done = [c for c in chunks if c.get("done")]
    assert done and done[-1]["editing_rfq_id"] == rfq["id"]
