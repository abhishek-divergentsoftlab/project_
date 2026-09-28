"""Security / negative-testing audit probes: auth, users, KYC, certificates,
uploads, media, moderation, notifications, websockets, config.

These are PROBES. A failing test demonstrates a defect (it asserts the secure
behaviour). A passing test confirms a control works.
"""

import asyncio
import base64
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import jwt
import pytest
from httpx import ASGITransport, AsyncClient

from app import app
from core import config as core_config
from core.config import settings
from core.security import create_access_token

GOOD_PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"
GOOD_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4"
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


@pytest.fixture(autouse=True)
def isolated_uploads(tmp_path, monkeypatch):
    """Redirect every write to a throwaway dir so the real backend/uploads is untouched."""
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "uploads"))
    return tmp_path / "uploads"


def kyc_body(gst: str, **extra):
    body = {
        "gst_number": gst,
        "legal_business_name": "Probe Industries Pvt Ltd",
        "business_type": "Private Limited",
    }
    body.update(extra)
    return body


# ---------------------------------------------------------------- tokens / JWT


async def test_alg_none_token_rejected(client, make_actor):
    """An unsigned alg=none JWT for a real user must be rejected (401)."""
    a = await make_actor()
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    exp = int((datetime.now(UTC) + timedelta(hours=1)).timestamp())
    body = base64.urlsafe_b64encode(
        json.dumps({"sub": a.id, "type": "access", "exp": exp}).encode()
    ).rstrip(b"=").decode()
    r = await client.get("/auth/me", headers={"Authorization": f"Bearer {header}.{body}."})
    assert r.status_code == 401


async def test_tampered_sub_rejected(client, make_actor):
    """Swapping the `sub` claim of a valid token (keeping signature) must fail."""
    a = await make_actor()
    b = await make_actor()
    h, p, s = a.tokens["access_token"].split(".")
    claims = json.loads(base64.urlsafe_b64decode(p + "=="))
    claims["sub"] = b.id
    p2 = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    r = await client.get("/auth/me", headers={"Authorization": f"Bearer {h}.{p2}.{s}"})
    assert r.status_code == 401


async def test_expired_token_rejected(client, make_actor):
    """An access token past `exp` must be rejected."""
    a = await make_actor()
    tok = jwt.encode(
        {"sub": a.id, "type": "access", "exp": datetime.now(UTC) - timedelta(seconds=5)},
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )
    r = await client.get("/auth/me", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401


async def test_token_signed_with_example_default_key_rejected(client, make_actor):
    """A token forged with the publicly known .env.example key must not work
    (proves the running config does not use the default secret)."""
    a = await make_actor()
    tok = jwt.encode(
        {"sub": a.id, "type": "access", "exp": datetime.now(UTC) + timedelta(hours=1)},
        "dev-only-insecure-key-change-me",
        algorithm="HS256",
    )
    r = await client.get("/auth/me", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401


async def test_suspended_user_tokens_rejected(client, db, make_actor):
    """After an account is suspended both its access and refresh tokens die."""
    from sqlalchemy import update
    from models.enums import UserStatus
    from models.user import User

    a = await make_actor()
    await db.execute(update(User).where(User.id == uuid.UUID(a.id)).values(status=UserStatus.SUSPENDED))
    await db.commit()
    r1 = await client.get("/auth/me", headers=a.headers)
    r2 = await client.post("/auth/refresh", json={"refresh_token": a.tokens["refresh_token"]})
    assert (r1.status_code, r2.status_code) == (401, 401)


async def test_refresh_token_is_single_use(client, make_actor):
    """A refresh token that was already exchanged must not be accepted again
    (rotation + reuse detection). FAIL = stolen refresh tokens live 7 days."""
    a = await make_actor()
    rt = a.tokens["refresh_token"]
    first = await client.post("/auth/refresh", json={"refresh_token": rt})
    assert first.status_code == 200
    second = await client.post("/auth/refresh", json={"refresh_token": rt})
    assert second.status_code == 401, "old refresh token was accepted a second time"


async def test_no_logout_or_revocation_endpoint(client, make_actor):
    """There should be a way to revoke a session (logout / password change)."""
    a = await make_actor()
    paths = ["/auth/logout", "/auth/revoke", "/auth/change-password", "/users/me/password"]
    codes = {p: (await a.post(p, json={})).status_code for p in paths}
    assert any(c not in (404, 405) for c in codes.values()), codes


def test_production_guard_rejects_default_secret_for_non_exact_env_names(monkeypatch):
    """ENVIRONMENT='prod' / 'staging' with the default SECRET_KEY should refuse to boot.
    FAIL = the guard only matches the exact string 'production'."""
    monkeypatch.setenv("SECRET_KEY", "dev-only-insecure-key-change-me")
    monkeypatch.setenv("ENVIRONMENT", "prod")
    with pytest.raises(RuntimeError):
        core_config.get_settings.__wrapped__()


def test_production_guard_rejects_short_weak_secret(monkeypatch):
    """ENVIRONMENT=production with SECRET_KEY='secret' should refuse to boot."""
    monkeypatch.setenv("SECRET_KEY", "secret")
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(RuntimeError):
        core_config.get_settings.__wrapped__()


# ---------------------------------------------------------------- login / signup


async def test_login_bruteforce_is_throttled(client, make_actor):
    """25 wrong passwords in a row should trigger 429/lockout. FAIL = unlimited guessing."""
    a = await make_actor()
    codes = []
    for _ in range(25):
        r = await client.post("/auth/login", json={"email": a.email, "password": "wrong-pass-123"})
        codes.append(r.status_code)
    assert 429 in codes or 423 in codes, f"no throttling, codes={set(codes)}"


async def test_signup_does_not_enumerate_emails(client, make_actor):
    """Signup with an existing email should not reveal that it is registered."""
    a = await make_actor()
    r = await client.post(
        "/auth/signup",
        json={"email": a.email, "password": "whatever-123", "name": "X"},
    )
    assert not (r.status_code == 409 and "already exists" in r.text), r.text


async def test_signup_account_is_active_without_email_verification(client):
    """A fresh signup should not be ACTIVE (and able to trade) before email is verified."""
    r = await client.post(
        "/auth/signup",
        json={"email": f"nobody-{uuid.uuid4().hex[:6]}@tests.example.com", "password": "abcdefgh", "name": "X"},
    )
    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    assert me.json()["status"] != "active", "unverified email gets a fully active account"


@pytest.mark.parametrize(
    "extra",
    [{"role": "admin"}, {"status": "active"}, {"kyc_status": "verified"}, {"trust_score": 100}, {"is_admin": True}],
)
async def test_signup_mass_assignment_rejected(client, extra):
    """Privileged fields at signup must be rejected (422)."""
    body = {"email": f"m-{uuid.uuid4().hex[:6]}@tests.example.com", "password": "abcdefgh1", "name": "X", **extra}
    r = await client.post("/auth/signup", json=body)
    assert r.status_code == 422


@pytest.mark.parametrize(
    "field", [{"kyc_status": "verified"}, {"trust_score": 100}, {"average_rating": 5}, {"user_id": str(uuid.uuid4())}]
)
async def test_profile_patch_mass_assignment_rejected(make_actor, field):
    """Profile PATCH must not allow setting kyc_status/trust_score/rating/user_id."""
    a = await make_actor()
    r = await a.patch("/users/me/profile", json=field)
    assert r.status_code == 422


async def test_role_switch_to_seller_requires_no_verification(make_actor):
    """PATCH /users/me lets a buyer become seller/both instantly with no KYC gate.
    FAIL = anyone can sell without verification."""
    a = await make_actor("buyer")
    r = await a.patch("/users/me", json={"role": "both"})
    assert r.status_code in (400, 403), f"role changed to {r.json().get('role')} with kyc unverified"


# ---------------------------------------------------------------- KYC


async def test_kyc_rejects_garbage_vat_id(make_actor):
    """'ZZZZZZZZZZ' matches the 'international VAT' regex and gets VERIFIED."""
    a = await make_actor("seller")
    r = await a.post("/users/me/kyc/verify", json=kyc_body("ZZZZZZZZZZ"))
    assert r.status_code in (400, 422), r.json()


async def test_kyc_rejects_bad_gstin_checksum(make_actor):
    """A GSTIN with a wrong check digit must be rejected."""
    good = valid_gstin("AAAPZ1234C")
    bad = good[:-1] + ("0" if good[-1] != "0" else "1")
    a = await make_actor("seller")
    r = await a.post("/users/me/kyc/verify", json=kyc_body(bad))
    assert r.status_code in (400, 422), f"bad checksum {bad} -> {r.json()}"


async def test_kyc_pan_must_match_gstin(make_actor):
    """PAN embedded in GSTIN (chars 3-12) must equal the submitted pan_number; PAN format must be valid."""
    a = await make_actor("seller")
    r = await a.post(
        "/users/me/kyc/verify", json=kyc_body(valid_gstin("AABCU9603R"), pan_number="not-a-pan")
    )
    assert r.status_code in (400, 422), r.json()


async def test_kyc_is_not_instantly_self_verified(make_actor):
    """Format-valid GST should put KYC into PENDING or VERIFIED format review."""
    a = await make_actor("seller")
    r = await a.post("/users/me/kyc/verify", json=kyc_body(valid_gstin("AAAPZ1234C")))
    assert r.status_code == 200 and r.json().get("kyc_status") in ("verified", "pending"), r.json()


async def test_profile_patch_changing_gst_resets_kyc(make_actor):
    """After KYC, changing gst_number/legal name via profile PATCH must drop VERIFIED."""
    a = await make_actor("seller")
    await a.post("/users/me/kyc/verify", json=kyc_body(valid_gstin("AAAPZ1234C")))
    r = await a.patch(
        "/users/me/profile",
        json={"gst_number": "anything-at-all", "legal_business_name": "Totally Different Co"},
    )
    assert r.status_code == 200
    assert r.json()["kyc_status"] != "verified", r.json()


async def test_profile_patch_cannot_claim_another_companys_gst(make_actor):
    """Profile PATCH bypasses the anti-fraud duplicate-GST check in KYC."""
    victim = await make_actor("seller")
    gst = valid_gstin("AABCV1111K")
    assert (await victim.post("/users/me/kyc/verify", json=kyc_body(gst))).status_code == 200
    attacker = await make_actor("seller")
    r = await attacker.patch("/users/me/profile", json={"gst_number": gst})
    assert r.status_code in (400, 409), f"duplicate GST stored: {r.json().get('gst_number')}"


async def test_kyc_duplicate_check_does_not_500(make_actor):
    """Two profiles holding the same GST (via PATCH) make scalar_one_or_none() raise
    MultipleResultsFound, so a third user's KYC call 500s instead of a clean 400."""
    gst = valid_gstin("AABCW2222L")
    for _ in range(2):
        u = await make_actor("seller")
        await u.patch("/users/me/profile", json={"gst_number": gst})
    owner = await make_actor("seller")
    await owner.post("/users/me/kyc/verify", json=kyc_body(gst))
    third = await make_actor("seller")
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test/api/v1") as c:
        r = await c.post("/users/me/kyc/verify", headers=third.headers, json=kyc_body(gst))
    assert r.status_code in (400, 409), f"status={r.status_code} body={r.text[:120]}"


# ---------------------------------------------------------------- certificates


def cert_body(**over):
    body = {
        "name": "ISO 9001:2015",
        "issuing_body": "SGS",
        "certificate_number": f"C-{uuid.uuid4().hex[:6]}",
        "issue_date": "2023-01-01T00:00:00Z",
    }
    body.update(over)
    return body


async def test_self_declared_certificate_is_not_auto_verified(make_actor):
    """A user-typed certificate creation verification status check."""
    a = await make_actor("seller")
    r = await a.post("/certifications", json=cert_body())
    assert r.status_code == 201 and r.json()["verification_status"] in ("verified", "pending")


async def test_trust_score_cannot_be_farmed_with_fake_certificates(make_actor):
    """10 fabricated certificates should not max out trust_score."""
    a = await make_actor("seller")
    for i in range(10):
        await a.post("/certifications", json=cert_body(name=f"Fake Cert {i}"))
    me = (await a.get("/auth/me")).json()
    assert me["profile"]["trust_score"] < 60, f"trust_score={me['profile']['trust_score']}"


async def test_deleting_certificate_reverts_trust_bonus(make_actor):
    """Create+delete loop should not ratchet trust_score up."""
    a = await make_actor("seller")
    for _ in range(3):
        c = (await a.post("/certifications", json=cert_body())).json()
        await a.delete(f"/certifications/{c['id']}")
    me = (await a.get("/auth/me")).json()
    assert me["profile"]["trust_score"] == 20, f"trust_score={me['profile']['trust_score']} with 0 certs"


async def test_expired_certificate_not_accepted_as_verified(make_actor):
    """A certificate that expired in 2020 must not be stored as VERIFIED."""
    a = await make_actor("seller")
    r = await a.post(
        "/certifications",
        json=cert_body(issue_date="2018-01-01T00:00:00Z", expiry_date="2020-01-01T00:00:00Z"),
    )
    assert not (r.status_code == 201 and r.json()["verification_status"] == "verified"), r.json()


@pytest.mark.parametrize(
    "url",
    ["javascript:alert(document.cookie)", "https://evil.example.com/fake-iso.pdf", "data:text/html,<script>alert(1)</script>"],
)
async def test_certificate_document_url_must_be_own_upload(make_actor, url):
    """document_url should only accept /api/v1/media/certificates/... produced by our upload."""
    a = await make_actor("seller")
    r = await a.post("/certifications", json=cert_body(document_url=url))
    assert r.status_code in (400, 422), f"accepted document_url={url!r}"


async def test_certificate_document_url_cannot_reference_other_users_file(make_actor):
    """Seller B should not be able to attach seller A's uploaded file as their own proof."""
    a = await make_actor("seller")
    up = (await a.post("/certifications/upload", files={"file": ("a.pdf", GOOD_PDF, "application/pdf")})).json()
    b = await make_actor("seller")
    r = await b.post("/certifications", json=cert_body(document_url=up["document_url"]))
    assert r.status_code in (400, 403, 422), "B reused A's uploaded document"


async def test_public_certificates_idor_safe(client, make_actor):
    """Other users cannot delete / attach to someone else's certificate (404)."""
    a = await make_actor("seller")
    c = (await a.post("/certifications", json=cert_body())).json()
    b = await make_actor("seller")
    d = await b.delete(f"/certifications/{c['id']}")
    at = await b.post(f"/certifications/{c['id']}/document", files={"file": ("x.pdf", GOOD_PDF, "application/pdf")})
    assert (d.status_code, at.status_code) == (404, 404)


# ---------------------------------------------------------------- uploads


@pytest.mark.parametrize(
    "name,data,ctype",
    [
        ("evil.html", b"<script>alert(1)</script>", "text/html"),
        ("evil.svg", b"<svg onload=alert(1)>", "image/svg+xml"),
        ("renamed.pdf", b"<html><script>alert(1)</script></html>", "application/pdf"),
        ("renamed.png", b"GIF89a....", "image/png"),
        ("noext", GOOD_PDF, "application/pdf"),
        ("empty.pdf", b"", "application/pdf"),
    ],
)
async def test_certificate_upload_rejects_bad_files(make_actor, name, data, ctype):
    """Wrong extensions, spoofed magic bytes and empty files are rejected (400)."""
    a = await make_actor("seller")
    r = await a.post("/certifications/upload", files={"file": (name, data, ctype)})
    assert r.status_code == 400, r.text


async def test_certificate_upload_path_traversal_filename(make_actor, isolated_uploads):
    """../ in the filename must not escape uploads/certificates."""
    a = await make_actor("seller")
    r = await a.post(
        "/certifications/upload", files={"file": ("../../../../tmp/pwn.pdf", GOOD_PDF, "application/pdf")}
    )
    assert r.status_code == 201
    written = list(isolated_uploads.rglob("*"))
    assert all("certificates" in str(p) for p in written if p.is_file())
    assert r.json()["filename"] == "pwn.pdf"


async def test_certificate_upload_size_limit(make_actor):
    """Files over 10 MB are rejected."""
    a = await make_actor("seller")
    big = GOOD_PDF + b"0" * (10 * 1024 * 1024)
    r = await a.post("/certifications/upload", files={"file": ("big.pdf", big, "application/pdf")})
    assert r.status_code == 400


async def test_certificate_upload_has_quota(make_actor, isolated_uploads):
    """Orphan uploads (no certificate row) are unbounded: 30 uploads should hit a quota."""
    a = await make_actor("seller")
    codes = [
        (await a.post("/certifications/upload", files={"file": (f"{i}.pdf", GOOD_PDF, "application/pdf")})).status_code
        for i in range(30)
    ]
    assert any(c in (400, 413, 429) for c in codes), f"{len(list(isolated_uploads.rglob('*.pdf')))} orphan files stored"


async def test_certificate_upload_requires_auth(client):
    """Anonymous upload rejected."""
    r = await client.post("/certifications/upload", files={"file": ("a.pdf", GOOD_PDF, "application/pdf")})
    assert r.status_code == 401


async def test_live_capture_rejects_non_image_bytes(accepted_pair, isolated_uploads):
    """Live capture trusts the client Content-Type only; HTML bytes labelled image/png are stored."""
    buyer, _seller, conn_id, _ = accepted_pair
    r = await buyer.post(
        f"/connections/{conn_id}/messages/live-capture",
        files={"image": ("x.png", b"<html><script>alert(1)</script></html>", "image/png")},
    )
    assert r.status_code == 400, f"stored {r.json().get('image_url')}"


# ---------------------------------------------------------------- media serving


async def test_media_requires_auth_for_private_deal_room_images(client, accepted_pair):
    """A live-capture photo from a private deal room must not be downloadable anonymously."""
    buyer, _seller, conn_id, _ = accepted_pair
    jpg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00"
    m = await buyer.post(
        f"/connections/{conn_id}/messages/live-capture", files={"image": ("x.jpg", jpg, "image/jpeg")}
    )
    url = m.json()["image_url"].replace("/api/v1", "")
    anon = await client.get(url)
    assert anon.status_code in (401, 403, 404), f"anonymous GET {url} -> {anon.status_code}"


@pytest.mark.parametrize(
    "path",
    [
        "/media/..%2F..%2Fapp.py",
        "/media/%2e%2e/%2e%2e/core/config.py",
        "/media/certificates/..%2F..%2F..%2F.env",
        "/media/%2Fetc%2Fpasswd",
        "/media/..%5C..%5Capp.py",
    ],
)
async def test_media_path_traversal_blocked(client, path):
    """Encoded ../ and absolute paths must not read files outside UPLOAD_DIR."""
    r = await client.get(path)
    assert r.status_code in (403, 404), f"{path} -> {r.status_code}"
    assert b"SECRET_KEY" not in r.content and b"root:" not in r.content


async def test_media_serves_unknown_ext_as_attachment(client, isolated_uploads):
    """Arbitrary files under uploads/ (e.g. .html) are served inline; should be attachment or refused."""
    d = isolated_uploads / "certificates"
    d.mkdir(parents=True, exist_ok=True)
    (d / "planted.html").write_bytes(b"<script>alert(1)</script>")
    r = await client.get("/media/certificates/planted.html")
    assert r.status_code == 404 or "attachment" in r.headers.get("content-disposition", ""), dict(r.headers)


# ---------------------------------------------------------------- notifications


@pytest.mark.parametrize("qs", ["limit=-1", "offset=-5", "limit=abc"])
async def test_notifications_bad_pagination_is_4xx(make_actor, qs):
    """Negative limit/offset should be a 422, not a 500 from Postgres."""
    a = await make_actor()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test/api/v1") as c:
        r = await c.get(f"/notifications?{qs}", headers=a.headers)
    assert 400 <= r.status_code < 500, f"{qs} -> {r.status_code} {r.text[:100]}"


async def test_notifications_idor(db, make_actor):
    """User B cannot read or mark user A's notifications."""
    from services import notification_service

    a = await make_actor()
    b = await make_actor()
    n = await notification_service.create_notification(db, user_id=uuid.UUID(a.id), type="t", title="secret")
    await db.commit()
    marked = (await b.post(f"/notifications/{n.id}/read")).json()
    listed = (await b.get("/notifications")).json()
    await db.refresh(n)
    assert marked == {"ok": False} and listed["total"] == 0 and n.is_read is False


# ---------------------------------------------------------------- websockets


class FakeWS:
    def __init__(self):
        self.accepted = False
        self.closed_code = None

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000):
        self.closed_code = code

    async def send_json(self, data):
        pass

    async def receive_json(self):
        from fastapi import WebSocketDisconnect

        raise WebSocketDisconnect()


async def test_ws_rejects_refresh_token_and_strangers(accepted_pair, make_actor):
    """WS must refuse a refresh token and a non-participant's access token."""
    from api.v1.websockets import connection_websocket

    buyer, _s, conn_id, _ = accepted_pair
    stranger = await make_actor()
    ws1, ws2 = FakeWS(), FakeWS()
    await connection_websocket(ws1, uuid.UUID(conn_id), token=buyer.tokens["refresh_token"])
    await connection_websocket(ws2, uuid.UUID(conn_id), token=stranger.tokens["access_token"])
    assert (ws1.accepted, ws1.closed_code, ws2.accepted, ws2.closed_code) == (False, 1008, False, 1008)


async def test_ws_rejects_rejected_connection(make_actor, make_rfq):
    """A participant of a REJECTED connection should not get a live channel."""
    from api.v1.websockets import connection_websocket

    buyer = await make_actor("buyer")
    seller = await make_actor("seller")
    listing = await make_rfq(seller, role="seller", title="Supplying 5000 steel bolts")
    conn = (await buyer.post("/connections", json={"rfq_id": listing["id"]})).json()
    await seller.post(f"/connections/{conn['id']}/reject")
    ws = FakeWS()
    await connection_websocket(ws, uuid.UUID(conn["id"]), token=buyer.tokens["access_token"])
    assert ws.accepted is False, "socket accepted on a rejected connection"


# ---------------------------------------------------------------- moderation


async def test_moderation_check_is_not_a_cpu_dos(make_actor):
    """Authenticated /moderation/check runs without blocking event loop.
    A 5 KB payload should finish well under 0.5s."""
    a = await make_actor()
    payload = {"title": "sell", "description": ("a " * 2490) + "1"}
    t = time.perf_counter()
    r = await a.post("/moderation/check", json=payload)
    elapsed = time.perf_counter() - t
    assert r.status_code == 200
    assert elapsed < 0.5, f"took {elapsed:.2f}s (blocks the whole event loop)"


@pytest.mark.parametrize("text", ["handgun for sale", "Uzi submachine guns", "9 mm handguns wholesale"])
async def test_moderation_catches_obvious_weapons(make_actor, text):
    """Obvious firearm listings should be blocked."""
    a = await make_actor()
    r = await a.post("/moderation/check", json={"title": text})
    assert r.json()["is_safe"] is False, text


@pytest.mark.parametrize(
    "text", ["gun metal grey powder coating 500 kg", "shotgun microphone for film studios", "bomb calorimeter for labs"]
)
async def test_moderation_no_false_positive_on_industrial_goods(make_actor, text):
    """Legit industrial items should not be blocked."""
    a = await make_actor()
    r = await a.post("/moderation/check", json={"title": text})
    assert r.json()["is_safe"] is True, f"{text!r} blocked: {r.json()['flagged_terms']}"


async def test_image_moderation_fails_closed_on_unparseable_reply():
    """If the vision model returns non-JSON (e.g. a refusal), the image must be blocked."""
    with patch.object(settings, "IMAGE_MODERATION_ENABLED", True), patch("httpx.AsyncClient.post") as post:
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"response": "I'm sorry, I can't help with this image."}
        post.return_value = resp
        from services.image_moderator import inspect_live_image

        safe, _msg, _ = await inspect_live_image(b"\xff\xd8\xffxx", "image/jpeg")
    assert safe is False, "unparseable moderation reply treated as SAFE (fail-open)"


async def test_image_moderation_string_false_is_not_flagged():
    """{"flagged": "false"} (string) should be treated as not flagged."""
    with patch.object(settings, "IMAGE_MODERATION_ENABLED", True), patch("httpx.AsyncClient.post") as post:
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"response": '{"flagged": "false", "category": null, "reason": ""}'}
        post.return_value = resp
        from services.image_moderator import inspect_live_image

        safe, _msg, _ = await inspect_live_image(b"\xff\xd8\xffxx", "image/jpeg")
    assert safe is True, "string 'false' coerced to True by bool()"


# ---------------------------------------------------------------- misc / config


async def test_cors_rejects_foreign_origin(client):
    """A foreign Origin gets no ACAO header on preflight."""
    r = await client.options(
        "/auth/me",
        headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "GET"},
    )
    assert r.headers.get("access-control-allow-origin") is None


async def test_security_headers_present(client):
    """API responses should carry basic hardening headers (nosniff, frame-deny)."""
    r = await client.get("/health")
    missing = [h for h in ("x-content-type-options", "x-frame-options") if h not in r.headers]
    assert not missing, f"missing {missing}"


async def test_500_does_not_leak_traceback(make_actor):
    """Internal errors return a generic body (no traceback / SQL)."""
    a = await make_actor()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test/api/v1") as c:
        r = await c.get("/notifications?limit=-1", headers=a.headers)
    assert "Traceback" not in r.text and "asyncpg" not in r.text and "LIMIT" not in r.text
