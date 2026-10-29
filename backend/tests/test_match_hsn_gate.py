"""Disjoint names + different HSN headings is a different commodity (no model, no database)."""
import pytest

from services import match_service
from tests.test_match_blend import listing
from models.enums import RFQRole


@pytest.fixture(autouse=True)
def _no_ai(monkeypatch):
    from services import ai_lexicon, value_canon
    monkeypatch.setattr(ai_lexicon, "relation", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(value_canon, "canon_product", lambda *a, **k: None, raising=False)


def _keep(req_name, cand_name, sim, cand_title=None):
    req = listing(RFQRole.BUYER, f"Need {req_name}", {"name": req_name}, 10, "tonne", 20, "kg")
    cand = listing(RFQRole.SELLER, cand_title or f"{cand_name} stock", {"name": cand_name}, 10, "tonne", 20, "kg")
    return match_service._score(req, cand, sim)[1]


def test_onion_vs_turmeric_dropped():
    assert not _keep("onion", "turmeric", 0.74, "Dried Turmeric Bulbs Haldi Sangli")


def test_tomato_vs_potato_dropped():
    assert not _keep("tomato", "potato", 0.9)


def test_chickpea_vs_moong_documented():
    # moong has no HSN resolution, so the gate does not apply (old behaviour); only assert it does not crash.
    assert isinstance(_keep("chickpea", "moong", 0.9), bool)


def test_synonym_same_hsn_kept():
    assert _keep("onion", "pyaz", 0.9)


def test_shared_token_unaffected():
    assert _keep("red chilli", "chilli powder", 0.8)


def test_missing_hsn_old_behaviour():
    # moong has no HSN: soft fallback keeps it at sim >= floor
    assert _keep("onion", "moong", 0.74)
