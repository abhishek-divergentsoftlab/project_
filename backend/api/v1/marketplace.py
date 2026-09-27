"""Marketplace catalog and sourcing endpoints."""

from typing import Annotated, Optional
import uuid

from fastapi import APIRouter, HTTPException, Query, status

from api.deps import CurrentUser, DbSession
from models.enums import RFQRole
from schemas.marketplace import CatalogItemOut, CatalogListOut, CategoryCountOut
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
    saved_only: Annotated[bool, Query(description="Filter saved listings only")] = False,
    sort_by: Annotated[
        str,
        Query(
            pattern="^(newest|price_asc|price_desc|trust_desc)$",
            description="Sort order",
        ),
    ] = "newest",
    limit: Annotated[int, Query(ge=1, le=100)] = 24,
    offset: Annotated[int, Query(ge=0)] = 0,
    lat: Annotated[Optional[float], Query(ge=-90, le=90, description="Reference latitude for radius and distance")] = None,
    lon: Annotated[Optional[float], Query(ge=-180, le=180, description="Reference longitude for radius and distance")] = None,
    radius_km: Annotated[Optional[float], Query(gt=0, description="Max distance in kilometers")] = None,
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
        saved_only=saved_only,
        sort_by=sort_by,
        limit=limit,
        offset=offset,
        lat=lat,
        lon=lon,
        radius_km=radius_km,
    )


@router.get("/categories", response_model=list[CategoryCountOut])
async def get_marketplace_categories(
    current_user: CurrentUser,
    db: DbSession,
) -> list[CategoryCountOut]:
    """Retrieve top marketplace categories and active listing counts."""
    return await catalog_service.get_categories_summary(db)


@router.get("/catalog/{rfq_id}", response_model=CatalogItemOut)
async def get_marketplace_listing(
    rfq_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
) -> CatalogItemOut:
    """Retrieve details of a single marketplace listing to inspect or connect with."""
    item = await catalog_service.get_catalog_item(db, current_user, rfq_id)
    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="RFQ listing not found or is no longer available in the marketplace.",
        )
    return item

