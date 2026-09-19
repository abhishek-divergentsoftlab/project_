"""RFQ lifecycle, ownership and validation."""

import uuid

import pytest

from tests.conftest import rfq_body


async def test_create_returns_the_stored_projection(make_actor):
    actor = await make_actor("buyer")
    response = await actor.post("/rfqs", json=rfq_body())
    assert response.status_code == 201

    body = response.json()
    assert body["role"] == "buyer"
    assert body["status"] == "active"
    assert body["quantity"] == {"value": 6000.0, "unit": "pcs"}
    assert body["price_target"]["currency"] == "INR"
    assert body["location"]["city"] == "Indore"
    # Derived at write time so matching has something to work with.
    assert body["search_tags"]
    assert body["deadline"]["date"]


async def test_numeric_fields_come_back_as_numbers_not_strings(make_actor):
    """Numeric(18,4) serialises as "8000.0000" unless it is normalised."""
    actor = await make_actor("seller")
    body = (await actor.post("/rfqs", json=rfq_body("seller"))).json()
    assert isinstance(body["quantity"]["value"], (int, float))
    assert isinstance(body["price_target"]["amount"], (int, float))


async def test_a_draft_stays_a_draft_until_published(make_actor):
    actor = await make_actor("buyer")
    created = (await actor.post("/rfqs", json=rfq_body(status="draft"))).json()
    assert created["status"] == "draft"

    published = await actor.post(f"/rfqs/{created['id']}/publish")
    assert published.status_code == 200
    assert published.json()["status"] == "active"


async def test_closing_takes_an_rfq_out_of_circulation(make_actor):
    actor = await make_actor("buyer")
    created = (await actor.post("/rfqs", json=rfq_body())).json()
    closed = await actor.post(f"/rfqs/{created['id']}/close")
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"


async def test_a_buyer_account_cannot_post_on_the_sell_side(make_actor):
    actor = await make_actor("buyer")
    response = await actor.post("/rfqs", json=rfq_body("seller"))
    assert response.status_code == 422


async def test_a_both_account_may_post_on_either_side(make_actor):
    actor = await make_actor("both")
    assert (await actor.post("/rfqs", json=rfq_body("buyer"))).status_code == 201
    assert (await actor.post("/rfqs", json=rfq_body("seller"))).status_code == 201


async def test_listing_only_ever_returns_your_own_rfqs(make_actor, make_rfq):
    mine = await make_actor("buyer")
    theirs = await make_actor("buyer")
    await make_rfq(mine, title="Mine")
    await make_rfq(theirs, title="Theirs")

    body = (await mine.get("/rfqs")).json()
    assert body["total"] == 1
    assert body["items"][0]["title"] == "Mine"


async def test_someone_elses_rfq_is_a_404_not_a_403(make_actor, make_rfq):
    """A 403 would confirm the id exists, which is an enumeration oracle."""
    owner = await make_actor("buyer")
    stranger = await make_actor("buyer")
    rfq = await make_rfq(owner)

    assert (await stranger.get(f"/rfqs/{rfq['id']}")).status_code == 404
    assert (await stranger.post(f"/rfqs/{rfq['id']}/close")).status_code == 404
    assert (await stranger.post(f"/rfqs/{rfq['id']}/publish")).status_code == 404


async def test_an_unknown_rfq_id_is_a_404(make_actor):
    actor = await make_actor("buyer")
    assert (await actor.get(f"/rfqs/{uuid.uuid4()}")).status_code == 404


async def test_a_malformed_rfq_id_is_a_422(make_actor):
    actor = await make_actor("buyer")
    assert (await actor.get("/rfqs/not-a-uuid")).status_code == 422


async def test_status_filter_narrows_the_list(make_actor, make_rfq):
    actor = await make_actor("buyer")
    await make_rfq(actor, title="Live one")
    await make_rfq(actor, title="Draft one", status="draft")

    active = (await actor.get("/rfqs", params={"status": "active"})).json()
    assert [item["title"] for item in active["items"]] == ["Live one"]

    drafts = (await actor.get("/rfqs", params={"status": "draft"})).json()
    assert [item["title"] for item in drafts["items"]] == ["Draft one"]


async def test_an_unknown_status_filter_is_rejected(make_actor):
    actor = await make_actor("buyer")
    response = await actor.get("/rfqs", params={"status": "banana"})
    assert response.status_code == 422


async def test_pagination_walks_the_list_without_repeats(make_actor, make_rfq):
    actor = await make_actor("buyer")
    for index in range(5):
        await make_rfq(actor, title=f"RFQ {index}")

    first = (await actor.get("/rfqs", params={"limit": 2, "offset": 0})).json()
    second = (await actor.get("/rfqs", params={"limit": 2, "offset": 2})).json()

    assert first["total"] == second["total"] == 5
    assert len(first["items"]) == len(second["items"]) == 2
    assert not {i["id"] for i in first["items"]} & {i["id"] for i in second["items"]}


@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"limit": 500}, {"offset": -1}]
)
async def test_pagination_bounds_are_enforced(make_actor, params):
    actor = await make_actor("buyer")
    assert (await actor.get("/rfqs", params=params)).status_code == 422


@pytest.mark.parametrize(
    "patch",
    [
        {"title": ""},
        {"category": ""},
        {"title": "x" * 400},
        {"role": "sideways"},
        {"status": "banana"},
    ],
)
async def test_invalid_rfq_bodies_are_rejected(make_actor, patch):
    actor = await make_actor("both")
    body = rfq_body()
    body.update(patch)
    assert (await actor.post("/rfqs", json=body)).status_code == 422


async def test_unknown_fields_are_rejected_rather_than_silently_dropped(make_actor):
    actor = await make_actor("buyer")
    body = rfq_body()
    body["totally_made_up"] = "value"
    assert (await actor.post("/rfqs", json=body)).status_code == 422


async def test_an_rfq_with_almost_nothing_filled_in_is_still_valid(make_actor):
    """Matching drops the dimensions it cannot evaluate, so this must work."""
    actor = await make_actor("buyer")
    response = await actor.post(
        "/rfqs",
        json=rfq_body(
            quantity=None, price=None, city=None, state=None, country=None,
            latitude=None, longitude=None, deadline_days=None,
        ),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["quantity"] is None
    assert body["price_target"] is None


async def test_product_details_accept_arbitrary_nested_json(make_actor):
    """The whole point of the JSONB column: no migration per product type."""
    actor = await make_actor("seller")
    details = {
        "capacity": {"value": 20000, "unit": "mAh"},
        "ports": 3,
        "c_to_c": True,
        "certification": ["CE", "UN38.3"],
    }
    body = (
        await actor.post("/rfqs", json=rfq_body("seller", attributes=details))
    ).json()
    assert body["product_details"]["capacity"] == {"value": 20000, "unit": "mAh"}
    assert body["product_details"]["certification"] == ["CE", "UN38.3"]


async def test_every_rfq_route_requires_authentication(client, make_actor, make_rfq):
    owner = await make_actor("buyer")
    rfq = await make_rfq(owner)

    assert (await client.get("/rfqs")).status_code == 401
    assert (await client.post("/rfqs", json=rfq_body())).status_code == 401
    assert (await client.get(f"/rfqs/{rfq['id']}")).status_code == 401
    assert (await client.post(f"/rfqs/{rfq['id']}/publish")).status_code == 401


async def test_a_known_city_is_geocoded_even_when_no_coordinates_were_sent(
    make_actor,
):
    """The new-RFQ form has a city box and no map."""
    actor = await make_actor("buyer")
    body = (
        await actor.post(
            "/rfqs",
            json=rfq_body(city="ludhiana", state=None, country=None,
                          latitude=None, longitude=None),
        )
    ).json()

    assert body["location"]["latitude"] == pytest.approx(30.901, abs=0.01)
    assert body["location"]["longitude"] == pytest.approx(75.857, abs=0.01)
    # Canonicalised, so "ludhiana" and "Ludhiana" are one place.
    assert body["location"]["city"] == "Ludhiana"
    assert body["location"]["state"] == "Punjab"
    assert body["location"]["country"] == "India"


async def test_supplied_coordinates_are_never_overwritten(make_actor):
    actor = await make_actor("buyer")
    body = (
        await actor.post(
            "/rfqs", json=rfq_body(city="Indore", latitude=1.5, longitude=2.5)
        )
    ).json()
    assert body["location"]["latitude"] == pytest.approx(1.5)
    assert body["location"]["longitude"] == pytest.approx(2.5)


async def test_an_unknown_city_is_kept_as_written(make_actor):
    actor = await make_actor("buyer")
    body = (
        await actor.post(
            "/rfqs",
            json=rfq_body(city="Nowhereville", state=None, country=None,
                          latitude=None, longitude=None),
        )
    ).json()
    assert body["location"]["city"] == "Nowhereville"
    assert body["location"]["latitude"] is None


async def test_a_listing_city_that_contradicts_its_country_is_not_geocoded(make_actor):
    """The form used to default every listing's country to India."""
    actor = await make_actor("buyer")
    body = (
        await actor.post(
            "/rfqs",
            json=rfq_body(city="Hamburg", state=None, country="India",
                          latitude=None, longitude=None),
        )
    ).json()
    assert body["location"]["country"] == "India"
    assert body["location"]["latitude"] is None


async def test_an_rfq_can_be_edited_after_it_is_published(make_actor, make_rfq):
    """PATCH existed but nothing called it, so a typo in a live listing was permanent."""
    actor = await make_actor("buyer")
    rfq = await make_rfq(actor, title="Need 6000 red Tpye-C cables")

    fixed = await actor.patch(
        f"/rfqs/{rfq['id']}",
        json={
            "title": "Need 6000 red Type-C cables",
            "quantity": {"value": 9000, "unit": "pcs"},
            "product_details": {"name": "usb type-c cable", "colour": "red"},
        },
    )
    assert fixed.status_code == 200
    body = fixed.json()
    assert body["title"] == "Need 6000 red Type-C cables"
    assert body["quantity"]["value"] == 9000
    assert body["status"] == "active"
    # The edit changes what the embedding should encode, so it is re-derived.
    assert "9000" in " ".join(body["search_tags"]) or body["search_tags"]


async def test_an_edit_leaves_out_fields_untouched(make_actor, make_rfq):
    actor = await make_actor("buyer")
    rfq = await make_rfq(actor)

    edited = (await actor.patch(f"/rfqs/{rfq['id']}", json={"title": "New title"})).json()
    assert edited["title"] == "New title"
    assert edited["category"] == rfq["category"]
    assert edited["quantity"] == rfq["quantity"]
    assert edited["location"]["city"] == rfq["location"]["city"]


async def test_you_cannot_edit_someone_elses_rfq(make_actor, make_rfq):
    owner = await make_actor("buyer")
    stranger = await make_actor("buyer")
    rfq = await make_rfq(owner)
    response = await stranger.patch(f"/rfqs/{rfq['id']}", json={"title": "Mine now"})
    assert response.status_code == 404


async def test_an_edit_cannot_flip_which_side_of_the_market_a_listing_is_on(
    make_actor, make_rfq
):
    """It would invalidate every match already shown against it."""
    actor = await make_actor("both")
    rfq = await make_rfq(actor, role="buyer")
    response = await actor.patch(f"/rfqs/{rfq['id']}", json={"role": "seller"})
    assert response.status_code == 422


async def test_editing_a_location_regeocodes_it(make_actor, make_rfq):
    actor = await make_actor("buyer")
    rfq = await make_rfq(actor, city="Indore")

    moved = (
        await actor.patch(
            f"/rfqs/{rfq['id']}",
            json={"location": {"city": "hamburg", "state": None, "country": None}},
        )
    ).json()
    assert moved["location"]["city"] == "Hamburg"
    assert moved["location"]["country"] == "Germany"
    assert moved["location"]["latitude"] == pytest.approx(53.55, abs=0.1)


async def test_a_full_page_of_listings_is_reachable(make_actor, make_rfq):
    """The default page size is 20 and seeded accounts own 25."""
    actor = await make_actor("buyer")
    for index in range(25):
        await make_rfq(actor, title=f"Listing {index:02d}")

    first = (await actor.get("/rfqs", params={"limit": 24, "offset": 0})).json()
    second = (await actor.get("/rfqs", params={"limit": 24, "offset": 24})).json()

    assert first["total"] == 25
    assert len(first["items"]) == 24
    assert len(second["items"]) == 1
    seen = {item["id"] for item in first["items"]} | {item["id"] for item in second["items"]}
    assert len(seen) == 25
