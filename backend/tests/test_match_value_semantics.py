"""Matching value semantics: identifiers vs magnitudes, conflicts below "unstated", unit-dimension alignment,
blend (relevance saturation, steeper over-budget), listing breadth. Pure functions, no model, no database."""
import pytest

from services import match_scoring as m
from services import spec_catalog, unit_converter


def attr(wanted, offered):
    return m.attribute_score_with_breakdown(wanted, offered)


def test_identifiers_are_not_magnitudes():
    # grade 304 vs 316, variety 1121 vs 1509, model A52 vs A54 are different items, not "96 % similar"
    assert attr({"grade": "304"}, {"grade": "316"})[0] < 0.2
    assert attr({"variety": "1121"}, {"variety": "1509"})[0] < 0.2
    assert attr({"model": "A52"}, {"model": "A54"})[0] < 0.2
    assert attr({"model": "5200"}, {"model": "5400"})[0] < 0.2


def test_same_identifier_written_differently():
    assert attr({"grade": "304"}, {"grade": "AISI 304"})[0] == 1.0
    assert attr({"variety": "1121"}, {"variety": "pusa 1121"})[0] >= 0.85
    assert attr({"variety": "pusa 1121"}, {"variety": "1509"})[0] < 0.2


def test_numeric_conflict_scores_below_unstated():
    unstated = attr({"gsm": "180"}, {"colour": "red"})[0]
    conflict = attr({"gsm": "180"}, {"gsm": "120"})[0]
    assert unstated == pytest.approx(0.5)
    assert conflict < 0.3 < unstated
    # within tolerance is a match, then a steep decay
    assert attr({"gsm": "180"}, {"gsm": "185"})[0] == 1.0
    assert attr({"gsm": "180"}, {"gsm": "162"})[0] == pytest.approx(0.7, abs=0.02)


def test_magnitude_keys_stay_numeric():
    assert attr({"thickness": "2"}, {"thickness": "2.05"})[0] == 1.0
    assert attr({"thickness": "2mm"}, {"thickness": "0.2 cm"})[0] == 1.0
    assert attr({"thickness": "2mm"}, {"thickness": "5 mm"})[0] < 0.3
    assert attr({"purity": "99%"}, {"purity": "99 percent"})[0] == 1.0
    assert attr({"purity": "99%"}, {"purity": "48%"})[0] < 0.3


def test_align_by_unit_dimension_when_keys_differ():
    score, breakdown = attr({"gsm": "180"}, {"weight": "180 g/m2"})
    assert score == 1.0 and breakdown[0]["offered_key"] == "weight"
    assert attr({"weight": "180 g/m2"}, {"gsm": "180"})[0] == 1.0
    assert attr({"gsm": "180"}, {"weight": "5.3 oz/yd2"})[0] == 1.0
    # a conflicting value under the other key is a conflict, not "unstated"
    assert attr({"gsm": "180"}, {"weight": "120 g/m2"})[0] < 0.3


def test_shared_dimension_is_not_aligned_on_a_conflict():
    # thickness vs width are both lengths: a different value under another key means "thickness not stated"
    score, breakdown = attr({"thickness": "2mm"}, {"width": "1250 mm"})
    assert breakdown[0]["status"] == "unstated" and score == pytest.approx(0.5)
    # ...but a matching value under a synonym-less key still aligns
    assert attr({"dia": "20mm"}, {"thickness": "2cm"})[0] == 1.0


def test_unit_dimensions_added():
    assert unit_converter.dimension_of_unit("gsm") == "areal_density"
    assert unit_converter.dimension_of_unit("g/m²") == "areal_density"
    assert unit_converter.compare_measurements("180 gsm", None, "5.3 oz/yd2", None) == 1.0
    assert unit_converter.compare_measurements("30 den", None, "3.3 tex", None) == 1.0
    assert unit_converter.compare_measurements("1 m2", None, "10.76 sqft", None) == 1.0
    assert unit_converter.compare_measurements("10 bar", None, "145 psi", None) == 1.0
    assert unit_converter.compare_measurements("12 gsm", None, "12 mm", None) == 0.0
    assert unit_converter.numeric_closeness(100, 105) == 1.0
    assert unit_converter.numeric_closeness(100, 67) < 0.05


def test_numeric_similarity_policy():
    f = spec_catalog.numeric_similarity
    assert f("grade", "304", "316")[0] == 0.0
    assert f("grade", "304", "304")[0] == 1.0
    assert f("size", "40/50", "30/40") is None  # a size range is a code, not a fraction
    assert f("grade", "316L", "304L") is None  # grade codes go through spec_codes
    assert f("count", "30s", "40s") is None  # not a physical unit
    assert f("gsm", "180", "180 g/m2")[0] == 1.0
    assert f("length", "1 m", "100 cm")[0] == 1.0
    assert f("length", "1 m", "2 kg")[0] == 0.0


def test_price_over_budget_is_steep():
    from decimal import Decimal as D

    ok = m.price_score(D(24000), D(23), "INR", "INR", "tonne", "kg")
    over = m.price_score(D(24000), D(2700), "INR", "INR", "tonne", "quintal")
    assert ok == 1.0 and over < 0.75
    assert m.price_score(D(100), D(140), "INR", "INR", "kg", "kg") == pytest.approx(0.0)


def test_relevance_saturates_and_breadth_penalises():
    assert m.saturate_relevance(0.90) == m.saturate_relevance(0.97) == 1.0
    assert m.saturate_relevance(0.5) == 0.5
    assert m.saturate_relevance(0.85) == pytest.approx(0.9)
    assert m.breadth_factor(1) == 1.0 and m.breadth_factor(2) < 1.0 and m.breadth_factor(9) < m.breadth_factor(3) < m.breadth_factor(2)


def test_canon_block_is_not_an_attribute():
    canon = {"v": 1, "product": "onion", "values": {"color": "red"}, "n": 1}
    score, breakdown = attr({"color": "red", "_canon": canon}, {"color": "red", "_canon": canon})
    assert score == 1.0 and [b["key"] for b in breakdown] == ["color"]


def test_canonical_values_bridge_languages():
    w = {"color": "red", "_canon": {"values": {"color": "red"}}}
    o = {"color": "lal", "_canon": {"values": {"color": "red"}}}
    assert attr(w, o)[0] == 1.0
    o_no = {"color": "lal"}
    assert attr(w, o_no)[0] < 0.3  # without canonical terms on both sides: the old logic
    other = {"color": "safed", "_canon": {"values": {"color": "white"}}}
    assert attr(w, other)[0] < 0.3


def test_percent_on_bounded_scale_uses_the_complement():
    assert attr({"purity": "99%"}, {"purity": "99.99%"})[0] < 0.1  # 1 % vs 0.01 % impurity
    assert attr({"purity": "99"}, {"purity": "99.99"})[0] < 0.1
    assert attr({"purity": "99%"}, {"purity": "99%"})[0] == 1.0
    assert attr({"purity": "99%"}, {"purity": "98.5%"})[0] > 0.5
    assert attr({"purity": "99.5%"}, {"purity": "99.9%"})[0] < 0.1
    assert 0.4 < attr({"moisture": "12%"}, {"moisture": "14%"})[0] < 0.7  # near 0 the value itself decides
    assert attr({"moisture": "12%"}, {"moisture": "12.3%"})[0] == 1.0


def test_inflections_containment_and_identifier_conflict_separate():
    wanted = {"variety": "1509", "type": "steam"}
    right = attr(wanted, {"variety": "pusa 1509", "type": "steamed"})[0]
    wrong = attr(wanted, {"variety": "1121", "type": "steam"})[0]
    assert right > 0.9 and wrong < 0.6 and right - wrong > 0.3
    assert attr({"type": "organic"}, {"type": "non organic"})[0] < 0.5


def test_alphanumeric_identifiers_are_compared_whole():
    g = lambda a, b: attr({"grade": a}, {"grade": b})[0]  # noqa: E731
    assert g("S30", "M30") < 0.2 and g("M20", "C20") < 0.2 and g("A52", "A54") < 0.2
    assert g("S30", "s-30") == 1.0 and g("S 30", "S30") == 1.0 and g("M-20", "m 20") == 1.0
    assert g("pusa 1509", "1509") >= 0.85  # complete identifier contained
    assert g("S30", "30") < 0.2  # letters are part of the identifier
    assert match_scoring_identifiers("S-30") == match_scoring_identifiers("s 30") == ["s30"]
    assert match_scoring_identifiers("30s") == ["30"] and match_scoring_identifiers("Fe 500D") == ["fe500"]


def match_scoring_identifiers(text):
    return m.identifiers(text)
