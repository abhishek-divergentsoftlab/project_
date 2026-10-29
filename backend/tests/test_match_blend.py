"""Stage-2 blend on transient listings (no database, no model): relevance saturation, over-budget, listing breadth,
canonical product equality."""
import uuid
from decimal import Decimal

import pytest

from models.enums import RFQRole, RFQStatus
from models.rfq import RFQ
from services import match_service


def listing(role, title, pd, qty, unit, price, per, city="Indore"):
    return RFQ(
        id=uuid.uuid4(), user_id=uuid.uuid4(), role=role, status=RFQStatus.ACTIVE, category="Agriculture", title=title,
        product_details=pd, quantity_value=Decimal(qty), quantity_unit=unit, price_amount=Decimal(price),
        price_currency="INR", price_per_unit=per, location_city=city, location_state="Madhya Pradesh",
        location_country="India", search_tags=[], search_text=title,
    )


def total(req, cand, sim):
    score, keep = match_service._score(req, cand, sim)
    return score.total, keep


def test_same_product_relevance_noise_does_not_outvote_price():
    req = listing(RFQRole.BUYER, "Wheat 50 tonne", {"name": "wheat"}, 50, "tonne", 24000, "tonne")
    cheap = listing(RFQRole.SELLER, "Wheat lot A", {"name": "wheat"}, 100, "tonne", 23, "kg")
    dear = listing(RFQRole.SELLER, "Wheat lot B", {"name": "wheat"}, 100, "tonne", 2700, "quintal")
    t_cheap, k1 = total(req, cheap, 0.90)
    t_dear, k2 = total(req, dear, 0.94)
    assert k1 and k2 and t_cheap > t_dear + 0.03


def test_keyword_stuffer_ranks_clearly_below_specific_listing():
    req = listing(RFQRole.BUYER, "Red onion 20 tonne", {"name": "onion", "color": "red"}, 20, "tonne", 15, "kg")
    real = listing(RFQRole.SELLER, "Red onion Nashik", {"name": "onion", "color": "red"}, 50, "tonne", 14, "kg")
    stuffer = listing(
        RFQRole.SELLER, "onion potato tomato garlic ginger rice wheat dal sugar all available",
        {"name": "vegetables", "_canon": {"v": 1, "product": "vegetables", "values": {}, "n": 9}}, 50, "tonne", 14, "kg",
    )
    t_real, _ = total(req, real, 0.93)
    t_stuff, _ = total(req, stuffer, 0.90)
    assert t_real - t_stuff >= 0.1


def test_canonical_product_equality_lifts_a_translated_name():
    canon = lambda p: {"v": 1, "product": p, "values": {}, "n": 1}  # noqa: E731
    req = listing(RFQRole.BUYER, "Frobnitz", {"name": "frobnitz", "_canon": canon("frobnitz")}, 10, "tonne", 15, "kg")
    cand = listing(RFQRole.SELLER, "Grelmok", {"name": "grelmok", "_canon": canon("frobnitz")}, 20, "tonne", 14, "kg")
    plain = listing(RFQRole.SELLER, "Grelmok", {"name": "grelmok"}, 20, "tonne", 14, "kg")
    t_canon, keep = total(req, cand, 0.55)
    t_plain, keep_plain = total(req, plain, 0.55)
    assert keep and t_canon > t_plain + 0.2


def test_wording_difference_alone_does_not_drop_relevance():
    # requester's structured data carries code-like spec tokens; the candidate says the same thing in other words
    req = listing(RFQRole.BUYER, "MS pipe", {"name": "ms pipe", "grade": "IS1239"}, 10, "tonne", 60, "kg")
    cand = listing(RFQRole.SELLER, "Mild steel tube", {"name": "mild steel tube"}, 20, "tonne", 58, "kg")
    cand.search_text = "Mild steel tube"
    score, keep = match_service._score(req, cand, 0.95)
    assert keep and score.relevance >= 0.9  # was 0.38 (x0.4 drop) on wording alone


def test_drop_applies_on_positive_evidence():
    canon = lambda p: {"v": 1, "product": p, "values": {}, "n": 1}  # noqa: E731
    req = listing(RFQRole.BUYER, "Coil", {"name": "frobnitz", "grade": "IS1239", "_canon": canon("frobnitz")}, 10, "tonne", 60, "kg")
    other = listing(RFQRole.SELLER, "Sheet", {"name": "zorbex", "_canon": canon("zorbex")}, 20, "tonne", 58, "kg")
    other.search_text = "Sheet"
    low = listing(RFQRole.SELLER, "Sheet", {"name": "zorbex"}, 20, "tonne", 58, "kg")
    low.search_text = "Sheet"
    assert match_service._score(req, other, 0.95)[0].relevance < 0.5  # different canonical products
    assert match_service._score(req, low, 0.60)[0].relevance < 0.5  # low embedding similarity


def test_vocabulary_breadth_without_canon():
    from services import value_canon

    assert value_canon.listing_breadth({"name": "spices"}, "best price all spices cumin turmeric chilli coriander pepper cardamom available") >= 4
    assert value_canon.listing_breadth({"name": "onion"}, "Red onion Nashik") == 1
    assert value_canon.listing_breadth({"name": "stainless steel coil"}, "Stainless steel coil 304") == 1
    req = listing(RFQRole.BUYER, "Cumin", {"name": "cumin"}, 5, "tonne", 300, "kg")
    real = listing(RFQRole.SELLER, "Cumin seeds", {"name": "cumin"}, 10, "tonne", 290, "kg")
    spam = listing(RFQRole.SELLER, "best price all spices cumin turmeric chilli coriander pepper cardamom available",
                   {"name": "spices"}, 10, "tonne", 290, "kg")
    assert total(req, real, 0.93)[0] - total(req, spam, 0.93)[0] >= 0.1
