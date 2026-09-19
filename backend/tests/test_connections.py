"""Connection requests and the messages an accepted one unlocks."""

import asyncio
import uuid

import pytest


@pytest.fixture
async def pair(make_actor, make_rfq):
    """A buyer, a seller with a published listing, and that listing."""
    buyer = await make_actor("buyer", name="Anil Buyer", company_name="Anil Traders")
    seller = await make_actor(
        "seller", name="Rekha Seller", company_name="Nandan Wires",
        phone="+91 98250 12345", city="Indore",
    )
    listing = await make_rfq(
        seller, role="seller", title="Supplying 8000 red Type-C cables"
    )
    return buyer, seller, listing


async def test_a_request_starts_pending_and_names_the_listing(pair):
    buyer, _seller, listing = pair
    response = await buyer.post("/connections", json={"rfq_id": listing["id"]})
    assert response.status_code == 201

    body = response.json()
    assert body["status"] == "pending"
    assert body["direction"] == "sent"
    assert body["rfq_title"] == "Supplying 8000 red Type-C cables"
    assert body["rfq_role"] == "seller"
    assert body["counterparty"]["company_name"] == "Nandan Wires"
    # Not yet agreed to, so not yet visible.
    assert body["counterparty"]["email"] is None


async def test_the_receiver_sees_the_request_from_the_other_side(pair):
    buyer, seller, listing = pair
    await buyer.post("/connections", json={"rfq_id": listing["id"]})

    inbox = (await seller.get("/connections")).json()
    assert len(inbox) == 1
    assert inbox[0]["direction"] == "received"
    assert inbox[0]["counterparty"]["company_name"] == "Anil Traders"


async def test_asking_twice_returns_the_same_request(pair):
    buyer, _seller, listing = pair
    first = (await buyer.post("/connections", json={"rfq_id": listing["id"]})).json()
    second = (await buyer.post("/connections", json={"rfq_id": listing["id"]})).json()
    assert first["id"] == second["id"]

    assert len((await buyer.get("/connections")).json()) == 1


async def test_two_simultaneous_clicks_create_one_request(pair):
    """The unique index is the guard; the service turns the race into an echo."""
    buyer, _seller, listing = pair
    responses = await asyncio.gather(
        buyer.post("/connections", json={"rfq_id": listing["id"]}),
        buyer.post("/connections", json={"rfq_id": listing["id"]}),
        return_exceptions=True,
    )
    ok = [r for r in responses if not isinstance(r, Exception) and r.status_code < 400]
    assert ok, responses
    assert len((await buyer.get("/connections")).json()) == 1


async def test_you_cannot_request_your_own_listing(pair):
    _buyer, seller, listing = pair
    response = await seller.post("/connections", json={"rfq_id": listing["id"]})
    assert response.status_code == 400


async def test_you_cannot_request_a_listing_that_is_no_longer_live(pair):
    """A stale match page must not be able to poke a withdrawn listing."""
    buyer, seller, listing = pair
    await seller.post(f"/rfqs/{listing['id']}/close")

    response = await buyer.post("/connections", json={"rfq_id": listing["id"]})
    assert response.status_code == 409


async def test_you_cannot_request_a_draft_listing(make_actor, make_rfq):
    buyer = await make_actor("buyer")
    seller = await make_actor("seller")
    draft = await make_rfq(
        seller, role="seller", title="Supplying cables", status="draft"
    )
    response = await buyer.post("/connections", json={"rfq_id": draft["id"]})
    assert response.status_code == 409


async def test_requesting_an_unknown_listing_is_a_404(make_actor):
    buyer = await make_actor("buyer")
    response = await buyer.post("/connections", json={"rfq_id": str(uuid.uuid4())})
    assert response.status_code == 404


async def test_accepting_reveals_contact_details_to_both_sides(pair):
    buyer, seller, listing = pair
    created = (await buyer.post("/connections", json={"rfq_id": listing["id"]})).json()

    accepted = await seller.post(f"/connections/{created['id']}/accept")
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"

    # The requester now sees the seller.
    theirs = (await buyer.get(f"/connections/{created['id']}")).json()
    assert theirs["counterparty"]["email"] == seller.email
    assert theirs["counterparty"]["phone"] == "+91 98250 12345"
    assert theirs["counterparty"]["contact_name"] == "Rekha Seller"

    # And the seller sees the buyer: consent runs both ways.
    ours = (await seller.get(f"/connections/{created['id']}")).json()
    assert ours["counterparty"]["email"] == buyer.email
    assert ours["counterparty"]["contact_name"] == "Anil Buyer"


async def test_only_the_receiver_can_answer_a_request(pair):
    buyer, _seller, listing = pair
    created = (await buyer.post("/connections", json={"rfq_id": listing["id"]})).json()

    assert (await buyer.post(f"/connections/{created['id']}/accept")).status_code == 403
    assert (await buyer.post(f"/connections/{created['id']}/reject")).status_code == 403


async def test_a_stranger_can_neither_see_nor_answer_a_request(pair, make_actor):
    buyer, _seller, listing = pair
    stranger = await make_actor("buyer")
    created = (await buyer.post("/connections", json={"rfq_id": listing["id"]})).json()

    assert (await stranger.get(f"/connections/{created['id']}")).status_code == 403
    assert (
        await stranger.post(f"/connections/{created['id']}/accept")
    ).status_code == 403


async def test_a_decision_cannot_be_taken_twice(pair):
    """Re-accepting a refusal used to be allowed, which undid the refusal."""
    buyer, seller, listing = pair
    created = (await buyer.post("/connections", json={"rfq_id": listing["id"]})).json()

    assert (await seller.post(f"/connections/{created['id']}/reject")).status_code == 200
    assert (await seller.post(f"/connections/{created['id']}/accept")).status_code == 409
    assert (await seller.post(f"/connections/{created['id']}/reject")).status_code == 409

    still_rejected = (await buyer.get(f"/connections/{created['id']}")).json()
    assert still_rejected["status"] == "rejected"
    assert still_rejected["counterparty"]["email"] is None


async def test_a_declined_request_cannot_simply_be_sent_again(pair):
    buyer, seller, listing = pair
    created = (await buyer.post("/connections", json={"rfq_id": listing["id"]})).json()
    await seller.post(f"/connections/{created['id']}/reject")

    retry = await buyer.post("/connections", json={"rfq_id": listing["id"]})
    assert retry.status_code == 409
    assert "declined" in retry.json()["detail"].lower()


async def test_messages_need_an_accepted_connection(pair):
    buyer, seller, listing = pair
    created = (await buyer.post("/connections", json={"rfq_id": listing["id"]})).json()

    blocked = await buyer.post(
        f"/connections/{created['id']}/messages", json={"content": "Hello?"}
    )
    assert blocked.status_code == 409

    await seller.post(f"/connections/{created['id']}/accept")
    allowed = await buyer.post(
        f"/connections/{created['id']}/messages", json={"content": "Hello!"}
    )
    assert allowed.status_code == 201


async def test_a_conversation_reads_back_in_order_for_both_parties(pair):
    buyer, seller, listing = pair
    created = (await buyer.post("/connections", json={"rfq_id": listing["id"]})).json()
    await seller.post(f"/connections/{created['id']}/accept")

    for text in ["Do you have 6000 in red?", "Yes, 8000 in stock.", "Send a quote."]:
        sender = seller if text.startswith("Yes") else buyer
        assert (
            await sender.post(
                f"/connections/{created['id']}/messages", json={"content": text}
            )
        ).status_code == 201

    for viewer in (buyer, seller):
        messages = (await viewer.get(f"/connections/{created['id']}/messages")).json()
        assert [m["content"] for m in messages] == [
            "Do you have 6000 in red?",
            "Yes, 8000 in stock.",
            "Send a quote.",
        ]


async def test_a_stranger_cannot_read_a_conversation(pair, make_actor):
    buyer, seller, listing = pair
    stranger = await make_actor("buyer")
    created = (await buyer.post("/connections", json={"rfq_id": listing["id"]})).json()
    await seller.post(f"/connections/{created['id']}/accept")
    await buyer.post(f"/connections/{created['id']}/messages", json={"content": "hi"})

    assert (
        await stranger.get(f"/connections/{created['id']}/messages")
    ).status_code == 403
    assert (
        await stranger.post(
            f"/connections/{created['id']}/messages", json={"content": "sneaky"}
        )
    ).status_code == 403


@pytest.mark.parametrize("content", ["", " " * 10 and "", "x" * 4001])
async def test_empty_and_oversized_messages_are_rejected(pair, content):
    buyer, seller, listing = pair
    created = (await buyer.post("/connections", json={"rfq_id": listing["id"]})).json()
    await seller.post(f"/connections/{created['id']}/accept")

    response = await buyer.post(
        f"/connections/{created['id']}/messages", json={"content": content}
    )
    assert response.status_code == 422


async def test_the_listing_owner_sees_who_asked(pair, make_actor):
    buyer, seller, listing = pair
    other = await make_actor("buyer", company_name="Second Buyer Ltd")
    await buyer.post("/connections", json={"rfq_id": listing["id"]})
    await other.post("/connections", json={"rfq_id": listing["id"]})

    requests = (await seller.get(f"/rfqs/{listing['id']}/connections")).json()
    assert len(requests) == 2
    assert {r["counterparty"]["company_name"] for r in requests} == {
        "Anil Traders",
        "Second Buyer Ltd",
    }


async def test_only_the_owner_can_list_a_listings_requests(pair, make_actor):
    buyer, _seller, listing = pair
    stranger = await make_actor("buyer")
    await buyer.post("/connections", json={"rfq_id": listing["id"]})
    assert (await stranger.get(f"/rfqs/{listing['id']}/connections")).status_code == 404


async def test_pending_requests_show_as_a_count_on_the_listing(pair):
    buyer, seller, listing = pair
    assert (await seller.get(f"/rfqs/{listing['id']}")).json()["pending_connections"] == 0

    created = (await buyer.post("/connections", json={"rfq_id": listing["id"]})).json()
    assert (await seller.get(f"/rfqs/{listing['id']}")).json()["pending_connections"] == 1

    await seller.post(f"/connections/{created['id']}/accept")
    assert (await seller.get(f"/rfqs/{listing['id']}")).json()["pending_connections"] == 0


async def test_the_connection_list_can_be_filtered_by_status(pair, make_actor, make_rfq):
    buyer, seller, listing = pair
    second_seller = await make_actor("seller")
    second = await make_rfq(
        second_seller, role="seller", title="Supplying Type-C cables too"
    )

    first = (await buyer.post("/connections", json={"rfq_id": listing["id"]})).json()
    await buyer.post("/connections", json={"rfq_id": second["id"]})
    await seller.post(f"/connections/{first['id']}/accept")

    accepted = (await buyer.get("/connections", params={"status": "accepted"})).json()
    pending = (await buyer.get("/connections", params={"status": "pending"})).json()
    assert [c["id"] for c in accepted] == [first["id"]]
    assert len(pending) == 1


async def test_every_connection_route_requires_authentication(client, pair):
    buyer, _seller, listing = pair
    created = (await buyer.post("/connections", json={"rfq_id": listing["id"]})).json()

    assert (await client.get("/connections")).status_code == 401
    assert (
        await client.post("/connections", json={"rfq_id": listing["id"]})
    ).status_code == 401
    assert (await client.get(f"/connections/{created['id']}")).status_code == 401
    assert (await client.post(f"/connections/{created['id']}/accept")).status_code == 401
    assert (
        await client.get(f"/connections/{created['id']}/messages")
    ).status_code == 401
