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


# Latin language distinctive stopword and commercial vocabulary sets
_LATIN_STOPWORDS: dict[str, set[str]] = {
    "es": {
        "de", "la", "que", "el", "en", "y", "a", "los", "se", "del", "las", "por", "un", "para",
        "con", "no", "una", "su", "al", "lo", "como", "más", "pero", "sus", "le", "ya", "o", "este",
        "sí", "porque", "esta", "son", "entre", "está", "cuando", "muy", "sin", "sobre", "también",
        "me", "hasta", "hay", "donde", "quien", "desde", "todo", "nos", "durante", "todos", "uno",
        "les", "ni", "contra", "otros", "ese", "eso", "ante", "ellos", "esto", "mí", "antes", "algunos",
        "qué", "unos", "yo", "otro", "otras", "otra", "él", "tanto", "esa", "estos", "mucho", "quienes",
        "nada", "muchos", "cual", "sea", "poco", "ella", "estar", "haber", "estas", "estaba", "estamos",
        "están", "precio", "cantidad", "hola", "gracias", "favor", "necesitamos", "producto", "muestras",
        "envío", "cotización", "pedido", "entrega", "pago", "plazo", "factura", "unidades", "detalles"
    },
    "de": {
        "der", "die", "das", "und", "in", "den", "von", "zu", "mit", "sich", "des", "auf", "für",
        "ist", "im", "dem", "nicht", "ein", "eine", "als", "auch", "es", "an", "werden", "aus",
        "er", "hat", "dass", "sie", "nach", "wird", "bei", "einer", "um", "am", "sind", "noch",
        "wie", "einem", "über", "einen", "so", "zum", "war", "haben", "nur", "oder", "aber", "vor",
        "zur", "bis", "mehr", "durch", "man", "sein", "wurde", "sei", "wir", "danke", "bitte",
        "preis", "lieferung", "angebot", "brauchen", "bestellung", "muster", "stück", "kosten", "hallo"
    },
    "fr": {
        "de", "la", "le", "et", "les", "des", "en", "un", "du", "une", "que", "est", "pour", "qui",
        "dans", "par", "plus", "pas", "au", "sur", "ne", "ce", "avec", "sont", "se", "ou", "son",
        "nous", "vous", "il", "elle", "ont", "été", "mais", "comme", "on", "tout", "merci", "prix",
        "devis", "commande", "livraison", "pièces", "échantillons", "besoin", "bonjour", "combien",
        "produit", "délai", "facture", "veuillez", "salutations"
    },
    "pt": {
        "de", "a", "o", "que", "e", "do", "da", "em", "um", "para", "é", "com", "não", "uma",
        "os", "no", "se", "na", "por", "mais", "as", "dos", "como", "mas", "foi", "ao", "ele",
        "das", "tem", "à", "seu", "sua", "ou", "ser", "quando", "muito", "há", "nos", "já",
        "está", "eu", "também", "só", "pelo", "pela", "até", "isso", "ela", "entre", "era", "depois",
        "sem", "mesmo", "aos", "ter", "seus", "quem", "obrigado", "obrigada", "preço", "orçamento",
        "entrega", "amostras", "quantidade", "precisamos", "pedido", "olá", "favor"
    },
    "it": {
        "di", "e", "il", "la", "che", "in", "a", "per", "una", "un", "sono", "mi", "si", "ho",
        "ma", "ha", "del", "da", "al", "le", "dei", "non", "lo", "con", "ed", "della", "nel",
        "anche", "come", "ci", "io", "se", "noi", "voi", "loro", "questo", "questa", "questi",
        "queste", "grazie", "prezzo", "preventivo", "ordine", "consegna", "campioni", "pezzi",
        "buongiorno", "spedizione", "fattura"
    },
    "en": {
        "the", "be", "to", "of", "and", "a", "in", "that", "have", "i", "it", "for", "not", "on",
        "with", "he", "as", "you", "do", "at", "this", "but", "his", "by", "from", "they", "we",
        "say", "her", "she", "or", "an", "will", "my", "one", "all", "would", "there", "their",
        "what", "so", "up", "out", "if", "about", "who", "get", "which", "go", "me", "when", "make",
        "can", "like", "time", "no", "just", "him", "know", "take", "people", "into", "year", "your",
        "good", "some", "could", "them", "see", "other", "than", "then", "now", "look", "only", "come",
        "its", "over", "think", "also", "back", "after", "use", "two", "how", "our", "work", "first",
        "well", "way", "even", "new", "want", "because", "any", "these", "give", "day", "most", "us",
        "please", "price", "order", "delivery", "samples", "quote", "quantity", "need", "units", "thanks"
    },
}


def detect_language(text: str) -> str:
    """Robust heuristic language detection based on Unicode script ranges and token frequency."""
    sample = text.strip()[:400]
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
        return "ja" if counts["ja"] > 0 else "zh"

    # Latin-based languages: Tokenize words
    words = re.findall(r"[a-zA-Záéíóúüñäößàèùâêîôûçãõìò]+", sample.lower())
    if not words:
        return "en"

    scores: dict[str, float] = {lang: 0.0 for lang in _LATIN_STOPWORDS}
    for w in words:
        for lang, vocab in _LATIN_STOPWORDS.items():
            if w in vocab:
                scores[lang] += 1.0

    # Diacritics weighting bonus
    lower_sample = sample.lower()
    if any(c in lower_sample for c in ["¿", "¡", "ñ", "á", "í", "ó", "ú"]):
        scores["es"] += 2.0
    if any(c in lower_sample for c in ["ä", "ö", "ü", "ß"]):
        scores["de"] += 2.5
    if any(c in lower_sample for c in ["œ", "ê", "ë", "è", "ç"]):
        scores["fr"] += 2.0
    if any(c in lower_sample for c in ["ã", "õ"]):
        scores["pt"] += 2.5
    if any(c in lower_sample for c in ["ì", "ò", "ù"]):
        scores["it"] += 1.5

    best_lang, best_score = max(scores.items(), key=lambda item: item[1])
    # If the non-English score clearly outweighs English or has at least 1 match, choose it
    if best_score > 0 and (best_lang != "en" or scores["en"] >= max(scores[l] for l in scores if l != "en")):
        return best_lang

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

    model_name = getattr(settings, "TRANSLATION_MODEL", "qwen2.5:1.5b")
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        "stream": False,
        "options": {
            "temperature": 0.1,
            "num_predict": 1024,
        },
    }

    url = f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/chat"
    timeout = float(getattr(settings, "TRANSLATION_TIMEOUT_SECONDS", 10.0))

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                msg = data.get("message") or {}
                content = (msg.get("content") or "").strip()
                content = re.sub(r'^["\'`]+|["\'`]+$', '', content).strip()
                if content:
                    return content
    except Exception as exc:  # noqa: BLE001
        logger.debug("Ollama translation unavailable (%s), trying online fallback", exc)

    return None


async def translate_via_service(
    text: str,
    target_lang: str,
    source_lang: Optional[str] = None,
) -> Optional[tuple[str, str]]:
    """Instant, highly accurate translation via public translation service with language detection.
    
    Returns tuple of (translated_text, detected_source_lang) on success, or None on failure.
    """
    sl = source_lang if (source_lang and source_lang != "auto") else "auto"
    tl = target_lang
    url = "https://translate.googleapis.com/translate_a/single"
    params = {
        "client": "gtx",
        "sl": sl,
        "tl": tl,
        "dt": "t",
        "q": text,
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                if data and isinstance(data, list) and len(data) > 0 and isinstance(data[0], list):
                    translated_chunks = [part[0] for part in data[0] if part and len(part) > 0 and part[0]]
                    translated_result = "".join(translated_chunks).strip()
                    detected_sl = data[2] if len(data) > 2 and isinstance(data[2], str) else (source_lang or "en")
                    if translated_result:
                        return translated_result, _normalize_lang_code(detected_sl)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Public translation service unavailable (%s)", exc)

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
            if text.strip().endswith("?") and not dict_trans.endswith("?"):
                return f"{dict_trans}?"
            return dict_trans

    # Offline marker (DO NOT CACHE THIS)
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
    """Translate text from source_language (or auto-detected) into target_language.
    
    Order of operations:
    1. Check LRU Cache
    2. Check B2B phrase dictionary (instant zero-latency)
    3. Try local Ollama LLM translation
    4. Try real-time Google Translation service
    5. Fallback to dictionary or indicator
    """
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

    if translated:
        set_cached_translation(clean_text, source_lang, target_lang, translated)
        return TranslationResponse(
            original_text=text,
            translated_text=translated,
            source_language=source_lang,
            target_language=target_lang,
            cached=False,
        )

    # 3. Try Ollama LLM Translation
    translated = await translate_via_ollama(clean_text, source_lang, target_lang)

    # 4. If Ollama was unavailable or timed out, try translation service
    if not translated:
        service_res = await translate_via_service(clean_text, target_lang, source_lang)
        if service_res:
            translated, actual_detected_source = service_res
            # Update source language if it was refined by the service
            if not source_language and actual_detected_source:
                source_lang = actual_detected_source

    # 5. Last resort fallback
    is_genuine_translation = True
    if not translated:
        translated = translate_fallback(clean_text, source_lang, target_lang)
        # If it returned a dummy bracketed string like "[ES] ...", don't mark as genuine
        if translated.startswith("[") and "]" in translated[:5]:
            is_genuine_translation = False

    # Store genuine translations in cache so future requests are instantaneous
    if is_genuine_translation and translated:
        set_cached_translation(clean_text, source_lang, target_lang, translated)

    return TranslationResponse(
        original_text=text,
        translated_text=translated,
        source_language=source_lang,
        target_language=target_lang,
        cached=False,
    )
