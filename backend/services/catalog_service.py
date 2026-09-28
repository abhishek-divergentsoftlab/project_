"""Marketplace Catalog Service: public procurement catalog and faceted search."""

from datetime import UTC, datetime
from decimal import Decimal
import math
import re
from typing import Any, Optional, Sequence
import uuid

from sqlalchemy import Float, and_, case, cast, false, func, literal, not_, or_, select
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
from services.categories import category_variants, normalize_category
from services.match_scoring import haversine_km


BOILERPLATE_SEARCH_WORDS = {
    "role", "category", "product", "quantity", "target", "price", "location",
    "deadline", "attributes", "notes", "buyer", "seller", "units"
}


# A search box query is short. Every token becomes an AND-ed group of five
# regex scans, so an unbounded query (2,000 words took 1.9 s) is a cheap DoS.
MAX_QUERY_CHARS = 200
MAX_QUERY_TOKENS = 10
EARTH_RADIUS_KM = 6371.0
KM_PER_DEGREE_LAT = 111.32


def clean_text_param(value: Optional[str], max_chars: int = MAX_QUERY_CHARS) -> Optional[str]:
    """Strip NUL bytes (Postgres rejects them -> 500) and bound the length."""
    if value is None:
        return None
    cleaned = value.replace("\x00", "").strip()[:max_chars].strip()
    return cleaned or None


def escape_like(value: str) -> str:
    """Escape LIKE/ILIKE wildcards so user input matches literally ('%' is not 'anything')."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _contains_ci(column: Any, value: str) -> Any:
    """``column ILIKE '%value%'`` with the value's wildcards escaped (trigram-index friendly)."""
    return column.ilike(f"%{escape_like(value)}%", escape="\\")


def _finite_price(value: Optional[float]) -> Optional[Decimal]:
    """Price bound as a Decimal, ignoring NaN/inf and negatives (the API already 422s them)."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return Decimal(str(number))


def _category_condition(category: str) -> Any:
    """Match a category filter the way the facets group: normalized.

    New rows are normalized on write; legacy rows are matched through the
    known spellings of the canonical name, plus a literal substring match.
    """
    canonical = normalize_category(category)
    variants = category_variants(canonical) | {category.lower()}
    return or_(
        func.lower(func.trim(RFQ.category)).in_(sorted(variants)),
        _contains_ci(RFQ.category, category),
        _contains_ci(RFQ.category, canonical),
    )


def _haversine_sql(lat_col: Any, lon_col: Any, ref_lat: float, ref_lon: float) -> Any:
    """Great-circle distance in km as a SQL expression (same formula as haversine_km)."""
    lat = cast(lat_col, Float)
    lon = cast(lon_col, Float)
    d_lat = func.radians(lat - ref_lat)
    d_lon = func.radians(lon - ref_lon)
    a = func.power(func.sin(d_lat / 2), 2) + (
        math.cos(math.radians(ref_lat))
        * func.cos(func.radians(lat))
        * func.power(func.sin(d_lon / 2), 2)
    )
    # least(): float rounding can push a hair above 1 and asin() would raise.
    return 2 * EARTH_RADIUS_KM * func.asin(func.sqrt(func.least(literal(1.0), a)))


def _within_radius_sql(lat_col: Any, lon_col: Any, ref_lat: float, ref_lon: float, radius_km: float) -> Any:
    """Bounding-box prefilter (index/planner friendly) AND the exact haversine."""
    d_lat = radius_km / KM_PER_DEGREE_LAT
    conds = [
        lat_col.is_not(None),
        lon_col.is_not(None),
        lat_col >= ref_lat - d_lat,
        lat_col <= ref_lat + d_lat,
    ]
    cos_lat = math.cos(math.radians(ref_lat))
    if cos_lat > 1e-6:
        d_lon = radius_km / (KM_PER_DEGREE_LAT * cos_lat)
        # Skip the longitude box where it would wrap the antimeridian.
        if d_lon < 180 and -180 <= ref_lon - d_lon and ref_lon + d_lon <= 180:
            conds += [lon_col >= ref_lon - d_lon, lon_col <= ref_lon + d_lon]
    conds.append(_haversine_sql(lat_col, lon_col, ref_lat, ref_lon) <= radius_km)
    return and_(*conds)


def _token_regex(keys: Sequence[str]) -> Optional[str]:
    """POSIX regex matching any gazetteer key as a whole token (see _normalised_place)."""
    parts = [re.escape(k.replace("-", " ")) for k in keys if k]
    if not parts:
        return None
    return " (" + "|".join(sorted(parts, key=len, reverse=True)) + ") "


def _normalised_place(column: Any) -> Any:
    """SQL twin of locations.find_location's text cleanup: lower-case, every
    run of non-alphanumerics becomes one space, padded with spaces."""
    return (
        literal(" ")
        + func.regexp_replace(func.lower(func.coalesce(column, "")), "[^[:alnum:]]+", " ", "g")
        + literal(" ")
    )


def radius_condition(ref_lat: float, ref_lon: float, radius_km: float) -> Any:
    """``WHERE`` clause for "listing within radius_km of (ref_lat, ref_lon)".

    Mirrors the coordinate fallback used to *display* a listing's position, so
    the filter, the total and the pages agree (the filter used to run in Python
    after LIMIT/OFFSET and COUNT):

    1. the RFQ's own latitude/longitude;
    2. else a gazetteer city/region named in ``location_city`` (then
       ``location_state``) -- resolved here by pre-computing which gazetteer
       entries fall inside the radius;
    3. else the owner's profile coordinates.
    """
    has_coords = and_(RFQ.latitude.is_not(None), RFQ.longitude.is_not(None))
    own = and_(has_coords, _within_radius_sql(RFQ.latitude, RFQ.longitude, ref_lat, ref_lon, radius_km))

    gazetteer = locations._UNIFIED_LOCATIONS  # noqa: SLF001 - read-only lookup table
    nearby_keys = [
        key
        for key, loc in gazetteer.items()
        if haversine_km(ref_lat, ref_lon, loc.latitude, loc.longitude) <= radius_km
    ]
    all_regex = _token_regex(list(gazetteer))
    nearby_regex = _token_regex(nearby_keys)

    city_text = _normalised_place(RFQ.location_city)
    state_text = _normalised_place(RFQ.location_state)
    city_known = city_text.op("~")(all_regex) if all_regex else false()
    state_known = state_text.op("~")(all_regex) if all_regex else false()

    clauses = [own]
    if nearby_regex:
        clauses.append(and_(not_(has_coords), city_text.op("~")(nearby_regex)))
        clauses.append(
            and_(not_(has_coords), not_(city_known), state_text.op("~")(nearby_regex))
        )
    clauses.append(
        and_(
            not_(has_coords),
            not_(city_known),
            not_(state_known),
            UserProfile.latitude != 0,
            UserProfile.longitude != 0,
            _within_radius_sql(UserProfile.latitude, UserProfile.longitude, ref_lat, ref_lon, radius_km),
        )
    )
    return or_(*clauses)


def build_search_conditions(q: str) -> list[Any]:
    """Tokenize search phrase and produce word-boundary matching clauses.

    Prevents false-positive matches (such as 'rice' matching the substring 'price'
    in the standard 'Target price:' metadata block). The query is capped at
    ``MAX_QUERY_CHARS`` characters and ``MAX_QUERY_TOKENS`` tokens.
    """
    q = clean_text_param(q) or ""
    raw_terms = [
        w.strip()
        for w in re.split(r"[^\w\+\-\./]+", q.strip())
        if w.strip()
        and w.lower() not in ("in", "at", "from", "for", "the", "a", "an", "to", "with", "and", "or", "of", "on", "by", "is")
        and (len(w.strip()) >= 2 or w.isalnum())
    ]
    if not raw_terms:
        term = q.strip()
        raw_terms = [term] if term else []
    # Dedupe (case-insensitively) and cap the number of AND-ed token groups.
    seen: set[str] = set()
    unique_terms = []
    for term in raw_terms:
        if term.lower() not in seen:
            seen.add(term.lower())
            unique_terms.append(term)
    raw_terms = unique_terms[:MAX_QUERY_TOKENS]

    token_conditions = []
    for t in raw_terms:
        escaped = re.escape(t)
        pattern = rf"\m{escaped}" if re.match(r"^\w", t) else escaped
        clauses = [
            RFQ.title.op("~*")(pattern),
            RFQ.category.op("~*")(pattern),
            RFQ.description.op("~*")(pattern),
            RFQ.location_city.op("~*")(pattern),
        ]
        if t.lower() not in BOILERPLATE_SEARCH_WORDS:
            clauses.append(RFQ.search_text.op("~*")(pattern))
        token_conditions.append(or_(*clauses))

    return token_conditions


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

    category = clean_text_param(category, 120)
    city = clean_text_param(city, 120)
    country = clean_text_param(country, 120)
    currency = clean_text_param(currency, 64)

    if category:
        conditions.append(_category_condition(category))

    if city:
        conditions.append(_contains_ci(RFQ.location_city, city))

    if country:
        conditions.append(_contains_ci(RFQ.location_country, country))

    if currency:
        conditions.append(RFQ.price_currency == currency.upper())

    min_bound = _finite_price(min_price)
    if min_bound is not None:
        conditions.append(RFQ.price_amount >= min_bound)

    max_bound = _finite_price(max_price)
    if max_bound is not None:
        conditions.append(RFQ.price_amount <= max_bound)

    if q and q.strip():
        search_conds = build_search_conditions(q)
        if search_conds:
            conditions.append(and_(*search_conds))

    # Viewer coordinates: the reference point for distance and radius.
    viewer_lat = getattr(viewer.profile, "latitude", None) if viewer.profile else None
    viewer_lon = getattr(viewer.profile, "longitude", None) if viewer.profile else None
    ref_lat = lat if lat is not None else viewer_lat
    ref_lon = lon if lon is not None else viewer_lon

    if radius_km is not None:
        # Applied in SQL so COUNT, LIMIT and OFFSET see the same rows.
        if ref_lat is None or ref_lon is None:
            conditions.append(false())
        else:
            conditions.append(radius_condition(float(ref_lat), float(ref_lon), float(radius_km)))

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

        # radius_km is applied in SQL (radius_condition) so totals and pages agree.

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
            Quantity.from_stored(rfq.quantity_value, rfq.quantity_unit)
            if rfq.quantity_value is not None
            else None
        )
        min_order = (
            Quantity.from_stored(rfq.min_order_value, rfq.min_order_unit)
            if rfq.min_order_value is not None
            else None
        )
        price_target = (
            Money.from_stored(rfq.price_amount, rfq.price_currency, rfq.price_per_unit)
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


async def get_categories_summary(
    db: AsyncSession,
    viewer: Optional[User] = None,
    *,
    q: Optional[str] = None,
    city: Optional[str] = None,
    country: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    currency: Optional[str] = None,
    verified_only: bool = False,
    saved_only: bool = False,
) -> list[CategoryCountOut]:
    """Return top categories with active listing counts matching catalog search filters."""
    now = datetime.now(UTC)

    # Normalize category casing and whitespace to eliminate duplicate entries
    norm_category = func.initcap(func.trim(RFQ.category))

    conditions = [
        RFQ.status == RFQStatus.ACTIVE,
        or_(RFQ.expires_at.is_(None), RFQ.expires_at > now),
        RFQ.category.is_not(None),
        func.length(func.trim(RFQ.category)) > 0,
    ]

    if viewer:
        conditions.append(RFQ.user_id != viewer.id)

    if saved_only and viewer:
        saved_subquery = select(SavedRFQ.rfq_id).where(SavedRFQ.user_id == viewer.id)
        conditions.append(RFQ.id.in_(saved_subquery))

    city = clean_text_param(city, 120)
    country = clean_text_param(country, 120)
    currency = clean_text_param(currency, 64)

    if city:
        conditions.append(_contains_ci(RFQ.location_city, city))

    if country:
        conditions.append(_contains_ci(RFQ.location_country, country))

    if currency:
        conditions.append(RFQ.price_currency == currency.upper())

    min_bound = _finite_price(min_price)
    if min_bound is not None:
        conditions.append(RFQ.price_amount >= min_bound)

    max_bound = _finite_price(max_price)
    if max_bound is not None:
        conditions.append(RFQ.price_amount <= max_bound)

    if q and q.strip():
        search_conds = build_search_conditions(q)
        if search_conds:
            conditions.append(and_(*search_conds))

    query = select(
        norm_category.label("category"),
        func.count(RFQ.id).label("total_count"),
        func.count(case((RFQ.role == RFQRole.SELLER, 1))).label("seller_count"),
        func.count(case((RFQ.role == RFQRole.BUYER, 1))).label("buyer_count"),
    )

    if verified_only:
        query = query.join(User, RFQ.user_id == User.id).outerjoin(
            UserProfile, User.id == UserProfile.user_id
        )
        conditions.append(
            or_(
                UserProfile.kyc_status == KYCStatus.VERIFIED,
                UserProfile.trust_score >= 60,
            )
        )

    query = (
        query.where(and_(*conditions))
        .group_by(norm_category)
        .order_by(func.count(RFQ.id).desc())
    )

    rows = (await db.execute(query)).all()

    # Merge spellings that normalize to one canonical category ("electronic",
    # "Electronics") -- rows written before normalization-on-write existed.
    merged: dict[str, list[int]] = {}
    for row in rows:
        name = normalize_category(row.category)
        counts = merged.setdefault(name, [0, 0, 0])
        counts[0] += row.total_count
        counts[1] += row.seller_count
        counts[2] += row.buyer_count

    ordered = sorted(merged.items(), key=lambda item: (-item[1][0], item[0]))[:50]
    return [
        CategoryCountOut(
            category=name,
            total_count=counts[0],
            seller_count=counts[1],
            buyer_count=counts[2],
        )
        for name, counts in ordered
    ]


async def get_catalog_item(
    db: AsyncSession,
    viewer: User,
    rfq_id: uuid.UUID,
) -> Optional[CatalogItemOut]:
    """Retrieve a single RFQ listing with full counterparty trust data.

    Only publicly listed RFQs (active and not past ``expires_at``) are served,
    unless the viewer owns the RFQ or already has a connection about it (a deal
    room must keep working after the listing is closed). Anything else --
    another user's draft, a closed or an expired listing -- is ``None`` (404),
    indistinguishable from an id that does not exist.
    """
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

    # Check connection between viewer and this RFQ
    conn_query = select(Connection).where(
        Connection.rfq_id == rfq.id,
        or_(
            Connection.sender_id == viewer.id,
            Connection.receiver_id == viewer.id,
        ),
    )
    conn = (await db.scalars(conn_query)).first()

    now = datetime.now(UTC)
    expires_at = rfq.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    publicly_listed = rfq.status == RFQStatus.ACTIVE and (expires_at is None or expires_at > now)
    if not publicly_listed and rfq.user_id != viewer.id and conn is None:
        return None

    # Load owner's verified certs
    certs_query = select(Certificate.name).where(
        Certificate.user_id == rfq.user_id,
        Certificate.verification_status == CertificationStatus.VERIFIED,
    )
    verified_certs = list((await db.scalars(certs_query)).all())

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
        Quantity.from_stored(rfq.quantity_value, rfq.quantity_unit)
        if rfq.quantity_value is not None
        else None
    )
    min_order = (
        Quantity.from_stored(rfq.min_order_value, rfq.min_order_unit)
        if rfq.min_order_value is not None
        else None
    )
    price_target = (
        Money.from_stored(rfq.price_amount, rfq.price_currency, rfq.price_per_unit)
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

