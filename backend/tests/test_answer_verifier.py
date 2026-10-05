"""The verifier must accept numbers that trace to data and flag everything else."""

from services.answer_verifier import (
    BANNED_PHRASES,
    extract_numeric_claims,
    render_facts_answer,
    verify,
)


def _values(text: str) -> list[float]:
    return [c.value for c in extract_numeric_claims(text)]


def test_currency_and_symbol_forms():
    claims = extract_numeric_claims("Your target is ₹18 per bottle, versus Rs. 1,250.50 or 20 INR.")
    assert [(c.value, c.currency) for c in claims] == [(18.0, "INR"), (1250.5, "INR"), (20.0, "INR")]


def test_quantities_with_units_and_grouping():
    claims = extract_numeric_claims("Procurement of 50,000 pcs and 1,23,456 kg")
    assert [c.value for c in claims] == [50000.0, 123456.0]
    assert claims[0].unit == "pcs"


def test_scaling_k_lakh_crore():
    assert _values("5k units") == [5000.0]
    assert _values("budget 2.5 lakh") == [250000.0]
    assert _values("worth 1.2 crore") == [12000000.0]


def test_percent():
    c = extract_numeric_claims("margin of 12.5%")[0]
    assert c.percent and c.raw == 12.5


def test_ignores_list_markers_headings_ids_and_urls():
    text = (
        "### 1. Summary\n"
        "1. First point\n"
        "2) Second point\n"
        "- 3. nested\n"
        "See [RFQ](/marketplace?rfq=123e4567-e89b-12d3-a456-426614174000) id 123e4567-e89b-12d3-a456-426614174000."
    )
    assert _values(text) == []


def test_small_bare_numbers_are_prose_but_counts_are_claims():
    assert _values("Here are 2 options") == []
    assert _values("There are 5 buyer RFQs") == [5.0]
    assert _values("0 sellers") == [0.0]


def test_allowed_vs_unverified():
    tools = [{"tool": "market_price_stats", "ok": True, "data": {"median": 4166.67, "n_listings": 3}, "facts": [], "notes": []}]
    ok = verify("Median is ₹4,167 across 3 listings.", tools, "what is the price?")
    assert ok.ok and ok.checked == 2
    bad = verify("That is 4,167 cases at ₹18.", tools, "what is the price?")
    assert not bad.ok
    assert [c.value for c in bad.unverified] == [18.0]


def test_user_message_and_draft_numbers_are_allowed():
    res = verify("You want 50,000 pcs at ₹18 within 7 days.", [], "buy 50000 pcs at 18", draft={"deadline": {"in_days": 7}})
    assert res.ok, res.unverified


def test_scaled_user_numbers():
    assert verify("Budget ₹2,50,000", [], "my budget is 2.5 lakh").ok
    assert verify("Qty 5,000", [], "need 5k").ok


def test_tolerance_and_rounding():
    tools = [{"tool": "t", "ok": True, "data": {"x": 1234.5}, "facts": [], "notes": []}]
    assert verify("About 1,235", tools, "").ok
    assert not verify("About 1,300", tools, "").ok


def test_no_tools_flags_volunteered_prices():
    res = verify("Buyers pay ₹18 and there are 5 buyer RFQs.", [], "analyze my listing")
    assert not res.ok and len(res.unverified) == 2 and res.read_tools_called == 0


def test_year_only_allowed_if_in_inputs():
    assert not verify("Prices rose in 2024.", [], "hello").ok
    assert verify("Prices in 2024.", [], "what about 2024?").ok


def test_percent_present_in_data():
    tools = [{"tool": "t", "ok": True, "data": {"score": 85}, "facts": [], "notes": []}]
    assert verify("85% match", tools, "").ok


def test_banned_phrases_case_insensitive():
    assert "market equilibrium" in BANNED_PHRASES
    res = verify("This sits at Market Equilibrium.", [], "")
    assert not res.ok and res.banned == ["market equilibrium"]
    assert verify("Typical Market conditions apply", [], "").banned == ["typical market"]
    assert verify("the stock market equilibriums", [], "").ok


def test_fallback_without_tools():
    text = render_facts_answer([], "price?")
    assert text.startswith("I don't have verified platform data to answer that yet.")


def test_fallback_uses_only_facts_and_passes_verification():
    tools = [
        {"tool": "search_listings", "args": {"product": "Appy Fizz"}, "ok": True,
         "data": {"distinct_listings": 0}, "notes": ["No matching active listings on the platform."],
         "facts": [{"id": "F1", "label": "seller listings", "value": 0, "unit": ""}]},
        {"tool": "market_price_stats", "args": {"product": "x"}, "ok": True, "data": {"n_listings": 4, "median": 1234.5},
         "notes": [], "facts": [{"id": "F1", "label": "median price", "value": 1234.5, "unit": "INR/pcs"}]},
    ]
    text = render_facts_answer(tools, "")
    assert "no matching data" in text.lower() and "| seller listings | 0 |" in text and "1,234.5 INR/pcs" in text
    assert verify(text, tools, "").ok, verify(text, tools, "").unverified


def test_existence_claim_without_a_data_tool_is_ungrounded():
    claim = "There are no active seller listings for Frostberry on the platform."
    res = verify(claim, [], "price of frostberry?")
    assert not res.ok and res.ungrounded
    tools = [{"tool": "search_listings", "ok": True, "data": {}, "facts": [], "notes": []}]
    assert verify(claim, tools, "price of frostberry?").ok
    # asking for details is not a marketplace claim
    assert verify("Please share the product so I can benchmark against marketplace listings.", [], "").ok
