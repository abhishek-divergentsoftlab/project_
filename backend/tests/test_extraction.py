"""What the parser makes of a sentence typed into the search box.

Pure functions again, so every phrasing that has caused trouble can be pinned
down cheaply rather than rediscovered by clicking.
"""

import pytest

from services.query_extractor import extract, merge, merge_answer, parse_answer


def test_a_full_sentence_is_taken_apart_correctly():
    r = extract(
        "i want 6000 pcs of white usb type c cable in indore within 7 days "
        "at 200 inr per piece"
    )
    assert r.quantity_value == 6000
    assert r.quantity_unit == "pcs"
    assert r.price_amount == 200
    assert r.price_currency == "INR"
    assert r.city == "Indore"
    assert r.country == "India"
    assert r.deadline_days == 7
    assert r.attributes["color"] == "white"
    assert r.attributes["type"] == "C"


# --- numbers that are specifications, not order sizes ------------------------


def test_a_capacity_is_not_also_an_order_quantity():
    """"a 20000 mah power bank" is not an order for 20,000 of anything.

    Reading it as one meant the assistant never asked how many the buyer
    wanted, and searched with a quantity nobody had given it.
    """
    r = extract("i want 20000 mah power bank with 65w usb pd and type c to c")
    assert r.quantity_value is None
    assert r.attributes["capacity"] == {"value": 20000, "unit": "mAh"}
    assert r.attributes["output_power"] == {"value": 65, "unit": "W"}
    assert r.attributes["c_to_c"] is True
    # No version was typed, so none is invented. "USB PD" shares every token
    # with "USB PD 3.0" and "USB PD 3.1", so it matches either.
    assert r.attributes["fast_charge_protocol"] == "USB PD"


def test_a_real_quantity_and_a_capacity_can_coexist():
    r = extract("i want 5000 pcs of 20000 mah power bank")
    assert (r.quantity_value, r.quantity_unit) == (5000, "pcs")
    assert r.attributes["capacity"] == {"value": 20000, "unit": "mAh"}


def test_an_alloy_grade_is_not_read_as_litres():
    """"l" is a real order unit, so "SS 316L" parsed as 316 litres."""
    r = extract("i want 3mm ss 316l stainless sheet")
    assert r.quantity_value is None
    assert r.attributes["grade"] == "SS 316L"
    assert r.attributes["thickness"] == {"value": 3, "unit": "mm"}


def test_a_grade_alongside_a_real_quantity():
    r = extract("i want 2 tonnes of 316l stainless sheet 3mm")
    assert (r.quantity_value, r.quantity_unit) == (2, "tonnes")
    assert r.attributes["grade"] == "SS 316L"


def test_an_order_in_kg_is_not_duplicated_as_a_capacity_spec():
    """It would cost every seller who did not declare "capacity: 500kg"."""
    r = extract("500 kg of basmati rice")
    assert (r.quantity_value, r.quantity_unit) == (500, "kg")
    assert "capacity" not in r.attributes


def test_a_genuine_capacity_spec_still_survives():
    r = extract("20 pallets with 1000 kg capacity")
    assert (r.quantity_value, r.quantity_unit) == (20, "pallet")
    assert r.attributes["capacity"] == {"value": 1000, "unit": "kg"}


@pytest.mark.parametrize(
    "message,unit",
    [
        ("200 bags of cement", "bag"),
        ("3 containers of denim fabric", "containers"),
        ("40 cartons of mango pulp", "carton"),
        ("12 rolls of kraft paper", "rolls"),
    ],
)
def test_trade_packaging_units_are_recognised(message, unit):
    r = extract(message)
    assert r.quantity_unit == unit


def test_seats_and_ply_are_specs_not_quantities():
    assert extract("i want a 6 seater dining table").quantity_value is None
    assert extract("i want a 6 seater dining table").attributes["seats"] == 6
    assert extract("5 ply corrugated boxes 10000 pcs").attributes["ply"] == "5-ply"
    assert extract("5 ply corrugated boxes 10000 pcs").quantity_value == 10000


# --- answering a question ----------------------------------------------------


def test_a_date_answer_does_not_become_the_product():
    """"i need it before 30 sep" once set product="sep" and quantity=30."""
    answer = parse_answer("deadline", "i need it before 30 sep")
    assert answer is not None
    assert answer.product is None
    assert answer.quantity_value is None
    assert answer.deadline_days is not None


def test_an_answer_only_fills_the_field_it_was_asked_about():
    base = extract("i want white usb type c cable")
    product_before = base.product

    answer = parse_answer("quantity", "5000")
    merged = merge_answer(base, answer)
    assert merged.quantity_value == 5000
    assert merged.product == product_before


def test_a_refinement_keeps_everything_it_did_not_mention():
    base = extract(
        "i want 5000 pcs of white usb type c cable in indore within 7 days "
        "at 200 inr per piece"
    )
    refined = merge(base, extract("show me black instead"), "show me black instead")

    assert refined.attributes["color"] == "black"
    assert refined.quantity_value == 5000
    assert refined.city == "Indore"
    assert refined.deadline_days == 7


def test_chitchat_is_not_mistaken_for_a_product():
    for message in ["hi", "hello there", "thanks", "ok"]:
        assert extract(message).product in (None, "")


def test_the_selling_side_is_recognised():
    assert extract("i can supply 10000 corrugated boxes from pune").role.value == "seller"
    assert extract("i need 10000 corrugated boxes in pune").role.value == "buyer"


def test_a_city_carries_its_country_and_coordinates():
    r = extract("i need cables in hamburg")
    assert r.city == "Hamburg"
    assert r.country == "Germany"
    assert r.latitude is not None
    assert r.currency_hint == "EUR"


def test_certifications_and_protocols_are_picked_up():
    r = extract("power bank with usb pd 3.1 and CE, UN38.3 certification")
    assert r.attributes["fast_charge_protocol"] == "USB PD 3.1"
    assert set(r.attributes["certification"]) >= {"CE", "UN38.3"}


def test_an_unversioned_protocol_matches_any_version_of_it():
    from services.match_scoring import attribute_score

    wanted = extract("power bank with usb pd").attributes
    assert wanted["fast_charge_protocol"] == "USB PD"

    assert attribute_score(wanted, {"fast_charge_protocol": "USB PD 3.1"}) == 1.0
    assert attribute_score(wanted, {"fast_charge_protocol": "USB PD 3.0"}) == 1.0
    assert attribute_score(wanted, {"fast_charge_protocol": "Quick Charge 4+"}) == 0.0


def test_a_version_that_was_typed_is_kept():
    assert extract("usb pd 3.1 charger").attributes["fast_charge_protocol"] == "USB PD 3.1"


def test_payment_phrase_with_rupees_typo_extracts_price_not_product_or_quantity():
    r = extract("i can pay 210 ruppes per unit")
    assert r.price_amount == 210
    assert r.price_currency == "INR"
    assert r.price_per_unit == "pcs"
    assert r.quantity_value is None
    assert r.product is None


def test_refining_price_and_currency_preserves_established_product_and_quantity():
    base = extract(
        "i want usb type c cable of white color in indore within 7 days at price 2 dollar per unit"
    )
    base.quantity_value = 50000
    refined = merge(base, extract("i can pay 210 ruppes per unit"), "i can pay 210 ruppes per unit")

    assert refined.product == base.product
    assert refined.quantity_value == 50000
    assert refined.price_amount == 210
    assert refined.price_currency == "INR"
    assert refined.price_per_unit == "pcs"
    assert refined.city == "Indore"


def test_standalone_currency_change_updates_currency_without_polluting_product():
    base = extract("i want 5000 pcs of white usb cables at 2 dollars")
    assert base.price_currency == "USD"

    refined = merge(base, extract("change currency to inr"), "change currency to inr")
    assert refined.price_currency == "INR"
    assert refined.price_amount == 2
    assert refined.product == base.product


def test_natural_language_currencies_in_extraction():
    # "india ruppes"
    r1 = extract("i want 1000 units of cotton shirts at 450 india ruppes per unit")
    assert r1.price_amount == 450
    assert r1.price_currency == "INR"

    # "us dollar"
    r2 = extract("need 500 widgets at 12 us dollar per piece")
    assert r2.price_amount == 12
    assert r2.price_currency == "USD"

    # "kr"
    r3 = extract("need 200 bearings at 85 kr per unit")
    assert r3.price_amount == 85
    assert r3.price_currency == "SEK"


# --- Conversational Adaptation, Negation & Date Handling ----------------------


def test_conversational_negation_and_product_refinement():
    """User changes requirement: rejects type C, specifies simple usb cables."""
    base = extract("i want usb type c cable of white color in indore within 7 days at price 2 dollar per unit")
    assert base.attributes.get("type") == "C"
    assert base.attributes.get("color") == "white"

    negated_update = extract("actully i dont need type c cables i need just simple usb cables")
    assert "type" in negated_update.negated_attributes
    assert "type" not in negated_update.attributes
    assert "dont" not in (negated_update.product or "")
    assert "actully" not in (negated_update.product or "")
    assert "cables" in (negated_update.product or "") or "cable" in (negated_update.product or "")

    merged = merge(base, negated_update, "actully i dont need type c cables i need just simple usb cables")
    assert "type" not in merged.attributes
    assert merged.attributes.get("color") == "white"
    assert "usb" in merged.product


def test_product_pivot_prunes_incompatible_technical_attributes():
    """Pivoting from USB cable to HDMI cables prunes type: C but keeps color, city, etc."""
    base = extract("i want usb type c cable of white color in indore within 7 days at price 2 dollar per unit")
    base.quantity_value = 3000
    base.quantity_unit = "units"

    hdmi_update = extract("actully sorry i need hdmi cables now")
    assert "actully" not in (hdmi_update.product or "")
    assert "sorry" not in (hdmi_update.product or "")
    assert "hdmi" in hdmi_update.product

    merged = merge(base, hdmi_update, "actully sorry i need hdmi cables now")
    assert "type" not in merged.attributes
    assert merged.attributes.get("color") == "white"
    assert "hdmi" in merged.product
    assert merged.quantity_value == 3000
    assert merged.city == "Indore"


def test_calendar_date_with_apostrophe_ordinal_and_of():
    """'it can be before 12\'th of november' must parse deadline and NOT become a product or quantity."""
    r = extract("it can be before 12'th of november")
    assert r.deadline_days is not None
    assert r.product is None
    assert r.quantity_value is None


def test_deadline_month_year_without_day():
    """'before november 2026' must parse deadline and NOT become product or quantity."""
    r = extract("before november 2026")
    assert r.deadline_days is not None
    assert r.product is None
    assert r.quantity_value is None


def test_deadline_answer_bare_number():
    """Answering '23' to deadline question parses as 23 days, not quantity."""
    ans = parse_answer("deadline", "23")
    assert ans is not None
    assert ans.deadline_days == 23
    assert ans.quantity_value is None


def test_filler_and_connector_type_a():
    """'okey then find usb type a cables' must strip filler and capture type: A."""
    r = extract("okey then find usb type a cables")
    assert r.product == "usb type cables"
    assert r.attributes.get("type") == "A"
    assert "okey" not in (r.product or "")
    assert "then" not in (r.product or "")


def test_state_and_region_extraction_does_not_become_product():
    """'in hariyana' and 'find in punjab' must extract states, never products."""
    r1 = extract("in hariyana")
    assert r1.product is None
    assert r1.state == "Haryana"
    assert r1.city is None

    r2 = extract("find in punjab")
    assert r2.product is None
    assert r2.state == "Punjab"
    assert r2.city is None

    r3 = extract("tomatoes in punjab")
    assert r3.product == "tomatoes"
    assert r3.state == "Punjab"
    assert r3.city is None


def test_location_pivot_in_merge_clears_old_city():
    """When switching location to a new state, the prior city must be cleared."""
    initial = extract("i need tomatos in indore")
    assert initial.product == "tomatos"
    assert initial.city == "Indore"

    haryana_req = extract("in hariyana")
    merged1 = merge(initial, haryana_req, "in hariyana")
    assert merged1.product == "tomatos"
    assert merged1.city is None
    assert merged1.state == "Haryana"

    punjab_req = extract("find in punjab")
    merged2 = merge(merged1, punjab_req, "find in punjab")
    assert merged2.product == "tomatos"
    assert merged2.city is None
    assert merged2.state == "Punjab"


def test_parse_answer_location_with_state():
    """When asked for location, answering with a state should succeed."""
    ans1 = parse_answer("location", "in hariyana")
    assert ans1 is not None
    assert ans1.state == "Haryana"
    assert ans1.city is None

    ans2 = parse_answer("location", "punjab")
    assert ans2 is not None
    assert ans2.state == "Punjab"
    assert ans2.city is None



