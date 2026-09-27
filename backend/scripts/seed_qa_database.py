"""Complete Database Reset and QA Seed Suite.

Wipes all existing database tables and Qdrant vector collections,
then seeds rich, realistic, end-to-end B2B marketplace test data covering:
- 10 Buyer accounts (verified, pending KYC, international)
- 10 Seller accounts (verified + ISO/CE/FDA certificates, pending, unverified)
- 2 Dual-role accounts ('both')
- 40 Background trading companies for deep catalog diversity
- ~400 carefully specified RFQs across all 8 market categories
- 6 Deal Rooms with real conversation threads, pending/accepted connections
- Formal Quotations (pending, accepted, countered, completed)
- Smart 3-Tranche Milestone Escrow Accounts (funded, release-requested, completed, disputed)
- Freight Shipments with live tracking events, waybills, and multi-modal logistics
- Formal Deal Dispute for arbitration testing
- Mutual 4-D Reviews with dynamic Trust Score updates
- Compliance Certificates (ISO 9001, ISO 14001, CE, RoHS, FDA)
- User Notifications (unread and read)
- Moderation Logs for AI guardrail audit
- AI Copilot Conversations and Message Events
- Qdrant Vector Collection recreation & batch embedding

Run:
    .venv/bin/python scripts/seed_qa_database.py
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import logging
from pathlib import Path
import random
import sys
import uuid
from typing import Any

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.security import hash_password
from db.session import SessionLocal, engine
from models.certificate import Certificate
from models.connection import Connection, ConnectionMessage
from models.conversation import Conversation, Message, MessageEvent
from models.enums import (
    CertificationStatus,
    ConnectionStatus,
    ConversationType,
    EmbeddingStatus,
    Incoterm,
    KYCStatus,
    MessageRole,
    QuotationStatus,
    RFQRole,
    RFQStatus,
    ShipmentStatus,
    ShippingMode,
    UserRole,
    UserStatus,
)
from models.escrow import DealDispute, EscrowAccount, EscrowMilestone
from models.match import MatchResult, MatchSearch
from models.moderation import ModerationLog
from models.notification import Notification
from models.quotation import Quotation
from models.review import Review
from models.rfq import RFQ
from models.saved_rfq import SavedRFQ
from models.shipment import Shipment
from models.user import User, UserProfile
from scripts.catalogue import CATALOGUE, COHERENCE
from services import currency, qdrant_index
from services.embeddings import embed_many
from services.locations import CITIES, City, dialling_code
from services.rfq_indexing import build_match_text, refresh_index_fields

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

MASTER_PASSWORD = "trade2026demo"
LEGACY_DEMO_PASSWORD = "demo password 123"
SEED_DOMAIN = "seed.marketplace.dev"
DEMO_DOMAIN = "marketplace.dev"

TABLES_TO_TRUNCATE = [
    "deal_disputes",
    "escrow_milestones",
    "escrow_accounts",
    "shipments",
    "reviews",
    "certificates",
    "moderation_logs",
    "notifications",
    "saved_rfqs",
    "connection_messages",
    "quotations",
    "connections",
    "match_results",
    "match_searches",
    "message_events",
    "messages",
    "conversations",
    "rfqs",
    "user_profiles",
    "users",
]


def _render(value: Any) -> str:
    """Compact human rendering of a spec value."""
    if isinstance(value, dict) and "value" in value:
        inner = value["value"]
        if isinstance(inner, dict):
            inner = "x".join(str(v) for v in inner.values())
        unit = value.get("unit", "")
        return f"{inner}{unit}" if unit and not str(unit).startswith(" ") else f"{inner} {unit}".strip()
    if isinstance(value, list):
        return "/".join(str(v) for v in value)
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _draw_specs(rng: random.Random, spec: dict) -> dict[str, Any]:
    attrs = {key: rng.choice(options) for key, options in spec["specs"].items()}
    fix = COHERENCE.get(spec["product"])
    if fix:
        fix(attrs)
    return attrs


def _title(spec: dict, role: RFQRole, quantity: int, attrs: dict) -> str:
    highlights = [
        key for key in (
            "capacity", "output_power", "ply", "grade", "gsm",
            "connector", "variety", "size", "material", "thickness",
            "crystal_form", "bore_diameter", "back_material", "fabric"
        )
        if key in attrs
    ][:2]
    detail = ", ".join(_render(attrs[key]) for key in highlights)
    colour = attrs.get("color")
    name = f"{colour} {spec['product']}" if isinstance(colour, str) else spec["product"]
    verb = "Need" if role is RFQRole.BUYER else "Supplying"
    tail = f" — {detail}" if detail else ""
    return f"{verb} {quantity:,} {spec['unit']} of {name}{tail}"[:300]


def _price(rng: random.Random, spec: dict, role: RFQRole, city: City) -> Decimal:
    swing = rng.uniform(-0.18, 0.05) if role is RFQRole.SELLER else rng.uniform(-0.05, 0.20)
    usd = Decimal(str(spec["price_usd"])) * Decimal(str(1 + swing))
    converted = currency.convert(usd, "USD", city.currency) or usd
    quantum = Decimal("0.01") if converted < 100 else Decimal("1")
    return converted.quantize(quantum)


async def wipe_all_database_data() -> None:
    """Completely wipe all business data from PostgreSQL and Qdrant."""
    logger.info("Purging all tables in PostgreSQL...")
    async with SessionLocal() as db:
        truncate_sql = f"TRUNCATE TABLE {', '.join(TABLES_TO_TRUNCATE)} RESTART IDENTITY CASCADE;"
        await db.execute(text(truncate_sql))
        await db.commit()
    logger.info("✓ PostgreSQL tables truncated successfully.")

    logger.info("Resetting Qdrant vector collection...")
    try:
        qclient = qdrant_index.client()
        if await qclient.collection_exists(settings.QDRANT_COLLECTION):
            await qclient.delete_collection(settings.QDRANT_COLLECTION)
            logger.info("✓ Deleted old collection '%s'", settings.QDRANT_COLLECTION)
        await qdrant_index.ensure_collection()
        logger.info("✓ Created fresh collection '%s'", settings.QDRANT_COLLECTION)
    except Exception as exc:
        logger.warning("Qdrant reset notice: %s", exc)


async def seed_all_qa_data(rfq_count: int = 70) -> dict[str, Any]:
    rng = random.Random(20260927)
    now = datetime.now(UTC)
    demo_hash = hash_password(MASTER_PASSWORD)
    legacy_hash = hash_password(LEGACY_DEMO_PASSWORD)

    created_accounts: list[dict[str, Any]] = []

    async with SessionLocal() as db:
        logger.info("Seeding Credentialed Users & Profiles...")

        # =====================================================================
        # 1. CORE CREDENTIALED USERS
        # =====================================================================

        # Buyer 1: Apex Industrial Procurement (Mumbai, India)
        b1 = User(email="buyer1@marketplace.dev", password_hash=demo_hash, role=UserRole.BUYER, status=UserStatus.ACTIVE)
        b1.profile = UserProfile(
            name="Aarti Deshmukh",
            company_name="Apex Industrial Procurement",
            phone="+91 9820012345",
            address="42 Nariman Point, Marine Drive",
            city="Mumbai",
            state="Maharashtra",
            country="India",
            latitude=Decimal("18.921984"),
            longitude=Decimal("72.834654"),
            gst_number="27AABCU9603R1ZM",
            legal_business_name="Apex Industrial Procurement Pvt Ltd",
            business_type="Procurement & Distribution",
            registration_number="U51909MH2018PTC312456",
            year_established=2018,
            website="https://apexprocure.example.com",
            pan_number="AABCU9603R",
            signatory_name="Aarti Deshmukh",
            kyc_status=KYCStatus.VERIFIED,
            trust_score=90,
            average_rating=Decimal("4.95"),
            total_reviews=14,
        )
        db.add(b1)

        # Buyer 2: Sterling Imports Ltd (London, UK)
        b2 = User(email="buyer2@marketplace.dev", password_hash=demo_hash, role=UserRole.BUYER, status=UserStatus.ACTIVE)
        b2.profile = UserProfile(
            name="Edward Sterling",
            company_name="Sterling Imports Ltd",
            phone="+44 2079460192",
            address="14 St Mary Axe, City of London",
            city="London",
            state="England",
            country="United Kingdom",
            latitude=Decimal("51.5144"),
            longitude=Decimal("-0.0803"),
            registration_number="UK-COMP-09418241",
            year_established=2014,
            website="https://sterlingimports.co.uk",
            kyc_status=KYCStatus.VERIFIED,
            trust_score=88,
            average_rating=Decimal("4.80"),
            total_reviews=9,
        )
        db.add(b2)

        # Buyer 3: Pacific Retailers Inc (Singapore)
        b3 = User(email="buyer3@marketplace.dev", password_hash=demo_hash, role=UserRole.BUYER, status=UserStatus.ACTIVE)
        b3.profile = UserProfile(
            name="Wei Chen",
            company_name="Pacific Retailers Inc",
            phone="+65 67891234",
            address="8 Marina View, Asia Square",
            city="Singapore",
            state="Central",
            country="Singapore",
            latitude=Decimal("1.2789"),
            longitude=Decimal("103.8536"),
            registration_number="SG-2016-89410A",
            year_established=2016,
            website="https://pacificretailers.sg",
            kyc_status=KYCStatus.VERIFIED,
            trust_score=85,
            average_rating=Decimal("4.75"),
            total_reviews=6,
        )
        db.add(b3)

        # Buyer 4: Nordic Tech Sourcing (Berlin, Germany) - Pending KYC
        b4 = User(email="buyer4@marketplace.dev", password_hash=demo_hash, role=UserRole.BUYER, status=UserStatus.ACTIVE)
        b4.profile = UserProfile(
            name="Lukas Becker",
            company_name="Nordic Tech Sourcing GmbH",
            phone="+49 301234567",
            address="Friedrichstraße 120",
            city="Berlin",
            state="Berlin",
            country="Germany",
            latitude=Decimal("52.5200"),
            longitude=Decimal("13.4050"),
            registration_number="HRB-194820-B",
            year_established=2021,
            website="https://nordictech.de",
            kyc_status=KYCStatus.PENDING,
            trust_score=45,
            average_rating=None,
            total_reviews=0,
        )
        db.add(b4)

        # Buyer 5: Orient Supply Group (Dubai, UAE)
        b5 = User(email="buyer5@marketplace.dev", password_hash=demo_hash, role=UserRole.BUYER, status=UserStatus.ACTIVE)
        b5.profile = UserProfile(
            name="Omar Al-Maktoum",
            company_name="Orient Supply Group",
            phone="+971 43210987",
            address="Business Bay Tower 4",
            city="Dubai",
            state="Dubai",
            country="United Arab Emirates",
            latitude=Decimal("25.1857"),
            longitude=Decimal("55.2713"),
            registration_number="DED-884192",
            year_established=2015,
            website="https://orientsupply.ae",
            kyc_status=KYCStatus.VERIFIED,
            trust_score=87,
            average_rating=Decimal("4.85"),
            total_reviews=8,
        )
        db.add(b5)

        # Buyers 6 to 10
        buyers_pool = [b1, b2, b3, b4, b5]
        for i in range(6, 11):
            city = list(CITIES.values())[i * 3 % len(CITIES)]
            u = User(email=f"buyer{i}@marketplace.dev", password_hash=demo_hash, role=UserRole.BUYER, status=UserStatus.ACTIVE)
            u.profile = UserProfile(
                name=f"Procurement Manager {i}",
                company_name=f"Vanguard Global Sourcing {i}",
                phone=f"+{dialling_code(city.country)} {rng.randrange(100000000, 999999999)}",
                address=f"{rng.randrange(10, 200)} Commercial Road",
                city=city.name,
                state=city.region,
                country=city.country,
                latitude=Decimal(str(city.latitude)),
                longitude=Decimal(str(city.longitude)),
                kyc_status=KYCStatus.VERIFIED,
                trust_score=80 + (i % 15),
                average_rating=Decimal("4.70"),
                total_reviews=4,
            )
            db.add(u)
            buyers_pool.append(u)

        # Seller 1: Global Pack Solutions Ltd (Surat, India) - Packaging Specialist
        s1 = User(email="seller1@marketplace.dev", password_hash=demo_hash, role=UserRole.SELLER, status=UserStatus.ACTIVE)
        s1.profile = UserProfile(
            name="Rohit Sharma",
            company_name="Global Pack Solutions Ltd",
            phone="+91 9898012345",
            address="Plot 105, GIDC Sachin Industrial Estate",
            city="Surat",
            state="Gujarat",
            country="India",
            latitude=Decimal("21.1702"),
            longitude=Decimal("72.8311"),
            gst_number="24AABCG1234F1Z5",
            legal_business_name="Global Pack Solutions Private Limited",
            business_type="Manufacturer & Exporter",
            registration_number="U21020GJ2012PTC068912",
            year_established=2012,
            website="https://globalpacksolutions.com",
            pan_number="AABCG1234F",
            signatory_name="Rohit Sharma",
            kyc_status=KYCStatus.VERIFIED,
            trust_score=95,
            average_rating=Decimal("5.00"),
            total_reviews=18,
        )
        db.add(s1)

        # Seller 2: Nova Electronics Co (Shenzhen, China) - Electronics Manufacturer
        s2 = User(email="seller2@marketplace.dev", password_hash=demo_hash, role=UserRole.SELLER, status=UserStatus.ACTIVE)
        s2.profile = UserProfile(
            name="Chen Zhang",
            company_name="Nova Electronics Co Ltd",
            phone="+86 75583921045",
            address="Building B, High-Tech Industrial Park, Nanshan",
            city="Shenzhen",
            state="Guangdong",
            country="China",
            latitude=Decimal("22.5431"),
            longitude=Decimal("114.0579"),
            registration_number="91440300MA5EXXXX",
            year_established=2013,
            website="https://novaelectronics.cn",
            kyc_status=KYCStatus.VERIFIED,
            trust_score=94,
            average_rating=Decimal("4.92"),
            total_reviews=22,
        )
        db.add(s2)

        # Seller 3: Indus Agro Exports (Amritsar, India) - Agricultural Commodities
        s3 = User(email="seller3@marketplace.dev", password_hash=demo_hash, role=UserRole.SELLER, status=UserStatus.ACTIVE)
        s3.profile = UserProfile(
            name="Harpreet Singh",
            company_name="Indus Agro Exports",
            phone="+91 9814098765",
            address="Grain Market Road, GT Road",
            city="Amritsar",
            state="Punjab",
            country="India",
            latitude=Decimal("31.6340"),
            longitude=Decimal("74.8723"),
            gst_number="03AABCI5678D1Z2",
            legal_business_name="Indus Agro Food Exports LLP",
            business_type="Agro Processing & Exports",
            registration_number="AAA-8912",
            year_established=2008,
            website="https://indusagro.in",
            pan_number="AABCI5678D",
            signatory_name="Harpreet Singh",
            kyc_status=KYCStatus.VERIFIED,
            trust_score=91,
            average_rating=Decimal("4.88"),
            total_reviews=15,
        )
        db.add(s3)

        # Seller 4: Zenith Metal Works (Dubai, UAE) - Steel & Precision Metals
        s4 = User(email="seller4@marketplace.dev", password_hash=demo_hash, role=UserRole.SELLER, status=UserStatus.ACTIVE)
        s4.profile = UserProfile(
            name="Tariq Mansoor",
            company_name="Zenith Metal Works FZE",
            phone="+971 48819200",
            address="Jebel Ali Free Zone, South Sector",
            city="Dubai",
            state="Dubai",
            country="United Arab Emirates",
            latitude=Decimal("24.9942"),
            longitude=Decimal("55.0745"),
            registration_number="JAFZA-10948",
            year_established=2011,
            website="https://zenithmetal.ae",
            kyc_status=KYCStatus.VERIFIED,
            trust_score=89,
            average_rating=Decimal("4.80"),
            total_reviews=11,
        )
        db.add(s4)

        # Seller 5: Konark Chemicals (Ahmedabad, India) - Unverified / Pending KYC
        s5 = User(email="seller5@marketplace.dev", password_hash=demo_hash, role=UserRole.SELLER, status=UserStatus.ACTIVE)
        s5.profile = UserProfile(
            name="Sanjay Patel",
            company_name="Konark Chemicals",
            phone="+91 9723019876",
            address="Phase 2, Vatva Industrial Estate",
            city="Ahmedabad",
            state="Gujarat",
            country="India",
            latitude=Decimal("23.0225"),
            longitude=Decimal("72.5714"),
            registration_number="GUJ-CHEM-9812",
            year_established=2022,
            website="https://konarkchem.com",
            kyc_status=KYCStatus.PENDING,
            trust_score=35,
            average_rating=Decimal("3.50"),
            total_reviews=2,
        )
        db.add(s5)

        # Sellers 6 to 10
        sellers_pool = [s1, s2, s3, s4, s5]
        seller_categories = ["Textiles", "Furniture", "Construction", "Chemicals", "Industrial"]
        for i in range(6, 11):
            city = list(CITIES.values())[(i * 4 + 7) % len(CITIES)]
            cat = seller_categories[(i - 6) % len(seller_categories)]
            u = User(email=f"seller{i}@marketplace.dev", password_hash=demo_hash, role=UserRole.SELLER, status=UserStatus.ACTIVE)
            u.profile = UserProfile(
                name=f"Director {i}",
                company_name=f"Sterling {cat} Works {i}",
                phone=f"+{dialling_code(city.country)} {rng.randrange(100000000, 999999999)}",
                address=f"{rng.randrange(1, 150)} Industrial Boulevard",
                city=city.name,
                state=city.region,
                country=city.country,
                latitude=Decimal(str(city.latitude)),
                longitude=Decimal(str(city.longitude)),
                kyc_status=KYCStatus.VERIFIED,
                trust_score=82 + (i % 12),
                average_rating=Decimal("4.75"),
                total_reviews=5,
            )
            db.add(u)
            sellers_pool.append(u)

        # Dual accounts (Role = BOTH)
        demo_user = User(email="demo@marketplace.dev", password_hash=legacy_hash, role=UserRole.BOTH, status=UserStatus.ACTIVE)
        demo_user.profile = UserProfile(
            name="Vikram Rao",
            company_name="TransWorld Trading Corp",
            phone="+91 9820987654",
            address="Maker Chambers V, Nariman Point",
            city="Mumbai",
            state="Maharashtra",
            country="India",
            latitude=Decimal("18.9270"),
            longitude=Decimal("72.8220"),
            gst_number="27AABCT9981K1Z3",
            legal_business_name="TransWorld Trading Corporation LLP",
            business_type="International Trading House",
            registration_number="LLP-99120",
            year_established=2010,
            website="https://transworldtrade.com",
            kyc_status=KYCStatus.VERIFIED,
            trust_score=85,
            average_rating=Decimal("4.85"),
            total_reviews=12,
        )
        db.add(demo_user)

        trader1 = User(email="trader1@marketplace.dev", password_hash=demo_hash, role=UserRole.BOTH, status=UserStatus.ACTIVE)
        trader1.profile = UserProfile(
            name="Jan Van Dijk",
            company_name="Meridian Global Trade BV",
            phone="+31 104567890",
            address="Wilhelminakade 12",
            city="Rotterdam",
            state="South Holland",
            country="Netherlands",
            latitude=Decimal("51.9244"),
            longitude=Decimal("4.4777"),
            registration_number="NL-KVK-889104",
            year_established=2009,
            website="https://meridianglobal.nl",
            kyc_status=KYCStatus.VERIFIED,
            trust_score=92,
            average_rating=Decimal("4.90"),
            total_reviews=16,
        )
        db.add(trader1)

        # Background trading companies
        background_users: list[User] = []
        for i in range(1, 41):
            city = list(CITIES.values())[i % len(CITIES)]
            role = UserRole.SELLER if i % 2 == 0 else UserRole.BUYER
            u = User(email=f"co{i}@{SEED_DOMAIN}", password_hash=demo_hash, role=role, status=UserStatus.ACTIVE)
            u.profile = UserProfile(
                name=f"Representative {i}",
                company_name=f"NovaTrade Alliance {i}",
                phone=f"+{dialling_code(city.country)} {rng.randrange(100000000, 999999999)}",
                address=f"{rng.randrange(1, 200)} Logistics Lane",
                city=city.name,
                state=city.region,
                country=city.country,
                latitude=Decimal(str(city.latitude)),
                longitude=Decimal(str(city.longitude)),
                kyc_status=KYCStatus.VERIFIED,
                trust_score=75 + (i % 20),
                average_rating=Decimal("4.65"),
                total_reviews=3,
            )
            db.add(u)
            background_users.append(u)

        await db.flush()
        logger.info("✓ 22 credentialed users + 40 background companies created.")

        # =====================================================================
        # 2. SEED CERTIFICATES
        # =====================================================================
        # Ensure physical dummy PDF files exist for certificates
        sample_pdf_bytes = (
            b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n"
            b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R/Resources<<>>>>endobj\n"
            b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n"
            b"0000000115 00000 n \ntrailer<</Size 4/Root 1 0 R>>\nstartxref\n190\n%%EOF\n"
        )
        cert_dir_backend = Path("uploads/certificates").resolve()
        cert_dir_root = Path("../uploads/certificates").resolve()
        cert_dir_backend.mkdir(parents=True, exist_ok=True)
        cert_dir_root.mkdir(parents=True, exist_ok=True)

        for filename in [
            "sample_iso_9001.pdf", "sample_iso_14001.pdf", "sample_ce_mark.pdf",
            "sample_rohs.pdf", "sample_fda.pdf", "sample_bv.pdf", "sample_audit.pdf"
        ]:
            (cert_dir_backend / filename).write_bytes(sample_pdf_bytes)
            (cert_dir_root / filename).write_bytes(sample_pdf_bytes)

        logger.info("Seeding Enterprise Compliance Certificates...")
        certs = [
            Certificate(
                user_id=s1.id,
                name="ISO 9001:2015 Quality Management System",
                issuing_body="SGS United Kingdom Ltd",
                certificate_number="ISO-9001-2024-8841",
                issue_date=now - timedelta(days=200),
                expiry_date=now + timedelta(days=530),
                document_url="/api/v1/media/certificates/sample_iso_9001.pdf",
                verification_status=CertificationStatus.VERIFIED,
            ),
            Certificate(
                user_id=s1.id,
                name="ISO 14001:2015 Environmental Management",
                issuing_body="SGS India Ltd",
                certificate_number="ISO-14001-2024-3190",
                issue_date=now - timedelta(days=150),
                expiry_date=now + timedelta(days=580),
                document_url="/api/v1/media/certificates/sample_iso_14001.pdf",
                verification_status=CertificationStatus.VERIFIED,
            ),
            Certificate(
                user_id=s2.id,
                name="CE Declaration of Conformity (EMC & LVD)",
                issuing_body="TÜV Rheinland International",
                certificate_number="CE-2025-EU-9921",
                issue_date=now - timedelta(days=120),
                expiry_date=now + timedelta(days=610),
                document_url="/api/v1/media/certificates/sample_ce_mark.pdf",
                verification_status=CertificationStatus.VERIFIED,
            ),
            Certificate(
                user_id=s2.id,
                name="RoHS 2.0 Directive (EU 2015/863)",
                issuing_body="Intertek Testing Services",
                certificate_number="ROHS-2025-0812",
                issue_date=now - timedelta(days=100),
                expiry_date=now + timedelta(days=630),
                document_url="/api/v1/media/certificates/sample_rohs.pdf",
                verification_status=CertificationStatus.VERIFIED,
            ),
            Certificate(
                user_id=s3.id,
                name="FDA Food Facility Registration",
                issuing_body="U.S. Food and Drug Administration",
                certificate_number="FDA-REG-1948201",
                issue_date=now - timedelta(days=180),
                expiry_date=now + timedelta(days=550),
                document_url="/api/v1/media/certificates/sample_fda.pdf",
                verification_status=CertificationStatus.VERIFIED,
            ),
            Certificate(
                user_id=s4.id,
                name="Bureau Veritas Material Inspection Certificate",
                issuing_body="Bureau Veritas Dubai",
                certificate_number="BV-DXB-2026-4401",
                issue_date=now - timedelta(days=30),
                expiry_date=now + timedelta(days=335),
                document_url="/api/v1/media/certificates/sample_bv.pdf",
                verification_status=CertificationStatus.PENDING,
            ),
            Certificate(
                user_id=s5.id,
                name="Quality Audit Certificate",
                issuing_body="Regional Inspection Agency",
                certificate_number="QAC-2021-998",
                issue_date=now - timedelta(days=600),
                expiry_date=now - timedelta(days=10),
                document_url="/api/v1/media/certificates/sample_audit.pdf",
                verification_status=CertificationStatus.REJECTED,
            ),
        ]
        db.add_all(certs)
        await db.flush()
        logger.info("✓ 7 Compliance Certificates seeded.")

        # =====================================================================
        # 3. SEED CORE RFQs (HIGH-COMPATIBILITY PAIRS & DIVERSE STATUSES)
        # =====================================================================
        logger.info("Seeding Golden Path RFQs for 7-D Matchmaking...")

        # Match Pair 1: Packaging (Buyer1 & Seller1)
        # Spec from catalogue
        spec_box = next(p for p in CATALOGUE if p["product"] == "corrugated box")
        box_buyer_specs = {
            "ply": 5, "burst_strength": {"value": 14, "unit": "kg/cm2"},
            "flute": "BC", "color": "brown", "gsm": 180,
            "dimensions": {"value": {"l": 45, "w": 30, "h": 30}, "unit": "cm"}
        }
        rfq_b1_box = RFQ(
            user_id=b1.id,
            role=RFQRole.BUYER,
            status=RFQStatus.ACTIVE,
            category="Packaging",
            title="Need 10,000 pcs of 5-ply corrugated carton box — 180 GSM, 32 ECT",
            description="Procuring heavy-duty 5-ply corrugated shipper boxes for industrial component transit. FOB/EXW delivery to Mumbai warehouse.",
            quantity_value=Decimal("10000"),
            quantity_unit="pcs",
            price_amount=Decimal("45.00"),
            price_currency="INR",
            price_per_unit="pcs",
            location_city="Mumbai",
            location_state="Maharashtra",
            location_country="India",
            latitude=Decimal("18.921984"),
            longitude=Decimal("72.834654"),
            location_raw="Mumbai, Maharashtra, India",
            deadline_at=now + timedelta(days=30),
            deadline_raw="within 30 days",
            expires_at=now + timedelta(days=60),
            product_details={"name": "corrugated box", **box_buyer_specs},
            embedding_status=EmbeddingStatus.PENDING,
        )
        refresh_index_fields(rfq_b1_box)
        db.add(rfq_b1_box)

        box_seller_specs = {
            "ply": 5, "burst_strength": {"value": 14.5, "unit": "kg/cm2"},
            "flute": "BC", "color": "brown", "gsm": 180,
            "dimensions": {"value": {"l": 45, "w": 30, "h": 30}, "unit": "cm"}
        }
        rfq_s1_box = RFQ(
            user_id=s1.id,
            role=RFQRole.SELLER,
            status=RFQStatus.ACTIVE,
            category="Packaging",
            title="Supplying 50,000 pcs of 5-ply corrugated carton box — 180 GSM, heavy duty",
            description="Direct manufacturer stock of high burst strength 5-ply corrugated master cartons. Fully certified, bulk packaging ready.",
            quantity_value=Decimal("50000"),
            quantity_unit="pcs",
            price_amount=Decimal("42.00"),
            price_currency="INR",
            price_per_unit="pcs",
            location_city="Surat",
            location_state="Gujarat",
            location_country="India",
            latitude=Decimal("21.1702"),
            longitude=Decimal("72.8311"),
            location_raw="Surat, Gujarat, India",
            deadline_at=now + timedelta(days=45),
            deadline_raw="within 45 days",
            expires_at=now + timedelta(days=90),
            product_details={"name": "corrugated box", **box_seller_specs},
            embedding_status=EmbeddingStatus.PENDING,
        )
        refresh_index_fields(rfq_s1_box)
        db.add(rfq_s1_box)

        # Match Pair 2: Electronics - Power Bank (Buyer1 & Seller2)
        spec_power = next(p for p in CATALOGUE if p["product"] == "power bank")
        pb_specs_b = {
            "capacity": {"value": 10000, "unit": "mAh"}, "output_power": {"value": 45, "unit": "W"},
            "ports": 3, "port_layout": "2x USB-C + 1x USB-A", "cell_chemistry": "lithium polymer",
            "fast_charge_protocol": "USB PD 3.0"
        }
        rfq_b1_pb = RFQ(
            user_id=b1.id,
            role=RFQRole.BUYER,
            status=RFQStatus.ACTIVE,
            category="Electronics",
            title="Need 5,000 pcs of 10000 mAh power bank — 2x USB-C + 1x USB-A, 45W",
            description="Sourcing OEM 10000mAh slim power banks with USB PD 3.0 45W fast charge support. Seeking CE & BIS certified vendors.",
            quantity_value=Decimal("5000"),
            quantity_unit="pcs",
            price_amount=Decimal("950.00"),
            price_currency="INR",
            price_per_unit="pcs",
            location_city="Mumbai",
            location_state="Maharashtra",
            location_country="India",
            latitude=Decimal("18.921984"),
            longitude=Decimal("72.834654"),
            location_raw="Mumbai, India",
            deadline_at=now + timedelta(days=45),
            deadline_raw="within 45 days",
            expires_at=now + timedelta(days=90),
            product_details={"name": "power bank", **pb_specs_b},
            embedding_status=EmbeddingStatus.PENDING,
        )
        refresh_index_fields(rfq_b1_pb)
        db.add(rfq_b1_pb)

        pb_specs_s = {
            "capacity": {"value": 10000, "unit": "mAh"}, "output_power": {"value": 45, "unit": "W"},
            "ports": 3, "port_layout": "2x USB-C + 1x USB-A", "cell_chemistry": "lithium polymer",
            "fast_charge_protocol": "USB PD 3.0", "certification": ["CE", "RoHS", "UN38.3"]
        }
        rfq_s2_pb = RFQ(
            user_id=s2.id,
            role=RFQRole.SELLER,
            status=RFQStatus.ACTIVE,
            category="Electronics",
            title="Supplying 30,000 pcs of 10000 mAh power bank — 45W PD, Li-polymer",
            description="Factory direct supply of 10000mAh PD45W fast charging power banks. CE, RoHS, and UN38.3 aviation approved. Custom logo printing available.",
            quantity_value=Decimal("30000"),
            quantity_unit="pcs",
            price_amount=Decimal("10.50"),
            price_currency="USD",
            price_per_unit="pcs",
            location_city="Shenzhen",
            location_state="Guangdong",
            location_country="China",
            latitude=Decimal("22.5431"),
            longitude=Decimal("114.0579"),
            location_raw="Shenzhen, Guangdong, China",
            deadline_at=now + timedelta(days=60),
            deadline_raw="within 60 days",
            expires_at=now + timedelta(days=90),
            product_details={"name": "power bank", **pb_specs_s},
            embedding_status=EmbeddingStatus.PENDING,
        )
        refresh_index_fields(rfq_s2_pb)
        db.add(rfq_s2_pb)

        # Match Pair 3: Agriculture - Basmati Rice (Buyer1 & Seller3)
        rice_specs = {"variety": "1121 steam", "grain_length": {"value": 8.35, "unit": "mm"}, "broken": {"value": 1.0, "unit": "%"}}
        rfq_b1_rice = RFQ(
            user_id=b1.id,
            role=RFQRole.BUYER,
            status=RFQStatus.ACTIVE,
            category="Agriculture",
            title="Need 25 metric tons of 1121 steam basmati rice — 8.35mm grain length",
            description="Bulk procurement of premium export-grade 1121 steam basmati rice in 25kg PP bags. Requires phytosanitary and fumigation certificates.",
            quantity_value=Decimal("25"),
            quantity_unit="tons",
            price_amount=Decimal("95000.00"),
            price_currency="INR",
            price_per_unit="tons",
            location_city="Mumbai",
            location_state="Maharashtra",
            location_country="India",
            latitude=Decimal("18.921984"),
            longitude=Decimal("72.834654"),
            location_raw="Mumbai Port, India",
            deadline_at=now + timedelta(days=25),
            deadline_raw="within 25 days",
            expires_at=now + timedelta(days=60),
            product_details={"name": "basmati rice", **rice_specs},
            embedding_status=EmbeddingStatus.PENDING,
        )
        refresh_index_fields(rfq_b1_rice)
        db.add(rfq_b1_rice)

        rfq_s3_rice = RFQ(
            user_id=s3.id,
            role=RFQRole.SELLER,
            status=RFQStatus.ACTIVE,
            category="Agriculture",
            title="Supplying 100 metric tons of 1121 steam basmati rice — 8.4mm aged 2 years",
            description="A-grade aged 1121 steamed basmati rice with exceptional elongation ratio. Direct from mill in Amritsar. Container stuffed at ICD Ludhiana.",
            quantity_value=Decimal("100"),
            quantity_unit="tons",
            price_amount=Decimal("92000.00"),
            price_currency="INR",
            price_per_unit="tons",
            location_city="Amritsar",
            location_state="Punjab",
            location_country="India",
            latitude=Decimal("31.6340"),
            longitude=Decimal("74.8723"),
            location_raw="Amritsar, Punjab, India",
            deadline_at=now + timedelta(days=40),
            deadline_raw="within 40 days",
            expires_at=now + timedelta(days=90),
            product_details={"name": "basmati rice", **rice_specs},
            embedding_status=EmbeddingStatus.PENDING,
        )
        refresh_index_fields(rfq_s3_rice)
        db.add(rfq_s3_rice)

        # Match Pair 4: Metals - Stainless Steel Sheet (Buyer2 & Seller4)
        steel_specs = {"grade": "304", "finish": "2B", "thickness": {"value": 1.5, "unit": "mm"}}
        rfq_b2_steel = RFQ(
            user_id=b2.id,
            role=RFQRole.BUYER,
            status=RFQStatus.ACTIVE,
            category="Industrial",
            title="Need 15 metric tons of stainless steel sheet — Grade 304 2B finish 1.5mm",
            description="Sourcing cold-rolled AISI 304 2B stainless steel sheets for fabrication. Mill test certificates (EN 10204 3.1) required.",
            quantity_value=Decimal("15"),
            quantity_unit="tons",
            price_amount=Decimal("2350.00"),
            price_currency="GBP",
            price_per_unit="tons",
            location_city="London",
            location_state="England",
            location_country="United Kingdom",
            latitude=Decimal("51.5144"),
            longitude=Decimal("-0.0803"),
            location_raw="London, UK",
            deadline_at=now + timedelta(days=35),
            deadline_raw="within 35 days",
            expires_at=now + timedelta(days=70),
            product_details={"name": "stainless steel sheet", **steel_specs},
            embedding_status=EmbeddingStatus.PENDING,
        )
        refresh_index_fields(rfq_b2_steel)
        db.add(rfq_b2_steel)

        rfq_s4_steel = RFQ(
            user_id=s4.id,
            role=RFQRole.SELLER,
            status=RFQStatus.ACTIVE,
            category="Industrial",
            title="Supplying 50 metric tons of stainless steel sheet — Grade 304, 1.5mm cold rolled",
            description="Ready stock prime SS304 coils and cut sheets, PVC film coated, export seaworthy packing. CIF London or FOB Jebel Ali.",
            quantity_value=Decimal("50"),
            quantity_unit="tons",
            price_amount=Decimal("10500.00"),
            price_currency="AED",
            price_per_unit="tons",
            location_city="Dubai",
            location_state="Dubai",
            location_country="United Arab Emirates",
            latitude=Decimal("24.9942"),
            longitude=Decimal("55.0745"),
            location_raw="Dubai, JAFZA, UAE",
            deadline_at=now + timedelta(days=50),
            deadline_raw="within 50 days",
            expires_at=now + timedelta(days=90),
            product_details={"name": "stainless steel sheet", **steel_specs},
            embedding_status=EmbeddingStatus.PENDING,
        )
        refresh_index_fields(rfq_s4_steel)
        db.add(rfq_s4_steel)

        # Match Pair 5: Packaging - FIBC Bulk Bag (Buyer3 & Seller1)
        fibc_specs = {"capacity": {"value": 1000, "unit": "kg"}, "loops": 4, "baffle": True}
        rfq_b3_fibc = RFQ(
            user_id=b3.id,
            role=RFQRole.BUYER,
            status=RFQStatus.ACTIVE,
            category="Packaging",
            title="Need 5,000 pcs of FIBC bulk bag — 1000kg safe working load",
            description="Seeking food-grade 1-ton jumbo bags with corner loops and discharge spout for mineral and grain export.",
            quantity_value=Decimal("5000"),
            quantity_unit="pcs",
            price_amount=Decimal("18.00"),
            price_currency="SGD",
            price_per_unit="pcs",
            location_city="Singapore",
            location_state="Central",
            location_country="Singapore",
            latitude=Decimal("1.2789"),
            longitude=Decimal("103.8536"),
            location_raw="Singapore Port",
            deadline_at=now + timedelta(days=30),
            deadline_raw="within 30 days",
            expires_at=now + timedelta(days=60),
            product_details={"name": "FIBC bulk bag", **fibc_specs},
            embedding_status=EmbeddingStatus.PENDING,
        )
        refresh_index_fields(rfq_b3_fibc)
        db.add(rfq_b3_fibc)

        rfq_s1_fibc = RFQ(
            user_id=s1.id,
            role=RFQRole.SELLER,
            status=RFQStatus.ACTIVE,
            category="Packaging",
            title="Supplying 20,000 pcs of FIBC bulk bag — 1000kg safe working load",
            description="UV-stabilized PP woven jumbo bulk bags with 5:1 safety factor. High-tensile cross-corner loops, laminated coating.",
            quantity_value=Decimal("20000"),
            quantity_unit="pcs",
            price_amount=Decimal("950.00"),
            price_currency="INR",
            price_per_unit="pcs",
            location_city="Surat",
            location_state="Gujarat",
            location_country="India",
            latitude=Decimal("21.1702"),
            longitude=Decimal("72.8311"),
            location_raw="Surat, Gujarat, India",
            deadline_at=now + timedelta(days=45),
            deadline_raw="within 45 days",
            expires_at=now + timedelta(days=90),
            product_details={"name": "FIBC bulk bag", **fibc_specs},
            embedding_status=EmbeddingStatus.PENDING,
        )
        refresh_index_fields(rfq_s1_fibc)
        db.add(rfq_s1_fibc)

        # Match Pair 6: Tiles (Buyer1 & Seller5 for Dispute Testing)
        tile_specs = {"size": "600x600mm", "thickness": {"value": 9, "unit": "mm"}, "finish": "glossy"}
        rfq_b1_tile = RFQ(
            user_id=b1.id,
            role=RFQRole.BUYER,
            status=RFQStatus.ACTIVE,
            category="Construction",
            title="Need 2,000 sqm of vitrified floor tile — 600x600mm glossy finish",
            description="Procuring double charge vitrified polished floor tiles for a commercial warehouse project.",
            quantity_value=Decimal("2000"),
            quantity_unit="sqm",
            price_amount=Decimal("400.00"),
            price_currency="INR",
            price_per_unit="sqm",
            location_city="Mumbai",
            location_state="Maharashtra",
            location_country="India",
            latitude=Decimal("18.921984"),
            longitude=Decimal("72.834654"),
            location_raw="Mumbai, Maharashtra, India",
            deadline_at=now + timedelta(days=20),
            deadline_raw="within 20 days",
            expires_at=now + timedelta(days=60),
            product_details={"name": "vitrified floor tile", **tile_specs},
            embedding_status=EmbeddingStatus.PENDING,
        )
        refresh_index_fields(rfq_b1_tile)
        db.add(rfq_b1_tile)

        rfq_s5_tile = RFQ(
            user_id=s5.id,
            role=RFQRole.SELLER,
            status=RFQStatus.ACTIVE,
            category="Construction",
            title="Supplying 10,000 sqm of vitrified floor tile — 600x600mm polished",
            description="Export grade nano-polished double-charge vitrified floor tiles. Box packaged on wooden pallets with shrink wrap.",
            quantity_value=Decimal("10000"),
            quantity_unit="sqm",
            price_amount=Decimal("380.00"),
            price_currency="INR",
            price_per_unit="sqm",
            location_city="Ahmedabad",
            location_state="Gujarat",
            location_country="India",
            latitude=Decimal("23.0225"),
            longitude=Decimal("72.5714"),
            location_raw="Ahmedabad, Gujarat, India",
            deadline_at=now + timedelta(days=30),
            deadline_raw="within 30 days",
            expires_at=now + timedelta(days=90),
            product_details={"name": "vitrified floor tile", **tile_specs},
            embedding_status=EmbeddingStatus.PENDING,
        )
        refresh_index_fields(rfq_s5_tile)
        db.add(rfq_s5_tile)

        # Special RFQ Statuses for QA testing
        # 1. Draft RFQ
        rfq_draft = RFQ(
            user_id=b1.id,
            role=RFQRole.BUYER,
            status=RFQStatus.DRAFT,
            category="Furniture",
            title="Need 1,000 units of ergonomic office chair — breathable mesh, 3D armrests",
            description="Draft procurement spec for corporate HQ fit-out. Needs gas lift class 4 and tilt-lock mechanism.",
            quantity_value=Decimal("1000"),
            quantity_unit="pcs",
            price_amount=Decimal("4500.00"),
            price_currency="INR",
            price_per_unit="pcs",
            location_city="Mumbai",
            location_country="India",
            deadline_at=now + timedelta(days=60),
            expires_at=now + timedelta(days=90),
            product_details={"name": "ergonomic office chair", "back_material": "mesh", "lumbar_support": True},
            embedding_status=EmbeddingStatus.PENDING,
        )
        refresh_index_fields(rfq_draft)
        db.add(rfq_draft)

        # 2. Expired RFQ
        rfq_expired = RFQ(
            user_id=b1.id,
            role=RFQRole.BUYER,
            status=RFQStatus.EXPIRED,
            category="Electronics",
            title="Need 500 pcs of GaN wall charger — 65W dual USB-C (Expired Listing)",
            description="Listing has passed its active deadline for procurement.",
            quantity_value=Decimal("500"),
            quantity_unit="pcs",
            price_amount=Decimal("1200.00"),
            price_currency="INR",
            price_per_unit="pcs",
            location_city="Mumbai",
            location_country="India",
            deadline_at=now - timedelta(days=15),
            expires_at=now - timedelta(days=5),
            product_details={"name": "GaN wall charger", "output_power": {"value": 65, "unit": "W"}},
            embedding_status=EmbeddingStatus.PENDING,
        )
        refresh_index_fields(rfq_expired)
        db.add(rfq_expired)

        # 3. Closed RFQ
        rfq_closed = RFQ(
            user_id=b1.id,
            role=RFQRole.BUYER,
            status=RFQStatus.CLOSED,
            category="Chemicals",
            title="Need 3,000 kg of titanium dioxide — Anatase grade 98% (Contract Closed)",
            description="Procurement successfully completed and contract awarded.",
            quantity_value=Decimal("3000"),
            quantity_unit="kg",
            price_amount=Decimal("220.00"),
            price_currency="INR",
            price_per_unit="kg",
            location_city="Mumbai",
            location_country="India",
            deadline_at=now - timedelta(days=30),
            expires_at=now + timedelta(days=30),
            product_details={"name": "titanium dioxide", "grade": "Anatase"},
            embedding_status=EmbeddingStatus.PENDING,
        )
        refresh_index_fields(rfq_closed)
        db.add(rfq_closed)

        # Dual Account listings for demo@marketplace.dev
        spec_cables = next(p for p in CATALOGUE if p["product"] == "USB Type-C cable")
        rfq_demo_buy = RFQ(
            user_id=demo_user.id,
            role=RFQRole.BUYER,
            status=RFQStatus.ACTIVE,
            category="Electronics",
            title="Need 15,000 pcs of USB Type-C cable — 100W PD braided nylon 1.8m",
            description="High-spec retail blister packaged USB-C cables with e-marker IC.",
            quantity_value=Decimal("15000"),
            quantity_unit="pcs",
            price_amount=Decimal("175.00"),
            price_currency="INR",
            price_per_unit="pcs",
            location_city="Mumbai",
            location_country="India",
            deadline_at=now + timedelta(days=30),
            expires_at=now + timedelta(days=60),
            product_details={"name": "USB Type-C cable", "power_delivery": {"value": 100, "unit": "W"}, "length": {"value": 1.8, "unit": "m"}},
            embedding_status=EmbeddingStatus.PENDING,
        )
        refresh_index_fields(rfq_demo_buy)
        db.add(rfq_demo_buy)

        rfq_demo_sell = RFQ(
            user_id=demo_user.id,
            role=RFQRole.SELLER,
            status=RFQStatus.ACTIVE,
            category="Packaging",
            title="Supplying 25,000 pcs of 3-ply corrugated box — custom printed mailers",
            description="E-commerce die-cut mailer boxes with self-sealing adhesive strips.",
            quantity_value=Decimal("25000"),
            quantity_unit="pcs",
            price_amount=Decimal("28.00"),
            price_currency="INR",
            price_per_unit="pcs",
            location_city="Mumbai",
            location_country="India",
            deadline_at=now + timedelta(days=45),
            expires_at=now + timedelta(days=90),
            product_details={"name": "corrugated box", "ply": 3, "color": "white", "gsm": 150},
            embedding_status=EmbeddingStatus.PENDING,
        )
        refresh_index_fields(rfq_demo_sell)
        db.add(rfq_demo_sell)

        await db.flush()

        # Generate additional active RFQs across categories to give market depth
        all_active_users = buyers_pool + sellers_pool + background_users + [demo_user, trader1]
        logger.info("Generating %d varied listings across 8 categories from 62 global cities...", rfq_count)
        for i in range(rfq_count):
            user = all_active_users[i % len(all_active_users)]
            role = RFQRole.SELLER if user.role is UserRole.SELLER else (RFQRole.BUYER if user.role is UserRole.BUYER else (RFQRole.BUYER if i % 2 == 0 else RFQRole.SELLER))
            spec = rng.choice(CATALOGUE)
            city = rng.choice(list(CITIES.values()))
            attrs = _draw_specs(rng, spec)
            low, high = spec["qty"]
            quantity = rng.randrange(low, high, max(1, low // 5))
            days = rng.choice([7, 14, 21, 30, 45, 60, 90])
            deadline = now + timedelta(days=days)

            r = RFQ(
                user_id=user.id,
                role=role,
                status=RFQStatus.ACTIVE,
                category=spec["category"],
                title=_title(spec, role, quantity, attrs),
                description=f"{'Sourcing high-grade' if role is RFQRole.BUYER else 'Ready factory dispatch of'} {spec['product']}. Standard export packaging with inspection certs.",
                quantity_value=Decimal(str(quantity)),
                quantity_unit=spec["unit"],
                price_amount=_price(rng, spec, role, city),
                price_currency=city.currency,
                price_per_unit=spec["unit"],
                location_city=city.name,
                location_state=city.region,
                location_country=city.country,
                latitude=Decimal(str(city.latitude)),
                longitude=Decimal(str(city.longitude)),
                location_raw=f"{city.name}, {city.country}",
                deadline_at=deadline,
                deadline_raw=f"within {days} days",
                expires_at=deadline + timedelta(days=30),
                product_details={"name": spec["product"], **attrs},
                embedding_status=EmbeddingStatus.PENDING,
            )
            refresh_index_fields(r)
            db.add(r)
            if i % 100 == 0:
                await db.flush()

        await db.flush()
        logger.info("✓ RFQs generated and flushed to database.")

        # =====================================================================
        # 4. BOOKMARKED / SAVED RFQs
        # =====================================================================
        logger.info("Seeding Saved / Bookmarked RFQs...")
        saved_items = [
            SavedRFQ(user_id=b1.id, rfq_id=rfq_s1_box.id),
            SavedRFQ(user_id=b1.id, rfq_id=rfq_s2_pb.id),
            SavedRFQ(user_id=s1.id, rfq_id=rfq_b1_box.id),
            SavedRFQ(user_id=b2.id, rfq_id=rfq_s4_steel.id),
        ]
        db.add_all(saved_items)
        await db.flush()
        logger.info("✓ 4 Saved RFQs created.")

        # =====================================================================
        # 5. DEAL ROOMS: CONNECTIONS, MESSAGES, QUOTES, ESCROW, SHIPMENTS, REVIEWS
        # =====================================================================
        logger.info("Seeding Realistic Deal Rooms & Complete Transaction Lifecycle...")

        # ---------------------------------------------------------------------
        # Deal Room 1: FULL GOLDEN PATH COMPLETED (Buyer 1 + Seller 1 on Corrugated Boxes)
        # ---------------------------------------------------------------------
        conn1 = Connection(
            sender_id=b1.id,
            receiver_id=s1.id,
            rfq_id=rfq_s1_box.id,
            status=ConnectionStatus.ACCEPTED,
        )
        db.add(conn1)
        await db.flush()

        # Messages
        t_base = now - timedelta(days=8)
        msgs1 = [
            ConnectionMessage(
                connection_id=conn1.id, sender_id=b1.id,
                content="Hello Rohit, we reviewed your 5-ply corrugated carton listing. We require 500 units initially for test dispatch with 180 GSM burst factor. Can you supply this week?",
                created_at=t_base,
            ),
            ConnectionMessage(
                connection_id=conn1.id, sender_id=s1.id,
                content="Hello Aarti! Yes, we have ready stock at our Surat plant. We can dispatch 500 units within 48 hours of PO confirmation. Burst factor tested at 14.5 kg/cm².",
                created_at=t_base + timedelta(hours=2),
            ),
            ConnectionMessage(
                connection_id=conn1.id, sender_id=b1.id,
                content="Excellent. Please issue a formal quotation with FOB terms and standard 3-tranche milestone escrow.",
                created_at=t_base + timedelta(hours=4),
            ),
            ConnectionMessage(
                connection_id=conn1.id, sender_id=s1.id,
                content="I have issued Quotation #QT-2026-001 at ₹42.00/unit with 18% GST and 10 days lead time.",
                created_at=t_base + timedelta(hours=5),
            ),
            ConnectionMessage(
                connection_id=conn1.id, sender_id=b1.id,
                content="Quotation accepted and Purchase Order #PO-2026-09-001 generated. Escrow vault has been funded.",
                created_at=t_base + timedelta(days=1),
            ),
            ConnectionMessage(
                connection_id=conn1.id, sender_id=s1.id,
                content="Goods have been strapped on pallets and handed over to Blue Dart. Waybill #WB-2026-09-55102 uploaded.",
                created_at=t_base + timedelta(days=3),
            ),
            ConnectionMessage(
                connection_id=conn1.id, sender_id=b1.id,
                content="Consignment received in Mumbai warehouse in immaculate condition. Releasing final milestone and submitting our 5-star review!",
                created_at=t_base + timedelta(days=5),
            ),
        ]
        db.add_all(msgs1)
        await db.flush()

        # Quotation 1 (COMPLETED)
        q1 = Quotation(
            connection_id=conn1.id,
            sender_id=s1.id,
            receiver_id=b1.id,
            rfq_id=rfq_s1_box.id,
            quote_number="QT-2026-001",
            version=1,
            status=QuotationStatus.COMPLETED,
            unit_price=Decimal("42.00"),
            currency="INR",
            quantity=Decimal("500"),
            quantity_unit="pcs",
            total_amount=Decimal("21000.00"),
            lead_time_days=10,
            incoterms=Incoterm.FOB,
            payment_terms="30-40-30 Escrow Milestones",
            valid_until=now + timedelta(days=30),
            notes="Standard brown export shipper carton with moisture barrier.",
            purchase_order_reference="PO-2026-09-001",
        )
        db.add(q1)
        await db.flush()

        # Escrow 1 (COMPLETED & ALL RELEASED)
        escrow1 = EscrowAccount(
            connection_id=conn1.id,
            quotation_id=q1.id,
            buyer_id=b1.id,
            seller_id=s1.id,
            currency="INR",
            total_amount=Decimal("21000.00"),
            funded_amount=Decimal("21000.00"),
            released_amount=Decimal("21000.00"),
            refunded_amount=Decimal("0.00"),
            status="completed",
        )
        db.add(escrow1)
        await db.flush()

        m1_1 = EscrowMilestone(
            escrow_account_id=escrow1.id,
            title="30% Advance Deposit against Confirmed PO",
            percentage=Decimal("30.00"),
            amount=Decimal("6300.00"),
            order_index=1,
            status="released",
            released_at=t_base + timedelta(days=2),
            release_note="Approved upon PO generation",
        )
        m1_2 = EscrowMilestone(
            escrow_account_id=escrow1.id,
            title="40% Dispatch Milestone upon Cargo Waybill Issuance",
            percentage=Decimal("40.00"),
            amount=Decimal("8400.00"),
            order_index=2,
            status="released",
            released_at=t_base + timedelta(days=3, hours=4),
            release_note="Approved upon Blue Dart tracking verification",
        )
        m1_3 = EscrowMilestone(
            escrow_account_id=escrow1.id,
            title="30% Final Delivery & Inspection Acceptance",
            percentage=Decimal("30.00"),
            amount=Decimal("6300.00"),
            order_index=3,
            status="released",
            released_at=t_base + timedelta(days=5),
            release_note="Consignment inspected and accepted at warehouse",
        )
        db.add_all([m1_1, m1_2, m1_3])

        # Shipment 1 (DELIVERED)
        shipment1 = Shipment(
            connection_id=conn1.id,
            quotation_id=q1.id,
            sender_id=s1.id,
            receiver_id=b1.id,
            tracking_number="BD-IN-2026-98124",
            carrier_name="Blue Dart Express",
            carrier_service="Apex Priority Freight",
            shipping_mode=ShippingMode.ROAD.value,
            status=ShipmentStatus.DELIVERED.value,
            origin_city="Surat",
            origin_state="Gujarat",
            origin_country="India",
            origin_address="GIDC Sachin Industrial Area, Surat",
            destination_city="Mumbai",
            destination_state="Maharashtra",
            destination_country="India",
            destination_address="42 Nariman Point, Mumbai",
            weight_kg=Decimal("420.000"),
            volume_cbm=Decimal("2.800"),
            package_count=50,
            package_type="Strapped Pallets",
            bill_of_lading_number="WB-2026-09-55102",
            estimated_delivery_date=now - timedelta(days=3),
            dispatched_at=now - timedelta(days=5),
            delivered_at=now - timedelta(days=3),
            tracking_events=[
                {"timestamp": (now - timedelta(days=5)).isoformat(), "location": "Surat Hub", "status": "Manifest Booked", "description": "Consignment booked and sealed onto carrier truck."},
                {"timestamp": (now - timedelta(days=4)).isoformat(), "location": "Valsad Interchange", "status": "In Transit", "description": "Intercity transit checkpoint cleared."},
                {"timestamp": (now - timedelta(days=3, hours=12)).isoformat(), "location": "Bhiwandi Gateway", "status": "Out for Delivery", "description": "Loaded on local delivery van for Mumbai south."},
                {"timestamp": (now - timedelta(days=3)).isoformat(), "location": "Nariman Point, Mumbai", "status": "Delivered", "description": "Delivered and signed by warehouse in-charge Mr. K. Patil."},
            ],
        )
        db.add(shipment1)

        # Mutual Reviews (Both submitted)
        rev1_buyer = Review(
            quotation_id=q1.id,
            connection_id=conn1.id,
            reviewer_id=b1.id,
            reviewee_id=s1.id,
            rating=5,
            communication_rating=5,
            delivery_rating=5,
            quality_rating=5,
            comment="Flawless transaction! Corrugated boxes exceeded burst strength specs and arrived 1 day ahead of schedule. Global Pack is now our preferred packaging supplier.",
        )
        rev1_seller = Review(
            quotation_id=q1.id,
            connection_id=conn1.id,
            reviewer_id=s1.id,
            reviewee_id=b1.id,
            rating=5,
            communication_rating=5,
            delivery_rating=5,
            quality_rating=5,
            comment="Outstanding client. Crystal clear specification parameters and instant escrow milestone releases upon verification. Highly recommended buyer!",
        )
        db.add_all([rev1_buyer, rev1_seller])
        await db.flush()

        # ---------------------------------------------------------------------
        # Deal Room 2: ACTIVE NEGOTIATION / QUOTE PENDING (Buyer 1 + Seller 2 on Power Banks)
        # ---------------------------------------------------------------------
        conn2 = Connection(
            sender_id=b1.id,
            receiver_id=s2.id,
            rfq_id=rfq_s2_pb.id,
            status=ConnectionStatus.ACCEPTED,
        )
        db.add(conn2)
        await db.flush()

        msgs2 = [
            ConnectionMessage(
                connection_id=conn2.id, sender_id=b1.id,
                content="Hello Nova Electronics, we are interested in placing a 2,500 unit trial order for your 10,000mAh PD45W power banks. Can you support custom laser engraving?",
                created_at=now - timedelta(days=2),
            ),
            ConnectionMessage(
                connection_id=conn2.id, sender_id=s2.id,
                content="Hello Aarti! Absolutely. Laser logo engraving is complimentary for orders over 2,000 units. We include the UN38.3 battery declaration for air/sea transit.",
                created_at=now - timedelta(days=1, hours=18),
            ),
            ConnectionMessage(
                connection_id=conn2.id, sender_id=s2.id,
                content="I have submitted Quotation #QT-2026-002 at $10.20/pc CIF Mumbai. Please review the terms.",
                created_at=now - timedelta(hours=8),
            ),
        ]
        db.add_all(msgs2)

        # Quotation 2 (PENDING - Ready for QA to Accept/Counter/Reject)
        q2 = Quotation(
            connection_id=conn2.id,
            sender_id=s2.id,
            receiver_id=b1.id,
            rfq_id=rfq_s2_pb.id,
            quote_number="QT-2026-002",
            version=1,
            status=QuotationStatus.PENDING,
            unit_price=Decimal("10.20"),
            currency="USD",
            quantity=Decimal("2500"),
            quantity_unit="pcs",
            total_amount=Decimal("25500.00"),
            lead_time_days=20,
            incoterms=Incoterm.CIF,
            payment_terms="30-40-30 Escrow Milestones",
            valid_until=now + timedelta(days=25),
            notes="Includes custom laser branding and CE/RoHS compliance certification.",
        )
        db.add(q2)
        await db.flush()

        # ---------------------------------------------------------------------
        # Deal Room 3: ESCROW FUNDED & SHIPMENT IN TRANSIT (Buyer 2 + Seller 4 on Steel)
        # ---------------------------------------------------------------------
        conn3 = Connection(
            sender_id=b2.id,
            receiver_id=s4.id,
            rfq_id=rfq_s4_steel.id,
            status=ConnectionStatus.ACCEPTED,
        )
        db.add(conn3)
        await db.flush()

        q3 = Quotation(
            connection_id=conn3.id,
            sender_id=s4.id,
            receiver_id=b2.id,
            rfq_id=rfq_s4_steel.id,
            quote_number="QT-2026-003",
            version=1,
            status=QuotationStatus.DISPATCHED,
            unit_price=Decimal("2850.00"),
            currency="USD",
            quantity=Decimal("5"),
            quantity_unit="tons",
            total_amount=Decimal("14250.00"),
            lead_time_days=14,
            incoterms=Incoterm.CIF,
            payment_terms="30-40-30 Escrow Milestones",
            valid_until=now + timedelta(days=20),
            notes="Prime cold rolled SS304 sheets, seaworthy wooden box packing.",
            purchase_order_reference="PO-2026-09-003",
        )
        db.add(q3)
        await db.flush()

        escrow3 = EscrowAccount(
            connection_id=conn3.id,
            quotation_id=q3.id,
            buyer_id=b2.id,
            seller_id=s4.id,
            currency="USD",
            total_amount=Decimal("14250.00"),
            funded_amount=Decimal("14250.00"),
            released_amount=Decimal("4275.00"),
            refunded_amount=Decimal("0.00"),
            status="funded",
        )
        db.add(escrow3)
        await db.flush()

        m3_1 = EscrowMilestone(
            escrow_account_id=escrow3.id,
            title="30% Advance Deposit against Confirmed PO",
            percentage=Decimal("30.00"),
            amount=Decimal("4275.00"),
            order_index=1,
            status="released",
            released_at=now - timedelta(days=3),
            release_note="Advance released upon PO issuance",
        )
        m3_2 = EscrowMilestone(
            escrow_account_id=escrow3.id,
            title="40% Dispatch Milestone upon Cargo Waybill Issuance",
            percentage=Decimal("40.00"),
            amount=Decimal("5700.00"),
            order_index=2,
            status="release_requested",
            release_note="Seller requested release: Container MSKU-881920 loaded onto vessel MSC GULSUN.",
        )
        m3_3 = EscrowMilestone(
            escrow_account_id=escrow3.id,
            title="30% Final Delivery & Inspection Acceptance",
            percentage=Decimal("30.00"),
            amount=Decimal("4275.00"),
            order_index=3,
            status="pending",
        )
        db.add_all([m3_1, m3_2, m3_3])

        shipment3 = Shipment(
            connection_id=conn3.id,
            quotation_id=q3.id,
            sender_id=s4.id,
            receiver_id=b2.id,
            tracking_number="MSK-SEA-8840192",
            carrier_name="Maersk Ocean Line",
            carrier_service="AE11 Asia-Europe Seaborne",
            shipping_mode=ShippingMode.OCEAN.value,
            status=ShipmentStatus.IN_TRANSIT.value,
            origin_city="Dubai",
            origin_country="United Arab Emirates",
            destination_city="London",
            destination_country="United Kingdom",
            weight_kg=Decimal("5120.000"),
            volume_cbm=Decimal("8.500"),
            package_count=4,
            package_type="Seaworthy Wooden Crates",
            bill_of_lading_number="BL-MSK-2026-771",
            estimated_delivery_date=now + timedelta(days=12),
            dispatched_at=now - timedelta(days=3),
            tracking_events=[
                {"timestamp": (now - timedelta(days=3)).isoformat(), "location": "Jebel Ali Port", "status": "Loaded onto Vessel", "description": "Container MSKU-881920 loaded on vessel MSC GULSUN."},
                {"timestamp": (now - timedelta(days=1)).isoformat(), "location": "Strait of Hormuz", "status": "In Transit", "description": "Vessel en route to Port of Felixstowe / London Gateway."},
            ],
        )
        db.add(shipment3)

        # ---------------------------------------------------------------------
        # Deal Room 4: PENDING INCOMING REQUEST FOR SELLER 1 (From Buyer 3)
        # ---------------------------------------------------------------------
        conn4 = Connection(
            sender_id=b3.id,
            receiver_id=s1.id,
            rfq_id=rfq_s1_fibc.id,
            status=ConnectionStatus.PENDING,
        )
        db.add(conn4)
        await db.flush()

        msg4 = ConnectionMessage(
            connection_id=conn4.id,
            sender_id=b3.id,
            content="Hello Global Pack Solutions, we need 5,000 units of 1-ton FIBC bulk bags for grain shipment from Singapore. Please accept connection to discuss specifications.",
            created_at=now - timedelta(hours=3),
        )
        db.add(msg4)

        # ---------------------------------------------------------------------
        # Deal Room 5: PENDING OUTGOING REQUEST FOR BUYER 1 (To Seller 3)
        # ---------------------------------------------------------------------
        conn5 = Connection(
            sender_id=b1.id,
            receiver_id=s3.id,
            rfq_id=rfq_s3_rice.id,
            status=ConnectionStatus.PENDING,
        )
        db.add(conn5)
        await db.flush()

        msg5 = ConnectionMessage(
            connection_id=conn5.id,
            sender_id=b1.id,
            content="Greetings Indus Agro. We are looking to contract 25 tons of aged 1121 steam basmati rice. Please accept connection to review delivery timeline.",
            created_at=now - timedelta(hours=5),
        )
        db.add(msg5)

        # ---------------------------------------------------------------------
        # Deal Room 6: ACTIVE DISPUTE (Buyer 1 + Seller 5 on Tiles)
        # ---------------------------------------------------------------------
        conn6 = Connection(
            sender_id=b1.id,
            receiver_id=s5.id,
            rfq_id=rfq_s5_tile.id,
            status=ConnectionStatus.ACCEPTED,
        )
        db.add(conn6)
        await db.flush()

        q6 = Quotation(
            connection_id=conn6.id,
            sender_id=s5.id,
            receiver_id=b1.id,
            rfq_id=rfq_s5_tile.id,
            quote_number="QT-2026-006",
            version=1,
            status=QuotationStatus.DELIVERED,
            unit_price=Decimal("380.00"),
            currency="INR",
            quantity=Decimal("500"),
            quantity_unit="sqm",
            total_amount=Decimal("190000.00"),
            lead_time_days=15,
            incoterms=Incoterm.EXW,
            payment_terms="30-40-30 Escrow",
            valid_until=now + timedelta(days=15),
            purchase_order_reference="PO-2026-09-006",
        )
        db.add(q6)
        await db.flush()

        escrow6 = EscrowAccount(
            connection_id=conn6.id,
            quotation_id=q6.id,
            buyer_id=b1.id,
            seller_id=s5.id,
            currency="INR",
            total_amount=Decimal("190000.00"),
            funded_amount=Decimal("190000.00"),
            released_amount=Decimal("133000.00"),
            refunded_amount=Decimal("0.00"),
            status="disputed",
        )
        db.add(escrow6)
        await db.flush()

        m6_1 = EscrowMilestone(
            escrow_account_id=escrow6.id,
            title="30% Advance Deposit against Confirmed PO",
            percentage=Decimal("30.00"),
            amount=Decimal("57000.00"),
            order_index=1,
            status="released",
            released_at=now - timedelta(days=10),
        )
        m6_2 = EscrowMilestone(
            escrow_account_id=escrow6.id,
            title="40% Dispatch Milestone upon Cargo Waybill Issuance",
            percentage=Decimal("40.00"),
            amount=Decimal("76000.00"),
            order_index=2,
            status="released",
            released_at=now - timedelta(days=6),
        )
        m6_3 = EscrowMilestone(
            escrow_account_id=escrow6.id,
            title="30% Final Delivery & Inspection Acceptance",
            percentage=Decimal("30.00"),
            amount=Decimal("57000.00"),
            order_index=3,
            status="disputed",
        )
        db.add_all([m6_1, m6_2, m6_3])

        dispute = DealDispute(
            connection_id=conn6.id,
            escrow_account_id=escrow6.id,
            raised_by_id=b1.id,
            title="Severe batch color variance and edge chipping in delivered tiles",
            category="quality",
            severity="high",
            reason="Upon opening 50 boxes at our site, approximately 18% of tiles had edge fractures and color shading differed markedly from agreed sample #V-882.",
            status="under_review",
            suggested_resolution="Replacement of 90 boxes or partial refund of ₹57,000 from escrow vault.",
        )
        db.add(dispute)
        await db.flush()
        logger.info("✓ 6 Deal Rooms, Quotations, Escrow, and Disputes seeded.")

        # =====================================================================
        # 6. SEED NOTIFICATIONS
        # =====================================================================
        logger.info("Seeding Notifications...")
        notifs = [
            Notification(
                user_id=b1.id,
                type="quotation",
                title="New Quotation Received: Nova Electronics",
                body="Nova Electronics Co submitted formal quotation #QT-2026-002 ($25,500.00) for 10000 mAh Power Bank.",
                link="/messages",
                is_read=False,
            ),
            Notification(
                user_id=b1.id,
                type="escrow",
                title="Dispute Ticket Logged #DISP-001",
                body="Dispute filed on Escrow Vault for Vitrified Tile delivery under review by compliance team.",
                link="/messages",
                is_read=False,
            ),
            Notification(
                user_id=b1.id,
                type="shipment",
                title="Shipment Delivered: BD-IN-2026-98124",
                body="Blue Dart consignment for 5-ply Corrugated Boxes was delivered successfully in Mumbai.",
                link="/messages",
                is_read=True,
            ),
            Notification(
                user_id=s1.id,
                type="connection",
                title="Incoming Connection Request: Pacific Retailers",
                body="Pacific Retailers Inc (Singapore) requested a connection for your FIBC Bulk Bag listing.",
                link="/messages",
                is_read=False,
            ),
            Notification(
                user_id=s1.id,
                type="review",
                title="5-Star Review Received!",
                body="Apex Industrial Procurement rated your transaction 5.0 stars with detailed praise.",
                link="/profile",
                is_read=True,
            ),
            Notification(
                user_id=b2.id,
                type="escrow",
                title="Milestone Release Requested: Tranche 2",
                body="Zenith Metal Works requested release of 40% dispatch milestone ($5,700.00).",
                link="/messages",
                is_read=False,
            ),
        ]
        db.add_all(notifs)

        # =====================================================================
        # 7. SEED AI CHAT CONVERSATIONS
        # =====================================================================
        logger.info("Seeding AI Copilot Conversations...")
        conv_onboard = Conversation(
            user_id=b1.id,
            type=ConversationType.ONBOARDING,
            title="Onboarding: Sourcing Corrugated Cartons",
            state={"product": "corrugated box", "category": "Packaging", "quantity": 10000},
            rfq_id=rfq_b1_box.id,
        )
        db.add(conv_onboard)
        await db.flush()

        msg_c1 = Message(
            conversation_id=conv_onboard.id,
            role=MessageRole.USER,
            content="I need to source 10,000 units of heavy duty 5-ply corrugated carton boxes in Mumbai under ₹45/box.",
        )
        db.add(msg_c1)
        await db.flush()

        msg_c2 = Message(
            conversation_id=conv_onboard.id,
            role=MessageRole.ASSISTANT,
            content="I have extracted your procurement request:\n- **Product**: 5-ply corrugated box\n- **Quantity**: 10,000 pcs\n- **Target Price**: ₹45.00 / pcs\n- **Location**: Mumbai, India\n\nListing has been published to the marketplace and matched against active suppliers.",
        )
        db.add(msg_c2)
        await db.flush()

        # =====================================================================
        # 8. SEED MODERATION LOGS
        # =====================================================================
        logger.info("Seeding Moderation Audit Logs...")
        mod_logs = [
            ModerationLog(
                user_id=b4.id,
                action="rfq_create",
                category="weapons_and_explosives",
                flagged_terms="military suppressor",
                snippet="Looking to import 50 units tactical rifle suppressors and muzzle brakes.",
                blocked=True,
            ),
            ModerationLog(
                user_id=s5.id,
                action="image_moderation",
                category="prohibited_documentation",
                flagged_terms="tampered tax watermark",
                snippet="Uploaded GST certificate showed mismatched digital signature metadata.",
                blocked=True,
            ),
        ]
        db.add_all(mod_logs)

        await db.commit()
        logger.info("✓ All relational database records committed to PostgreSQL.")

    # =====================================================================
    # 9. VECTOR INDEXING INTO QDRANT
    # =====================================================================
    logger.info("Indexing active RFQ vector embeddings into Qdrant (%s)...", settings.QDRANT_COLLECTION)
    async with SessionLocal() as db:
        active_rfqs = (await db.scalars(
            select(RFQ).where(RFQ.status == RFQStatus.ACTIVE).order_by(RFQ.created_at)
        )).all()

        total = len(active_rfqs)
        logger.info("Total active RFQs to embed: %d", total)

        batch_size = 8
        indexed = 0
        for i in range(0, total, batch_size):
            chunk = active_rfqs[i : i + batch_size]
            texts = [build_match_text(r) for r in chunk]
            vectors = await embed_many(texts)
            if vectors and len(vectors) == len(chunk):
                points = [
                    (r.id, v, qdrant_index.build_payload(r))
                    for r, v in zip(chunk, vectors)
                ]
                success = await qdrant_index.upsert(points)
                if success:
                    for r in chunk:
                        r.embedding_status = EmbeddingStatus.INDEXED
                        r.embedded_at = now
                    await db.commit()
                    indexed += len(chunk)
                    logger.info("  Indexed %d / %d RFQs...", indexed, total)
                else:
                    logger.warning("  Qdrant upsert returned False for chunk %d", i)
            else:
                logger.warning("  Embedding model returned None or length mismatch for chunk %d", i)

        logger.info("✓ Vector indexing completed: %d points indexed in Qdrant.", indexed)

    return {
        "status": "success",
        "credentialed_accounts": [
            {"email": "buyer1@marketplace.dev", "role": "buyer", "company": "Apex Industrial Procurement (Mumbai)", "password": MASTER_PASSWORD},
            {"email": "buyer2@marketplace.dev", "role": "buyer", "company": "Sterling Imports Ltd (London)", "password": MASTER_PASSWORD},
            {"email": "buyer3@marketplace.dev", "role": "buyer", "company": "Pacific Retailers Inc (Singapore)", "password": MASTER_PASSWORD},
            {"email": "buyer4@marketplace.dev", "role": "buyer (pending KYC)", "company": "Nordic Tech Sourcing (Berlin)", "password": MASTER_PASSWORD},
            {"email": "buyer5@marketplace.dev", "role": "buyer", "company": "Orient Supply Group (Dubai)", "password": MASTER_PASSWORD},
            {"email": "seller1@marketplace.dev", "role": "seller (ISO 9001)", "company": "Global Pack Solutions Ltd (Surat)", "password": MASTER_PASSWORD},
            {"email": "seller2@marketplace.dev", "role": "seller (CE Mark)", "company": "Nova Electronics Co (Shenzhen)", "password": MASTER_PASSWORD},
            {"email": "seller3@marketplace.dev", "role": "seller (FDA)", "company": "Indus Agro Exports (Punjab)", "password": MASTER_PASSWORD},
            {"email": "seller4@marketplace.dev", "role": "seller", "company": "Zenith Metal Works (Dubai)", "password": MASTER_PASSWORD},
            {"email": "seller5@marketplace.dev", "role": "seller (pending KYC)", "company": "Konark Chemicals (Ahmedabad)", "password": MASTER_PASSWORD},
            {"email": "demo@marketplace.dev", "role": "both (dual role)", "company": "TransWorld Trading Corp (Mumbai)", "password": LEGACY_DEMO_PASSWORD},
            {"email": "trader1@marketplace.dev", "role": "both (dual role)", "company": "Meridian Global Trade (Rotterdam)", "password": MASTER_PASSWORD},
        ],
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Clean database and seed comprehensive QA test data.")
    parser.add_argument("--skip-wipe", action="store_true", help="Do not wipe tables before seeding")
    parser.add_argument("--rfqs", type=int, default=70, help="Number of background market RFQs to seed (default: 70)")
    args = parser.parse_args()

    print("\n=======================================================")
    print("   B2B Marketplace QA Database Reset & Seeding Tool   ")
    print("=======================================================\n")

    if not args.skip_wipe:
        await wipe_all_database_data()

    results = await seed_all_qa_data(rfq_count=args.rfqs)

    print("\n✓ SUCCESS: Database reset and seeded with complete QA test corpus!\n")
    print("=======================================================")
    print("  QA TEST CREDENTIALS (MASTER PASSWORD: trade2026demo)")
    print("=======================================================")
    print(f"  {'EMAIL':<30}{'ROLE':<22}COMPANY")
    print("  " + "-" * 75)
    for acc in results["credentialed_accounts"]:
        print(f"  {acc['email']:<30}{acc['role']:<22}{acc['company']}")

    print("\n  Legacy Single Account:")
    print(f"  {'demo@marketplace.dev':<30}{'both (dual role)':<22}Password: {LEGACY_DEMO_PASSWORD}")
    print("\n=======================================================\n")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
