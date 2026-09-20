"""Tests for dynamic currency conversion, caching, and rate endpoints."""

from decimal import Decimal
import pytest
from services import currency


def test_rates_info_structure():
    info = currency.get_rates_info()
    assert info["base"] == "USD"
    assert "rates" in info
    assert "INR" in info["rates"]
    assert "EUR" in info["rates"]
    assert "as_of" in info


def test_convert_known_currencies():
    # 100 USD to INR
    inr_amount = currency.convert(Decimal("100"), "USD", "INR")
    assert inr_amount is not None
    assert inr_amount == Decimal("8300")

    # Identical currency conversion
    same = currency.convert(Decimal("500"), "EUR", "EUR")
    assert same == Decimal("500")


def test_convert_unknown_currency_returns_none():
    assert currency.convert(Decimal("100"), "XYZ", "USD") is None
    assert currency.convert(Decimal("100"), "USD", "ABC") is None


def test_update_rates_dynamically():
    original_inr = currency.RATES["INR"]
    try:
        currency.update_rates({"INR": "85.50"})
        assert currency.RATES["INR"] == Decimal("85.50")
        converted = currency.convert(Decimal("10"), "USD", "INR")
        assert converted == Decimal("855.00")
    finally:
        currency.update_rates({"INR": original_inr})


async def test_currency_rates_api_endpoint(client):
    response = await client.get("/currency/rates")
    assert response.status_code == 200
    data = response.json()
    assert data["base"] == "USD"
    assert "rates" in data
    assert data["rates"]["INR"] > 0


def test_normalize_currency_synonyms_and_patterns():
    # Indian Rupee variations (as explicitly requested: "india ruppes")
    assert currency.normalize_currency("india ruppes") == "INR"
    assert currency.normalize_currency("indian rupees") == "INR"
    assert currency.normalize_currency("india rupee") == "INR"
    assert currency.normalize_currency("ruppes") == "INR"
    assert currency.normalize_currency("rupees") == "INR"
    assert currency.normalize_currency("rs") == "INR"
    assert currency.normalize_currency("₹") == "INR"

    # US Dollar variations (as explicitly requested: "us dollar")
    assert currency.normalize_currency("us dollar") == "USD"
    assert currency.normalize_currency("us dollars") == "USD"
    assert currency.normalize_currency("bucks") == "USD"
    assert currency.normalize_currency("$") == "USD"

    # Scandinavian Krona variations (as explicitly requested: "kr")
    assert currency.normalize_currency("kr") == "SEK"
    assert currency.normalize_currency("krona") == "SEK"
    assert currency.normalize_currency("norwegian krone") == "NOK"
    assert currency.normalize_currency("danish krone") == "DKK"

    # Other common currencies
    assert currency.normalize_currency("euro") == "EUR"
    assert currency.normalize_currency("pounds") == "GBP"
    assert currency.normalize_currency("yen") == "JPY"
    assert currency.normalize_currency("yuan") == "CNY"


def test_money_schema_auto_normalization():
    from schemas.common import Money

    # From raw natural language in dict/kwargs
    m1 = Money(amount=Decimal("150"), currency="india ruppes")
    assert m1.currency == "INR"

    m2 = Money(amount=Decimal("20.5"), currency="us dollar")
    assert m2.currency == "USD"

    m3 = Money(amount=Decimal("99"), currency="kr")
    assert m3.currency == "SEK"

    m4 = Money.model_validate({"amount": 500, "currency": "norwegian krone"})
    assert m4.currency == "NOK"


def test_convert_with_natural_language_currency():
    # Convert 83 "india ruppes" to "us dollar" (should map INR -> USD)
    usd = currency.convert(Decimal("83"), "india ruppes", "us dollar")
    assert usd is not None
    assert usd == Decimal("1")

    # Convert 1 "us dollar" to "kr" (should map USD -> SEK at 10.5)
    sek = currency.convert(Decimal("1"), "us dollar", "kr")
    assert sek is not None
    assert sek == Decimal("10.5")


async def test_currency_normalize_api_endpoint(client):
    # Test "india ruppes" -> INR
    res_inr = await client.get("/currency/normalize", params={"query": "india ruppes"})
    assert res_inr.status_code == 200
    data_inr = res_inr.json()
    assert data_inr["currency"] == "INR"
    assert data_inr["name"] == "Indian Rupee"
    assert data_inr["supported"] is True

    # Test "us dollar" -> USD
    res_usd = await client.get("/currency/normalize", params={"query": "us dollar"})
    assert res_usd.status_code == 200
    assert res_usd.json()["currency"] == "USD"

    # Test "kr" -> SEK
    res_kr = await client.get("/currency/normalize", params={"query": "kr"})
    assert res_kr.status_code == 200
    assert res_kr.json()["currency"] == "SEK"

