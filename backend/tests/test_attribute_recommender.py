import pytest
from httpx import AsyncClient
from services.attribute_recommender import recommend_product_attributes


@pytest.mark.asyncio
async def test_recommend_milk_attributes():
    attrs, source = await recommend_product_attributes("Pure Milk", category="Food & Beverages")
    assert len(attrs) >= 4
    assert source in ("llm", "cache")
    
    keys = {a["key"] for a in attrs}
    assert "fat_content" in keys
    assert "animal_source" in keys

    # Verify options exist for Animal Source (e.g. Cow, Buffalo, Goat)
    animal_attr = next(a for a in attrs if a["key"] == "animal_source")
    assert len(animal_attr["options"]) >= 2
    assert any("cow" in opt.lower() or "buffalo" in opt.lower() or "goat" in opt.lower() for opt in animal_attr["options"])


@pytest.mark.asyncio
async def test_recommend_cable_attributes():
    attrs, source = await recommend_product_attributes("USB Type-C Fast Charging Cable", category="Electronics")
    assert len(attrs) >= 4
    assert source in ("llm", "cache")

    keys = {a["key"] for a in attrs}
    assert "connector_type" in keys or "power_delivery" in keys or "jacket_material" in keys

    # Verify options for jacket material or power delivery
    jacket_attr = next((a for a in attrs if a["key"] in ("jacket_material", "outer_cover", "outer_sheath")), None)
    if jacket_attr:
        assert len(jacket_attr["options"]) >= 2


@pytest.mark.asyncio
async def test_recommend_soft_drink_attributes():
    attrs, source = await recommend_product_attributes("Appy Fizz Sparkling Drink")
    assert len(attrs) >= 3
    keys = {a["key"] for a in attrs}
    assert "flavor_variant" in keys or "carbonation_level" in keys


@pytest.mark.asyncio
async def test_suggest_attributes_endpoint(client: AsyncClient):
    response = await client.get("/rfqs/suggest-attributes", params={"product_name": "Buffalo Milk", "category": "Food & Beverages"})
    assert response.status_code == 200
    data = response.json()
    assert data["product_name"] == "Buffalo Milk"
    assert len(data["suggested_attributes"]) >= 4
    
    attr_keys = [a["key"] for a in data["suggested_attributes"]]
    assert "fat_content" in attr_keys
