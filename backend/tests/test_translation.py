"""Automated tests for Deal Room multilingual real-time translation."""

import pytest
import uuid


async def test_get_supported_languages(make_actor):
    actor = await make_actor()
    res = await actor.get("/translation/languages")
    assert res.status_code == 200
    languages = res.json()
    assert isinstance(languages, list)
    assert len(languages) >= 10
    
    codes = [l["code"] for l in languages]
    for required in ["en", "es", "zh", "hi", "de", "fr", "ar", "ja"]:
        assert required in codes


async def test_translate_text_dictionary_and_caching(make_actor):
    actor = await make_actor()

    # 1. First translation of known B2B phrase
    payload = {
        "text": "What is your MOQ?",
        "target_language": "es",
        "source_language": "en",
    }
    res1 = await actor.post("/translation/translate", json=payload)
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["original_text"] == "What is your MOQ?"
    assert "¿Cuál es su cantidad mínima de pedido (MOQ)?" in data1["translated_text"] or "MOQ" in data1["translated_text"]
    assert data1["target_language"] == "es"
    assert data1["cached"] is False

    # 2. Second translation of identical phrase should hit in-memory LRU cache
    res2 = await actor.post("/translation/translate", json=payload)
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["translated_text"] == data1["translated_text"]
    assert data2["cached"] is True


async def test_translate_same_language(make_actor):
    actor = await make_actor()
    payload = {
        "text": "We need CIF Hamburg delivery terms.",
        "target_language": "en",
        "source_language": "en",
    }
    res = await actor.post("/translation/translate", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["translated_text"] == "We need CIF Hamburg delivery terms."
    assert data["cached"] is True


async def test_batch_translation(make_actor):
    actor = await make_actor()
    payload = {
        "texts": [
            "Hello",
            "Can you provide CIF price?",
            "Samples are ready for dispatch",
        ],
        "target_language": "de",
        "source_language": "en",
    }
    res = await actor.post("/translation/batch", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert "translations" in data
    assert len(data["translations"]) == 3
    assert data["translations"][0]["target_language"] == "de"


async def test_connection_translate_permissions(accepted_pair, make_actor):
    buyer, seller, conn_id, _listing = accepted_pair
    outsider = await make_actor()

    payload = {
        "text": "Can you provide FOB price?",
        "target_language": "fr",
    }

    # 1. Participant (Buyer) can translate in the connection
    buyer_res = await buyer.post(f"/translation/connections/{conn_id}/translate", json=payload)
    assert buyer_res.status_code == 200
    assert buyer_res.json()["target_language"] == "fr"

    # 2. Participant (Seller) can translate in the connection
    seller_res = await seller.post(f"/translation/connections/{conn_id}/translate", json=payload)
    assert seller_res.status_code == 200

    # 3. Outsider is forbidden
    outsider_res = await outsider.post(f"/translation/connections/{conn_id}/translate", json=payload)
    assert outsider_res.status_code == 403
    assert "not an authorized participant" in outsider_res.json()["detail"]

    # 4. Non-existent connection returns 404
    random_id = str(uuid.uuid4())
    not_found_res = await buyer.post(f"/translation/connections/{random_id}/translate", json=payload)
    assert not_found_res.status_code == 404


async def test_translate_arbitrary_foreign_text(make_actor):
    actor = await make_actor()
    payload = {
        "text": "Necesitamos 500 unidades para la próxima semana",
        "target_language": "en",
    }
    res = await actor.post("/translation/translate", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["target_language"] == "en"
    assert data["source_language"] == "es"
    assert "500" in data["translated_text"]
    assert "week" in data["translated_text"].lower() or "units" in data["translated_text"].lower()


async def test_language_detection():
    from services.translation_service import detect_language

    assert detect_language("Gracias por su oferta") == "es"
    assert detect_language("Danke schön für das Angebot") == "de"
    assert detect_language("Merci beaucoup pour votre message") == "fr"
    assert detect_language("Muito obrigado pela proposta") == "pt"
    assert detect_language("Grazie mille per la risposta") == "it"
    assert detect_language("We need delivery terms CIF Mumbai") == "en"
    assert detect_language("आपकी न्यूनतम ऑर्डर मात्रा क्या है?") == "hi"
    assert detect_language("Каковы условия доставки?") == "ru"
    assert detect_language("ما هي شروط الدفع؟") == "ar"
    assert detect_language("请问最低订购量是多少？") == "zh"
    assert detect_language("こんにちは、価格を教えてください") == "ja"
