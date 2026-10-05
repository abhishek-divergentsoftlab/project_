"""Posting the same active RFQ twice must not create a second listing."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update

from models.enums import RFQStatus
from models.rfq import RFQ
from models.user import User
from tests.conftest import rfq_body


async def test_api_returns_409_with_existing_id(make_actor):
    actor = await make_actor("buyer")
    first = await actor.post("/rfqs", json=rfq_body())
    assert first.status_code == 201
    again = await actor.post("/rfqs", json=rfq_body(title="  need 6000 RED type-c cables "))
    assert again.status_code == 409
    detail = again.json()["detail"]
    assert detail["existing_rfq_id"] == first.json()["id"]


async def test_different_price_quantity_role_or_user_is_allowed(make_actor):
    a = await make_actor("buyer")
    b = await make_actor("buyer")
    assert (await a.post("/rfqs", json=rfq_body())).status_code == 201
    assert (await a.post("/rfqs", json=rfq_body(price=(210, "INR", "pcs")))).status_code == 201
    assert (await a.post("/rfqs", json=rfq_body(quantity=(7000, "pcs")))).status_code == 201
    assert (await b.post("/rfqs", json=rfq_body())).status_code == 201


async def test_old_or_closed_duplicates_do_not_block(make_actor, db):
    actor = await make_actor("buyer")
    first = await actor.post("/rfqs", json=rfq_body())
    rid = uuid.UUID(first.json()["id"])
    await db.execute(update(RFQ).where(RFQ.id == rid).values(created_at=datetime.now(UTC) - timedelta(hours=25)))
    await db.commit()
    assert (await actor.post("/rfqs", json=rfq_body())).status_code == 201

    await db.execute(update(RFQ).where(RFQ.user_id == uuid.UUID(actor.id)).values(status=RFQStatus.CLOSED))
    await db.commit()
    assert (await actor.post("/rfqs", json=rfq_body())).status_code == 201


async def test_chat_create_path_reports_existing_rfq(make_actor, db):
    from services import ai_chat_service

    actor = await make_actor("buyer")
    user = await db.get(User, uuid.UUID(actor.id))
    draft = {
        "role": "buyer",
        "category": "Electronics",
        "product_details": {"name": "usb type-c cable"},
        "quantity": {"value": 6000, "unit": "pcs"},
    }
    created, err = await ai_chat_service.execute_create_rfq_call(draft, {"title": "Need cables"}, db=db, user=user)
    assert created and not err

    again, err2 = await ai_chat_service.execute_create_rfq_call(draft, {"title": "Need cables"}, db=db, user=user)
    assert again is None
    assert err2.startswith("You already have this RFQ: Need cables") and created["id"] in err2
    count = await db.scalar(select(func.count(RFQ.id)).where(RFQ.user_id == user.id))
    assert count == 1
