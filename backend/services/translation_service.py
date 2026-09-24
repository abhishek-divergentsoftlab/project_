"""Multilingual real-time translation service for B2B Deal Rooms.

Supports AI translation powered by local Ollama models with in-memory LRU caching,
automatic language detection, and resilient offline fallbacks.
"""

from __future__ import annotations

import collections
import logging
import re
from typing import Optional
import unicodedata

import httpx

from core.config import settings
from schemas.translation import LanguageInfo, TranslationResponse

logger = logging.getLogger(__name__)

SUPPORTED_LANGUAGES: list[LanguageInfo] = [
    LanguageInfo(code="en", name="English", native_name="English", flag="🇺🇸"),
    LanguageInfo(code="es", name="Spanish", native_name="Español", flag="🇪🇸"),
    LanguageInfo(code="zh", name="Chinese (Simplified)", native_name="简体中文", flag="🇨🇳"),
    LanguageInfo(code="hi", name="Hindi", native_name="हिन्दी", flag="🇮🇳"),
    LanguageInfo(code="de", name="German", native_name="Deutsch", flag="🇩🇪"),
    LanguageInfo(code="fr", name="French", native_name="Français", flag="🇫🇷"),
    LanguageInfo(code="ar", name="Arabic", native_name="العربية", flag="🇦🇪"),
    LanguageInfo(code="ja", name="Japanese", native_name="日本語", flag="🇯🇵"),
    LanguageInfo(code="ru", name="Russian", native_name="Русский", flag="🇷🇺"),
    LanguageInfo(code="pt", name="Portuguese", native_name="Português", flag="🇧🇷"),
    LanguageInfo(code="it", name="Italian", native_name="Italiano", flag="🇮🇹"),
]

LANGUAGE_MAP: dict[str, LanguageInfo] = {lang.code: lang for lang in SUPPORTED_LANGUAGES}

# LRU Cache to avoid hitting LLM repeatedly for identical trade phrases
_MAX_CACHE_SIZE = 2048
_translation_cache: collections.OrderedDict[tuple[str, str, str], str] = collections.OrderedDict()


# Common B2B Trade Phrases dictionary for instant zero-latency responses & offline fallbacks
B2B_PHRASE_DICTIONARY: dict[str, dict[str, str]] = {
    "hello": {
        "es": "Hola", "zh": "您好", "hi": "नमस्ते", "de": "Hallo",
        "fr": "Bonjour", "ar": "مرحبا", "ja": "こんにちは", "ru": "Здравствуйте",
        "pt": "Olá", "it": "Ciao", "en": "Hello"
    },
    "what is your moq?": {
        "es": "¿Cuál es su cantidad mínima de pedido (MOQ)?",
        "zh": "你们的最小起订量（MOQ）是多少？",
        "hi": "आपकी न्यूनतम ऑर्डर मात्रा (MOQ) क्या है?",
        "de": "Wie hoch ist Ihre Mindestbestellmenge (MOQ)?",
        "fr": "Quelle est votre quantité minimale de commande (MOQ) ?",
        "ar": "ما هو الحد الأدنى لكمية الطلب (MOQ)؟",
        "ja": "最小発注数量（MOQ）はいくらですか？",
        "ru": "Каков ваш минимальный объем заказа (MOQ)?",
        "pt": "Qual é a sua quantidade mínima de pedido (MOQ)?",
        "it": "Qual è il vostro ordine minimo (MOQ)?",
        "en": "What is your minimum order quantity (MOQ)?"
    },
    "can you provide cif price?": {
        "es": "¿Puede proporcionar precio CIF?",
        "zh": "您能提供到岸价（CIF）吗？",
        "hi": "क्या आप सीआईएफ (CIF) मूल्य प्रदान कर सकते हैं?",
        "de": "Können Sie den CIF-Preis angeben?",
        "fr": "Pouvez-vous fournir un prix CIF ?",
        "ar": "هل يمكنك تقديم سعر سيف (CIF)؟",
        "ja": "CIF価格をご提示いただけますか？",
        "ru": "Можете ли вы предоставить цену на условиях CIF?",
        "pt": "Você pode fornecer o preço CIF?",
        "it": "Potete fornire il prezzo CIF?",
        "en": "Can you provide CIF price?"
    },
    "can you provide fob price?": {
        "es": "¿Puede proporcionar precio FOB?",
        "zh": "您能提供离岸价（FOB）吗？",
        "hi": "क्या आप एफओबी (FOB) मूल्य प्रदान कर सकते हैं?",
        "de": "Können Sie den FOB-Preis angeben?",
        "fr": "Pouvez-vous fournir un prix FOB ?",
        "ar": "هل يمكنك تقديم سعر فوب (FOB)؟",
        "ja": "FOB価格をご提示いただけますか？",
        "ru": "Можете ли вы предоставить цену на условиях FOB?",
        "pt": "Você pode fornecer o preço FOB?",
        "it": "Potete fornire il prezzo FOB?",
        "en": "Can you provide FOB price?"
    },
    "we accept your quotation": {
        "es": "Aceptamos su cotización",
        "zh": "我们接受您的报价",
        "hi": "हम आपका कोटेशन स्वीकार करते हैं",
        "de": "Wir akzeptieren Ihr Angebot",
        "fr": "Nous acceptons votre devis",
        "ar": "نحن نقبل عرض الأسعار الخاص بك",
        "ja": "お見積もりを受諾いたします",
        "ru": "Мы принимаем ваше коммерческое предложение",
        "pt": "Aceitamos a sua cotação",
        "it": "Accettiamo il vostro preventivo",
        "en": "We accept your quotation"
    },
    "please share product specifications": {
        "es": "Por favor comparta las especificaciones del producto",
        "zh": "请分享产品规格",
        "hi": "कृपया उत्पाद विनिर्देश साझा करें",
        "de": "Bitte teilen Sie die Produktspezifikationen mit",
        "fr": "Veuillez partager les spécifications du produit",
        "ar": "يرجى مشاركة مواصفات المنتج",
        "ja": "製品の仕様書を共有してください",
        "ru": "Пожалуйста, поделитесь техническими характеристиками товара",
        "pt": "Por favor compartilhe as especificações do produto",
        "it": "Si prega di condividere le specifiche del prodotto",
        "en": "Please share product specifications"
    },
    "samples are ready for dispatch": {
        "es": "Las muestras están listas para el envío",
        "zh": "样品已准备好发货",
        "hi": "नमूने प्रेषण के लिए तैयार हैं",
        "de": "Die Muster sind versandbereit",
        "fr": "Les échantillons sont prêts pour l'expédition",
        "ar": "العينات جاهزة للإرسال",
        "ja": "サンプルの発送準備が整いました",
        "ru": "Образцы готовы к отправке",
        "pt": "As amostras estão prontas para envio",
        "it": "I campioni sono pronti per la spedizione",
        "en": "Samples are ready for dispatch"
    }
}


def detect_language(text: str) -> str:
    """Heuristic language detection based on Unicode script ranges and character sets."""
    sample = text.strip()[:300]
    if not sample:
        return "en"

    # Count script characters
    counts = {
        "zh": 0,
        "ja": 0,
        "hi": 0,
        "ar": 0,
        "ru": 0,
        "latin": 0,
    }

    for ch in sample:
        name = unicodedata.name(ch, "")
        if "DEVANAGARI" in name:
            counts["hi"] += 1
        elif "ARABIC" in name:
            counts["ar"] += 1
        elif "CYRILLIC" in name:
            counts["ru"] += 1
        elif "HIRAGANA" in name or "KATAKANA" in name:
            counts["ja"] += 1
        elif "CJK UNIFIED" in name:
            counts["zh"] += 1
        elif "LATIN" in name:
            counts["latin"] += 1

    # Check non-Latin dominant scripts
    for lang in ["hi", "ar", "ru", "ja"]:
        if counts[lang] >= 2 and counts[lang] > counts["latin"]:
            return lang

    if counts["zh"] >= 2 and counts["zh"] > counts["latin"]:
        # Japanese may have Kanji (CJK), but if Hiragana/Katakana present it was matched as ja
        return "ja" if counts["ja"] > 0 else "zh"

    # Latin-based languages heuristics (check common diacritics / markers)
    lower = sample.lower()
    if any(word in lower for word in [" gracias", " por favor", "¿", "¡", "precio", "cantidad", "hola"]):
        return "es"
    if any(word in lower for word in [" danke", " bitte", "guten", "preis", "lieferung", "hallo"]):
        return "de"
    if any(word in lower for word in [" merci", " s'il vous plaît", "bonjour", "prix", "devis"]):
        return "fr"
    if any(word in lower for word in [" obrigado", " por favor", "preço", "orçamento", "olá"]):
        return "pt"
    if any(word in lower for word in [" grazie", " per favore", "prezzo", "preventivo", "buongiorno"]):
        return "it"

    return "en"


def _normalize_lang_code(code: str) -> str:
    cleaned = code.strip().lower()
    if "-" in cleaned:
        cleaned = cleaned.split("-")[0]
    if "_" in cleaned:
        cleaned = cleaned.split("_")[0]
    return cleaned if cleaned in LANGUAGE_MAP else "en"


def get_cached_translation(text: str, source_lang: str, target_lang: str) -> Optional[str]:
    key = (source_lang, target_lang, text.strip())
    if key in _translation_cache:
        # Move to end (most recently used)
        _translation_cache.move_to_end(key)
        return _translation_cache[key]
    return None


def set_cached_translation(text: str, source_lang: str, target_lang: str, translated: str) -> None:
    key = (source_lang, target_lang, text.strip())
    _translation_cache[key] = translated
    if len(_translation_cache) > _MAX_CACHE_SIZE:
        _translation_cache.popitem(last=False)


async def translate_via_ollama(text: str, source_lang: str, target_lang: str) -> Optional[str]:
    """Invoke local Ollama model with specialized B2B trade prompt."""
    target_info = LANGUAGE_MAP.get(target_lang) or LANGUAGE_MAP["en"]
    source_info = LANGUAGE_MAP.get(source_lang) or LANGUAGE_MAP["en"]

    system_prompt = (
        f"You are a professional B2B cross-border trade translator. "
        f"Translate the following commercial trade negotiation message from {source_info.name} into {target_info.name} ({target_info.native_name}).\n\n"
        f"Strict translation rules:\n"
        f"1. Accurately preserve all Incoterms (e.g. FOB, CIF, CFR, EXW, DDP, DAP), technical specifications, model numbers, metrics, quantities, currency symbols, and formal tone.\n"
        f"2. Output ONLY the raw translated message directly.\n"
        f"3. Do NOT wrap the translation in quotes. Do NOT add any preamble, conversational filler, pronunciation guides, or explanation."
    )

    payload = {
        "model": "qwen2.5:1.5b",  # Fast local model
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        "stream": False,
        "options": {
            "temperature": 0.1,  # Low temperature for deterministic, accurate translations
            "num_predict": 1024,
        },
    }

    url = f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/chat"
    timeout = 10.0  # Rapid timeout for chat translation

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                msg = data.get("message") or {}
                content = (msg.get("content") or "").strip()
                # Clean up any surrounding quotes or backticks if generated
                content = re.sub(r'^["\'`]+|["\'`]+$', '', content).strip()
                if content:
                    return content
    except Exception as exc:  # noqa: BLE001
        logger.debug("Ollama translation unavailable (%s), using fallback", exc)

    return None


def translate_fallback(text: str, source_lang: str, target_lang: str) -> str:
    """Fallback translator using exact dictionary or marked translation for offline/mock test environments."""
    norm_text = text.strip().lower()
    
    # Check exact phrase dictionary
    if norm_text in B2B_PHRASE_DICTIONARY:
        dict_trans = B2B_PHRASE_DICTIONARY[norm_text].get(target_lang)
        if dict_trans:
            return dict_trans

    # Check punctuation-stripped dictionary
    clean_norm = re.sub(r'[?!.,;]+$', '', norm_text).strip()
    if clean_norm in B2B_PHRASE_DICTIONARY:
        dict_trans = B2B_PHRASE_DICTIONARY[clean_norm].get(target_lang)
        if dict_trans:
            # Preserve original question mark if present
            if text.strip().endswith("?") and not dict_trans.endswith("?"):
                return f"{dict_trans}?"
            return dict_trans

    # Clean fallback: Return text with target language indicator in offline mode
    target_info = LANGUAGE_MAP.get(target_lang) or LANGUAGE_MAP["en"]
    if target_lang == "es":
        return f"[ES] {text}"
    elif target_lang == "zh":
        return f"[ZH] {text}"
    elif target_lang == "hi":
        return f"[HI] {text}"
    elif target_lang == "de":
        return f"[DE] {text}"
    elif target_lang == "fr":
        return f"[FR] {text}"
    
    return f"[{target_info.code.upper()}] {text}"


async def translate_text(
    text: str,
    target_language: str,
    source_language: Optional[str] = None,
) -> TranslationResponse:
    """Translate text from source_language (or auto-detected) into target_language."""
    clean_text = text.strip()
    target_lang = _normalize_lang_code(target_language)

    if not clean_text:
        return TranslationResponse(
            original_text=text,
            translated_text="",
            source_language=source_language or "en",
            target_language=target_lang,
            cached=False,
        )

    # Detect source language if omitted
    source_lang = _normalize_lang_code(source_language) if source_language else detect_language(clean_text)

    # Identical languages don't need translation
    if source_lang == target_lang:
        return TranslationResponse(
            original_text=text,
            translated_text=clean_text,
            source_language=source_lang,
            target_language=target_lang,
            cached=True,
        )

    # 1. Check LRU Cache
    cached = get_cached_translation(clean_text, source_lang, target_lang)
    if cached is not None:
        return TranslationResponse(
            original_text=text,
            translated_text=cached,
            source_language=source_lang,
            target_language=target_lang,
            cached=True,
        )

    # 2. Check B2B phrase dictionary for instant zero-latency match
    norm_text = clean_text.lower()
    clean_norm = re.sub(r'[?!.,;]+$', '', norm_text).strip()
    translated: Optional[str] = None

    if norm_text in B2B_PHRASE_DICTIONARY and target_lang in B2B_PHRASE_DICTIONARY[norm_text]:
        translated = B2B_PHRASE_DICTIONARY[norm_text][target_lang]
    elif clean_norm in B2B_PHRASE_DICTIONARY and target_lang in B2B_PHRASE_DICTIONARY[clean_norm]:
        dict_trans = B2B_PHRASE_DICTIONARY[clean_norm][target_lang]
        translated = f"{dict_trans}?" if clean_text.endswith("?") and not dict_trans.endswith("?") else dict_trans

    # 3. Try Ollama LLM Translation if not in phrase dictionary
    if not translated:
        translated = await translate_via_ollama(clean_text, source_lang, target_lang)

    # 4. If Ollama was unavailable or timed out, use fallback translator
    if not translated:
        translated = translate_fallback(clean_text, source_lang, target_lang)

    # Store in cache
    set_cached_translation(clean_text, source_lang, target_lang, translated)

    return TranslationResponse(
        original_text=text,
        translated_text=translated,
        source_language=source_lang,
        target_language=target_lang,
        cached=False,
    )
