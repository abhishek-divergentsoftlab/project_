"""Matching: who is shown to whom, in what order, and with what visible."""

import uuid

import pytest

from tests.conftest import rfq_body

# Coordinates used across these tests, so distances are a known quantity.
INDORE = (22.7196, 75.8577, "Indore", "Madhya Pradesh")
PUNE = (18.5204, 73.8567, "Pune", "Maharashtra")
LUDHIANA = (30.9010, 75.8573, "Ludhiana", "Punjab")
JAIPUR = (26.9124, 75.7873, "Jaipur", "Rajasthan")


def at(place, **overrides):
    lat, lng, city, state = place
    body = dict(latitude=lat, longitude=lng, city=city, state=state, country="India")
    body.update(overrides)
    return body


async def test_a_buyer_is_shown_sellers_and_never_other_buyers(
    make_actor, make_rfq
):
    buyer = await make_actor("buyer")
    seller = await make_actor("seller")
    rival = await make_actor("buyer")

    mine = await make_rfq(buyer)
    await make_rfq(seller, role="seller", title="Supplying 8000 red Type-C cables")
    await make_rfq(rival, title="Also need red Type-C cables")

    response = await buyer.post(f"/rfqs/{mine['id']}/matches")
    assert response.status_code == 200

    body = response.json()
    assert body["requester_role"] == "buyer"
    assert body["target_role"] == "seller"
    assert body["total"] >= 1
    assert {r["role"] for r in body["results"]} == {"seller"}


async def test_a_seller_is_shown_buyers(make_actor, make_rfq):
    seller = await make_actor("seller")
    buyer = await make_actor("buyer")

    mine = await make_rfq(seller, role="seller", title="Supplying 8000 Type-C cables")
    await make_rfq(buyer, title="Need 6000 Type-C cables")

    body = (await seller.post(f"/rfqs/{mine['id']}/matches")).json()
    assert body["target_role"] == "buyer"
    assert {r["role"] for r in body["results"]} == {"buyer"}


async def test_your_own_listings_are_never_matched_against_each_other(
    make_actor, make_rfq
):
    trader = await make_actor("both")
    mine = await make_rfq(trader, role="buyer")
    await make_rfq(trader, role="seller", title="Supplying Type-C cables")

    body = (await trader.post(f"/rfqs/{mine['id']}/matches")).json()
    assert body["total"] == 0


async def test_only_live_listings_are_matchable(make_actor, make_rfq):
    buyer = await make_actor("buyer")
    seller = await make_actor("seller")
    mine = await make_rfq(buyer)

    draft = await make_rfq(
        seller, role="seller", title="Supplying Type-C cables", status="draft"
    )
    assert (await buyer.post(f"/rfqs/{mine['id']}/matches")).json()["total"] == 0

    await seller.post(f"/rfqs/{draft['id']}/publish")
    assert (await buyer.post(f"/rfqs/{mine['id']}/matches")).json()["total"] == 1

    await seller.post(f"/rfqs/{draft['id']}/close")
    assert (await buyer.post(f"/rfqs/{mine['id']}/matches")).json()["total"] == 0


async def test_matching_a_draft_is_a_conflict_not_an_empty_page(make_actor, make_rfq):
    buyer = await make_actor("buyer")
    draft = await make_rfq(buyer, status="draft")
    response = await buyer.post(f"/rfqs/{draft['id']}/matches")
    assert response.status_code == 409


async def test_you_cannot_run_matching_on_someone_elses_rfq(make_actor, make_rfq):
    owner = await make_actor("buyer")
    stranger = await make_actor("buyer")
    rfq = await make_rfq(owner)
    assert (await stranger.post(f"/rfqs/{rfq['id']}/matches")).status_code == 404


async def test_matching_an_unknown_rfq_is_a_404(make_actor):
    actor = await make_actor("buyer")
    assert (await actor.post(f"/rfqs/{uuid.uuid4()}/matches")).status_code == 404


# --- what the card shows ----------------------------------------------------


async def test_a_candidate_carries_the_distance_it_is_scored_on(make_actor, make_rfq):
    """The card said "73%" and could not say why; now it can say "444 km"."""
    buyer = await make_actor("buyer")
    seller = await make_actor("seller")

    mine = await make_rfq(buyer, **at(LUDHIANA))
    await make_rfq(
        seller, role="seller", title="Supplying Type-C cables", **at(JAIPUR)
    )

    result = (await buyer.post(f"/rfqs/{mine['id']}/matches")).json()["results"][0]
    assert 400 < result["distance_km"] < 500
    assert 0.35 < result["score"]["location"] < 0.55


async def test_a_counterparty_in_the_same_city_scores_a_full_location_match(
    make_actor, make_rfq
):
    buyer = await make_actor("buyer")
    seller = await make_actor("seller")

    mine = await make_rfq(buyer, **at(LUDHIANA))
    await make_rfq(
        seller, role="seller", title="Supplying Type-C cables", **at(LUDHIANA)
    )

    result = (await buyer.post(f"/rfqs/{mine['id']}/matches")).json()["results"][0]
    assert result["distance_km"] == pytest.approx(0, abs=1)
    assert result["score"]["location"] == 1.0


async def test_distance_is_null_when_a_listing_could_not_be_geocoded(
    make_actor, make_rfq
):
    """A city the gazetteer does not know still matches by name, without a distance."""
    buyer = await make_actor("buyer")
    seller = await make_actor("seller")

    mine = await make_rfq(
        buyer, city="Bhiwandi", state="Maharashtra", country="India",
        latitude=None, longitude=None,
    )
    await make_rfq(
        seller,
        role="seller",
        title="Supplying Type-C cables",
        city="Bhiwandi",
        state="Maharashtra",
        country="India",
        latitude=None,
        longitude=None,
    )

    result = (await buyer.post(f"/rfqs/{mine['id']}/matches")).json()["results"][0]
    assert result["distance_km"] is None
    # The city names still agree, so the dimension is answerable.
    assert result["score"]["location"] == 1.0


async def test_the_score_breakdown_explains_the_total(make_actor, make_rfq):
    buyer = await make_actor("buyer")
    seller = await make_actor("seller")
    mine = await make_rfq(buyer, attributes={"colour": "red"})
    await make_rfq(
        seller,
        role="seller",
        title="Supplying 8000 red Type-C cables",
        attributes={"colour": "red"},
    )

    result = (await buyer.post(f"/rfqs/{mine['id']}/matches")).json()["results"][0]
    score = result["score"]
    assert 0 <= score["total"] <= 1
    for dimension in (
        "relevance", "attributes", "category", "price", "quantity",
        "location", "deadline",
    ):
        assert dimension in score
    assert score["attributes"] == 1.0


async def test_a_dimension_neither_side_filled_in_comes_back_null(
    make_actor, make_rfq
):
    """Null means "not comparable", which the card renders differently to 0%."""
    buyer = await make_actor("buyer")
    seller = await make_actor("seller")
    mine = await make_rfq(buyer, price=None)
    await make_rfq(
        seller, role="seller", title="Supplying 8000 Type-C cables", price=None
    )

    result = (await buyer.post(f"/rfqs/{mine['id']}/matches")).json()["results"][0]
    assert result["score"]["price"] is None


async def test_a_better_matching_seller_outranks_a_worse_one(make_actor, make_rfq):
    buyer = await make_actor("buyer")
    good = await make_actor("seller")
    poor = await make_actor("seller")

    mine = await make_rfq(
        buyer, attributes={"colour": "red"}, quantity=(6000, "pcs"),
        price=(200, "INR", "pcs"), **at(INDORE),
    )
    await make_rfq(
        good, role="seller", title="Supplying 8000 red Type-C cables",
        attributes={"colour": "red"}, quantity=(8000, "pcs"),
        price=(150, "INR", "pcs"), **at(INDORE),
    )
    await make_rfq(
        poor, role="seller", title="Supplying 1000 blue Type-C cables",
        attributes={"colour": "blue"}, quantity=(1000, "pcs"),
        price=(900, "INR", "pcs"), **at(LUDHIANA),
    )

    results = (await buyer.post(f"/rfqs/{mine['id']}/matches")).json()["results"]
    assert len(results) == 2
    assert results[0]["title"].startswith("Supplying 8000 red")
    assert results[0]["score"]["total"] > results[1]["score"]["total"]
    assert [r["rank"] for r in results] == [1, 2]


async def test_pagination_ranks_continue_across_pages(make_actor, make_rfq):
    buyer = await make_actor("buyer")
    mine = await make_rfq(buyer)
    for index in range(4):
        seller = await make_actor("seller")
        await make_rfq(
            seller, role="seller", title=f"Supplying Type-C cables lot {index}"
        )

    first = (
        await buyer.post(f"/rfqs/{mine['id']}/matches", params={"limit": 2, "offset": 0})
    ).json()
    second = (
        await buyer.post(f"/rfqs/{mine['id']}/matches", params={"limit": 2, "offset": 2})
    ).json()

    assert first["total"] == second["total"] == 4
    assert [r["rank"] for r in first["results"]] == [1, 2]
    assert [r["rank"] for r in second["results"]] == [3, 4]
    assert not {r["rfq_id"] for r in first["results"]} & {
        r["rfq_id"] for r in second["results"]
    }


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 500}, {"offset": -1}])
async def test_match_pagination_bounds_are_enforced(make_actor, make_rfq, params):
    buyer = await make_actor("buyer")
    mine = await make_rfq(buyer)
    response = await buyer.post(f"/rfqs/{mine['id']}/matches", params=params)
    assert response.status_code == 422


async def test_an_unrelated_product_is_not_returned_as_a_weak_match(
    make_actor, make_rfq
):
    """A cable search used to return every Electronics listing in the corpus."""
    buyer = await make_actor("buyer")
    seller = await make_actor("seller")

    mine = await make_rfq(buyer, title="Need 6000 red Type-C cables",
                          product="usb type-c cable")
    await make_rfq(
        seller, role="seller", title="Supplying 300 dining tables",
        category="Furniture", product="teak dining table",
        attributes={"seats": 6}, quantity=(300, "pcs"),
    )

    assert (await buyer.post(f"/rfqs/{mine['id']}/matches")).json()["total"] == 0


# --- privacy ----------------------------------------------------------------


async def test_contact_details_are_hidden_until_a_connection_is_accepted(
    make_actor, make_rfq
):
    """Otherwise the match page is a scrapeable lead list."""
    buyer = await make_actor("buyer")
    seller = await make_actor(
        "seller", company_name="Nandan Wires", phone="+91 98250 12345", city="Indore"
    )

    mine = await make_rfq(buyer)
    await make_rfq(seller, role="seller", title="Supplying 8000 Type-C cables")

    result = (await buyer.post(f"/rfqs/{mine['id']}/matches")).json()["results"][0]
    counterparty = result["counterparty"]

    # Safe to show on cards: who, location, and verified email for direct contact.
    assert counterparty["company_name"] == "Nandan Wires"
    assert counterparty["city"] == "Indore"
    assert counterparty["email"] == seller.email
    # Phone remains protected until connection accepted
    assert counterparty["phone"] is None
    assert counterparty["connection_status"] is None


async def test_the_card_reports_a_pending_request(make_actor, make_rfq):
    buyer = await make_actor("buyer")
    seller = await make_actor("seller", phone="+91 98250 12345")

    mine = await make_rfq(buyer)
    theirs = await make_rfq(seller, role="seller", title="Supplying Type-C cables")

    await buyer.post("/connections", json={"rfq_id": theirs["id"]})

    result = (await buyer.post(f"/rfqs/{mine['id']}/matches")).json()["results"][0]
    assert result["counterparty"]["connection_status"] == "pending"
    assert result["counterparty"]["connection_id"]
    assert result["counterparty"]["phone"] is None


async def test_accepting_a_connection_reveals_contact_details_on_the_card(
    make_actor, make_rfq
):
    buyer = await make_actor("buyer")
    seller = await make_actor(
        "seller", name="Rekha Nandan", company_name="Nandan Wires",
        phone="+91 98250 12345",
    )

    mine = await make_rfq(buyer)
    theirs = await make_rfq(seller, role="seller", title="Supplying Type-C cables")

    created = (
        await buyer.post("/connections", json={"rfq_id": theirs["id"]})
    ).json()
    await seller.post(f"/connections/{created['id']}/accept")

    result = (await buyer.post(f"/rfqs/{mine['id']}/matches")).json()["results"][0]
    counterparty = result["counterparty"]
    assert counterparty["connection_status"] == "accepted"
    assert counterparty["email"] == seller.email
    assert counterparty["phone"] == "+91 98250 12345"
    assert counterparty["contact_name"] == "Rekha Nandan"


async def test_a_rejected_request_does_not_reveal_contact_details(
    make_actor, make_rfq
):
    buyer = await make_actor("buyer")
    seller = await make_actor("seller", phone="+91 98250 12345")

    mine = await make_rfq(buyer)
    theirs = await make_rfq(seller, role="seller", title="Supplying Type-C cables")

    created = (await buyer.post("/connections", json={"rfq_id": theirs["id"]})).json()
    await seller.post(f"/connections/{created['id']}/reject")

    counterparty = (await buyer.post(f"/rfqs/{mine['id']}/matches")).json()["results"][
        0
    ]["counterparty"]
    assert counterparty["connection_status"] == "rejected"
    assert counterparty["email"] == seller.email
    assert counterparty["phone"] is None


async def test_someone_elses_accepted_connection_unlocks_nothing_for_you(
    make_actor, make_rfq
):
    """The unlock is per viewer, not per listing."""
    seller = await make_actor("seller", phone="+91 98250 12345")
    insider = await make_actor("buyer")
    outsider = await make_actor("buyer")

    theirs = await make_rfq(seller, role="seller", title="Supplying Type-C cables")
    insider_rfq = await make_rfq(insider)
    outsider_rfq = await make_rfq(outsider)

    created = (
        await insider.post("/connections", json={"rfq_id": theirs["id"]})
    ).json()
    await seller.post(f"/connections/{created['id']}/accept")

    unlocked = (await insider.post(f"/rfqs/{insider_rfq['id']}/matches")).json()
    assert unlocked["results"][0]["counterparty"]["phone"] == "+91 98250 12345"

    still_locked = (await outsider.post(f"/rfqs/{outsider_rfq['id']}/matches")).json()
    assert still_locked["results"][0]["counterparty"]["phone"] is None
    assert still_locked["results"][0]["counterparty"]["connection_status"] is None


async def test_matching_requires_authentication(client, make_actor, make_rfq):
    buyer = await make_actor("buyer")
    rfq = await make_rfq(buyer)
    assert (await client.post(f"/rfqs/{rfq['id']}/matches")).status_code == 401


async def test_matching_degrades_to_sql_when_the_vector_index_is_down(
    make_actor, make_rfq
):
    """Qdrant and Ollama are both unavailable here; results still come back.

    This is the whole point of keeping PostgreSQL as the source of truth: the
    index is an accelerator, not a dependency.
    """
    buyer = await make_actor("buyer")
    seller = await make_actor("seller")
    mine = await make_rfq(buyer)
    await make_rfq(seller, role="seller", title="Supplying 8000 red Type-C cables")

    body = (await buyer.post(f"/rfqs/{mine['id']}/matches")).json()
    assert body["total"] >= 1


async def test_the_listing_location_is_reported_separately_from_the_company_city(
    make_actor, make_rfq
):
    """A trader's registered office is not where the goods are.

    The card used to print the profile city beside the match score, so a company
    registered in Sao Paulo with stock in Izmir read as "Sao Paulo" next to a
    100% location match against an Izmir buyer. Both values are returned; the
    scored one is the listing's.
    """
    buyer = await make_actor("buyer")
    seller = await make_actor("seller", company_name="Harbour Enterprises", city="Jaipur")

    mine = await make_rfq(buyer, **at(LUDHIANA))
    await make_rfq(
        seller, role="seller", title="Supplying Type-C cables", **at(LUDHIANA)
    )

    result = (await buyer.post(f"/rfqs/{mine['id']}/matches")).json()["results"][0]
    assert result["counterparty"]["city"] == "Jaipur"
    assert result["location"]["city"] == "Ludhiana"
    # Scored on the goods, not the letterhead.
    assert result["score"]["location"] == 1.0
    assert result["distance_km"] == pytest.approx(0, abs=1)
