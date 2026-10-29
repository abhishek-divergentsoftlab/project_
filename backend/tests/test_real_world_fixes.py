import pytest
from decimal import Decimal
from services import product_synonyms, spec_codes, match_scoring

def test_vernacular_product_synonyms():
    # Sariya / TMT bar
    assert product_synonyms.canonical_token("sariya") == "rebar"
    assert product_synonyms.canonical_token("saria") == "rebar"
    
    # Cartons / Box
    assert product_synonyms.canonical_token("carton") == "box"
    assert product_synonyms.canonical_token("cartons") == "box"
    
    # Bags / Katta / Bora
    assert product_synonyms.canonical_token("katta") == "bag"
    assert product_synonyms.canonical_token("bora") == "bag"
    assert product_synonyms.canonical_token("bori") == "bag"

    # Smartphone / Mobile
    assert product_synonyms.canonical_token("mobile") == "smartphone"
    assert product_synonyms.canonical_token("cellphone") == "smartphone"


def test_spec_codes_spaced_and_hyphenated():
    # Fe 550 D with spaces
    assert spec_codes.canonical_codes("Fe 550 D") == "fe550"
    assert spec_codes.canonical_codes("Fe-550-D") == "fe550"
    assert spec_codes.canonical_codes("Fe550D") == "fe550"

    # Type-C / USB Type C
    assert spec_codes.canonical_codes("USB Type C") == "type-c"
    assert spec_codes.canonical_codes("type-c") == "type-c"
    assert spec_codes.canonical_codes("USB-C") == "type-c"

    # Stainless steel grades
    assert spec_codes.canonical_codes("SS 304 L") == "ss304l"
    assert spec_codes.canonical_codes("SS-304L") == "ss304l"
    assert spec_codes.canonical_codes("304L stainless") == "ss304l stainless steel"


def test_price_score_total_budget_guard():
    # Buyer entered $10,000 total budget for 1,000 pcs (real per-unit budget is $10)
    # Seller asks $12 per piece (which is OVER budget)
    # Without guard, $12 <= $10,000 awards 1.0!
    # With guard, normalized budget is $10, seller $12 is 20% over budget -> clearly penalised (~0.5)
    score = match_scoring.price_score(
        buyer_target=Decimal("10000"),
        seller_ask=Decimal("12"),
        buyer_currency="USD",
        seller_currency="USD",
        buyer_unit="pcs",
        seller_unit="pcs",
        buyer_quantity=Decimal("1000"),
    )
    assert score is not None
    assert 0.4 <= score <= 0.6
    assert score < 1.0  # Must NOT be full marks since seller ask ($12) exceeds per-unit budget ($10)


def test_packaging_default_agri_mass():
    from services.unit_converter import resolve_to_base_unit
    # Bora / bag without pack_weight defaults to 50 kg in Indian agri trade
    res = resolve_to_base_unit("bora")
    assert res is not None
    factor, unit, dim = res
    assert factor == Decimal("50")
    assert unit == "kg"
    assert dim == "mass"
