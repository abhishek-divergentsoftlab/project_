"""Account and profile endpoints."""

from fastapi import APIRouter, HTTPException, status

from api.deps import CurrentUser, DbSession
from models.enums import KYCStatus, UserRole
from models.user import UserProfile
from schemas.kyc import KYCStatusOut, KYCVerificationPayload
from schemas.user import AccountUpdate, ProfileOut, ProfileUpdate, UserOut
from services import kyc_service, locations
from services.kyc_service import KYCError

router = APIRouter()

KYC_IDENTITY_FIELDS = ("gst_number", "pan_number", "legal_business_name")


def _geocode(profile: UserProfile) -> None:
    """Fill coordinates from the city name, as RFQ writes do.

    Keeps the two sides consistent: a profile city typed by hand should place
    the account on the map the same way a listing city does.
    """
    if profile.latitude is not None and profile.longitude is not None:
        return
    if not profile.city:
        return

    city = locations.find_city(profile.city)
    if city is None:
        return

    stated = (profile.country or "").strip().lower()
    if stated and stated != city.country.lower():
        return

    profile.latitude = locations.as_decimal(city.latitude)
    profile.longitude = locations.as_decimal(city.longitude)
    profile.city = city.name
    if not profile.state:
        profile.state = city.region
    if not profile.country:
        profile.country = city.country


@router.patch("/me", response_model=UserOut)
async def update_account(
    payload: AccountUpdate, current_user: CurrentUser, db: DbSession
) -> UserOut:
    """Change which side of the market this account may post on."""
    if payload.role in (UserRole.SELLER, UserRole.BOTH):
        from models.enums import KYCStatus
        kyc_status = current_user.profile.kyc_status if current_user.profile else None
        if kyc_status != KYCStatus.VERIFIED:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Role switch to seller requires verified KYC status.",
            )
    current_user.role = payload.role
    await db.commit()
    await db.refresh(current_user)
    return UserOut.model_validate(current_user)


@router.patch("/me/profile", response_model=ProfileOut)
async def update_profile(
    payload: ProfileUpdate, current_user: CurrentUser, db: DbSession
) -> ProfileOut:
    profile = current_user.profile
    if profile is None:
        # Possible for accounts created before the profile existed; a PATCH
        # should still succeed rather than 404 on a resource the user owns.
        profile = UserProfile(user_id=current_user.id, name=payload.name or "")
        db.add(profile)

    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes and not (changes["name"] or "").strip():
        # The column is NOT NULL, so clearing the name is an integrity error
        # rather than a stored blank. Say so instead of returning a 500.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "name cannot be empty"
        )

    # KYC-bound identity fields. Changing any of them invalidates a previous
    # verification (otherwise a verified account could swap in another
    # company's GSTIN or name and keep the badge), and a tax id that another
    # account has verified or has under review cannot be claimed here.
    for key in ("gst_number", "pan_number"):
        if key in changes:
            changes[key] = kyc_service.normalise_tax_id(changes[key])
    if "legal_business_name" in changes and changes["legal_business_name"] is not None:
        changes["legal_business_name"] = changes["legal_business_name"].strip() or None

    kyc_changed = any(
        key in changes and changes[key] != getattr(profile, key, None)
        for key in KYC_IDENTITY_FIELDS
    )
    new_gst = changes.get("gst_number")
    if (
        "gst_number" in changes
        and new_gst
        and new_gst != profile.gst_number
        and await kyc_service.gst_claimed_by_other(db, new_gst, current_user.id)
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This GST / Tax ID is already verified by another enterprise account.",
        )

    for field, value in changes.items():
        setattr(profile, field, value)

    if kyc_changed and profile.kyc_status in (KYCStatus.VERIFIED, KYCStatus.PENDING):
        # FLAGGED is deliberately left alone: editing must not clear a flag.
        profile.kyc_status = KYCStatus.UNVERIFIED

    # A new city invalidates everything that was derived from the old one.
    # Without this, moving from Indore to Hamburg left the account in India.
    if "city" in changes:
        for derived in ("latitude", "longitude", "state", "country"):
            if derived not in changes:
                setattr(profile, derived, None)
    _geocode(profile)

    await db.flush()
    await kyc_service.recompute_trust_score(db, profile)
    await db.commit()
    await db.refresh(profile)
    return ProfileOut.model_validate(profile)


@router.post("/me/kyc/verify", response_model=KYCStatusOut, summary="Submit company KYC and GST for verification")
async def verify_kyc(
    payload: KYCVerificationPayload, current_user: CurrentUser, db: DbSession
) -> KYCStatusOut:
    try:
        profile, message = await kyc_service.verify_company_kyc(
            db, user_id=current_user.id, payload=payload
        )
    except KYCError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc

    return KYCStatusOut(
        kyc_status=profile.kyc_status,
        trust_score=profile.trust_score,
        gst_number=profile.gst_number,
        legal_business_name=profile.legal_business_name,
        message=message,
    )
