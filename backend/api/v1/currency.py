"""Currency endpoints: real-time conversion rates and supported ISO codes."""

from fastapi import APIRouter

from services import currency

router = APIRouter(prefix="/currency", tags=["currency"])


@router.get("/rates")
async def get_rates() -> dict:
    """Return active FX conversion matrix with base currency and timestamp."""
    return currency.get_rates_info()


@router.get("/normalize")
async def normalize_currency(query: str) -> dict:
    """AI / Fuzzy normalizer converting natural language terms (e.g. 'india ruppes', 'us dollar', 'kr')

    into standardized 3-letter ISO 4217 currency codes.
    """
    normalized = currency.normalize_currency(query)
    iso_code = normalized or query.strip().upper()
    return {
        "raw": query,
        "currency": iso_code,
        "name": currency.CURRENCY_NAMES.get(iso_code, iso_code),
        "supported": currency.supported(iso_code),
    }

