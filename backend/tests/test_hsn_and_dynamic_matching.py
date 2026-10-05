"""Tests for Customs-Grade HSN Hierarchical Matching and Dynamic Open-World Services."""

import pytest
from services import currency, hsn_service, match_scoring


@pytest.fixture(autouse=True)
def clean_tables():
    """Bypass conftest db clean tables for pure unit tests."""
    yield


def test_hsn_normalization():
    assert hsn_service.normalize_hsn("1005.90.11") == "10059011"
    assert hsn_service.normalize_hsn(" 3305 30 ") == "330530"
    assert hsn_service.normalize_hsn("HS-8708.30") == "870830"
    assert hsn_service.normalize_hsn(None) is None
    assert hsn_service.normalize_hsn("") is None


def test_hsn_chapter_catalog():
    assert hsn_service.get_chapter("1005.90") == "10"
    assert "Cereals" in (hsn_service.get_chapter_description("1005") or "")
    assert hsn_service.get_chapter("3305.30") == "33"
    assert "cosmetic" in (hsn_service.get_chapter_description("3305") or "").lower()
    assert hsn_service.get_chapter("8708.30") == "87"
    assert "Vehicles" in (hsn_service.get_chapter_description("8708") or "")


def test_hsn_mathematical_compatibility():
    # 6+ digit match = 1.0 (Identical tariff commodity)
    assert hsn_service.hsn_similarity("1005.90.11", "1005.90.99") == 1.0
    assert hsn_service.hsn_similarity("3305.30", "3305.30") == 1.0

    # 4-digit match = 0.88 (Direct commercial substitute under same product heading)
    assert hsn_service.hsn_similarity("1005.10", "1005.90") == 0.88
    assert hsn_service.hsn_similarity("3305.10", "3305.90") == 0.88  # Shampoo vs Hair dye (both hair prep)

    # 2-digit match = 0.55 (Same broad trade chapter)
    assert hsn_service.hsn_similarity("1001", "1005") == 0.55  # Wheat vs Corn (both Cereals Chapter 10)
    assert hsn_service.hsn_similarity("8703", "8708") == 0.55  # Motor cars vs Auto parts (Chapter 87)

    # Cross-industry mismatch = 0.0 (Hard rejection of false positives)
    assert hsn_service.hsn_similarity("1005", "8708") == 0.0   # Corn (Ch 10) vs Brake pads (Ch 87)
    assert hsn_service.hsn_similarity("3305", "2710") == 0.0   # Hair spray (Ch 33) vs Engine oil (Ch 27)


def test_hsn_zero_shot_prediction():
    # Fast heading keywords
    assert hsn_service.predict_hsn_from_text("Bulk Yellow Sweet Corn grains 5000 kg") == "100590"
    assert hsn_service.predict_hsn_from_text("Professional salon hair spray aerosol can") == "330530"
    assert hsn_service.predict_hsn_from_text("Ceramic disc brake pads for passenger vehicles") == "870830"
    assert hsn_service.predict_hsn_from_text("550W monocrystalline solar panel module") == "854143"
    assert hsn_service.predict_hsn_from_text("Powder-free blue nitrile gloves box of 100") == "392620"
    assert hsn_service.predict_hsn_from_text("Sterile rubber examination gloves") == "401519"


def test_hsn_score_in_match_scoring():
    # Calling match_scoring.hsn_score directly
    assert match_scoring.hsn_score("1005.90", "1005.90") == 1.0
    assert match_scoring.hsn_score("1005.10", "1005.90") == 0.88
    assert match_scoring.hsn_score("1005", "8708") == 0.0
    assert match_scoring.hsn_score(None, "1005") is None


def test_dynamic_vector_and_hsn_commodity_matching():
    # True commercial equivalents resolve dynamically without static dictionaries
    s1 = match_scoring._dynamic_semantic_similarity("yellow maize", "corn kernels")
    assert s1 is not None and s1 >= 0.85

    s2 = match_scoring._dynamic_semantic_similarity("caustic soda", "sodium hydroxide")
    assert s2 is not None and s2 >= 0.85

    s3 = match_scoring._dynamic_semantic_similarity("shampoo", "hair cleanser")
    assert s3 is not None and s3 >= 0.85

    # Completely unrelated tokens evaluate to 0.0
    s4 = match_scoring._dynamic_semantic_similarity("foobar", "bazqux")
    assert s4 == 0.0

    s5 = match_scoring._dynamic_semantic_similarity("shampoo", "motor oil")
    assert s5 == 0.0


@pytest.mark.asyncio
async def test_live_fx_engine_and_universal_currencies():
    # Test live rate fetching from European Central Bank feed
    live_updated = await currency.fetch_live_rates()
    assert live_updated is True
    assert currency._SOURCE == "live_ecb_frankfurter"

    # INR rate is live market rate (> 90.0, not stale 83.0)
    inr_rate = currency.RATES.get("INR")
    assert inr_rate is not None and inr_rate > 90.0

    # Conversions calculate with real-time FX
    amt_inr = currency.convert(100, "USD", "INR")
    assert amt_inr is not None and amt_inr > 9000.0

    # Universal currencies support (Middle East, Asia, Crypto stablecoins)
    assert currency.supported("KWD")
    assert currency.supported("AED")
    assert currency.supported("SAR")
    assert currency.supported("USDT")
    assert currency.supported("USDC")

    # USDT to USD is parity
    assert currency.convert(500, "USDT", "USD") == 500.0
