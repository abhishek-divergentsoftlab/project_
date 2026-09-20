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
    assert result["counterparty"]["email"] is not None
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


async def test_prohibited_query_in_search_locks_session(make_actor, corpus):
    """Searching for prohibited items (e.g. 5.56 ammo) terminates and locks the session."""
    buyer = await make_actor("buyer")
    response = await buyer.post("/search", json={"message": "i want 5.56 ammo"})
    assert response.status_code == 200
    turn = response.json()

    # 1. Moderation guardrail flags the query
    assert turn["blocked"] is True
    assert turn["block_category"] == "firearms_and_weapons"
    assert "firearms" in turn["block_reason"].lower()
    assert "5.56 ammo" in turn["block_reason"].lower()

    # 2. Does NOT ask the next question or return results
    assert turn["pending_question"] is None
    assert turn["results"] == []
    assert "⚠️" in turn["reply"]
    assert "locked and terminated" in turn["reply"].lower()

    # 3. Subsequent message in the SAME session is strictly blocked with 422
    cid = turn["conversation_id"]
    next_turn = await buyer.post(
        "/search", json={"message": "can i get 100 rounds?", "conversation_id": cid}
    )
    assert next_turn.status_code == 422
    assert "terminated and locked" in next_turn.json()["detail"].lower()

    # 4. Starting a new session (without conversation_id) allows safe queries
    clean_turn = await say(buyer, "i want 1000 pcs of white usb type c cables")
    assert clean_turn["blocked"] is False
    assert clean_turn["conversation_id"] != cid


async def test_subsequent_turn_prohibited_query_locks_session(make_actor, corpus):
    """Starting with a safe query and then pivoting to prohibited goods locks the session."""
    buyer = await make_actor("buyer")
    turn1 = await say(buyer, "i want white usb type c cables")
    cid = turn1["conversation_id"]
    assert turn1["blocked"] is False
    assert turn1["pending_question"] is not None

    # Pivot to prohibited item in next turn
    turn2_resp = await buyer.post(
        "/search", json={"message": "actually provide 5.56 ammo with that", "conversation_id": cid}
    )
    assert turn2_resp.status_code == 200
    turn2 = turn2_resp.json()
    assert turn2["blocked"] is True
    assert turn2["block_category"] == "firearms_and_weapons"
    assert turn2["pending_question"] is None

    # Third turn in same session is rejected with 422
    turn3_resp = await buyer.post(
        "/search", json={"message": "hello?", "conversation_id": cid}
    )
    assert turn3_resp.status_code == 422


async def test_out_of_order_answer_does_not_hijack_product_or_quantity(make_actor, corpus):
    """When asked for location, answering with a deadline does not corrupt product or quantity."""
    buyer = await make_actor("buyer")
    turn1 = await say(buyer, "i want 1000 pcs of hdmi cables at 200 rs per unit")
    cid = turn1["conversation_id"]
    assert "hdmi" in turn1["requirements"]["product"]
    assert turn1["requirements"]["quantity"]["value"] == 1000
    assert turn1["pending_question"] == "location"

    # User answers with deadline instead of location
    turn2 = await say(buyer, "it can be before 12'th of november", cid)
    assert "hdmi" in turn2["requirements"]["product"]
    assert turn2["requirements"]["quantity"]["value"] == 1000
    assert turn2["requirements"]["deadline_days"] is not None
    # Now location is still pending
    assert turn2["pending_question"] == "location"

    # User answers location
    turn3 = await say(buyer, "delhi", cid)
    assert "hdmi" in turn3["requirements"]["product"]
    assert turn3["requirements"]["city"] == "Delhi"


async def test_conversational_negation_and_pivot_in_search(make_actor, corpus):
    """Refining query with negation and pivoting product preserves valid parameters."""
    buyer = await make_actor("buyer")
    turn1 = await say(buyer, "i want 1000 pcs of usb type c cables of white color in indore")
    cid = turn1["conversation_id"]
    assert turn1["requirements"]["attributes"].get("type") == "C"

    # Negate type C
    turn2 = await say(buyer, "actully i dont need type c cables i need just simple usb cables", cid)
    assert "type" not in turn2["requirements"]["attributes"]
    assert turn2["requirements"]["attributes"].get("color") == "white"
    assert "usb" in turn2["requirements"]["product"]
    assert "dont" not in turn2["requirements"]["product"]

    # Pivot to HDMI
    turn3 = await say(buyer, "actully sorry i need hdmi cables now", cid)
    assert "type" not in turn3["requirements"]["attributes"]
    assert turn3["requirements"]["attributes"].get("color") == "white"
    assert "hdmi" in turn3["requirements"]["product"]
    assert "sorry" not in turn3["requirements"]["product"]
    assert turn3["requirements"]["city"] == "Indore"


async def test_currency_conversion_does_not_warn_unscored(make_actor, corpus):
    """When target is in USD and listings are in INR, converted currencies do not trigger 'not scored' note."""
    buyer = await make_actor("buyer")
    turn = await say(buyer, "i want 1000 pcs of usb cables at price 2 usd per piece in indore")
    assert "so price was not scored" not in turn["reply"].lower()


async def test_conversational_filler_and_location_pivots_in_search(make_actor, corpus):
    """Conversational dialogue with 'okey then', type a connector, and state pivots (Haryana, Punjab)."""
    buyer = await make_actor("buyer")
    # Step 1: wizard for hdmi cables
    turn1 = await say(buyer, "i need hdmi cables in indore")
    cid = turn1["conversation_id"]

    # Step 2: "okey then find usb type a cables"
    turn2 = await say(buyer, "okey then find usb type a cables", cid)
    assert "okey" not in turn2["reply"].lower()
    assert "okey" not in turn2["requirements"]["product"].lower()
    assert "then" not in turn2["requirements"]["product"].lower()
    assert turn2["requirements"]["attributes"].get("type") == "A"

    # Step 3: "i need tomatos"
    turn3 = await say(buyer, "i need tomatos", cid)
    assert turn3["requirements"]["product"] == "tomatos"
    assert turn3["requirements"]["city"] == "Indore"

    # Step 4: "in hariyana" -> switches location to Haryana, clears city
    turn4 = await say(buyer, "in hariyana", cid)
    assert turn4["requirements"]["product"] == "tomatos"
    assert turn4["requirements"]["city"] is None
    assert turn4["requirements"]["state"] == "Haryana"
    assert "sellers for hariyana in indore" not in turn4["reply"].lower()
    assert "hariyana in indore" not in turn4["reply"].lower()
    assert "indore" not in turn4["reply"].lower()
    assert "haryana" in turn4["reply"].lower()

    # Step 5: "find in punjab" -> switches location to Punjab
    turn5 = await say(buyer, "find in punjab", cid)
    assert turn5["requirements"]["product"] == "tomatos"
    assert turn5["requirements"]["city"] is None
    assert turn5["requirements"]["state"] == "Punjab"
    assert "sellers for punjab in indore" not in turn5["reply"].lower()
    assert "punjab in indore" not in turn5["reply"].lower()
    assert "indore" not in turn5["reply"].lower()
    assert "punjab" in turn5["reply"].lower()


async def test_product_pivot_to_broad_category_asks_clarification(make_actor, make_rfq, corpus):
    """When pivoting from cables to vegetables:
    1. Category updates from Electronics to Agriculture.
    2. Incompatible attributes (color: white, type: C) are purged.
    3. Broad noun ('vagitables') triggers clarification instead of blind search.
    4. Next turn with specific product ('organic tomatoes') finalizes and executes search.
    """
    farm_seller = await make_actor("seller", company_name="Malwa Agro Farms", city="Indore")
    await make_rfq(
        farm_seller,
        role="seller",
        category="Agriculture",
        product="tomatoes",
        title="Fresh Organic Tomatoes",
        attributes={"organic": True, "grade": "A"},
        quantity=(5000, "kg"),
        price=(25, "INR", "kg"),
    )

    buyer = await make_actor("buyer")
    turn1 = await say(
        buyer,
        "i want usb type c cable of white color in indore within 7 days at price 2 dollar per unit",
    )
    cid = turn1["conversation_id"]
    assert turn1["requirements"]["category"] == "Electronics"
    assert turn1["requirements"]["attributes"].get("color") == "white"

    # Step 2: Answer quantity
    turn2 = await say(buyer, "5000 units", cid)
    assert len(turn2["results"]) > 0

    # Step 3: Pivot to "i need vagitables instead of this"
    turn3 = await say(buyer, "i need vagitables instead of this", cid)

    # Category updated to Agriculture
    assert turn3["requirements"]["category"] == "Agriculture"
    # Old cable specs purged
    assert "color" not in turn3["requirements"]["attributes"]
    assert "type" not in turn3["requirements"]["attributes"]

    # Did NOT run blind search for 'white vagitables'
    assert turn3["results"] == []
    # Asked what specific product/vegetable they need
    reply_lower = turn3["reply"].lower()
    assert "what specific product" in reply_lower or "which" in reply_lower or "tomato" in reply_lower

    # Step 4: User specifies organic tomatoes
    turn4 = await say(buyer, "organic tomatoes, 1000 kg at 30 inr", cid)
    assert turn4["requirements"]["category"] == "Agriculture"
    assert "tomato" in turn4["requirements"]["product"]
    assert turn4["requirements"]["attributes"].get("organic") is True
    # Search executed and found the farm seller
    assert len(turn4["results"]) > 0
    assert turn4["results"][0]["counterparty"]["company_name"] == "Malwa Agro Farms"


async def test_refinement_does_not_corrupt_product_and_apples_never_match_tomatoes(
    make_actor, make_rfq, corpus
):
    """Verifies that:
    1. 'instead of this i need tomatos' establishes product 'tomatos'.
    2. 'yes it must be organic and quality should be grade A' refines attributes (organic: True, grade: Grade A),
       and preserves 'tomatos' rather than becoming 'yes organic grade'.
    3. Apple listings in Agriculture never match a tomato search even if price, quantity, location, deadline match.
    """
    # Create an Apple seller in Agriculture in Indore with perfect location/price/quantity
    apple_seller = await make_actor("seller", company_name="Kashmir Orchards", city="Indore")
    await make_rfq(
        apple_seller,
        role="seller",
        category="Agriculture",
        title="Supplying 297,000 kg of Fuji apples — Extra Class, Fuji",
        attributes={"organic": True, "grade": "Grade A"},
        quantity=(10000, "kg"),
        price=(25, "INR", "kg"),
    )

    buyer = await make_actor("buyer")
    turn1 = await say(buyer, "i want 1000 pcs of white usb cables in indore")
    cid = turn1["conversation_id"]

    # Step 2: Pivot to tomatoes
    turn2 = await say(buyer, "instead of this i need tomatos", cid)
    assert turn2["requirements"]["product"] == "tomatos"
    assert turn2["requirements"]["category"] == "Agriculture"

    # Step 3: Refinement with organic and grade A
    turn3 = await say(buyer, "yes it must be organic and quality should be grade A", cid)
    assert turn3["requirements"]["product"] == "tomatos"
    assert "yes" not in turn3["requirements"]["product"]
    assert "organic" not in turn3["requirements"]["product"]
    assert "grade" not in turn3["requirements"]["product"]
    assert turn3["requirements"]["attributes"].get("organic") is True
    assert turn3["requirements"]["attributes"].get("grade") == "Grade A"
    assert turn3["requirements"]["category"] == "Agriculture"

    # Apple seller MUST NOT be returned for a tomato search
    for res in turn3["results"]:
        assert "apple" not in res["title"].lower()
    assert len(turn3["results"]) == 0



