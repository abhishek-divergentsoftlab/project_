"""Company Verification and Anti-Fraud KYC Service."""

import re
import uuid
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.enums import KYCStatus
from models.user import UserProfile
from schemas.kyc import KYCVerificationPayload

# Valid Indian State/UT GST prefixes (01 - 38, 97, 99)
VALID_GST_STATES = {
    f"{i:02d}" for i in range(1, 39)
} | {"97", "99"}

GSTIN_REGEX = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$")
GENERIC_TAX_REGEX = re.compile(r"^[A-Z0-9\-]{6,20}$", re.IGNORECASE)


class KYCError(Exception):
    """Domain-level rejection for KYC and anti-fraud checks."""


def validate_tax_id(tax_id: str) -> tuple[bool, str]:
    """Validates GSTIN or international tax identification number."""
    cleaned = tax_id.strip().upper()
    if not cleaned:
        return False, "Tax / GST number cannot be empty"

    # Check Indian GSTIN (15 chars)
    if len(cleaned) == 15:
        if not GSTIN_REGEX.match(cleaned):
            return False, "Invalid GSTIN format. Expected 15 characters (e.g. 27AABCU9603R1ZM)"
        state_code = cleaned[:2]
        if state_code not in VALID_GST_STATES:
            return False, f"Invalid GST state prefix '{state_code}'"
        return True, "Valid GSTIN"

    # International VAT (2-letter country code + 8-12 digits/letters) or US EIN (XX-XXXXXXX)
    vat_regex = re.compile(r"^[A-Z]{2}[0-9A-Z]{8,12}$")
    ein_regex = re.compile(r"^\d{2}-\d{7}$")
    if vat_regex.match(cleaned) or ein_regex.match(cleaned):
        return True, "Valid International Tax Identifier"

    return False, "Invalid GST / Tax ID format. Please provide a valid 15-character GSTIN or standard VAT/EIN format"


def calculate_trust_score(profile: UserProfile) -> int:
    """Calculates dynamic trust score (0 - 100) based on verified credentials."""
    score = 20  # baseline account creation

    if profile.kyc_status == KYCStatus.VERIFIED:
        score += 35
    elif profile.kyc_status == KYCStatus.PENDING:
        score += 15

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

    return max(0, min(100, score))


async def verify_company_kyc(
    db: AsyncSession, user_id: uuid.UUID, payload: KYCVerificationPayload
) -> tuple[UserProfile, str]:
    cleaned_gst = payload.gst_number.strip().upper()

    # 1. Format validation
    is_valid, reason = validate_tax_id(cleaned_gst)
    if not is_valid:
        raise KYCError(reason)

    # 2. Anti-fraud: Check for duplicate registration by another user
    dup_query = select(UserProfile).where(
        UserProfile.gst_number == cleaned_gst,
        UserProfile.user_id != user_id,
    )
    dup = (await db.execute(dup_query)).scalar_one_or_none()
    if dup is not None:
        raise KYCError(
            "Anti-Fraud Alert: This GST / Tax ID is already registered to another enterprise account. "
            "Please contact fraud-support if you believe this is in error."
        )

    # 3. Fetch current user's profile
    profile_query = select(UserProfile).where(UserProfile.user_id == user_id)
    profile = (await db.execute(profile_query)).scalar_one_or_none()
    if profile is None:
        raise KYCError("User profile does not exist")

    # 4. Update profile details
    profile.gst_number = cleaned_gst
    profile.legal_business_name = payload.legal_business_name.strip()
    profile.business_type = payload.business_type.strip()
    profile.registration_number = payload.registration_number.strip() if payload.registration_number else None
    profile.year_established = payload.year_established
    profile.website = payload.website.strip() if payload.website else None
    profile.pan_number = payload.pan_number.strip().upper() if payload.pan_number else None
    profile.signatory_name = payload.signatory_name.strip() if payload.signatory_name else None

    # Update KYC status to VERIFIED
    profile.kyc_status = KYCStatus.VERIFIED
    profile.trust_score = calculate_trust_score(profile)

    await db.commit()
    await db.refresh(profile)

    return profile, "Company successfully verified and authenticated. Trust badge awarded."
