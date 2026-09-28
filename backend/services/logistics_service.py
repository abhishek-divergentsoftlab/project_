"""Logistics, Multi-Modal Freight Rating, Incoterms Cost Allocation, and Shipment Service."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import logging
import math
from typing import Any, Optional, Union
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.connection import Connection
from models.enums import ConnectionStatus, QuotationStatus, ShipmentStatus, ShippingMode
from models.quotation import Quotation
from models.shipment import Shipment
from models.user import User
from schemas.logistics import (
    FreightEstimateRequest,
    FreightEstimateResponse,
    FreightRateOption,
    IncotermCostBreakdown,
    ShipmentCreatePayload,
    ShipmentOut,
    ShipmentStatusUpdatePayload,
)
from services import currency as currency_service
from services import notification_service
from services.deal_parties import resolve_buyer_seller
from services.locations import CITIES, find_city, find_location
from services.match_scoring import haversine_km
from services.websocket_manager import ws_manager

logger = logging.getLogger(__name__)

# Approximate geographic centroids, used when a city is not in the locations
# table but its country is known. Keys are lower-case names and ISO-3166 codes.
_COUNTRY_CENTROIDS: dict[str, tuple[float, float]] = {}
for _names, _coords in (
    (("india", "in", "ind", "bharat"), (20.5937, 78.9629)),
    (("united states", "usa", "us", "united states of america", "america"), (37.0902, -95.7129)),
    (("china", "cn", "chn", "prc"), (35.8617, 104.1954)),
    (("germany", "de", "deu"), (51.1657, 10.4515)),
    (("netherlands", "holland", "nl", "nld", "the netherlands"), (52.1326, 5.2913)),
    (("united arab emirates", "uae", "ae", "are", "dubai"), (23.4241, 53.8478)),
    (("united kingdom", "uk", "gb", "gbr", "great britain", "england", "britain"), (55.3781, -3.4360)),
    (("singapore", "sg", "sgp"), (1.3521, 103.8198)),
    (("japan", "jp", "jpn"), (36.2048, 138.2529)),
    (("australia", "au", "aus"), (-25.2744, 133.7751)),
    (("canada", "ca", "can"), (56.1304, -106.3468)),
    (("brazil", "br", "bra"), (-14.2350, -51.9253)),
    (("france", "fr", "fra"), (46.2276, 2.2137)),
    (("italy", "it", "ita"), (41.8719, 12.5674)),
    (("spain", "es", "esp"), (40.4637, -3.7492)),
    (("portugal", "pt", "prt"), (39.3999, -8.2245)),
    (("belgium", "be", "bel"), (50.5039, 4.4699)),
    (("switzerland", "ch", "che"), (46.8182, 8.2275)),
    (("austria", "at", "aut"), (47.5162, 14.5501)),
    (("poland", "pl", "pol"), (51.9194, 19.1451)),
    (("sweden", "se", "swe"), (60.1282, 18.6435)),
    (("norway", "no", "nor"), (60.4720, 8.4689)),
    (("denmark", "dk", "dnk"), (56.2639, 9.5018)),
    (("finland", "fi", "fin"), (61.9241, 25.7482)),
    (("ireland", "ie", "irl"), (53.4129, -8.2439)),
    (("greece", "gr", "grc"), (39.0742, 21.8243)),
    (("turkey", "tr", "tur", "turkiye"), (38.9637, 35.2433)),
    (("russia", "ru", "rus", "russian federation"), (61.5240, 105.3188)),
    (("ukraine", "ua", "ukr"), (48.3794, 31.1656)),
    (("saudi arabia", "sa", "sau", "ksa"), (23.8859, 45.0792)),
    (("qatar", "qa", "qat"), (25.3548, 51.1839)),
    (("kuwait", "kw", "kwt"), (29.3117, 47.4818)),
    (("oman", "om", "omn"), (21.5126, 55.9233)),
    (("bahrain", "bh", "bhr"), (26.0667, 50.5577)),
    (("israel", "il", "isr"), (31.0461, 34.8516)),
    (("egypt", "eg", "egy"), (26.8206, 30.8025)),
    (("south africa", "za", "zaf"), (-30.5595, 22.9375)),
    (("nigeria", "ng", "nga"), (9.0820, 8.6753)),
    (("kenya", "ke", "ken"), (-0.0236, 37.9062)),
    (("ghana", "gh", "gha"), (7.9465, -1.0232)),
    (("rwanda", "rw", "rwa"), (-1.9403, 29.8739)),
    (("ethiopia", "et", "eth"), (9.1450, 40.4897)),
    (("tanzania", "tz", "tza"), (-6.3690, 34.8888)),
    (("uganda", "ug", "uga"), (1.3733, 32.2903)),
    (("morocco", "ma", "mar"), (31.7917, -7.0926)),
    (("algeria", "dz", "dza"), (28.0339, 1.6596)),
    (("tunisia", "tn", "tun"), (33.8869, 9.5375)),
    (("pakistan", "pk", "pak"), (30.3753, 69.3451)),
    (("bangladesh", "bd", "bgd"), (23.6850, 90.3563)),
    (("sri lanka", "lk", "lka"), (7.8731, 80.7718)),
    (("nepal", "np", "npl"), (28.3949, 84.1240)),
    (("indonesia", "id", "idn"), (-0.7893, 113.9213)),
    (("malaysia", "my", "mys"), (4.2105, 101.9758)),
    (("thailand", "th", "tha"), (15.8700, 100.9925)),
    (("vietnam", "vn", "vnm", "viet nam"), (14.0583, 108.2772)),
    (("philippines", "ph", "phl"), (12.8797, 121.7740)),
    (("south korea", "kr", "kor", "korea", "republic of korea"), (35.9078, 127.7669)),
    (("taiwan", "tw", "twn"), (23.6978, 120.9605)),
    (("hong kong", "hk", "hkg"), (22.3193, 114.1694)),
    (("new zealand", "nz", "nzl"), (-40.9006, 174.8860)),
    (("mexico", "mx", "mex"), (23.6345, -102.5528)),
    (("argentina", "ar", "arg"), (-38.4161, -63.6167)),
    (("chile", "cl", "chl"), (-35.6751, -71.5430)),
    (("peru", "pe", "per"), (-9.1900, -75.0152)),
    (("colombia", "co", "col"), (4.5709, -74.2973)),
    (("venezuela", "ve", "ven"), (6.4238, -66.5897)),
    (("iran", "ir", "irn"), (32.4279, 53.6880)),
    (("iraq", "iq", "irq"), (33.2232, 43.6793)),
):
    for _n in _names:
        _COUNTRY_CENTROIDS[_n] = _coords


class UnknownLocationError(ValueError):
    """Neither the city nor the country could be placed on the map."""


def _get_city_coords(city_name: Optional[str], country_name: Optional[str] = "") -> tuple[float, float]:
    """Resolve coordinates for a city, falling back to the country's centroid.

    Raises UnknownLocationError when neither is known -- a freight quote for a
    place we cannot locate would be fiction (it used to silently price every
    unknown place as central India).
    """
    c_lower = (city_name or "").strip().lower()
    if c_lower in CITIES:
        c = CITIES[c_lower]
        return c.latitude, c.longitude

    if c_lower:
        matched = find_city(city_name or "")
        if matched:
            return matched.latitude, matched.longitude

    country_lower = (country_name or "").strip().lower()
    if country_lower in _COUNTRY_CENTROIDS:
        return _COUNTRY_CENTROIDS[country_lower]

    # A known state / region / country name typed into either field.
    for text in (city_name, country_name):
        if text and text.strip():
            loc = find_location(text)
            if loc is not None:
                return loc.latitude, loc.longitude
    if c_lower in _COUNTRY_CENTROIDS:
        return _COUNTRY_CENTROIDS[c_lower]

    raise UnknownLocationError(
        f"Unknown location: '{(city_name or '').strip()}, {(country_name or '').strip()}'"
    )


def calculate_chargeable_weight(
    weight_kg: Decimal,
    volume_cbm: Optional[Decimal] = None,
    length_cm: Optional[Decimal] = None,
    width_cm: Optional[Decimal] = None,
    height_cm: Optional[Decimal] = None,
) -> tuple[Decimal, Decimal, Decimal]:
    """Calculate (gross_weight_kg, volumetric_weight_kg, volume_cbm)."""
    cbm = volume_cbm or Decimal("0.000")
    if (length_cm and width_cm and height_cm) and (length_cm > 0 and width_cm > 0 and height_cm > 0):
        cbm_calc = (length_cm * width_cm * height_cm) / Decimal("1000000.0")
        if cbm == 0:
            cbm = cbm_calc

    # Standard air volumetric factor: 1 CBM = 167 kg (or L*W*H / 5000)
    if cbm > 0:
        volumetric_kg = cbm * Decimal("167.0")
    else:
        volumetric_kg = weight_kg

    return weight_kg, round(volumetric_kg, 2), round(cbm, 3)


def estimate_freight_rates(request: FreightEstimateRequest) -> FreightEstimateResponse:
    """Calculate multi-modal freight rates, transit ETAs, and Incoterms landed cost breakdown.

    Raises HTTP 422 for a location that cannot be placed or an unsupported
    currency. FX comes from services.currency (the platform's one rate table).
    """
    try:
        lat1, lon1 = _get_city_coords(request.origin_city, request.origin_country)
        lat2, lon2 = _get_city_coords(request.destination_city, request.destination_country)
    except UnknownLocationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    raw_distance = haversine_km(lat1, lon1, lat2, lon2)
    # Realistic road/sea route circuity factor (~1.25x straight-line distance)
    distance_km = max(50.0, raw_distance * 1.25)

    is_cross_border = request.origin_country.strip().lower() != request.destination_country.strip().lower()

    gross_wt, vol_wt, cbm = calculate_chargeable_weight(
        request.weight_kg,
        request.volume_cbm,
        request.length_cm,
        request.width_cm,
        request.height_cm,
    )
    chargeable_wt = max(gross_wt, vol_wt)

    target_currency = request.currency.upper()
    fx_rate = currency_service.usd_rate(target_currency)
    if fx_rate is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unsupported currency '{request.currency}'"
        )
    fx = float(fx_rate)

    rate_options: list[FreightRateOption] = []

    # 1. Road Freight (FTL / LTL Trucking)
    if not is_cross_border or distance_km < 3500:
        # Domestic or regional contiguous road transport
        # Base: $40, rate per ton-km: ~$0.09 USD
        ton_km = (float(chargeable_wt) / 1000.0) * distance_km
        road_usd = max(45.0, 40.0 + ton_km * 0.085)
        # Fuel surcharge & toll buffer
        road_usd *= 1.15
        road_converted = round(Decimal(str(road_usd * fx)), 2)

        days_min = max(1, int(distance_km / 500))
        days_max = days_min + (2 if distance_km > 300 else 1)

        base_amt = round(Decimal(str(max(45.0, 40.0 + ton_km * 0.085) * fx)), 2)
        fuel_amt = round(Decimal(str(float(road_converted) * 0.12)), 2)
        cust_amt = Decimal("0.00") if not is_cross_border else round(Decimal(str(45.0 * fx)), 2)

        rate_options.append(
            FreightRateOption(
                mode_id="road_freight",
                mode="road",
                mode_name="Road Freight (FTL / LTL Trucking)",
                mode_label="Road Freight (FTL / LTL Trucking)",
                carrier_sample="Safexpress / TCI Freight / DHL Freight",
                rate_amount=road_converted,
                total_estimated_usd=road_converted,
                base_freight_usd=base_amt,
                fuel_surcharge_usd=fuel_amt,
                customs_clearance_usd=cust_amt,
                basis="Per Ton-Km / Lane Matrix",
                currency=target_currency,
                transit_days_min=days_min,
                transit_days_max=days_max,
                chargeable_weight_kg=chargeable_wt,
                is_recommended=(not is_cross_border and float(gross_wt) >= 50.0),
                recommended=(not is_cross_border and float(gross_wt) >= 50.0),
                description=f"Direct surface freight via dedicated fleet ({round(distance_km)} km scheduled lane).",
            )
        )

    # 2. Ocean Freight (LCL Cargo / FCL Container)
    # Ideal for international or cross-border heavy cargo
    if is_cross_border or distance_km > 800:
        cbm_calc = float(cbm) if cbm > 0 else max(1.0, float(gross_wt) / 333.0)
        if cbm_calc > 15.0 or float(gross_wt) > 7500:
            # 20ft/40ft FCL container flat rate
            ocean_usd = 1850.0 + (distance_km * 0.22)
            mode_label = "Ocean Freight (FCL Container Load)"
        else:
            # LCL consolidated rate per CBM
            ocean_usd = 120.0 + (cbm_calc * 65.0) + (distance_km * 0.04)
            mode_label = "Ocean Freight (LCL Consolidated Cargo)"

        ocean_converted = round(Decimal(str(ocean_usd * fx)), 2)
        days_min = max(7, int(distance_km / 350))
        days_max = days_min + 7
        base_amt = round(ocean_converted * Decimal("0.80"), 2)
        fuel_amt = round(ocean_converted * Decimal("0.12"), 2)
        cust_amt = round(Decimal(str(150.0 * fx)), 2)
        basis_str = "Per CBM Consolidated (LCL)" if (cbm_calc <= 15.0 and float(gross_wt) <= 7500) else "Per 20ft/40ft Container (FCL)"

        rate_options.append(
            FreightRateOption(
                mode_id="ocean_freight",
                mode="ocean",
                mode_name=mode_label,
                mode_label=mode_label,
                carrier_sample="Maersk Line / CMA CGM / MSC Mediterranean",
                rate_amount=ocean_converted,
                total_estimated_usd=ocean_converted,
                base_freight_usd=base_amt,
                fuel_surcharge_usd=fuel_amt,
                customs_clearance_usd=cust_amt,
                basis=basis_str,
                currency=target_currency,
                transit_days_min=days_min,
                transit_days_max=days_max,
                chargeable_weight_kg=chargeable_wt,
                is_recommended=(is_cross_border and float(gross_wt) >= 250.0),
                recommended=(is_cross_border and float(gross_wt) >= 250.0),
                description=f"Port-to-port maritime shipment with Bill of Lading documentation ({days_min}–{days_max} days ETA).",
            )
        )

    # 3. Air Cargo (Commercial Heavy Cargo)
    # Rate per kg: ~$2.90 - $4.80 USD depending on distance
    air_rate_per_kg = 2.40 + min(3.0, (distance_km / 3000.0) * 1.5)
    air_usd = max(85.0, 65.0 + float(chargeable_wt) * air_rate_per_kg)
    air_converted = round(Decimal(str(air_usd * fx)), 2)
    air_days_min = 2 if is_cross_border else 1
    air_days_max = 5 if is_cross_border else 3
    base_amt = round(air_converted * Decimal("0.75"), 2)
    fuel_amt = round(air_converted * Decimal("0.15"), 2)
    cust_amt = round(Decimal(str(95.0 * fx)), 2)

    rate_options.append(
        FreightRateOption(
            mode_id="air_cargo",
            mode="air",
            mode_name="Air Cargo (Scheduled Commercial Flight)",
            mode_label="Air Cargo (Scheduled Commercial Flight)",
            carrier_sample="Emirates SkyCargo / Qatar Cargo / Lufthansa Cargo",
            rate_amount=air_converted,
            total_estimated_usd=air_converted,
            base_freight_usd=base_amt,
            fuel_surcharge_usd=fuel_amt,
            customs_clearance_usd=cust_amt,
            basis="Per Chargeable Kg",
            currency=target_currency,
            transit_days_min=air_days_min,
            transit_days_max=air_days_max,
            chargeable_weight_kg=chargeable_wt,
            is_recommended=(is_cross_border and float(gross_wt) < 250.0),
            recommended=(is_cross_border and float(gross_wt) < 250.0),
            description=f"Priority airport-to-airport air cargo with customs clearance fast-track ({air_days_min}–{air_days_max} days ETA).",
        )
    )

    # 4. Express Courier (Door-to-Door Parcel / Samples)
    if float(gross_wt) <= 500.0:
        courier_usd = 35.0 + float(chargeable_wt) * 4.90 + (distance_km * 0.015)
        courier_converted = round(Decimal(str(courier_usd * fx)), 2)
        c_min = 1 if not is_cross_border else 2
        c_max = 3 if not is_cross_border else 5
        base_amt = round(courier_converted * Decimal("0.80"), 2)
        fuel_amt = round(courier_converted * Decimal("0.10"), 2)
        cust_amt = round(courier_converted * Decimal("0.10"), 2)

        rate_options.append(
            FreightRateOption(
                mode_id="express_courier",
                mode="courier",
                mode_name="Express Courier (Door-to-Door)",
                mode_label="Express Courier (Door-to-Door)",
                carrier_sample="DHL Express / FedEx Priority / BlueDart",
                rate_amount=courier_converted,
                total_estimated_usd=courier_converted,
                base_freight_usd=base_amt,
                fuel_surcharge_usd=fuel_amt,
                customs_clearance_usd=cust_amt,
                basis="Doorstep Express",
                currency=target_currency,
                transit_days_min=c_min,
                transit_days_max=c_max,
                chargeable_weight_kg=chargeable_wt,
                is_recommended=(float(gross_wt) < 50.0),
                recommended=(float(gross_wt) < 50.0),
                description=f"End-to-end tracked courier pickup & delivery directly to facility ({c_min}–{c_max} days ETA).",
            )
        )

    # Calculate Incoterms Landed Cost Breakdown
    incoterm_norm = (request.incoterm or "FOB").upper().strip()
    primary_freight = rate_options[0].rate_amount if rate_options else Decimal("100.00")
    # Find recommended option for baseline freight
    for opt in rate_options:
        if opt.is_recommended:
            primary_freight = opt.rate_amount
            break

    breakdown = compute_incoterms_breakdown(
        incoterm_norm,
        primary_freight,
        target_currency,
        request.cargo_value or Decimal("5000.00"),
    )

    return FreightEstimateResponse(
        origin=f"{request.origin_city}, {request.origin_country}",
        destination=f"{request.destination_city}, {request.destination_country}",
        distance_km=round(distance_km, 1),
        is_cross_border=is_cross_border,
        gross_weight_kg=gross_wt,
        volumetric_weight_kg=vol_wt,
        chargeable_weight_kg=chargeable_wt,
        volume_cbm=cbm,
        cbm=cbm,
        currency=target_currency,
        rate_options=rate_options,
        rates=rate_options,
        incoterm_breakdown=breakdown,
    )


def compute_incoterms_breakdown(
    incoterm: str,
    base_freight: Decimal,
    currency: str,
    cargo_value: Decimal,
) -> IncotermCostBreakdown:
    """Calculate Incoterms 2020 cost responsibility allocation."""
    origin_handling = base_freight * Decimal("0.15")
    export_clearance = base_freight * Decimal("0.10")
    main_transit = base_freight * Decimal("0.55")
    insurance = cargo_value * Decimal("0.005") + base_freight * Decimal("0.05")
    import_tariffs = cargo_value * Decimal("0.08") + base_freight * Decimal("0.10")
    last_mile = base_freight * Decimal("0.15")

    seller_items: list[str] = []
    buyer_items: list[str] = []
    seller_cost = Decimal("0.00")
    buyer_cost = Decimal("0.00")
    risk_point = ""

    if incoterm == "EXW":
        seller_items = ["Packaging & factory floor readiness"]
        buyer_items = [
            "Factory pickup & loading",
            "Export customs clearance",
            "Main freight transit",
            "Cargo transit insurance",
            "Import customs duties & taxes",
            "Final destination delivery",
        ]
        seller_cost = Decimal("0.00")
        buyer_cost = (
            origin_handling + export_clearance + main_transit + insurance + import_tariffs + last_mile
        )
        risk_point = "Factory floor / Supplier warehouse gate"

    elif incoterm == "FOB":
        seller_items = [
            "Packaging & factory loading",
            "Inland transport to departure port",
            "Export customs clearance & port charges",
        ]
        buyer_items = [
            "Main freight (ocean/air transit)",
            "Marine/transit insurance",
            "Import customs tariffs & port charges",
            "Destination inland haulage",
        ]
        seller_cost = origin_handling + export_clearance
        buyer_cost = main_transit + insurance + import_tariffs + last_mile
        risk_point = "When goods pass ship's rail / loaded on carrier at origin port"

    elif incoterm in ("CIF", "CFR"):
        has_insurance = incoterm == "CIF"
        seller_items = [
            "Export customs clearance",
            "Inland origin transport",
            "Main ocean/air freight to destination port",
        ]
        if has_insurance:
            seller_items.append("Marine transit insurance (minimum cover)")

        buyer_items = [
            "Destination port handling charges",
            "Import customs clearance, VAT & tariffs",
            "Final delivery to buyer facility",
        ]
        if not has_insurance:
            buyer_items.append("Marine transit insurance")

        seller_cost = origin_handling + export_clearance + main_transit + (insurance if has_insurance else Decimal("0"))
        buyer_cost = import_tariffs + last_mile + (Decimal("0") if has_insurance else insurance)
        risk_point = "Origin port after loading (seller pays freight, buyer bears transit risk)"

    elif incoterm == "DDP":
        seller_items = [
            "Origin handling & export clearance",
            "Main freight transit",
            "Comprehensive cargo insurance",
            "Destination import duties, tariffs & taxes",
            "Doorstep delivery to buyer facility",
        ]
        buyer_items = ["Unloading at buyer warehouse"]
        seller_cost = (
            origin_handling + export_clearance + main_transit + insurance + import_tariffs + last_mile
        )
        buyer_cost = Decimal("0.00")
        risk_point = "Buyer's facility / designated delivery warehouse"

    else:  # CIP / CPT / standard
        seller_items = [
            "Origin handling & export clearance",
            "Multimodal carriage to destination terminal",
            "Comprehensive All-Risk cargo insurance",
        ]
        buyer_items = [
            "Import customs clearance & tariffs",
            "Final transit from terminal to destination",
        ]
        seller_cost = origin_handling + export_clearance + main_transit + insurance
        buyer_cost = import_tariffs + last_mile
        risk_point = "First carrier handover at origin"

    INCOTERM_NAMES = {
        "EXW": "Ex Works",
        "FCA": "Free Carrier",
        "CPT": "Carriage Paid To",
        "CIP": "Carriage and Insurance Paid To",
        "DAP": "Delivered at Place",
        "DPU": "Delivered at Place Unloaded",
        "DDP": "Delivered Duty Paid",
        "FAS": "Free Alongside Ship",
        "FOB": "Free On Board",
        "CFR": "Cost and Freight",
        "CIF": "Cost, Insurance and Freight",
    }

    return IncotermCostBreakdown(
        incoterm=incoterm,
        full_name=INCOTERM_NAMES.get(incoterm, incoterm),
        seller_pays=seller_items,
        buyer_pays=buyer_items,
        seller_responsibility="; ".join(seller_items) if seller_items else "None",
        buyer_responsibility="; ".join(buyer_items) if buyer_items else "None",
        seller_estimated_cost=round(seller_cost, 2),
        buyer_estimated_cost=round(buyer_cost, 2),
        estimated_seller_logistics_usd=round(seller_cost, 2),
        estimated_buyer_logistics_usd=round(buyer_cost, 2),
        currency=currency,
        risk_transfer_point=risk_point,
    )


async def get_or_create_connection_shipment(
    db: AsyncSession, connection_id: uuid.UUID, user_id: uuid.UUID
) -> Optional[Shipment]:
    """Retrieve the active shipment record for a connection."""
    conn = await db.get(Connection, connection_id)
    if conn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Deal Room connection not found")
    if user_id not in (conn.sender_id, conn.receiver_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not authorized to view this connection's logistics")

    stmt = select(Shipment).where(Shipment.connection_id == connection_id).order_by(Shipment.created_at.desc())
    res = await db.execute(stmt)
    return res.scalars().first()


async def create_shipment_dispatch(
    db: AsyncSession,
    connection_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: ShipmentCreatePayload,
) -> Shipment:
    """Create a new shipment and record initial carrier dispatch."""
    conn = await db.get(Connection, connection_id)
    if conn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found")
    if user_id not in (conn.sender_id, conn.receiver_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not authorized to dispatch on this connection")

    if conn.status is not ConnectionStatus.ACCEPTED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Cannot dispatch order on unaccepted connection",
        )

    # Identify counterparty
    counterparty_id = conn.receiver_id if conn.sender_id == user_id else conn.sender_id

    # Fetch latest quote if present
    stmt = (
        select(Quotation)
        .where(Quotation.connection_id == connection_id)
        .order_by(Quotation.version.desc())
    )
    quote_res = await db.execute(stmt)
    latest_quote = quote_res.scalars().first()

    # Generate tracking number if not given
    tracking = payload.tracking_number
    if not tracking or not tracking.strip():
        prefix = "TRK"
        if payload.shipping_mode == "ocean":
            prefix = "OCN"
        elif payload.shipping_mode == "air":
            prefix = "AWB"
        tracking = f"{prefix}-{str(uuid.uuid4())[:8].upper()}-{datetime.now(UTC).strftime('%y%m')}"

    now = datetime.now(UTC)
    eta = now + timedelta(days=payload.estimated_delivery_days or 5)

    origin_c = payload.origin_city or "Mumbai"
    origin_k = payload.origin_country or "India"
    dest_c = payload.destination_city or "New Delhi"
    dest_k = payload.destination_country or "India"

    initial_event = {
        "status": "dispatched",
        "location": f"{origin_c}, {origin_k}",
        "timestamp": now.isoformat(),
        "note": payload.dispatch_note or f"Order booked and dispatched with {payload.carrier_name}.",
    }

    shipment = Shipment(
        connection_id=connection_id,
        quotation_id=latest_quote.id if latest_quote else None,
        sender_id=user_id,
        receiver_id=counterparty_id,
        tracking_number=tracking,
        carrier_name=payload.carrier_name,
        carrier_service=payload.carrier_service,
        shipping_mode=payload.shipping_mode,
        status="dispatched",
        origin_city=origin_c,
        origin_country=origin_k,
        origin_address=payload.origin_address,
        destination_city=dest_c,
        destination_country=dest_k,
        destination_address=payload.destination_address,
        weight_kg=payload.weight_kg or Decimal("50.000"),
        volume_cbm=payload.volume_cbm or Decimal("0.250"),
        package_count=payload.package_count,
        package_type=payload.package_type,
        bill_of_lading_number=payload.bill_of_lading_number,
        estimated_delivery_date=eta,
        dispatched_at=now,
        tracking_events=[initial_event],
    )

    db.add(shipment)

    # Update quotation status to DISPATCHED if accepted
    if latest_quote and latest_quote.status in (QuotationStatus.ACCEPTED, QuotationStatus.PENDING):
        latest_quote.status = QuotationStatus.DISPATCHED
        latest_quote.updated_at = now

    # Optionally trigger Phase 4 Escrow Milestone 2 ("Dispatch & B/L") release request
    if payload.trigger_escrow_milestone:
        try:
            from services.escrow_service import get_or_create_escrow_for_connection, request_milestone_release
            esc = await get_or_create_escrow_for_connection(db, connection_id)
            if esc and esc.status in ("funded", "partially_released") and esc.milestones:
                # Milestone with order_index 1 is the 40% dispatch tranche
                dispatch_milestone = next((m for m in esc.milestones if m.order_index == 1), None)
                if dispatch_milestone and dispatch_milestone.status == "funded":
                    await request_milestone_release(
                        db,
                        connection_id,
                        dispatch_milestone.id,
                        user_id,
                        proof_note=f"Dispatched via {payload.carrier_name} under tracking {tracking}.",
                    )
        except Exception as exc:
            logger.warning("Could not auto-trigger escrow dispatch milestone: %s", exc)

    await db.commit()
    await db.refresh(shipment)

    # Real-time WebSocket notification
    await ws_manager.broadcast(
        connection_id,
        "shipment_dispatched",
        {
            "connection_id": str(connection_id),
            "shipment_id": str(shipment.id),
            "tracking_number": shipment.tracking_number,
            "carrier_name": shipment.carrier_name,
            "dispatched_by_id": str(user_id),
        },
    )

    return shipment


async def append_shipment_event(
    db: AsyncSession,
    shipment_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: ShipmentStatusUpdatePayload,
) -> Shipment:
    """Append a tracking checkpoint event and update shipment status."""
    shipment = await db.get(Shipment, shipment_id)
    if shipment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Shipment not found")

    if user_id not in (shipment.sender_id, shipment.receiver_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not authorized to update this shipment")

    now = datetime.now(UTC)
    new_event = {
        "status": payload.status,
        "location": payload.location,
        "timestamp": now.isoformat(),
        "note": payload.note or f"Status updated to {payload.status.replace('_', ' ')}",
    }

    events = list(shipment.tracking_events or [])
    events.append(new_event)
    shipment.tracking_events = events
    shipment.status = payload.status
    shipment.updated_at = now

    if payload.status == "delivered":
        shipment.delivered_at = now
        # Update quote status if linked
        if shipment.quotation_id:
            quote = await db.get(Quotation, shipment.quotation_id)
            if quote:
                quote.status = QuotationStatus.DELIVERED
                quote.updated_at = now

    await db.commit()
    await db.refresh(shipment)

    # WebSocket broadcast
    await ws_manager.broadcast(
        shipment.connection_id,
        "shipment_updated",
        {
            "connection_id": str(shipment.connection_id),
            "shipment_id": str(shipment.id),
            "status": shipment.status,
            "location": payload.location,
            "note": payload.note,
        },
    )

    return shipment
