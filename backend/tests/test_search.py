"""Direct search: conversational matching that creates no RFQ.

The contract the flow has to honour is that it gathers every requirement first
and only then runs the search, rather than searching on a half-understood query
and calling the rest a refinement.
"""

import uuid

import pytest


async def say(actor, text: str, conversation_id: str | None = None):
    response = await actor.post(
        "/search", json={"message": text, "conversation_id": conversation_id}
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
async def corpus(make_actor, make_rfq):
    """A handful of sellers to search against."""
    seller = await make_actor("seller", company_name="Nandan Wires", city="Indore")
    await make_rfq(
        seller, role="seller", title="Supplying 10000 white Type-C cables",
        attributes={"colour": "white", "length": "1m"},
        quantity=(10000, "pcs"), price=(150, "INR", "pcs"),
    )
    await make_rfq(
        seller, role="seller", title="Supplying 8000 black Type-C cables",
        attributes={"colour": "black", "length": "1m"},
        quantity=(8000, "pcs"), price=(160, "INR", "pcs"),
    )
    return seller


async def test_a_complete_query_searches_straight_away(make_actor, corpus):
    buyer = await make_actor("buyer")
    turn = await say(
        buyer,
        "i want 5000 pcs of white usb type c cable in indore within 7 days "
        "at 200 inr per piece",
    )
    assert turn["requirements"]["product"]
    assert turn["missing"] == []
    assert turn["results"]


async def test_an_incomplete_query_asks_before_it_searches(make_actor, corpus):
    """The correction that started this: gather everything, then search."""
    buyer = await make_actor("buyer")
    turn = await say(buyer, "i want white usb type c cable")

    assert turn["results"] == []
    assert turn["pending_question"] is not None
    assert turn["missing"]
    assert turn["reply"]


async def test_the_conversation_gathers_fields_one_at_a_time_then_searches(
    make_actor, corpus
):
    buyer = await make_actor("buyer")
    turn = await say(buyer, "i want white usb type c cable")
    conversation = turn["conversation_id"]

    answers = {
        "quantity": "5000 pcs",
        "price": "200 inr per piece",
        "location": "indore",
        "deadline": "7 days",
    }

    guard = 0
    while turn["pending_question"] and guard < 10:
        guard += 1
        field = turn["pending_question"]
        turn = await say(buyer, answers.get(field, "any"), conversation)

    assert guard < 10, "the assistant never stopped asking"
    assert turn["results"], turn["reply"]
    assert turn["requirements"]["quantity"]["value"] == 5000


async def test_an_answer_is_read_as_an_answer_not_as_a_fresh_query(
    make_actor, corpus
):
    """"i need it before 30 sep" once set product="sep" and quantity=30."""
    buyer = await make_actor("buyer")
    turn = await say(buyer, "i want white usb type c cable")
    conversation = turn["conversation_id"]
    product_before = turn["requirements"]["product"]

    guard = 0
    while turn["pending_question"] != "deadline" and guard < 10:
        guard += 1
        turn = await say(buyer, "skip", conversation)
    assert turn["pending_question"] == "deadline"

    turn = await say(buyer, "i need it before 30 sep", conversation)
    assert turn["requirements"]["product"] == product_before


async def test_a_skipped_field_is_remembered_as_skipped(make_actor, corpus):
    buyer = await make_actor("buyer")
    turn = await say(buyer, "i want white usb type c cable")
    conversation = turn["conversation_id"]
    field = turn["pending_question"]

    turn = await say(buyer, "skip", conversation)
    assert field in turn["requirements"]["skipped"]
    assert turn["pending_question"] != field


async def test_a_search_can_be_refined_without_starting_over(make_actor, corpus):
    buyer = await make_actor("buyer")
    turn = await say(
        buyer,
        "i want 5000 pcs of white usb type c cable in indore within 7 days "
        "at 200 inr per piece",
    )
    conversation = turn["conversation_id"]
    assert turn["requirements"]["attributes"].get("color") == "white"

    refined = await say(buyer, "show me black instead", conversation)
    assert refined["conversation_id"] == conversation
    assert refined["requirements"]["attributes"].get("color") == "black"
    # Everything else survives the refinement.
    assert refined["requirements"]["quantity"]["value"] == 5000
    assert refined["requirements"]["city"]


async def test_refining_price_and_currency_with_typo(make_actor, corpus):
    """The exact scenario reported: refining price from USD to 210 ruppes per unit."""
    buyer = await make_actor("buyer")
    turn = await say(
        buyer,
        "i want usb type c cable of white color in indore within 7 days at price 2 dollar per unit",
    )
    conversation = turn["conversation_id"]
    # Supply quantity to complete the questionnaire
    turn = await say(buyer, "50000", conversation)
    assert turn["requirements"]["quantity"]["value"] == 50000
    assert turn["requirements"]["price"]["amount"] == 2
    assert turn["requirements"]["price"]["currency"] == "USD"
    product = turn["requirements"]["product"]

    # Now refine price in INR with typo 'ruppes'
    turn = await say(buyer, "i can pay 210 ruppes per unit", conversation)
    assert turn["requirements"]["product"] == product
    assert turn["requirements"]["quantity"]["value"] == 50000
    assert turn["requirements"]["price"]["amount"] == 210
    assert turn["requirements"]["price"]["currency"] == "INR"
    assert turn["requirements"]["price"]["per_unit"] == "pcs"
    assert "pay ruppes" not in turn["reply"]
    assert "target is in USD" not in turn["reply"]


async def test_a_conversation_belongs_to_the_account_that_started_it(
    make_actor, corpus
):
    buyer = await make_actor("buyer")
    intruder = await make_actor("buyer")
    turn = await say(buyer, "i want white usb type c cable")

    response = await intruder.post(
        "/search", json={"message": "and now?", "conversation_id": turn["conversation_id"]}
    )
    assert response.status_code in (403, 404)


async def test_an_unknown_conversation_id_is_rejected(make_actor):
    buyer = await make_actor("buyer")
    response = await buyer.post(
        "/search", json={"message": "hello", "conversation_id": str(uuid.uuid4())}
    )
    assert response.status_code in (400, 403, 404)


async def test_direct_search_creates_no_rfq(make_actor, corpus):
    """The whole point: a query is not a listing."""
    buyer = await make_actor("buyer")
    await say(
        buyer,
        "i want 5000 pcs of white usb type c cable in indore within 7 days "
        "at 200 inr per piece",
    )
    assert (await buyer.get("/rfqs")).json()["total"] == 0


async def test_results_carry_the_same_shape_as_rfq_matches(make_actor, corpus):
    buyer = await make_actor("buyer")
    turn = await say(
        buyer,
        "i want 5000 pcs of white usb type c cable in indore within 7 days "
        "at 200 inr per piece",
    )
    result = turn["results"][0]
    assert result["counterparty"]["email"] is None
    assert "distance_km" in result
    assert 0 <= result["score"]["total"] <= 1
    assert result["rank"] == 1


@pytest.mark.parametrize("message", ["", " ", "x" * 2001])
async def test_empty_and_oversized_messages_are_rejected(make_actor, message):
    buyer = await make_actor("buyer")
    response = await buyer.post("/search", json={"message": message})
    assert response.status_code == 422


async def test_search_requires_authentication(client):
    response = await client.post("/search", json={"message": "hello"})
    assert response.status_code == 401


async def test_chitchat_does_not_become_a_product(make_actor, corpus):
    buyer = await make_actor("buyer")
    turn = await say(buyer, "hi there")
    assert turn["results"] == []
    assert turn["requirements"]["product"] in (None, "")
