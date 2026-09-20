"""Unit tests for the scoring rules.

These are pure functions, so every branch is reachable without a database. They
are the cheapest place to pin down behaviour that is expensive to check by
clicking: currency conversion, unit compatibility, graded attribute distance and
the distance curve.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from services import match_scoring as ms
from services.locations import CITIES


def place(city_key: str) -> dict:
    city = CITIES[city_key]
    return {
        "city": city.name,
        "state": city.region,
        "country": city.country,
        "latitude": city.latitude,
        "longitude": city.longitude,
    }


# --- location ---------------------------------------------------------------


def test_same_city_is_a_perfect_location_match():
    assert ms.location_score(place("ludhiana"), place("ludhiana")) == 1.0


def test_a_different_city_400km_away_is_not_a_strong_match():
    """The reported bug: Jaipur to Ludhiana is 444km and used to score 0.73.

    Two days by road, a different state and a different set of transporters
    should not read as "nearby" on a card.
    """
    distance = ms.distance_km(place("jaipur"), place("ludhiana"))
    assert 400 < distance < 500

    score = ms.location_score(place("jaipur"), place("ludhiana"))
    assert 0.35 < score < 0.55


def test_location_score_falls_as_distance_grows():
    origin = place("mumbai")
    # By road distance from Mumbai: itself, Pune ~120km, Nashik ~140km,
    # Delhi ~1,150km.
    ordered = ["mumbai", "pune", "nashik", "delhi"]
    scores = [ms.location_score(origin, place(k)) for k in ordered]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == 1.0


def test_domestic_never_scores_below_the_same_country_floor():
    """1,100km apart but no customs, one currency, one set of road rules."""
    far_domestic = ms.location_score(place("mumbai"), place("delhi"))
    assert far_domestic >= 0.3


def test_international_scores_the_cross_border_floor():
    assert ms.location_score(place("shenzhen"), place("dubai")) == pytest.approx(0.1)


def test_neighbouring_cities_inside_the_metro_band_are_full_marks():
    # Shenzhen to Dongguan is ~60km: one industrial cluster, same day delivery.
    assert ms.location_score(place("shenzhen"), place("dongguan")) > 0.9


def test_location_without_coordinates_falls_back_to_administrative_levels():
    assert ms.location_score({"city": "Surat"}, {"city": "surat"}) == 1.0
    assert (
        ms.location_score({"state": "Gujarat"}, {"state": "Gujarat"}) == 0.6
    )
    assert ms.location_score({"country": "India"}, {"country": "India"}) == 0.3


def test_location_is_unscored_when_one_side_said_nothing():
    """Silence is not distance. Scoring it would punish an incomplete listing."""
    assert ms.location_score(place("pune"), {}) is None
    assert ms.location_score({}, {}) is None


def test_distance_km_needs_both_sides_geocoded():
    assert ms.distance_km(place("pune"), {"city": "Indore"}) is None


def test_haversine_matches_a_known_distance():
    # London to Paris, 344km by great circle.
    assert ms.haversine_km(51.5074, -0.1278, 48.8566, 2.3522) == pytest.approx(
        344, abs=5
    )


# --- price ------------------------------------------------------------------


def test_price_at_or_under_target_is_full_marks():
    assert ms.price_score(Decimal("200"), Decimal("180")) == 1.0
    assert ms.price_score(Decimal("200"), Decimal("200")) == 1.0


def test_price_decays_above_target_and_bottoms_out_at_double():
    assert ms.price_score(Decimal("200"), Decimal("300")) == pytest.approx(0.5)
    assert ms.price_score(Decimal("200"), Decimal("400")) == 0.0
    assert ms.price_score(Decimal("200"), Decimal("1000")) == 0.0


def test_price_converts_between_currencies():
    """A seller quoting USD against an INR target must not be read as literal."""
    cheap = ms.price_score(Decimal("2000"), Decimal("10"), "INR", "USD")
    assert cheap == 1.0

    expensive = ms.price_score(Decimal("100"), Decimal("10"), "INR", "USD")
    assert expensive == 0.0


def test_price_is_unscored_for_an_unknown_currency():
    """Better a blank dimension than a comparison invented from nothing."""
    assert ms.price_score(Decimal("100"), Decimal("100"), "INR", "ZZZ") is None


def test_price_is_unscored_when_either_side_is_missing():
    assert ms.price_score(None, Decimal("10")) is None
    assert ms.price_score(Decimal("10"), None) is None
    assert ms.price_score(Decimal("0"), Decimal("10")) is None


# --- quantity ---------------------------------------------------------------


def test_quantity_covering_the_requirement_is_full_marks():
    assert ms.quantity_score(Decimal("6000"), "pcs", Decimal("8000"), "pcs") == 1.0


def test_partial_quantity_scores_proportionally():
    assert ms.quantity_score(Decimal("6000"), "pcs", Decimal("3000"), "pcs") == 0.5


def test_unit_synonyms_are_comparable():
    assert ms.quantity_score(Decimal("500"), "pieces", Decimal("500"), "pcs") == 1.0
    assert ms.quantity_score(Decimal("5"), "ton", Decimal("5"), "tonnes") == 1.0


def test_incomparable_units_are_left_unscored():
    """500kg against 500 cartons is not a 100% match, it is not a comparison."""
    assert ms.quantity_score(Decimal("500"), "kg", Decimal("500"), "cartons") is None


# --- attributes -------------------------------------------------------------


def test_attributes_all_matching_is_full_marks():
    wanted = {"colour": "black", "ply": 5}
    assert ms.attribute_score(wanted, {"colour": "black", "ply": 5}) == 1.0


def test_measurements_are_compared_numerically_not_as_text():
    """{'value': 65, 'unit': 'W'} used to "match" {'value': 18, 'unit': 'W'}.

    Both stringify to the shared tokens value/unit/W, so every numeric spec
    scored full marks no matter what the number was.
    """
    wanted = {"output_power": {"value": 65, "unit": "W"}}
    close = ms.attribute_score(wanted, {"output_power": {"value": 45, "unit": "W"}})
    far = ms.attribute_score(wanted, {"output_power": {"value": 18, "unit": "W"}})
    assert 1.0 > close > far
    assert far < 0.4


def test_a_measurement_in_the_wrong_unit_does_not_match():
    wanted = {"capacity": {"value": 20000, "unit": "mAh"}}
    assert ms.attribute_score(wanted, {"capacity": {"value": 20000, "unit": "Wh"}}) == 0.0


def test_similar_capacities_score_close_to_each_other():
    wanted = {"capacity": {"value": 20000, "unit": "mAh"}}
    near = ms.attribute_score(wanted, {"capacity": {"value": 27000, "unit": "mAh"}})
    wrong = ms.attribute_score(wanted, {"capacity": {"value": 5000, "unit": "mAh"}})
    assert near > 0.6
    assert wrong < 0.4


def test_a_named_grade_is_not_close_to_a_different_grade():
    """SS 316L and SS 304 share the token "SS" and nothing that matters."""
    wanted = {"grade": "SS 316L"}
    assert ms.attribute_score(wanted, {"grade": "SS 304"}) < 0.3
    assert ms.attribute_score(wanted, {"grade": "SS 316L"}) == 1.0


def test_an_unstated_attribute_scores_half_not_zero():
    """Silence is not a contradiction, but it is not a confirmation either."""
    assert ms.attribute_score({"colour": "black"}, {}) == 0.5
    assert ms.attribute_score({"colour": "black"}, {"colour": None}) == 0.5


def test_booleans_are_exact():
    assert ms.attribute_score({"c_to_c": True}, {"c_to_c": True}) == 1.0
    assert ms.attribute_score({"c_to_c": True}, {"c_to_c": False}) == 0.0


def test_the_product_name_is_not_scored_as_an_attribute():
    """It is the product, not a spec; relevance already covers it."""
    assert ms.attribute_score({"name": "cable"}, {"name": "box"}) is None


def test_attributes_are_unscored_when_nothing_was_asked_for():
    assert ms.attribute_score({}, {"colour": "red"}) is None


# --- semantic synonym matching -----------------------------------------------


def test_spelling_variants_score_full_marks():
    """grey vs gray, matte vs matt — same word, different spelling."""
    assert ms.attribute_score({"colour": "grey"}, {"colour": "gray"}) == 1.0
    assert ms.attribute_score({"finish": "matte"}, {"finish": "matt"}) == 1.0
    assert ms.attribute_score({"material": "aluminum"}, {"material": "aluminium"}) == 1.0
    assert ms.attribute_score({"coating": "galvanized"}, {"coating": "galvanised"}) == 1.0
    assert ms.attribute_score({"coating": "anodized"}, {"coating": "anodised"}) == 1.0


def test_related_color_shades_score_high():
    """charcoal grey is a shade of grey — should score high, not zero."""
    score = ms.attribute_score({"colour": "grey"}, {"colour": "charcoal grey"})
    assert score >= 0.8


def test_color_synonyms_score_high():
    """crimson is a shade of red — synonym, not a token match."""
    score = ms.attribute_score({"colour": "red"}, {"colour": "crimson"})
    assert score >= 0.7


def test_unrelated_colors_score_zero():
    """red and blue are different colors — should be zero."""
    assert ms.attribute_score({"colour": "red"}, {"colour": "blue"}) == 0.0


def test_material_synonyms():
    """wood/timber/lumber are the same material."""
    assert ms.attribute_score({"material": "wood"}, {"material": "timber"}) == 1.0
    assert ms.attribute_score({"material": "wood"}, {"material": "lumber"}) == 1.0
    # cotton variants
    assert ms.attribute_score({"material": "cotton"}, {"material": "pure cotton"}) == 1.0


def test_finish_synonyms():
    """glossy/gloss/shiny are the same finish."""
    score = ms.attribute_score({"finish": "glossy"}, {"finish": "shiny"})
    assert score >= 0.85


def test_trade_term_synonyms():
    """eco-friendly vs sustainable, waterproof vs water resistant."""
    eco = ms.attribute_score({"type": "eco-friendly"}, {"type": "sustainable"})
    assert eco >= 0.8
    wp = ms.attribute_score({"type": "waterproof"}, {"type": "water resistant"})
    assert wp >= 0.8


def test_stainless_steel_abbreviation():
    """SS is stainless steel in the materials world."""
    assert ms.attribute_score({"material": "stainless steel"}, {"material": "ss"}) == 1.0
    assert ms.attribute_score({"material": "stainless steel"}, {"material": "inox"}) == 1.0


def test_grade_distinction_preserved_with_synonyms():
    """SS 316L vs SS 304 must still be distinguished — synonym engine must not
    collapse them just because both contain 'SS'."""
    score = ms.attribute_score({"grade": "SS 316L"}, {"grade": "SS 304"})
    assert score < 0.3


def test_existing_exact_match_still_works():
    """Regression: exact matches must still score 1.0."""
    assert ms.attribute_score({"colour": "black"}, {"colour": "black"}) == 1.0
    assert ms.attribute_score({"colour": "red"}, {"colour": "red"}) == 1.0


def test_existing_numeric_comparison_unchanged():
    """Regression: numeric grading must not be affected by synonyms."""
    wanted = {"capacity": {"value": 20000, "unit": "mAh"}}
    near = ms.attribute_score(wanted, {"capacity": {"value": 27000, "unit": "mAh"}})
    assert near > 0.6


def test_unknown_values_fall_through_to_token_overlap():
    """Values not in the synonym graph should still use token overlap."""
    # "foobar xyz" vs "foobar xyz" — exact match
    assert ms.attribute_score({"spec": "foobar xyz"}, {"spec": "foobar xyz"}) == 1.0
    # "foobar" vs "bazqux" — no overlap
    assert ms.attribute_score({"spec": "foobar"}, {"spec": "bazqux"}) == 0.0

# --- deadline ---------------------------------------------------------------


def test_a_window_that_covers_the_requirement_is_full_marks():
    now = datetime.now(UTC)
    assert ms.deadline_score(now + timedelta(days=7), now + timedelta(days=30)) == 1.0


def test_a_shorter_window_decays_over_a_fortnight():
    now = datetime.now(UTC)
    assert ms.deadline_score(
        now + timedelta(days=21), now + timedelta(days=14)
    ) == pytest.approx(0.5, abs=0.01)
    assert ms.deadline_score(now + timedelta(days=60), now + timedelta(days=1)) == 0.0


def test_deadline_is_unscored_when_either_side_left_it_open():
    assert ms.deadline_score(None, datetime.now(UTC)) is None
    assert ms.deadline_score(datetime.now(UTC), None) is None


# --- relevance and category -------------------------------------------------


def test_hyphenation_does_not_break_product_matching():
    score = ms.relevance_score(
        ["type c cable"], "usb type c cable white",
        ["usb type-c cable"], "usb type-c cable white",
    )
    assert score > 0.8


def test_category_compares_by_token_not_by_string():
    assert ms.category_score("Electronics", "electronics") == 1.0
    assert ms.category_score("Electronics", "electronic") == 1.0
    assert ms.category_score("Electronics", "Packaging") == 0.0


def test_singular_and_plural_are_the_same_word():
    assert ms.singularise("cables") == "cable"
    assert ms.singularise("boxes") == "box"
    assert ms.singularise("batteries") == "battery"
    # Short words are left alone so units survive.
    assert ms.singularise("pcs") == "pcs"


# --- blending ---------------------------------------------------------------


def test_unscored_dimensions_are_dropped_and_the_rest_renormalised():
    """An RFQ is never punished for a field its counterparty left blank."""
    full = ms.blend({"relevance": 1.0, "price": 1.0, "location": 1.0})
    partial = ms.blend({"relevance": 1.0, "price": None, "location": 1.0})
    assert full == partial == 1.0


def test_blend_is_zero_when_nothing_could_be_evaluated():
    assert ms.blend({"relevance": None, "price": None}) == 0.0


def test_blend_is_a_weighted_mean():
    scores = {"relevance": 1.0, "location": 0.0}
    expected = ms.WEIGHTS["relevance"] / (
        ms.WEIGHTS["relevance"] + ms.WEIGHTS["location"]
    )
    assert ms.blend(scores) == pytest.approx(expected, abs=1e-4)


def test_every_weight_is_a_known_dimension_and_they_sum_to_one():
    assert sum(ms.WEIGHTS.values()) == pytest.approx(1.0)


# --- logistics estimation ----------------------------------------------------


def test_estimate_logistics_metro():
    res = ms.estimate_logistics(25.0)
    assert "Same day" in res["label"]
    assert res["customs_required"] is False
    assert res["transit_days_min"] == 0


def test_estimate_logistics_interstate():
    res = ms.estimate_logistics(350.0)
    assert "1–2 days" in res["label"]
    assert res["customs_required"] is False


def test_estimate_logistics_cross_border():
    res = ms.estimate_logistics(1200.0, is_cross_border=True)
    assert res["customs_required"] is True
    assert "International" in res["label"]

