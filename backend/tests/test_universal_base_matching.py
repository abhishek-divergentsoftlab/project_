"""Comprehensive tests for Universal Base SI Unit & Currency Normalization in RFQ Matching."""

from decimal import Decimal
import pytest

from services import match_scoring as ms
from services import unit_converter as uc
from services import currency


@pytest.fixture(autouse=True)
def clean_tables():
    """Bypass db cleaner for pure unit tests."""
    yield


# ==============================================================================
# 1. Base SI Unit Normalization Tests
# ==============================================================================

def test_mass_normalization_to_kg():
    # Tonnes to kg
    val, unit, dim = uc.normalize_quantity_to_base(Decimal("10"), "tonnes")
    assert val == Decimal("10000")
    assert unit == "kg"
    assert dim == "mass"

    # Metric tons / MT alias
    val, unit, dim = uc.normalize_quantity_to_base(Decimal("5"), "MT")
    assert val == Decimal("5000")
    assert unit == "kg"

    # Quintals to kg (1 quintal = 100 kg)
    val, unit, dim = uc.normalize_quantity_to_base(Decimal("50"), "quintals")
    assert val == Decimal("5000")
    assert unit == "kg"

    # Grams to kg
    val, unit, dim = uc.normalize_quantity_to_base(Decimal("500"), "grams")
    assert val == Decimal("0.5")
    assert unit == "kg"

    # Pounds to kg
    val, unit, dim = uc.normalize_quantity_to_base(Decimal("100"), "lbs")
    assert abs(float(val) - 45.3592) < 0.01
    assert unit == "kg"

    # Hindi / Mandi units
    val, unit, _ = uc.normalize_quantity_to_base(Decimal("20"), "क्विंटल")
    assert val == Decimal("2000")
    assert unit == "kg"

    val, unit, _ = uc.normalize_quantity_to_base(Decimal("3"), "टन")
    assert val == Decimal("3000")
    assert unit == "kg"


def test_volume_normalization_to_liters():
    # Kilolitres to L (1 kL = 1,000 L)
    val, unit, dim = uc.normalize_quantity_to_base(Decimal("2.5"), "kl")
    assert val == Decimal("2500")
    assert unit == "l"
    assert dim == "volume"

    # Millilitres to L
    val, unit, dim = uc.normalize_quantity_to_base(Decimal("750"), "ml")
    assert val == Decimal("0.75")
    assert unit == "l"

    # Cubic meters to L (1 m³ = 1,000 L)
    val, unit, dim = uc.normalize_quantity_to_base(Decimal("1.2"), "m3")
    assert val == Decimal("1200")
    assert unit == "l"

    # Hindi volume units
    val, unit, _ = uc.normalize_quantity_to_base(Decimal("50"), "लीटर")
    assert val == Decimal("50")
    assert unit == "l"


def test_count_normalization_to_pieces():
    # Dozen to pieces (1 dozen = 12 pcs)
    val, unit, dim = uc.normalize_quantity_to_base(Decimal("5"), "dozen")
    assert val == Decimal("60")
    assert unit == "pcs"
    assert dim == "count"

    # Pairs to pieces
    val, unit, dim = uc.normalize_quantity_to_base(Decimal("25"), "pairs")
    assert val == Decimal("50")
    assert unit == "pcs"

    # Gross to pieces (1 gross = 144 pcs)
    val, unit, dim = uc.normalize_quantity_to_base(Decimal("2"), "gross")
    assert val == Decimal("288")
    assert unit == "pcs"


def test_packaging_normalization():
    # Explicit pack weight in attributes: 100 bags of 25 kg
    val, unit, dim = uc.normalize_quantity_to_base(
        Decimal("100"), "bag", attributes={"pack_weight_kg": Decimal("25")}
    )
    assert val == Decimal("2500")
    assert unit == "kg"
    assert dim == "mass"

    # Standard mandi default (50kg per bag/bora when no weight given)
    val, unit, dim = uc.normalize_quantity_to_base(Decimal("40"), "bora")
    assert val == Decimal("2000")
    assert unit == "kg"
    assert dim == "mass"

    # Cartons with pack_count: 50 cartons with 24 pcs each
    val, unit, dim = uc.normalize_quantity_to_base(
        Decimal("50"), "carton", attributes={"pack_count": 24}
    )
    assert val == Decimal("1200")
    assert unit == "pcs"
    assert dim == "count"


# ==============================================================================
# 2. Universal Base Quantity Matching Tests
# ==============================================================================

def test_cross_unit_quantity_matching_mass():
    # Buyer needs 10 tonnes (=10,000 kg), seller offers 10,000 kg -> 100% match
    score = ms.quantity_score(Decimal("10"), "tonnes", Decimal("10000"), "kg")
    assert score == 1.0

    # Buyer needs 50 quintals (=5,000 kg), seller offers 4 tonnes (=4,000 kg) -> 80% coverage
    score = ms.quantity_score(Decimal("50"), "quintals", Decimal("4"), "tonne")
    assert score == pytest.approx(0.80)

    # Buyer needs 10 tonnes, seller offers 200 bags of standard 50kg (=10,000 kg) -> 100% match
    score = ms.quantity_score(Decimal("10"), "tonnes", Decimal("200"), "bora")
    assert score == 1.0


def test_cross_unit_quantity_matching_volume():
    # Buyer needs 2 kL (=2,000 L), seller offers 1,500 L -> 75% coverage
    score = ms.quantity_score(Decimal("2"), "kl", Decimal("1500"), "liters")
    assert score == pytest.approx(0.75)

    # Buyer needs 5,000 mL (=5 L), seller offers 10 L -> 100% match
    score = ms.quantity_score(Decimal("5000"), "ml", Decimal("10"), "litres")
    assert score == 1.0


def test_cross_unit_quantity_matching_count():
    # Buyer needs 2 dozen (=24 pcs), seller offers 24 pcs -> 100% match
    score = ms.quantity_score(Decimal("2"), "dozen", Decimal("24"), "pcs")
    assert score == 1.0

    # Buyer needs 1 gross (=144 pcs), seller offers 72 pcs -> 50% coverage
    score = ms.quantity_score(Decimal("1"), "gross", Decimal("72"), "pcs")
    assert score == pytest.approx(0.50)


# ==============================================================================
# 3. Universal Base Price & Currency Matching Tests
# ==============================================================================

def test_price_per_base_unit_normalization():
    # 28,000 INR / tonne -> 28 INR / kg
    amt, ccy, unit, dim = uc.normalize_price_to_base(
        Decimal("28000"), "INR", "tonne"
    )
    assert amt == Decimal("28")
    assert ccy == "INR"
    assert unit == "kg"
    assert dim == "mass"

    # 3,000 INR / quintal -> 30 INR / kg
    amt, ccy, unit, dim = uc.normalize_price_to_base(
        Decimal("3000"), "INR", "quintal"
    )
    assert amt == Decimal("30")
    assert unit == "kg"

    # 24 USD / dozen -> 2 USD / pcs
    amt, ccy, unit, dim = uc.normalize_price_to_base(
        Decimal("24"), "USD", "dozen"
    )
    assert amt == Decimal("2")
    assert unit == "pcs"
    assert dim == "count"

    # 5,000 INR / kL -> 5 INR / L
    amt, ccy, unit, dim = uc.normalize_price_to_base(
        Decimal("5000"), "INR", "kl"
    )
    assert amt == Decimal("5")
    assert unit == "l"
    assert dim == "volume"


def test_price_matching_cross_unit_mass():
    # Buyer target: 30 INR / kg
    # Seller ask: 28,000 INR / tonne (= 28 INR / kg, which is <= 30) -> 1.0 match!
    score = ms.price_score(
        buyer_target=Decimal("30"),
        seller_ask=Decimal("28000"),
        buyer_currency="INR",
        seller_currency="INR",
        buyer_unit="kg",
        seller_unit="tonne",
    )
    assert score == 1.0

    # Buyer target: 3,500 INR / quintal (= 35 INR / kg)
    # Seller ask: 40 INR / kg (overshoot by 5 on 35 = 14.3%) -> score ~ 0.857
    score = ms.price_score(
        buyer_target=Decimal("3500"),
        seller_ask=Decimal("40"),
        buyer_currency="INR",
        seller_currency="INR",
        buyer_unit="quintal",
        seller_unit="kg",
    )
    assert score == pytest.approx(0.857, abs=0.01)


def test_price_matching_cross_unit_volume():
    # Buyer target: 6 INR / L
    # Seller ask: 5,000 INR / kL (= 5 INR / L <= 6) -> 1.0 match!
    score = ms.price_score(
        buyer_target=Decimal("6"),
        seller_ask=Decimal("5000"),
        buyer_currency="INR",
        seller_currency="INR",
        buyer_unit="litre",
        seller_unit="kl",
    )
    assert score == 1.0


def test_price_matching_cross_unit_count():
    # Buyer target: $2.50 / piece
    # Seller ask: $24 / dozen (= $2.00 / piece <= $2.50) -> 1.0 match!
    score = ms.price_score(
        buyer_target=Decimal("2.50"),
        seller_ask=Decimal("24.00"),
        buyer_currency="USD",
        seller_currency="USD",
        buyer_unit="pcs",
        seller_unit="dozen",
    )
    assert score == 1.0


def test_price_matching_cross_currency_and_cross_unit():
    # Buyer target: 35 INR / kg
    # Seller ask: $0.35 / kg (with 1 USD = 83 INR -> $0.35 = 29.05 INR <= 35) -> 1.0 match!
    score = ms.price_score(
        buyer_target=Decimal("35"),
        seller_ask=Decimal("0.35"),
        buyer_currency="INR",
        seller_currency="USD",
        buyer_unit="kg",
        seller_unit="kg",
    )
    assert score == 1.0


def test_candidate_normalized_quantity_and_price_populated():
    import uuid
    from types import SimpleNamespace
    from models.enums import RFQRole
    from schemas.match import MatchScore
    from services.match_service import _to_candidate

    req = SimpleNamespace(
        id=uuid.uuid4(),
        role=RFQRole.BUYER,
        price_currency="INR",
        latitude=None,
        longitude=None,
        location_country="India",
    )
    cand = SimpleNamespace(
        id=uuid.uuid4(),
        role=RFQRole.SELLER,
        title="Basmati Rice 20 Tonnes",
        category="Agriculture",
        description="Fresh crop",
        quantity_value=Decimal("20"),
        quantity_unit="tonnes",
        price_amount=Decimal("28000"),
        price_currency="INR",
        price_per_unit="tonne",
        location_city="Karnal",
        location_state="Haryana",
        location_country="India",
        latitude=Decimal("29.6857"),
        longitude=Decimal("76.9905"),
        location_raw=None,
        deadline_at=None,
        deadline_raw=None,
        product_details={"name": "Basmati rice"},
        search_tags=["basmati", "rice"],
        user=None,
    )
    score = MatchScore(total=0.95, price=1.0, quantity=1.0)
    candidate_out = _to_candidate(req, cand, score, rank=1)

    assert candidate_out.normalized_quantity is not None
    assert candidate_out.normalized_quantity.value == 20000
    assert candidate_out.normalized_quantity.unit == "kg"

    assert candidate_out.normalized_price is not None
    assert candidate_out.normalized_price.amount == 28
    assert candidate_out.normalized_price.currency == "INR"
    assert candidate_out.normalized_price.per_unit == "kg"


def test_attribute_score_with_breakdown():
    from services.match_scoring import attribute_score_with_breakdown

    req_details = {
        "material": "100% cotton",
        "thickness": "2cm",
        "moisture": "12%",
        "power": "50W",
        "color": "navy blue",
        "grade": "A+",
    }
    cand_details = {
        "fabric": "cotton",       # synonym key 'fabric' and related value
        "dia": "20mm",             # cross-measurement key + 2cm == 20mm
        "moisture_pct": "12%",     # synonym key
        "power": "200W",           # differing value
        "shade": "blue",           # synonym key 'shade' + related shade
        # 'grade' is missing -> unstated
    }

    score, breakdown = attribute_score_with_breakdown(req_details, cand_details)
    assert 0.0 < score < 1.0
    assert len(breakdown) == len(req_details)

    breakdown_by_key = {item["key"]: item for item in breakdown}
    
    # 2cm vs 20mm
    assert breakdown_by_key["thickness"]["status"] == "matched"
    assert "2cm = 20mm" in breakdown_by_key["thickness"]["reason"] or "equivalent" in breakdown_by_key["thickness"]["reason"].lower()

    # moisture vs moisture_pct
    assert breakdown_by_key["moisture"]["status"] == "matched"
    assert breakdown_by_key["moisture"]["offered_key"] == "moisture_pct"

    # grade missing
    assert breakdown_by_key["grade"]["status"] == "unstated"
    assert breakdown_by_key["grade"]["score"] == 0.5

    # power mismatch 50W vs 200W
    assert breakdown_by_key["power"]["status"] == "mismatch"


