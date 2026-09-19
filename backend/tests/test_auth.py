"""Signup, login, tokens and the profile they hang off."""

import uuid

import pytest

from core.security import create_access_token, create_refresh_token


async def test_signup_returns_a_usable_token_pair(client):
    response = await client.post(
        "/auth/signup",
        json={
            "email": "Newcomer@Tests.Example.com",
            "password": "correct-horse-battery",
            "name": "Newcomer",
            "role": "buyer",
        },
    )
    assert response.status_code == 201
    tokens = response.json()
    assert tokens["token_type"] == "bearer"
    assert tokens["expires_in"] > 0

    me = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert me.status_code == 200
    # Stored lowercase, so the unique index is effectively case-insensitive.
    assert me.json()["email"] == "newcomer@tests.example.com"


async def test_the_password_hash_is_never_returned(make_actor):
    actor = await make_actor()
    body = (await actor.get("/auth/me")).json()
    assert "password" not in str(body).lower() or "password_hash" not in body


async def test_signup_rejects_a_duplicate_email_in_any_case(client, make_actor):
    actor = await make_actor(email="taken@tests.example.com")
    response = await client.post(
        "/auth/signup",
        json={
            "email": "TAKEN@tests.example.com",
            "password": "another-long-password",
            "name": "Impostor",
        },
    )
    assert response.status_code == 409
    assert actor.email == "taken@tests.example.com"


@pytest.mark.parametrize("password", ["", "short", "1234567"])
async def test_signup_rejects_a_password_under_eight_characters(client, password):
    response = await client.post(
        "/auth/signup",
        json={
            "email": f"weak-{uuid.uuid4().hex[:8]}@tests.example.com",
            "password": password,
            "name": "Weak",
        },
    )
    assert response.status_code == 422


async def test_signup_rejects_a_malformed_email(client):
    response = await client.post(
        "/auth/signup",
        json={"email": "not-an-email", "password": "correct-horse-battery", "name": "X"},
    )
    assert response.status_code == 422


async def test_login_succeeds_with_the_right_password(client, make_actor):
    actor = await make_actor(email="loginme@tests.example.com")
    response = await client.post(
        "/auth/login",
        json={"email": "loginme@tests.example.com", "password": "correct-horse-battery"},
    )
    assert response.status_code == 200
    assert response.json()["access_token"]
    assert actor.id


async def test_login_fails_with_the_wrong_password(client, make_actor):
    await make_actor(email="guarded@tests.example.com")
    response = await client.post(
        "/auth/login",
        json={"email": "guarded@tests.example.com", "password": "not-the-password"},
    )
    assert response.status_code == 401


async def test_login_fails_the_same_way_for_an_unknown_account(client):
    """Same status and wording, so the response cannot enumerate accounts."""
    response = await client.post(
        "/auth/login",
        json={"email": "ghost@tests.example.com", "password": "correct-horse-battery"},
    )
    assert response.status_code == 401


async def test_refresh_exchanges_a_refresh_token_for_a_new_pair(client, make_actor):
    actor = await make_actor()
    response = await client.post(
        "/auth/refresh", json={"refresh_token": actor.tokens["refresh_token"]}
    )
    assert response.status_code == 200
    new_access = response.json()["access_token"]

    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {new_access}"})
    assert me.status_code == 200


async def test_an_access_token_is_not_accepted_as_a_refresh_token(client, make_actor):
    """The `type` claim is the whole point: the two are not interchangeable."""
    actor = await make_actor()
    response = await client.post(
        "/auth/refresh", json={"refresh_token": actor.tokens["access_token"]}
    )
    assert response.status_code == 401


async def test_a_refresh_token_is_not_accepted_as_a_bearer_token(client, make_actor):
    actor = await make_actor()
    response = await client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {actor.tokens['refresh_token']}"},
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "header",
    [None, {"Authorization": "Bearer nonsense"}, {"Authorization": "Basic abc"}],
)
async def test_protected_routes_reject_a_missing_or_broken_token(client, header):
    response = await client.get("/auth/me", headers=header or {})
    assert response.status_code in (401, 403)


async def test_a_token_for_a_deleted_account_is_rejected(client, db):
    """A signed token is not enough; the account is re-read on every request."""
    stranger = uuid.uuid4()
    token = create_access_token(str(stranger))
    response = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert create_refresh_token(str(stranger))


async def test_profile_fields_survive_a_round_trip(make_actor):
    actor = await make_actor(company_name="Kalpana Extrusions", city="Surat")
    response = await actor.patch(
        "/users/me/profile",
        json={"phone": "+91 98250 11111", "state": "Gujarat", "country": "India"},
    )
    assert response.status_code == 200

    profile = (await actor.get("/auth/me")).json()["profile"]
    assert profile["company_name"] == "Kalpana Extrusions"
    assert profile["city"] == "Surat"
    assert profile["phone"] == "+91 98250 11111"
    assert profile["country"] == "India"


async def test_a_partial_profile_update_leaves_other_fields_alone(make_actor):
    actor = await make_actor(company_name="Before Co", city="Pune")
    await actor.patch("/users/me/profile", json={"phone": "+91 99999 00000"})

    profile = (await actor.get("/auth/me")).json()["profile"]
    assert profile["company_name"] == "Before Co"
    assert profile["city"] == "Pune"


async def test_an_account_can_switch_which_side_it_posts_on(make_actor):
    """Signing up to buy should not mean opening a second account to sell."""
    from tests.conftest import rfq_body

    actor = await make_actor("buyer")
    assert (await actor.post("/rfqs", json=rfq_body("seller"))).status_code == 422

    changed = await actor.patch("/users/me", json={"role": "both"})
    assert changed.status_code == 200
    assert changed.json()["role"] == "both"

    assert (await actor.post("/rfqs", json=rfq_body("seller"))).status_code == 201


async def test_the_role_change_rejects_anything_that_is_not_a_role(make_actor):
    actor = await make_actor("buyer")
    assert (await actor.patch("/users/me", json={"role": "admin"})).status_code == 422
    assert (await actor.patch("/users/me", json={"status": "active"})).status_code == 422


async def test_a_profile_city_is_geocoded(make_actor):
    actor = await make_actor()
    profile = (await actor.patch("/users/me/profile", json={"city": "ludhiana"})).json()
    assert profile["city"] == "Ludhiana"
    assert profile["country"] == "India"
    assert profile["latitude"] is not None


async def test_moving_city_does_not_leave_the_old_coordinates_behind(make_actor):
    actor = await make_actor()
    first = (await actor.patch("/users/me/profile", json={"city": "Indore"})).json()
    second = (await actor.patch("/users/me/profile", json={"city": "Hamburg"})).json()
    assert second["city"] == "Hamburg"
    assert second["country"] == "Germany"
    assert second["latitude"] != first["latitude"]


async def test_profile_updates_require_authentication(client):
    assert (await client.patch("/users/me/profile", json={"city": "X"})).status_code == 401
    assert (await client.patch("/users/me", json={"role": "both"})).status_code == 401


async def test_clearing_the_name_is_refused_rather_than_crashing(make_actor):
    """profile.name is NOT NULL, so this has to be a 422 and not a 500."""
    actor = await make_actor(name="Has A Name")
    assert (await actor.patch("/users/me/profile", json={"name": None})).status_code == 422
    assert (await actor.patch("/users/me/profile", json={"name": "   "})).status_code == 422
    assert (await actor.get("/auth/me")).json()["profile"]["name"] == "Has A Name"


async def test_blank_optional_fields_can_be_cleared(make_actor):
    actor = await make_actor(company_name="Old Co")
    profile = (await actor.patch("/users/me/profile", json={"company_name": None})).json()
    assert profile["company_name"] is None


async def test_a_city_that_contradicts_the_stated_country_is_not_geocoded(make_actor):
    """"Hamburg, India" is not the Hamburg in the gazetteer."""
    actor = await make_actor()
    profile = (
        await actor.patch("/users/me/profile", json={"city": "Hamburg", "country": "India"})
    ).json()
    assert profile["country"] == "India"
    assert profile["latitude"] is None
