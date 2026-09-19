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
    assert (r.quantity_value, r.quantity_unit) == (20, "pallets")
    assert r.attributes["capacity"] == {"value": 1000, "unit": "kg"}


@pytest.mark.parametrize(
    "message,unit",
    [
        ("200 bags of cement", "bags"),
        ("3 containers of denim fabric", "containers"),
        ("40 cartons of mango pulp", "cartons"),
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
