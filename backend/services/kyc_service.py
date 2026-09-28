"""Company Verification and Anti-Fraud KYC Service.

What "verified" means here
--------------------------
There is no GST-registry (GSTN) lookup and no admin review queue yet, so the
checks are *format-level*: an Indian GSTIN must have the official 15-character
structure, a valid state code, a PAN-shaped body and a correct check character
(the GSTN mod-36 algorithm); a PAN, when given, must be well-formed and equal
the PAN embedded in the GSTIN. That rejects typos and made-up numbers, but it
cannot prove the number belongs to the submitter -- the response message says
so. Plugging a registry lookup in belongs in ``verify_company_kyc`` after the
format checks.

Trust score
-----------
``trust_score`` is always *computed* from current facts
(``recompute_trust_score``) and never incremented, so deleting a certificate,
changing the GST number or letting a certificate expire takes the bonus away
again.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.enums import CertificationStatus, KYCStatus
from models.user import UserProfile
from schemas.kyc import KYCVerificationPayload

# Valid Indian State/UT GST prefixes (01 - 38, 97 other territory, 99 centre jurisdiction)
VALID_GST_STATES = {f"{i:02d}" for i in range(1, 39)} | {"97", "99"}

GSTIN_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
# 2 state digits, 10-char PAN, entity number (1-9, A-Z), 'Z', check char.
GSTIN_REGEX = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
PAN_REGEX = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
# 4th PAN character is the holder type: Company, Person, HUF, Firm, AOP,
# Trust, BOI, Local authority, artificial Juridical person, Government.
PAN_HOLDER_TYPES = set("CPHFATBLJG")

INDIA_NAMES = {"india", "in", "ind", "bharat", "republic of india"}

# Country name / code -> ISO 3166 alpha-2.
_COUNTRY_ALIASES: dict[str, str] = {
    "germany": "DE", "deutschland": "DE", "france": "FR", "italy": "IT", "spain": "ES",
    "netherlands": "NL", "the netherlands": "NL", "holland": "NL", "belgium": "BE",
    "austria": "AT", "poland": "PL", "sweden": "SE", "denmark": "DK", "finland": "FI",
    "ireland": "IE", "portugal": "PT", "united kingdom": "GB", "uk": "GB",
    "great britain": "GB", "england": "GB", "switzerland": "CH",
    "united arab emirates": "AE", "uae": "AE", "saudi arabia": "SA", "ksa": "SA",
    "australia": "AU", "singapore": "SG", "china": "CN", "prc": "CN",
    "united states": "US", "united states of america": "US", "usa": "US", "us": "US",
    "canada": "CA", "japan": "JP", "bangladesh": "BD", "sri lanka": "LK", "nepal": "NP",
    "vietnam": "VN", "viet nam": "VN", "indonesia": "ID", "malaysia": "MY", "thailand": "TH",
    "turkey": "TR", "turkiye": "TR", "south africa": "ZA", "brazil": "BR", "mexico": "MX",
}

# Per-country tax-id shapes. EU-style VAT numbers may be written with or
# without their country prefix, so the prefix is optional in each pattern.
_TAX_PATTERNS: dict[str, re.Pattern[str]] = {
    "DE": re.compile(r"^(?:DE)?[0-9]{9}$"),
    "FR": re.compile(r"^(?:FR)?[0-9A-HJ-NP-Z]{2}[0-9]{9}$"),
    "IT": re.compile(r"^(?:IT)?[0-9]{11}$"),
    "ES": re.compile(r"^(?:ES)?[0-9A-Z][0-9]{7}[0-9A-Z]$"),
    "NL": re.compile(r"^(?:NL)?[0-9]{9}B[0-9]{2}$"),
    "BE": re.compile(r"^(?:BE)?[01][0-9]{9}$"),
    "AT": re.compile(r"^(?:AT)?U[0-9]{8}$"),
    "PL": re.compile(r"^(?:PL)?[0-9]{10}$"),
    "SE": re.compile(r"^(?:SE)?[0-9]{10}01$"),
    "DK": re.compile(r"^(?:DK)?[0-9]{8}$"),
    "FI": re.compile(r"^(?:FI)?[0-9]{8}$"),
    "IE": re.compile(r"^(?:IE)?(?:[0-9]{7}[A-W][A-I]?|[0-9][A-Z+*][0-9]{5}[A-W])$"),
    "PT": re.compile(r"^(?:PT)?[0-9]{9}$"),
    "GB": re.compile(r"^(?:GB)?(?:[0-9]{9}|[0-9]{12}|GD[0-4][0-9]{2}|HA[5-9][0-9]{2})$"),
    "CH": re.compile(r"^CHE-?[0-9]{3}\.?[0-9]{3}\.?[0-9]{3}(?:MWST|TVA|IVA)?$"),
    "AE": re.compile(r"^(?:AE)?100[0-9]{12}$"),  # 15-digit TRN starts with 100
    "SA": re.compile(r"^(?:SA)?3[0-9]{13}3$"),  # 15-digit VAT, starts and ends with 3
    "AU": re.compile(r"^(?:AU)?[0-9]{11}$"),  # ABN
    "SG": re.compile(r"^(?:[0-9]{8,9}[A-Z]|[TSR][0-9]{2}[A-Z]{2}[0-9]{4}[A-Z]|M[0-9]{8}[A-Z])$"),  # UEN / GST reg no
    "CN": re.compile(r"^[0-9A-HJ-NPQRTUWXY]{2}[0-9]{6}[0-9A-HJ-NPQRTUWXY]{10}$"),  # USCC, 18 chars
    "US": re.compile(r"^[0-9]{2}-?[0-9]{7}$"),  # EIN
    "CA": re.compile(r"^[0-9]{9}(?:RT[0-9]{4})?$"),  # Business Number
    "JP": re.compile(r"^T?[0-9]{13}$"),  # Qualified invoice issuer number
}

GENERIC_TAX_REGEX = re.compile(r"^[A-Z0-9][A-Z0-9\-./]{5,19}$")


class KYCError(Exception):
    """Domain-level rejection for KYC and anti-fraud checks.

    ``status_code`` is 422 for input that fails validation and 409 when the
    tax id is already claimed by another account.
    """

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------- validation


def normalise_tax_id(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", "", value).upper()
    return cleaned or None


def gstin_check_char(first14: str) -> str:
    """Official GSTN check character (mod-36, alternating weights 1 and 2)."""
    total = 0
    for index, char in enumerate(first14):
        product = GSTIN_CHARSET.index(char) * (1 if index % 2 == 0 else 2)
        total += product // 36 + product % 36
    return GSTIN_CHARSET[(36 - total % 36) % 36]


def validate_pan(pan: str) -> tuple[bool, str]:
    if not PAN_REGEX.match(pan):
        return False, "Invalid PAN format. Expected 10 characters like AABCU9603R (5 letters, 4 digits, 1 letter)"
    if pan[3] not in PAN_HOLDER_TYPES:
        return False, f"Invalid PAN: 4th character '{pan[3]}' is not a recognised holder type"
    return True, "Valid PAN"


def validate_gstin(gstin: str) -> tuple[bool, str]:
    if len(gstin) != 15 or not GSTIN_REGEX.match(gstin):
        return False, "Invalid GSTIN format. Expected 15 characters (e.g. 27AABCU9603R1ZN)"
    state_code = gstin[:2]
    if state_code not in VALID_GST_STATES:
        return False, f"Invalid GSTIN state code '{state_code}'"
    ok, reason = validate_pan(gstin[2:12])
    if not ok:
        return False, f"Invalid GSTIN: embedded PAN is malformed ({reason})"
    expected = gstin_check_char(gstin[:14])
    if gstin[14] != expected:
        return False, "Invalid GSTIN: check character does not match (the number contains a typo or is not genuine)"
    return True, "Valid GSTIN"


def is_gstin_shaped(value: str) -> bool:
    return bool(GSTIN_REGEX.match(value))


def _looks_like_garbage(value: str) -> bool:
    body = re.sub(r"[^A-Z0-9]", "", value)
    if len(set(body)) <= 2:
        return True  # ZZZZZZZZZZ, 0000000000, ABABABAB
    digits = [c for c in body if c.isdigit()]
    if len(digits) >= 6 and "".join(digits) in "01234567890123456789" + "98765432109876543210":
        return True  # 123456789 style sequences
    return False


def country_code(country: Optional[str]) -> Optional[str]:
    if not country:
        return None
    cleaned = country.strip().lower()
    if cleaned in INDIA_NAMES:
        return "IN"
    if cleaned in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[cleaned]
    if len(cleaned) == 2 and cleaned.isalpha():
        return cleaned.upper()
    return "??"  # a named, non-Indian country we have no pattern for


def validate_tax_id(tax_id: str, country: Optional[str] = None) -> tuple[bool, str, bool]:
    """Validate a GSTIN or, for a clearly non-Indian business, a foreign tax id.

    Returns ``(ok, reason, strong)``. ``strong`` is True when the id passed a
    checksum or a country-specific pattern; a generic-pattern foreign id is
    accepted but only for review (PENDING), never for VERIFIED.
    """
    cleaned = normalise_tax_id(tax_id) or ""
    if not cleaned:
        return False, "Tax / GST number cannot be empty", False

    code = country_code(country)

    # A GSTIN-shaped id is judged as a GSTIN whatever the country.
    if is_gstin_shaped(cleaned):
        ok, reason = validate_gstin(cleaned)
        return ok, reason, ok

    if code in (None, "IN"):
        if len(cleaned) == 15:
            ok, reason = validate_gstin(cleaned)
            return ok, reason, ok
        where = "for an Indian business" if code == "IN" else "unless your profile country is outside India"
        return (
            False,
            "Invalid GSTIN format. A 15-character GSTIN (e.g. 27AABCU9603R1ZN) is required "
            f"{where}. Foreign VAT / tax ids are accepted once your business country is set.",
            False,
        )

    if _looks_like_garbage(cleaned):
        return False, "Invalid tax ID: the number is not a plausible registration number", False

    pattern = _TAX_PATTERNS.get(code)
    if pattern is not None:
        if not pattern.match(cleaned):
            return False, f"Invalid tax ID format for country {code}", False
        return True, f"Tax ID format valid for {code}", True

    # Named country with no specific pattern: a generic shape, needs digits.
    if GENERIC_TAX_REGEX.match(cleaned) and sum(c.isdigit() for c in cleaned) >= 5:
        return True, "Tax ID format accepted for review", False
    return False, "Invalid tax ID format. Please provide your registered VAT / tax number", False


# ---------------------------------------------------------------- trust score

KYC_VERIFIED_POINTS = 35
KYC_PENDING_POINTS = 15
CERTIFICATE_POINTS = 5
CERTIFICATE_CAP = 15
REVIEW_CAP = 20


def calculate_trust_score(
    profile: UserProfile,
    *,
    valid_certificates: int = 0,
    review_points: int = 0,
) -> int:
    """Trust score (0 - 100) from the profile's current facts.

    ``valid_certificates`` counts unexpired, non-rejected certificates (+5 each,
    capped at +15); ``review_points`` is the summed review bonus (capped at
    +/-20). Pure function: callers fetch the counts, see ``recompute_trust_score``.
    """
    score = 20  # baseline account creation

    if profile.kyc_status == KYCStatus.VERIFIED:
        score += KYC_VERIFIED_POINTS
    elif profile.kyc_status == KYCStatus.PENDING:
        score += KYC_PENDING_POINTS

    if profile.phone:
        score += 10
    if profile.address and profile.city:
        score += 10
    if profile.website:
        score += 5
    if profile.registration_number or profile.pan_number:
        score += 10
    if profile.year_established and profile.year_established < 2024:
        score += 10

    score += min(CERTIFICATE_CAP, CERTIFICATE_POINTS * max(0, valid_certificates))
    score += max(-REVIEW_CAP, min(REVIEW_CAP, review_points))

    return max(0, min(100, score))


async def recompute_trust_score(db: AsyncSession, profile: UserProfile) -> int:
    """Recompute and assign ``profile.trust_score`` from the database. No commit."""
    from models.certificate import Certificate
    from models.review import Review

    now = datetime.now(UTC)
    valid_certs = await db.scalar(
        select(func.count(Certificate.id)).where(
            Certificate.user_id == profile.user_id,
            Certificate.verification_status != CertificationStatus.REJECTED,
            or_(Certificate.expiry_date.is_(None), Certificate.expiry_date > now),
        )
    )
    # Same per-review weights review_service applies (+5 for 4-5 stars, +1 for 3, -5 below).
    ratings = (await db.scalars(select(Review.rating).where(Review.reviewee_id == profile.user_id))).all()
    review_points = sum(5 if r >= 4 else (1 if r == 3 else -5) for r in ratings)

    profile.trust_score = calculate_trust_score(
        profile, valid_certificates=int(valid_certs or 0), review_points=review_points
    )
    return profile.trust_score


# ---------------------------------------------------------------- duplicates

CLAIMING_STATUSES = (KYCStatus.VERIFIED, KYCStatus.PENDING)


async def gst_claimed_by_other(db: AsyncSession, gst_number: str, user_id: uuid.UUID) -> bool:
    """True when another account has this GST/tax id verified or under review.

    Unverified values typed into a profile do not count, so nobody can squat a
    company's GSTIN just by saving it on their profile. ``.first()`` rather
    than ``scalar_one_or_none()``: several rows may legitimately match.
    """
    row = (
        await db.execute(
            select(UserProfile.user_id)
            .where(
                UserProfile.gst_number == gst_number,
                UserProfile.user_id != user_id,
                UserProfile.kyc_status.in_(CLAIMING_STATUSES),
            )
            .limit(1)
        )
    ).first()
    return row is not None


# ---------------------------------------------------------------- verification


async def verify_company_kyc(
    db: AsyncSession, user_id: uuid.UUID, payload: KYCVerificationPayload
) -> tuple[UserProfile, str]:
    cleaned_gst = normalise_tax_id(payload.gst_number) or ""

    profile = (
        await db.execute(select(UserProfile).where(UserProfile.user_id == user_id))
    ).scalar_one_or_none()
    if profile is None:
        raise KYCError("User profile does not exist", status_code=404)

    if profile.kyc_status == KYCStatus.FLAGGED:
        raise KYCError(
            "This account is flagged for review. Please contact fraud-support.", status_code=403
        )

    country = payload.country if payload.country is not None else profile.country

    # 1. Format validation
    is_valid, reason, strong = validate_tax_id(cleaned_gst, country)
    if not is_valid:
        raise KYCError(reason)

    is_gstin = is_gstin_shaped(cleaned_gst)

    pan = normalise_tax_id(payload.pan_number)
    if pan is not None:
        ok, pan_reason = validate_pan(pan)
        if not ok:
            raise KYCError(pan_reason)
        if is_gstin and pan != cleaned_gst[2:12]:
            raise KYCError(
                "PAN does not match the GSTIN: characters 3-12 of a GSTIN are the holder's PAN "
                f"({cleaned_gst[2:12]})"
            )

    # 2. Anti-fraud: already claimed by another account
    if await gst_claimed_by_other(db, cleaned_gst, user_id):
        raise KYCError(
            "Anti-Fraud Alert: This GST / Tax ID is already registered to another enterprise account. "
            "Please contact fraud-support if you believe this is in error.",
            status_code=409,
        )

    # 3. Update profile details
    profile.gst_number = cleaned_gst
    profile.legal_business_name = payload.legal_business_name.strip()
    profile.business_type = payload.business_type.strip()
    profile.registration_number = payload.registration_number.strip() if payload.registration_number else None
    profile.year_established = payload.year_established
    profile.website = payload.website.strip() if payload.website else None
    profile.pan_number = pan
    profile.signatory_name = payload.signatory_name.strip() if payload.signatory_name else None
    if payload.country is not None and payload.country.strip():
        profile.country = payload.country.strip()

    if strong:
        profile.kyc_status = KYCStatus.VERIFIED
        if is_gstin:
            message = (
                "GSTIN format and checksum verified"
                + (" and PAN matches the GSTIN" if pan else "")
                + ". This is a format-level check; the number has not been confirmed against the GST registry."
            )
        else:
            message = (
                f"{reason}. This is a format-level check; the number has not been confirmed "
                "with the issuing tax authority."
            )
    else:
        profile.kyc_status = KYCStatus.PENDING
        message = "Tax ID format accepted. Verification is pending manual review."

    await recompute_trust_score(db, profile)

    await db.commit()
    await db.refresh(profile)

    return profile, message
