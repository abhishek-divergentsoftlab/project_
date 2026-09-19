"""Account and profile endpoints."""

from fastapi import APIRouter, HTTPException, status

from api.deps import CurrentUser, DbSession
from models.user import UserProfile
from schemas.user import AccountUpdate, ProfileOut, ProfileUpdate, UserOut
from services import locations

router = APIRouter()


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

    for field, value in changes.items():
        setattr(profile, field, value)

    # A new city invalidates everything that was derived from the old one.
    # Without this, moving from Indore to Hamburg left the account in India.
    if "city" in changes:
        for derived in ("latitude", "longitude", "state", "country"):
            if derived not in changes:
                setattr(profile, derived, None)
    _geocode(profile)

    await db.commit()
    await db.refresh(profile)
    return ProfileOut.model_validate(profile)
