"""Marketplace catalog and sourcing endpoints."""

from typing import Annotated, Optional

from fastapi import APIRouter, Query

from api.deps import CurrentUser, DbSession
from models.enums import RFQRole
from schemas.marketplace import CatalogListOut, CategoryCountOut
from services import catalog_service

router = APIRouter(prefix="/marketplace", tags=["marketplace"])


@router.get("/catalog", response_model=CatalogListOut)
async def get_marketplace_catalog(
    current_user: CurrentUser,
    db: DbSession,
    q: Annotated[Optional[str], Query(description="Search term")] = None,
    role: Annotated[Optional[RFQRole], Query(description="Market side (buyer or seller)")] = None,
    category: Annotated[Optional[str], Query(description="Category filter")] = None,
    city: Annotated[Optional[str], Query(description="City filter")] = None,
    country: Annotated[Optional[str], Query(description="Country filter")] = None,
    min_price: Annotated[Optional[float], Query(ge=0, description="Min target price")] = None,
    max_price: Annotated[Optional[float], Query(ge=0, description="Max target price")] = None,
    currency: Annotated[Optional[str], Query(description="Currency filter (e.g. USD, INR)")] = None,
    verified_only: Annotated[bool, Query(description="Filter KYC-verified or high-trust suppliers")] = False,
    sort_by: Annotated[
        str,
        Query(
            pattern="^(newest|price_asc|price_desc|trust_desc)$",
            description="Sort order",
        ),
    ] = "newest",
    limit: Annotated[int, Query(ge=1, le=100)] = 24,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CatalogListOut:
    """Browse and filter active listings across the wholesale marketplace."""
    return await catalog_service.get_catalog(
        db,
        current_user,
        q=q,
        role=role,
        category=category,
        city=city,
        country=country,
        min_price=min_price,
        max_price=max_price,
        currency=currency,
        verified_only=verified_only,
        sort_by=sort_by,
        limit=limit,
        offset=offset,
    )


@router.get("/categories", response_model=list[CategoryCountOut])
async def get_marketplace_categories(
    current_user: CurrentUser,
    db: DbSession,
) -> list[CategoryCountOut]:
    """Retrieve top marketplace categories and active listing counts."""
    return await catalog_service.get_categories_summary(db)
