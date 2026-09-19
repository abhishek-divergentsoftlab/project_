"""Populate the database with a large, international demo marketplace.

    .venv/bin/python scripts/seed_data.py --reset                 # 10,000 RFQs
    .venv/bin/python scripts/seed_data.py --reset --rfqs 2000     # smaller
    .venv/bin/python scripts/index_vectors.py --all               # then embed

Listings are drawn from ``scripts/catalogue.py``, which carries real
specifications per product -- cell chemistry, PD wattage, port layout, burst
strength, grain length -- because a matcher is only as good as the attributes it
has to compare.

Prices are quoted in each city's own currency, converted from a USD reference,
so the corpus exercises cross-currency comparison rather than pretending the
world trades in one unit.

Seeded accounts live on SEED_DOMAIN and the credentialed logins on DEMO_DOMAIN;
``--reset`` keys off both and never touches accounts you created yourself.
"""

import argparse
import asyncio
import random
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from core.security import hash_password  # noqa: E402
from db.session import SessionLocal, engine  # noqa: E402
from models.enums import RFQRole, RFQStatus, UserRole, UserStatus  # noqa: E402
from models.rfq import RFQ  # noqa: E402
from models.user import User, UserProfile  # noqa: E402
from services import currency  # noqa: E402
from services.locations import CITIES, City, dialling_code  # noqa: E402
from services.rfq_indexing import refresh_index_fields  # noqa: E402
from scripts.catalogue import CATALOGUE, COHERENCE  # noqa: E402

SEED_DOMAIN = "seed.marketplace.dev"
DEMO_DOMAIN = "marketplace.dev"

# One password for every seeded login, printed at the end.
DEMO_PASSWORD = "trade2026demo"

# The original single demo account is kept so older instructions still work.
DEMO_EMAIL = "demo@marketplace.dev"
LEGACY_PASSWORD = "demo password 123"

# Credentialed accounts get a readable dashboard; the depth of the market comes
# from background companies, so "My RFQs" is not 500 rows long.
RFQS_PER_CREDENTIALED_USER = 25
BACKGROUND_COMPANIES = 260
INSERT_BATCH = 500

COMPANY_PREFIX = [
    "Shakti", "Meridian", "Sunrise", "Orbit", "Deccan", "Kaveri", "Vertex",
    "Pioneer", "Anand", "Crest", "Nova", "Sagar", "Trident", "Vayu", "Indus",
    "Zenith", "Konark", "Sterling", "Rajhans", "Aurora", "Baltic", "Cedar",
    "Delta", "Everest", "Fairwind", "Granite", "Harbour", "Ironwood", "Juniper",
    "Keystone", "Lakeside", "Monsoon", "Northgate", "Obsidian", "Pinnacle",
    "Quarry", "Redwood", "Summit", "Trailhead", "Umbra", "Vanguard", "Westport",
]
COMPANY_SUFFIX = [
    "Industries", "Traders", "Enterprises", "Exports", "Supply Co",
    "Manufacturing", "Agro", "Packaging", "Textiles", "Electronics",
    "Global", "Trading LLC", "Group", "Works", "Logistics", "Sourcing",
]
FIRST_NAMES = [
    "Aarti", "Rohit", "Meera", "Imran", "Kavya", "Sanjay", "Neha", "Vikram",
    "Priya", "Arjun", "Divya", "Farhan", "Sneha", "Rahul", "Ananya", "Manish",
    "Wei", "Linh", "Mehmet", "Ayşe", "Lukas", "Sofia", "Diego", "Amara",
    "Chen", "Nguyen", "Omar", "Fatima", "Hans", "Elena", "Carlos", "Grace",
]
LAST_NAMES = [
    "Deshmukh", "Sharma", "Iyer", "Qureshi", "Nair", "Patel", "Kulkarni",
    "Reddy", "Joshi", "Menon", "Chauhan", "Banerjee", "Rao", "Gupta",
    "Zhang", "Tran", "Yilmaz", "Novak", "Silva", "Okafor", "Müller", "Rossi",
    "Kowalski", "Hassan", "Santos", "Wang", "Le", "Demir", "Fernandez",
]
# Only ever shown once a connection is accepted, so it exists to make that
# panel look like a real record rather than a blank.
STREETS = [
    "Industrial Estate Road", "Export Promotion Park", "Harbour Way",
    "Ring Road", "Trade Centre Avenue", "Warehouse Lane", "Dock Street",
    "Foundry Road", "Logistics Park", "Commerce Street",
]


def _render(value: Any) -> str:
    """Compact human rendering of a spec value, for titles."""
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


def _title(rng: random.Random, spec: dict, role: RFQRole, quantity: int, attrs: dict) -> str:
    """A title that carries the specs a buyer would actually scan for."""
    highlights = [
        key for key in ("capacity", "output_power", "ply", "grade", "gsm",
                        "connector", "variety", "size", "material", "thickness",
                        "crystal_form", "bore_diameter", "back_material", "fabric")
        if key in attrs
    ][:2]
    detail = ", ".join(_render(attrs[key]) for key in highlights)
    colour = attrs.get("color")
    name = f"{colour} {spec['product']}" if isinstance(colour, str) else spec["product"]
    verb = "Need" if role is RFQRole.BUYER else "Supplying"
    tail = f" — {detail}" if detail else ""
    return f"{verb} {quantity:,} {spec['unit']} of {name}{tail}"[:300]


def _price(rng: random.Random, spec: dict, role: RFQRole, city: City) -> Decimal:
    """Reference USD price, nudged by side, converted to the city's currency."""
    swing = rng.uniform(-0.20, 0.05) if role is RFQRole.SELLER else rng.uniform(-0.05, 0.25)
    usd = Decimal(str(spec["price_usd"])) * Decimal(str(1 + swing))
    converted = currency.convert(usd, "USD", city.currency) or usd
    quantum = Decimal("0.01") if converted < 100 else Decimal("1")
    return converted.quantize(quantum)


def _build_rfq(rng: random.Random, user_id, spec: dict, role: RFQRole) -> RFQ:
    city = rng.choice(list(CITIES.values()))
    attrs = _draw_specs(rng, spec)
    low, high = spec["qty"]
    quantity = rng.randrange(low, high, max(1, low // 5))
    days = rng.choice([5, 7, 10, 14, 21, 30, 45, 60, 90])

    from datetime import UTC, datetime, timedelta
    deadline = datetime.now(UTC) + timedelta(days=days)

    rfq = RFQ(
        user_id=user_id,
        role=role,
        status=RFQStatus.ACTIVE,
        category=spec["category"],
        title=_title(rng, spec, role, quantity, attrs),
        description=(
            f"{'Sourcing' if role is RFQRole.BUYER else 'Ready stock of'} "
            f"{spec['product']}. Bulk orders, export documentation available."
        ),
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
        expires_at=deadline,
        product_details={"name": spec["product"], **attrs},
    )
    refresh_index_fields(rfq)
    return rfq


def _make_user(rng: random.Random, email: str, role: UserRole, password_hash: str) -> User:
    city = rng.choice(list(CITIES.values()))
    user = User(
        email=email,
        password_hash=password_hash,
        role=role,
        status=UserStatus.ACTIVE,
    )
    user.profile = UserProfile(
        name=f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
        company_name=f"{rng.choice(COMPANY_PREFIX)} {rng.choice(COMPANY_SUFFIX)}",
        # A real dialling code for the company's own country, rather than a
        # random one to 99 -- "+5" is not a country, and the contact panel shows
        # this number to anyone the account accepts.
        phone=f"+{dialling_code(city.country)} {rng.randrange(100000000, 999999999)}",
        address=f"{rng.randrange(1, 240)} {rng.choice(STREETS)}, {city.name}",
        city=city.name,
        state=city.region,
        country=city.country,
        latitude=Decimal(str(city.latitude)),
        longitude=Decimal(str(city.longitude)),
    )
    return user


async def reset(db) -> int:
    seeded = (
        await db.scalars(
            select(User).where(
                User.email.like(f"%@{SEED_DOMAIN}")
                | User.email.like(f"%@{DEMO_DOMAIN}")
            )
        )
    ).all()
    for user in seeded:
        await db.delete(user)
    await db.commit()
    return len(seeded)


async def seed(total_rfqs: int, credentialed: int) -> list[tuple[str, str, str]]:
    rng = random.Random(20260919)

    # Hashed once: argon2 is intentionally slow, and every seeded account shares
    # this password anyway, so a shared hash is equivalent and saves minutes.
    demo_hash = hash_password(DEMO_PASSWORD)
    credentials: list[tuple[str, str, str]] = []

    async with SessionLocal() as db:
        if await db.scalar(select(User).where(User.email == DEMO_EMAIL)):
            print(f"! seeded accounts already exist -- run with --reset")
            return []

        # --- accounts --------------------------------------------------------
        legacy = _make_user(rng, DEMO_EMAIL, UserRole.BOTH, hash_password(LEGACY_PASSWORD))
        db.add(legacy)

        credentialed_users: list[tuple[User, RFQRole]] = []
        half = credentialed // 2
        for index in range(1, half + 1):
            email = f"buyer{index}@{DEMO_DOMAIN}"
            user = _make_user(rng, email, UserRole.BUYER, demo_hash)
            db.add(user)
            credentialed_users.append((user, RFQRole.BUYER))
            credentials.append((email, "buyer", user.profile.company_name))
        for index in range(1, credentialed - half + 1):
            email = f"seller{index}@{DEMO_DOMAIN}"
            user = _make_user(rng, email, UserRole.SELLER, demo_hash)
            db.add(user)
            credentialed_users.append((user, RFQRole.SELLER))
            credentials.append((email, "seller", user.profile.company_name))

        background: list[tuple[User, RFQRole]] = []
        for index in range(1, BACKGROUND_COMPANIES + 1):
            role = RFQRole.SELLER if index % 2 else RFQRole.BUYER
            user_role = UserRole.SELLER if role is RFQRole.SELLER else UserRole.BUYER
            user = _make_user(rng, f"co{index}@{SEED_DOMAIN}", user_role, demo_hash)
            db.add(user)
            background.append((user, role))

        await db.commit()
        print(f"  {len(credentialed_users)} credentialed + {len(background)} background accounts")

        # --- listings --------------------------------------------------------
        plan: list[tuple[Any, RFQRole]] = []
        for user, role in credentialed_users:
            plan.extend([(user, role)] * RFQS_PER_CREDENTIALED_USER)
        remaining = max(total_rfqs - len(plan), 0)
        for i in range(remaining):
            plan.append(background[i % len(background)])
        rng.shuffle(plan)

        # The demo account keeps one RFQ on each side, as before.
        for role in (RFQRole.BUYER, RFQRole.SELLER):
            spec = next(p for p in CATALOGUE if p["product"] == "USB Type-C cable")
            db.add(_build_rfq(rng, legacy.id, spec, role))

        written = 0
        for start in range(0, len(plan), INSERT_BATCH):
            for user, role in plan[start : start + INSERT_BATCH]:
                spec = rng.choice(CATALOGUE)
                db.add(_build_rfq(rng, user.id, spec, role))
            await db.commit()
            written += len(plan[start : start + INSERT_BATCH])
            print(f"    {written:,}/{len(plan):,} RFQs", end="\r", flush=True)

        print(f"    {written:,} RFQs written{' ' * 20}")

    return credentials


async def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the marketplace.")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--rfqs", type=int, default=10000)
    parser.add_argument("--users", type=int, default=20, help="credentialed logins")
    args = parser.parse_args()

    if args.reset:
        async with SessionLocal() as db:
            removed = await reset(db)
        print(f"  {removed} seeded accounts removed")

    credentials = await seed(args.rfqs, args.users)

    if credentials:
        print(f"\n  Every account below uses the password: {DEMO_PASSWORD}\n")
        print(f"  {'EMAIL':<30}{'ROLE':<10}COMPANY")
        for email, role, company in credentials:
            print(f"  {email:<30}{role:<10}{company}")
        print(f"\n  {DEMO_EMAIL:<30}{'both':<10}(password: {LEGACY_PASSWORD})")
        print("\n  Next: .venv/bin/python scripts/index_vectors.py --all")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
