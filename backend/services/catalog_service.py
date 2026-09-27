"""Marketplace Catalog Service: public procurement catalog and faceted search."""

from datetime import UTC, datetime
from decimal import Decimal
import re
from typing import Optional, Sequence
import uuid

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.certificate import Certificate
from models.connection import Connection
from models.enums import CertificationStatus, ConnectionStatus, KYCStatus, RFQRole, RFQStatus
from models.rfq import RFQ
from models.saved_rfq import SavedRFQ
from models.user import User, UserProfile
from schemas.common import DeadlineOut, Location, Money, Quantity
from schemas.marketplace import (
    CatalogCounterpartyOut,
    CatalogItemOut,
    CatalogListOut,
    CategoryCountOut,
)
from services import locations
from services.match_scoring import haversine_km


async def get_catalog(
    db: AsyncSession,
    viewer: User,
    *,
    q: Optional[str] = None,
    role: Optional[RFQRole] = None,
    category: Optional[str] = None,
    city: Optional[str] = None,
    country: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    currency: Optional[str] = None,
    verified_only: bool = False,
    saved_only: bool = False,
    sort_by: str = "newest",
    limit: int = 24,
    offset: int = 0,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    radius_km: Optional[float] = None,
) -> CatalogListOut:
    """Retrieve filtered, paginated catalog items with counterparty trust metadata."""
    now = datetime.now(UTC)

    # Base conditions: active, unexpired, and exclude caller's own RFQs
    conditions = [
        RFQ.status == RFQStatus.ACTIVE,
        or_(RFQ.expires_at.is_(None), RFQ.expires_at > now),
        RFQ.user_id != viewer.id,
    ]

    if role:
        conditions.append(RFQ.role == role)

    if saved_only:
        saved_subquery = select(SavedRFQ.rfq_id).where(SavedRFQ.user_id == viewer.id)
        conditions.append(RFQ.id.in_(saved_subquery))

    if category and category.strip():
        cat_clean = category.strip()
        conditions.append(
            or_(
                func.lower(func.trim(RFQ.category)) == cat_clean.lower(),
                RFQ.category.ilike(f"%{cat_clean}%"),
            )
        )

    if city and city.strip():
        conditions.append(RFQ.location_city.ilike(f"%{city.strip()}%"))

    if country and country.strip():
        conditions.append(RFQ.location_country.ilike(f"%{country.strip()}%"))

    if currency and currency.strip():
        conditions.append(RFQ.price_currency == currency.strip().upper())

    if min_price is not None:
        conditions.append(RFQ.price_amount >= Decimal(str(min_price)))

    if max_price is not None:
        conditions.append(RFQ.price_amount <= Decimal(str(max_price)))

    if q and q.strip():
        # Tokenize search phrase so compound queries like 'office chair in mumbai'
        # match across product title and location/category seamlessly.
        raw_terms = [
            w.strip()
            for w in re.split(r"[^\w\+\-\./]+", q.strip())
            if w.strip()
            and w.lower() not in ("in", "at", "from", "for", "the", "a", "an", "to", "with", "and", "or", "of", "on", "by", "is")
            and (len(w.strip()) >= 2 or w.isalnum())
        ]
        if raw_terms:
            token_conditions = []
            for t in raw_terms:
                term = f"%{t}%"
                token_conditions.append(
                    or_(
                        RFQ.title.ilike(term),
                        RFQ.category.ilike(term),
                        RFQ.description.ilike(term),
                        RFQ.search_text.ilike(term),
                        RFQ.location_city.ilike(term),
                    )
                )
            conditions.append(and_(*token_conditions))
        else:
            term = f"%{q.strip()}%"
            conditions.append(
                or_(
                    RFQ.title.ilike(term),
                    RFQ.category.ilike(term),
                    RFQ.description.ilike(term),
                    RFQ.search_text.ilike(term),
                    RFQ.location_city.ilike(term),
                )
            )

    # Base query
    base_query = (
        select(RFQ)
        .join(User, RFQ.user_id == User.id)
        .outerjoin(UserProfile, User.id == UserProfile.user_id)
        .options(selectinload(RFQ.user).selectinload(User.profile))
    )

    if verified_only:
        conditions.append(
            or_(
                UserProfile.kyc_status == KYCStatus.VERIFIED,
                UserProfile.trust_score >= 60,
            )
        )

    base_query = base_query.where(and_(*conditions))

    # Total count query
    count_query = (
        select(func.count(RFQ.id))
        .join(User, RFQ.user_id == User.id)
        .outerjoin(UserProfile, User.id == UserProfile.user_id)
        .where(and_(*conditions))
    )
    total_result = await db.scalar(count_query)
    total = total_result or 0

    # Sorting
    if sort_by == "price_asc":
        base_query = base_query.order_by(RFQ.price_amount.asc().nullslast(), RFQ.created_at.desc())
    elif sort_by == "price_desc":
        base_query = base_query.order_by(RFQ.price_amount.desc().nullslast(), RFQ.created_at.desc())
    elif sort_by == "trust_desc":
        base_query = base_query.order_by(
            UserProfile.trust_score.desc().nullslast(), RFQ.created_at.desc()
        )
    else:  # "newest"
        base_query = base_query.order_by(RFQ.created_at.desc())

    # Pagination
    base_query = base_query.limit(limit).offset(offset)
    rfqs = (await db.scalars(base_query)).all()

    if not rfqs:
        return CatalogListOut(items=[], total=total, limit=limit, offset=offset)

    rfq_ids = [r.id for r in rfqs]
    owner_user_ids = list({r.user_id for r in rfqs})

    # Batch load connection status between viewer and these RFQs
    connections_query = select(Connection).where(
        or_(
            and_(Connection.sender_id == viewer.id, Connection.rfq_id.in_(rfq_ids)),
            and_(Connection.receiver_id == viewer.id, Connection.rfq_id.in_(rfq_ids)),
        )
    )
    connections_rows = (await db.scalars(connections_query)).all()
    conn_map: dict[uuid.UUID, Connection] = {c.rfq_id: c for c in connections_rows}

    # Batch load verified certificates for counterparty users
    certs_query = select(Certificate).where(
        Certificate.user_id.in_(owner_user_ids),
        Certificate.verification_status == CertificationStatus.VERIFIED,
    )
    certs_rows = (await db.scalars(certs_query)).all()
    certs_map: dict[uuid.UUID, list[str]] = {}
    for cert in certs_rows:
        certs_map.setdefault(cert.user_id, []).append(cert.name)

    # Batch load saved status for viewer
    saved_query = select(SavedRFQ.rfq_id).where(
        SavedRFQ.user_id == viewer.id,
        SavedRFQ.rfq_id.in_(rfq_ids),
    )
    saved_rfq_ids = set((await db.scalars(saved_query)).all())

    # Viewer coordinates for distance computation
    viewer_lat = getattr(viewer.profile, "latitude", None) if viewer.profile else None
    viewer_lon = getattr(viewer.profile, "longitude", None) if viewer.profile else None

    items: list[CatalogItemOut] = []
    for rfq in rfqs:
        owner_user = rfq.user
        owner_profile = owner_user.profile if owner_user else None

        # Check connection status
        conn = conn_map.get(rfq.id)
        conn_id = conn.id if conn else None
        conn_status = conn.status.value if conn else None
        conn_direction = (
            ("sent" if conn.sender_id == viewer.id else "received") if conn else None
        )

        # Coordinate resolution with smart fallback so listings appear on map
        loc_lat = rfq.latitude
        loc_lon = rfq.longitude
        if (loc_lat is None or loc_lon is None) and (rfq.location_city or rfq.location_state):
            loc_candidate = locations.find_location(rfq.location_city or "") or locations.find_location(rfq.location_state or "")
            if loc_candidate:
                loc_lat = locations.as_decimal(loc_candidate.latitude)
                loc_lon = locations.as_decimal(loc_candidate.longitude)
        if (loc_lat is None or loc_lon is None) and owner_profile and owner_profile.latitude and owner_profile.longitude:
            loc_lat = owner_profile.latitude
            loc_lon = owner_profile.longitude

        # Reference coordinate for distance calculation (passed lat/lon or viewer's profile)
        ref_lat = lat if lat is not None else viewer_lat
        ref_lon = lon if lon is not None else viewer_lon

        # Distance calculation
        dist_km: Optional[float] = None
        if (
            ref_lat is not None
            and ref_lon is not None
            and loc_lat is not None
            and loc_lon is not None
        ):
            try:
                raw_dist = haversine_km(
                    float(ref_lat),
                    float(ref_lon),
                    float(loc_lat),
                    float(loc_lon),
                )
                dist_km = round(raw_dist, 1)
            except Exception:
                dist_km = None

        # Filter by radius if requested
        if radius_km is not None:
            if dist_km is None or dist_km > radius_km:
                continue

        company_name = (
            (owner_profile.company_name or owner_profile.name)
            if owner_profile
            else "Enterprise Trader"
        ) or "Enterprise Trader"

        counterparty_out = CatalogCounterpartyOut(
            company_name=company_name,
            contact_name=owner_profile.name if owner_profile else None,
            city=rfq.location_city or (owner_profile.city if owner_profile else None),
            country=rfq.location_country or (owner_profile.country if owner_profile else None),
            kyc_status=owner_profile.kyc_status if owner_profile else KYCStatus.UNVERIFIED,
            trust_score=owner_profile.trust_score if owner_profile else 20,
            verified_certs=certs_map.get(rfq.user_id, []),
            connection_id=conn_id,
            connection_status=conn_status,
            connection_direction=conn_direction,
        )

        quantity = (
            Quantity(value=float(rfq.quantity_value), unit=rfq.quantity_unit or "units")
            if rfq.quantity_value is not None
            else None
        )
        min_order = (
            Quantity(value=float(rfq.min_order_value), unit=rfq.min_order_unit or "units")
            if rfq.min_order_value is not None
            else None
        )
        price_target = (
            Money(
                amount=float(rfq.price_amount),
                currency=rfq.price_currency,
                per_unit=rfq.price_per_unit,
            )
            if rfq.price_amount is not None
            else None
        )
        location = Location(
            city=rfq.location_city,
            state=rfq.location_state,
            country=rfq.location_country,
            latitude=loc_lat,
            longitude=loc_lon,
            raw=rfq.location_raw,
        )
        deadline = (
            DeadlineOut(
                date=rfq.deadline_at.isoformat() if rfq.deadline_at else None,
                raw=rfq.deadline_raw,
            )
            if rfq.deadline_at or rfq.deadline_raw
            else None
        )

        items.append(
            CatalogItemOut(
                id=rfq.id,
                user_id=rfq.user_id,
                role=rfq.role,
                category=rfq.category,
                title=rfq.title,
                description=rfq.description,
                quantity=quantity,
                minimum_order=min_order,
                price_target=price_target,
                location=location,
                deadline=deadline,
                product_details=rfq.product_details or {},
                search_tags=rfq.search_tags or [],
                created_at=rfq.created_at,
                counterparty=counterparty_out,
                distance_km=dist_km,
                is_saved=rfq.id in saved_rfq_ids,
            )
        )

    return CatalogListOut(items=items, total=total, limit=limit, offset=offset)


async def get_categories_summary(db: AsyncSession) -> list[CategoryCountOut]:
    """Return top categories with active listing counts across market sides."""
    now = datetime.now(UTC)

    # Normalize category casing and whitespace to eliminate duplicate entries
    norm_category = func.initcap(func.trim(RFQ.category))

    query = (
        select(
            norm_category.label("category"),
            func.count(RFQ.id).label("total_count"),
            func.count(case((RFQ.role == RFQRole.SELLER, 1))).label("seller_count"),
            func.count(case((RFQ.role == RFQRole.BUYER, 1))).label("buyer_count"),
        )
        .where(
            RFQ.status == RFQStatus.ACTIVE,
            or_(RFQ.expires_at.is_(None), RFQ.expires_at > now),
            RFQ.category.is_not(None),
            func.length(func.trim(RFQ.category)) > 0,
        )
        .group_by(norm_category)
        .order_by(func.count(RFQ.id).desc())
        .limit(20)
    )

    rows = (await db.execute(query)).all()
    return [
        CategoryCountOut(
            category=row.category,
            total_count=row.total_count,
            seller_count=row.seller_count,
            buyer_count=row.buyer_count,
        )
        for row in rows
    ]


async def get_catalog_item(
    db: AsyncSession,
    viewer: User,
    rfq_id: uuid.UUID,
) -> Optional[CatalogItemOut]:
    """Retrieve a single active or accessible RFQ listing with full counterparty trust data."""
    query = (
        select(RFQ)
        .options(
            selectinload(RFQ.user).selectinload(User.profile),
        )
        .where(RFQ.id == rfq_id)
    )
    rfq = (await db.scalars(query)).first()
    if not rfq:
        return None

    # Load owner's verified certs
    certs_query = select(Certificate.name).where(
        Certificate.user_id == rfq.user_id,
        Certificate.verification_status == CertificationStatus.VERIFIED,
    )
    verified_certs = list((await db.scalars(certs_query)).all())

    # Check connection between viewer and this RFQ
    conn_query = select(Connection).where(
        Connection.rfq_id == rfq.id,
        or_(
            Connection.sender_id == viewer.id,
            Connection.receiver_id == viewer.id,
        ),
    )
    conn = (await db.scalars(conn_query)).first()
    conn_id = conn.id if conn else None
    conn_status = conn.status.value if conn else None
    conn_direction = (
        ("sent" if conn.sender_id == viewer.id else "received") if conn else None
    )

    # Check saved status
    saved_query = select(SavedRFQ.rfq_id).where(
        SavedRFQ.user_id == viewer.id,
        SavedRFQ.rfq_id == rfq.id,
    )
    is_saved = bool((await db.scalars(saved_query)).first())

    # Coordinates and distance
    viewer_lat = getattr(viewer.profile, "latitude", None) if viewer.profile else None
    viewer_lon = getattr(viewer.profile, "longitude", None) if viewer.profile else None

    loc_lat = rfq.latitude
    loc_lon = rfq.longitude
    owner_profile = rfq.user.profile if rfq.user else None
    if (loc_lat is None or loc_lon is None) and (rfq.location_city or rfq.location_state):
        loc_candidate = locations.find_location(rfq.location_city or "") or locations.find_location(rfq.location_state or "")
        if loc_candidate:
            loc_lat = locations.as_decimal(loc_candidate.latitude)
            loc_lon = locations.as_decimal(loc_candidate.longitude)
    if (loc_lat is None or loc_lon is None) and owner_profile and owner_profile.latitude and owner_profile.longitude:
        loc_lat = owner_profile.latitude
        loc_lon = owner_profile.longitude

    dist_km: Optional[float] = None
    if viewer_lat is not None and viewer_lon is not None and loc_lat is not None and loc_lon is not None:
        try:
            raw_dist = haversine_km(
                float(viewer_lat),
                float(viewer_lon),
                float(loc_lat),
                float(loc_lon),
            )
            dist_km = round(raw_dist, 1)
        except Exception:
            dist_km = None

    company_name = (
        (owner_profile.company_name or owner_profile.name)
        if owner_profile
        else "Enterprise Trader"
    ) or "Enterprise Trader"

    counterparty_out = CatalogCounterpartyOut(
        company_name=company_name,
        contact_name=owner_profile.name if owner_profile else None,
        city=rfq.location_city or (owner_profile.city if owner_profile else None),
        country=rfq.location_country or (owner_profile.country if owner_profile else None),
        kyc_status=owner_profile.kyc_status if owner_profile else KYCStatus.UNVERIFIED,
        trust_score=owner_profile.trust_score if owner_profile else 20,
        verified_certs=verified_certs,
        connection_id=conn_id,
        connection_status=conn_status,
        connection_direction=conn_direction,
    )

    quantity = (
        Quantity(value=float(rfq.quantity_value), unit=rfq.quantity_unit or "units")
        if rfq.quantity_value is not None
        else None
    )
    min_order = (
        Quantity(value=float(rfq.min_order_value), unit=rfq.min_order_unit or "units")
        if rfq.min_order_value is not None
        else None
    )
    price_target = (
        Money(
            amount=float(rfq.price_amount),
            currency=rfq.price_currency,
            per_unit=rfq.price_per_unit,
        )
        if rfq.price_amount is not None
        else None
    )
    location = Location(
        city=rfq.location_city,
        state=rfq.location_state,
        country=rfq.location_country,
        latitude=loc_lat,
        longitude=loc_lon,
        raw=rfq.location_raw,
    )
    deadline = (
        DeadlineOut(
            date=rfq.deadline_at.isoformat() if rfq.deadline_at else None,
            raw=rfq.deadline_raw,
        )
        if rfq.deadline_at or rfq.deadline_raw
        else None
    )

    return CatalogItemOut(
        id=rfq.id,
        user_id=rfq.user_id,
        role=rfq.role,
        category=rfq.category,
        title=rfq.title,
        description=rfq.description,
        quantity=quantity,
        minimum_order=min_order,
        price_target=price_target,
        location=location,
        deadline=deadline,
        product_details=rfq.product_details or {},
        search_tags=rfq.search_tags or [],
        created_at=rfq.created_at,
        counterparty=counterparty_out,
        distance_km=dist_km,
        is_saved=is_saved,
    )

