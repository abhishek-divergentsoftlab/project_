"""Regression tests for the 2026-09 security audit fixes (AUDIT_REPORT.md 2, 3.3).

Covers: secret-key guard, rate limiting, KYC validation, profile PATCH KYC
reset, certificate trust/URL rules, private media signing, moderation
DoS/accuracy, image-moderation fail-closed, notification pagination and
websocket admission. Every upload goes to a tmp dir, never backend/uploads.
"""

import time
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app import app
from core import config as core_config
from core import media_signing, rate_limit
from core.config import settings

GOOD_PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00"
GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def gstin_check_char(first14: str) -> str:
    total = 0
    for i, c in enumerate(first14):
        p = GSTIN_CHARS.index(c) * (1 if i % 2 == 0 else 2)
        total += p // 36 + p % 36
    return GSTIN_CHARS[(36 - total % 36) % 36]


def valid_gstin(pan: str = "AABCU9603R", state: str = "27") -> str:
    base = f"{state}{pan}1Z"
    return base + gstin_check_char(base)


def kyc_body(gst: str, **extra):
    body = {
        "gst_number": gst,
        "legal_business_name": "Probe Industries Pvt Ltd",
        "business_type": "Private Limited",
    }
    body.update(extra)
    return body


def cert_body(**over):
    body = {
        "name": "ISO 9001:2015",
        "issuing_body": "SGS",
        "certificate_number": f"C-{uuid.uuid4().hex[:6]}",
        "issue_date": "2023-01-01T00:00:00Z",
    }
    body.update(over)
    return body


@pytest.fixture(autouse=True)
def isolated_uploads(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "uploads"))
    return tmp_path / "uploads"


@pytest.fixture(autouse=True)
def fresh_limiter():
    rate_limit.limiter.reset()
    yield
    rate_limit.limiter.reset()


# ---------------------------------------------------------------- config


@pytest.mark.parametrize("env", ["prod", "staging", "production", "qa"])
def test_secret_guard_rejects_default_key_outside_dev(monkeypatch, env):
    monkeypatch.setenv("SECRET_KEY", "dev-only-insecure-key-change-me")
    monkeypatch.setenv("ENVIRONMENT", env)
    with pytest.raises(RuntimeError):
        core_config.get_settings.__wrapped__()


def test_secret_guard_rejects_short_key(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "secret")
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(RuntimeError):
        core_config.get_settings.__wrapped__()


@pytest.mark.parametrize("env", ["development", "dev", "local", "test"])
def test_secret_guard_allows_dev_environments(monkeypatch, env):
    monkeypatch.setenv("SECRET_KEY", "dev-only-insecure-key-change-me")
    monkeypatch.setenv("ENVIRONMENT", env)
    assert core_config.get_settings.__wrapped__().ENVIRONMENT == env


def test_secret_guard_accepts_strong_key_in_production(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "k" * 48)
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert core_config.get_settings.__wrapped__().is_production


def test_translation_external_fallback_defaults_off():
    assert settings.TRANSLATION_EXTERNAL_FALLBACK is False


# ---------------------------------------------------------------- rate limiting


def test_sliding_window_limiter_unit():
    lim = rate_limit.SlidingWindowRateLimiter()
    assert [lim.hit("k", 3, 10, now=t) for t in (0, 1, 2)] == [0, 0, 0]
    wait = lim.hit("k", 3, 10, now=3)
    assert 6.9 < wait <= 7.0  # oldest hit (t=0) ages out at t=10
    assert lim.hit("other", 3, 10, now=3) == 0
    assert lim.hit("k", 3, 10, now=10.5) == 0  # window slid
    lim.reset()
    assert lim.check("k", 3, 10, now=10.6) == 0


async def test_login_bruteforce_is_throttled_with_retry_after(client, make_actor):
    a = await make_actor()
    codes = []
    last = None
    for _ in range(25):
        last = await client.post("/auth/login", json={"email": a.email, "password": "wrong-pass-123"})
        codes.append(last.status_code)
    assert codes[:10] == [401] * 10
    assert 429 in codes
    assert int(last.headers["retry-after"]) > 0
    # Even the right password is refused while the account is throttled.
    ok = await client.post("/auth/login", json={"email": a.email, "password": "correct-horse-battery"})
    assert ok.status_code == 429


async def test_successful_logins_are_not_throttled(client, make_actor):
    a = await make_actor()
    for _ in range(15):
        r = await client.post("/auth/login", json={"email": a.email, "password": "correct-horse-battery"})
        assert r.status_code == 200


async def test_signup_is_rate_limited_per_ip(client, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_SIGNUP", "3/3600")
    codes = []
    for _ in range(5):
        r = await client.post(
            "/auth/signup",
            json={"email": f"s-{uuid.uuid4().hex[:8]}@tests.example.com", "password": "abcdefgh1", "name": "X"},
        )
        codes.append(r.status_code)
    assert codes == [201, 201, 201, 429, 429]


async def test_refresh_is_rate_limited(client, make_actor, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_REFRESH", "2/300")
    a = await make_actor()
    cur_token = a.tokens["refresh_token"]
    codes = []
    for _ in range(3):
        res = await client.post("/auth/refresh", json={"refresh_token": cur_token})
        codes.append(res.status_code)
        if res.status_code == 200:
            cur_token = res.json().get("refresh_token") or cur_token
    assert codes == [200, 200, 429]


# ---------------------------------------------------------------- KYC


@pytest.mark.parametrize(
    "gst,extra",
    [
        ("ZZZZZZZZZZ", {}),  # garbage "international VAT"
        ("ZZZZZZZZZZ", {"country": "Germany"}),  # garbage even for a foreign company
        (valid_gstin("AAAPZ1234C")[:-1] + "1", {}),  # wrong check char (valid one is 0)
        (valid_gstin("AABCU9603R"), {"pan_number": "not-a-pan"}),  # malformed PAN
        (valid_gstin("AABCU9603R"), {"pan_number": "AABCV1111K"}),  # PAN not the one in GSTIN
        ("27AABCU9603R1ZM", {}),  # widely quoted example, bad checksum
        ("DE12345678", {"country": "Germany"}),  # wrong length for DE
    ],
)
async def test_kyc_rejects_invalid_ids_with_422(make_actor, gst, extra):
    a = await make_actor("seller")
    r = await a.post("/users/me/kyc/verify", json=kyc_body(gst, **extra))
    assert r.status_code == 422, r.json()
    me = (await a.get("/auth/me")).json()["profile"]
    assert me["kyc_status"] == "unverified" and me["trust_score"] == 20


async def test_kyc_valid_gstin_is_format_verified_with_honest_message(make_actor):
    a = await make_actor("seller")
    gst = valid_gstin("AAAPZ1234C")
    r = await a.post("/users/me/kyc/verify", json=kyc_body(gst.lower(), pan_number="aaapz1234c"))
    assert r.status_code == 200, r.json()
    data = r.json()
    assert data["kyc_status"] == "verified"
    assert data["gst_number"] == gst
    assert "format and checksum verified" in data["message"].lower()
    assert "not been confirmed" in data["message"].lower()
    # 20 base + 35 verified + 10 PAN, computed not incremented: resubmitting is idempotent.
    assert data["trust_score"] == 65
    again = await a.post("/users/me/kyc/verify", json=kyc_body(gst, pan_number="AAAPZ1234C"))
    assert again.json()["trust_score"] == 65


async def test_kyc_accepts_foreign_vat_for_non_indian_country(make_actor):
    a = await make_actor("seller")
    r = await a.post("/users/me/kyc/verify", json=kyc_body("DE123456788", country="Germany"))
    assert r.status_code == 200, r.json()
    assert r.json()["kyc_status"] == "verified"
    # Country without a specific pattern: accepted for review only.
    b = await make_actor("seller")
    r2 = await b.post("/users/me/kyc/verify", json=kyc_body("KRA-P051234567X", country="Kenya"))
    assert r2.status_code == 200, r2.json()
    assert r2.json()["kyc_status"] == "pending"


async def test_kyc_duplicate_check_does_not_500(make_actor):
    gst = valid_gstin("AABCW2222L")
    for _ in range(2):
        u = await make_actor("seller")
        assert (await u.patch("/users/me/profile", json={"gst_number": gst})).status_code == 200
    owner = await make_actor("seller")
    # Unverified profile values do not block the real owner...
    assert (await owner.post("/users/me/kyc/verify", json=kyc_body(gst))).status_code == 200
    # ...but once verified, nobody else can verify or claim it.
    third = await make_actor("seller")
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test/api/v1") as c:
        r = await c.post("/users/me/kyc/verify", headers=third.headers, json=kyc_body(gst))
    assert r.status_code == 409, r.text
    assert "anti-fraud" in r.json()["detail"].lower()


# ---------------------------------------------------------------- profile PATCH


async def test_profile_patch_changing_gst_resets_kyc_and_trust(make_actor):
    a = await make_actor("seller")
    verified = await a.post("/users/me/kyc/verify", json=kyc_body(valid_gstin("AAAPZ1234C")))
    assert verified.json()["kyc_status"] == "verified"
    r = await a.patch(
        "/users/me/profile",
        json={"gst_number": "anything-at-all", "legal_business_name": "Totally Different Co"},
    )
    assert r.status_code == 200
    assert r.json()["kyc_status"] == "unverified"
    assert r.json()["trust_score"] == 20


@pytest.mark.parametrize("field", ["legal_business_name", "pan_number"])
async def test_profile_patch_changing_identity_field_resets_kyc(make_actor, field):
    a = await make_actor("seller")
    await a.post("/users/me/kyc/verify", json=kyc_body(valid_gstin("AAAPZ1234C"), pan_number="AAAPZ1234C"))
    value = {"legal_business_name": "Other Legal Name Ltd", "pan_number": "AABCU9603R"}[field]
    r = await a.patch("/users/me/profile", json={field: value})
    assert r.json()["kyc_status"] == "unverified"


async def test_profile_patch_unrelated_fields_keep_kyc(make_actor):
    a = await make_actor("seller")
    gst = valid_gstin("AAAPZ1234C")
    await a.post("/users/me/kyc/verify", json=kyc_body(gst))
    r = await a.patch(
        "/users/me/profile",
        json={"phone": "+91 99999 00000", "gst_number": gst.lower(), "legal_business_name": "Probe Industries Pvt Ltd"},
    )
    assert r.json()["kyc_status"] == "verified"
    assert r.json()["trust_score"] == 20 + 35 + 10  # phone now counts


async def test_profile_patch_cannot_claim_another_companys_gst(make_actor):
    victim = await make_actor("seller")
    gst = valid_gstin("AABCV1111K")
    assert (await victim.post("/users/me/kyc/verify", json=kyc_body(gst))).status_code == 200
    attacker = await make_actor("seller")
    r = await attacker.patch("/users/me/profile", json={"gst_number": gst.lower()})
    assert r.status_code == 409
    me = (await attacker.get("/auth/me")).json()["profile"]
    assert me["gst_number"] is None


# ---------------------------------------------------------------- certificates


async def test_trust_score_cannot_be_farmed_with_certificates(make_actor):
    a = await make_actor("seller")
    for i in range(10):
        assert (await a.post("/certifications", json=cert_body(name=f"Fake Cert {i}"))).status_code == 201
    me = (await a.get("/auth/me")).json()
    assert me["profile"]["trust_score"] == 20 + 15  # +5 each, capped at +15


async def test_deleting_certificates_reverts_trust_bonus(make_actor):
    a = await make_actor("seller")
    ids = []
    for _ in range(3):
        ids.append((await a.post("/certifications", json=cert_body())).json()["id"])
    assert (await a.get("/auth/me")).json()["profile"]["trust_score"] == 35
    await a.delete(f"/certifications/{ids[0]}")
    assert (await a.get("/auth/me")).json()["profile"]["trust_score"] == 30
    for cid in ids[1:]:
        await a.delete(f"/certifications/{cid}")
    assert (await a.get("/auth/me")).json()["profile"]["trust_score"] == 20


async def test_expired_certificate_rejected(make_actor):
    a = await make_actor("seller")
    r = await a.post(
        "/certifications",
        json=cert_body(issue_date="2018-01-01T00:00:00Z", expiry_date="2020-01-01T00:00:00Z"),
    )
    assert r.status_code == 422
    assert "expired" in r.json()["detail"].lower()


async def test_future_issue_date_rejected(make_actor):
    a = await make_actor("seller")
    future = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    r = await a.post("/certifications", json=cert_body(issue_date=future))
    assert r.status_code == 422


async def test_expired_certificates_do_not_count_toward_trust(db, make_actor):
    from sqlalchemy import update

    from models.certificate import Certificate
    from services import kyc_service
    from models.user import UserProfile
    from sqlalchemy import select

    a = await make_actor("seller")
    c = (await a.post("/certifications", json=cert_body(expiry_date="2099-01-01T00:00:00Z"))).json()
    await db.execute(
        update(Certificate)
        .where(Certificate.id == uuid.UUID(c["id"]))
        .values(expiry_date=datetime.now(UTC) - timedelta(days=1))
    )
    await db.commit()
    profile = (await db.execute(select(UserProfile).where(UserProfile.user_id == uuid.UUID(a.id)))).scalar_one()
    assert await kyc_service.recompute_trust_score(db, profile) == 20


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(document.cookie)",
        "https://evil.example.com/fake-iso.pdf",
        "data:text/html,<script>alert(1)</script>",
        "/api/v1/media/certificates/../../app.py",
        "/api/v1/media/live_captures/live_x.jpg",
        f"/api/v1/media/certificates/cert_{uuid.uuid4().hex}.pdf",  # legacy name: owner unknown
    ],
)
async def test_certificate_document_url_must_be_own_upload(make_actor, url):
    a = await make_actor("seller")
    r = await a.post("/certifications", json=cert_body(document_url=url))
    assert r.status_code == 422, f"accepted document_url={url!r}"


async def test_certificate_document_url_own_upload_accepted_other_users_refused(make_actor, isolated_uploads):
    a = await make_actor("seller")
    up = await a.post("/certifications/upload", files={"file": ("a.pdf", GOOD_PDF, "application/pdf")})
    assert up.status_code == 201
    url = up.json()["document_url"]
    assert url.startswith(f"/api/v1/media/certificates/cert_{uuid.UUID(a.id).hex}_")
    assert all(p.is_relative_to(isolated_uploads) for p in isolated_uploads.rglob("*"))

    b = await make_actor("seller")
    stolen = await b.post("/certifications", json=cert_body(document_url=url))
    assert stolen.status_code == 403

    own = await a.post("/certifications", json=cert_body(document_url=url))
    assert own.status_code == 201 and own.json()["document_url"] == url

    # Certificate files stay publicly readable (shown on public profiles).
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        served = await anon.get(url)
    assert served.status_code == 200 and served.content == GOOD_PDF
    assert served.headers["x-content-type-options"] == "nosniff"


async def test_certificate_upload_quota(make_actor, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_UPLOAD", "5/3600")
    a = await make_actor("seller")
    codes = [
        (await a.post("/certifications/upload", files={"file": (f"{i}.pdf", GOOD_PDF, "application/pdf")})).status_code
        for i in range(7)
    ]
    assert codes == [201] * 5 + [429, 429]


async def test_certificate_upload_default_quota_caps_orphans(make_actor, isolated_uploads):
    a = await make_actor("seller")
    codes = [
        (await a.post("/certifications/upload", files={"file": (f"{i}.pdf", GOOD_PDF, "application/pdf")})).status_code
        for i in range(30)
    ]
    assert 429 in codes
    assert len(list(isolated_uploads.rglob("*.pdf"))) <= 20


# ---------------------------------------------------------------- media


async def _live_capture(buyer, conn_id) -> str:
    m = await buyer.post(
        f"/connections/{conn_id}/messages/live-capture", files={"image": ("x.jpg", JPEG, "image/jpeg")}
    )
    assert m.status_code in (200, 201), m.text
    return m.json()["image_url"]


def _unsigned(url: str) -> str:
    return url.split("?", 1)[0]


async def test_live_capture_not_served_anonymously(client, accepted_pair):
    buyer, _seller, conn_id, _ = accepted_pair
    url = _unsigned(await _live_capture(buyer, conn_id))
    anon = await client.get(url.replace("/api/v1", ""))
    assert anon.status_code == 403


async def test_live_capture_signed_url_and_bearer_access(client, accepted_pair, make_actor):
    buyer, seller, conn_id, _ = accepted_pair
    url = _unsigned(await _live_capture(buyer, conn_id))
    path = url.replace("/api/v1", "")

    signed = media_signing.sign_media_url(url).replace("/api/v1", "")
    ok = await client.get(signed)
    assert ok.status_code == 200 and ok.content == JPEG
    assert ok.headers["x-content-type-options"] == "nosniff"
    assert ok.headers["content-type"] == "image/jpeg"

    tampered = signed[:-3] + ("AAA" if not signed.endswith("AAA") else "BBB")
    assert (await client.get(tampered)).status_code == 403

    other_file = signed.replace("live_", "live_0", 1)
    assert (await client.get(other_file)).status_code in (403, 404)

    expired = media_signing.sign_media_url(url, ttl=1).replace("/api/v1", "")
    with patch("core.media_signing.time.time", return_value=time.time() + 3600):
        assert (await client.get(expired)).status_code == 403

    # A participant's bearer token also works; a stranger's does not.
    assert (await seller.get(path)).status_code == 200
    assert (await buyer.get(path)).status_code == 200
    stranger = await make_actor()
    assert (await stranger.get(path)).status_code == 403


def test_sign_private_media_url_passthrough():
    assert media_signing.sign_private_media_url(None) is None
    assert media_signing.sign_private_media_url("https://x.example/a.jpg") == "https://x.example/a.jpg"
    cert = "/api/v1/media/certificates/cert_a.pdf"
    assert media_signing.sign_private_media_url(cert) == cert
    signed = media_signing.sign_private_media_url("/api/v1/media/live_captures/live_a.jpg")
    assert signed.startswith("/api/v1/media/live_captures/live_a.jpg?exp=") and "&sig=" in signed
    # Re-signing replaces the old query instead of stacking it.
    assert media_signing.sign_media_url(signed).count("?") == 1


async def test_media_serves_unknown_ext_as_attachment(client, isolated_uploads):
    d = isolated_uploads / "certificates"
    d.mkdir(parents=True, exist_ok=True)
    (d / "planted.html").write_bytes(b"<script>alert(1)</script>")
    r = await client.get("/media/certificates/planted.html")
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "path",
    ["/media/..%2F..%2Fapp.py", "/media/%2Fetc%2Fpasswd", "/media/live_captures/..%2F..%2F.env"],
)
async def test_media_path_traversal_blocked(client, path):
    r = await client.get(path)
    assert r.status_code in (403, 404)
    assert b"SECRET_KEY" not in r.content and b"root:" not in r.content


# ---------------------------------------------------------------- notifications


@pytest.mark.parametrize("qs", ["limit=-1", "offset=-5", "limit=abc", "limit=0", "limit=101"])
async def test_notifications_bad_pagination_is_422(make_actor, qs):
    a = await make_actor()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test/api/v1") as c:
        r = await c.get(f"/notifications?{qs}", headers=a.headers)
    assert r.status_code == 422, f"{qs} -> {r.status_code}"


# ---------------------------------------------------------------- websockets


class FakeWS:
    def __init__(self, headers=None, incoming=None):
        self.accepted = False
        self.subprotocol = None
        self.closed_code = None
        self.headers = headers or {}
        self.incoming = list(incoming or [])
        self.sent = []

    async def accept(self, subprotocol=None):
        self.accepted = True
        self.subprotocol = subprotocol

    async def close(self, code=1000):
        self.closed_code = code

    async def send_json(self, data):
        self.sent.append(data)

    async def receive_json(self):
        from fastapi import WebSocketDisconnect

        if self.incoming:
            return self.incoming.pop(0)
        raise WebSocketDisconnect()


async def test_ws_rejects_rejected_connection(make_actor, make_rfq):
    from api.v1.websockets import connection_websocket

    buyer = await make_actor("buyer")
    seller = await make_actor("seller")
    listing = await make_rfq(seller, role="seller", title="Supplying 5000 steel bolts")
    conn = (await buyer.post("/connections", json={"rfq_id": listing["id"]})).json()
    await seller.post(f"/connections/{conn['id']}/reject")
    ws = FakeWS()
    await connection_websocket(ws, uuid.UUID(conn["id"]), token=buyer.tokens["access_token"])
    assert ws.accepted is False and ws.closed_code == 1008


async def test_ws_accepts_token_via_subprotocol(accepted_pair):
    from api.v1.websockets import connection_websocket

    buyer, _s, conn_id, _ = accepted_pair
    ws = FakeWS(
        headers={"sec-websocket-protocol": f"bearer, {buyer.tokens['access_token']}"},
        incoming=[{"type": "ping"}],
    )
    await connection_websocket(ws, uuid.UUID(conn_id), token=None)
    assert ws.accepted is True and ws.subprotocol == "bearer"
    assert ws.sent == [{"type": "pong"}]


async def test_ws_accepts_token_via_first_message(accepted_pair):
    from api.v1.websockets import connection_websocket

    buyer, _s, conn_id, _ = accepted_pair
    ws = FakeWS(incoming=[{"type": "auth", "token": buyer.tokens["access_token"]}, {"type": "ping"}])
    await connection_websocket(ws, uuid.UUID(conn_id), token=None)
    assert ws.accepted is True and ws.closed_code is None
    assert ws.sent == [{"type": "pong"}]


async def test_ws_first_message_must_be_valid_auth(accepted_pair):
    from api.v1.websockets import connection_websocket

    buyer, _s, conn_id, _ = accepted_pair
    ws = FakeWS(incoming=[{"type": "ping"}])
    await connection_websocket(ws, uuid.UUID(conn_id), token=None)
    assert ws.closed_code == 1008 and ws.sent == []
    ws2 = FakeWS(incoming=[{"type": "auth", "token": buyer.tokens["refresh_token"]}])
    await connection_websocket(ws2, uuid.UUID(conn_id), token=None)
    assert ws2.closed_code == 1008


# ---------------------------------------------------------------- moderation


async def test_moderation_check_requires_auth(client):
    r = await client.post("/moderation/check", json={"title": "steel bolts"})
    assert r.status_code == 401


async def test_moderation_regex_is_linear_time():
    from services.moderation_service import AIContentModerator

    text = "sell\n" + ("a " * 2490) + "1\n"
    best = min(
        _timed(lambda: AIContentModerator.check_text(text)) for _ in range(3)
    )
    assert best < 0.05, f"took {best * 1000:.1f} ms"


def _timed(fn) -> float:
    t = time.perf_counter()
    fn()
    return time.perf_counter() - t


async def test_moderation_endpoint_handles_adversarial_payload(make_actor):
    a = await make_actor()
    t = time.perf_counter()
    r = await a.post("/moderation/check", json={"title": "sell", "description": ("a " * 2490) + "1"})
    assert r.status_code == 200
    assert time.perf_counter() - t < 0.5


async def test_moderation_check_is_rate_limited(make_actor, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_MODERATION", "3/60")
    a = await make_actor()
    codes = [(await a.post("/moderation/check", json={"title": "bolts"})).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


@pytest.mark.parametrize(
    "text",
    ["handgun for sale", "Uzi submachine guns", "9 mm handguns wholesale", "gun metal grey rifles", "9mm ammo"],
)
async def test_moderation_catches_obvious_weapons(make_actor, text):
    a = await make_actor()
    r = await a.post("/moderation/check", json={"title": text})
    assert r.json()["is_safe"] is False, text


@pytest.mark.parametrize(
    "text",
    [
        "gun metal grey powder coating 500 kg",
        "shotgun microphone for film studios",
        "bomb calorimeter for labs",
        "hot melt glue guns for packaging",
        "9mm plywood sheets",
        "lavender bath bombs",
    ],
)
async def test_moderation_no_false_positive_on_industrial_goods(make_actor, text):
    a = await make_actor()
    r = await a.post("/moderation/check", json={"title": text})
    assert r.json()["is_safe"] is True, f"{text!r} blocked: {r.json()['flagged_terms']}"


# ---------------------------------------------------------------- image moderation


def _ollama_reply(payload: dict):
    resp = MagicMock(status_code=200)
    resp.json.return_value = payload
    return resp


@pytest.mark.parametrize(
    "reply,fail_closed,expected_safe",
    [
        ({"response": "I'm sorry, I can't help with this image."}, True, False),
        ({"response": "I'm sorry, I can't help with this image."}, False, True),
        ({"response": '{"category": null}'}, True, False),  # no verdict
        ({"response": '{"flagged": "maybe"}'}, True, False),
        ({"response": '{"flagged": "false", "category": null, "reason": ""}'}, True, True),
        ({"response": '{"flagged": "true", "category": "violence", "reason": "x"}'}, False, False),
        ({"response": '{"flagged": false}'}, True, True),
        ({"response": '{"flagged": true, "category": "sexual"}'}, True, False),
    ],
)
async def test_image_moderation_verdict_parsing(reply, fail_closed, expected_safe):
    from services.image_moderator import inspect_live_image

    with (
        patch.object(settings, "IMAGE_MODERATION_ENABLED", True),
        patch.object(settings, "IMAGE_MODERATION_FAIL_CLOSED", fail_closed),
        patch("httpx.AsyncClient.post", return_value=_ollama_reply(reply)),
    ):
        safe, _msg, _ = await inspect_live_image(b"\xff\xd8\xffxx", "image/jpeg")
    assert safe is expected_safe
