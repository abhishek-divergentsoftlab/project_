"""AI-Powered Product Attribute Recommender.

Dynamically discovers and suggests discriminating wholesale specifications for any
product name live in real time using the LLM (e.g. for "Milk" -> Fat %, Animal Source,
SNF %, Pasteurization; for "Cable" -> Connector Type, Voltage/Power, Layers, Outer Cover).

Employs:
1. In-memory LRU cache for instant repeat lookups (<1ms).
2. Live real-time Big LLM generation via Ollama (qwen3-coder:30b on GPU).
"""

from collections import OrderedDict
import json
import logging
import re
from typing import Any, Optional

from core.config import settings

logger = logging.getLogger(__name__)

# LRU Cache for AI-generated attributes (product_name -> list[dict])
_CACHE_SIZE = 1000
_attributes_cache: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()




async def _query_llm_for_attributes(
    product_name: str, category: Optional[str] = None, role: Optional[str] = None
) -> Optional[list[dict[str, Any]]]:
    """Ask big LLM model via Ollama to discover deeply technical, discriminating wholesale specifications."""
    if not getattr(settings, "ATTRIBUTE_AI_ENABLED", True):
        return None

    try:
        import httpx

        timeout = float(getattr(settings, "ATTRIBUTE_AI_TIMEOUT_SECONDS", 45.0))
        cat_hint = f" (Category: {category})" if category else ""
        role_hint = f" for a {role}" if role else ""

        # Comprehensive prompt instructing the model as a Senior B2B Industrial Specification Specialist
        prompt = (
            "You are a Senior B2B Industrial Trade & Product Specification Specialist.\n"
            f"Analyze the wholesale trade product: '{product_name}'{cat_hint}{role_hint} for a Buyer/Seller RFQ.\n\n"
            "Your objective is to discover 6 to 8 deeply technical, product-specific wholesale commercial specifications "
            "that buyers and sellers use to verify grade, price, compatibility, and quality.\n\n"
            "CRITICAL SPECIFICATION INSTRUCTIONS:\n"
            f"1. First, identify what type of product '{product_name}' is.\n"
            f"2. Generate ONLY specifications that strictly and logically apply to '{product_name}'. "
            "NEVER include unrelated attributes from other domains.\n"
            "3. Attribute Input Types ('input_type'):\n"
            "   - 'numeric': For measurable, numerical, or percentage values (e.g. fat %, SNF %, voltage, length, GSM, purity, thickness, wattage).\n"
            "   - 'select': For categorical specifications with standardized industry options (e.g. animal source, connector type, packaging format, processing method, flavor, jacket material).\n"
            "   - 'text': For open alphanumeric descriptions, model codes, or freeform specifications.\n\n"
            "4. STRICT NUMERICAL DATA RULE FOR 'numeric' ATTRIBUTES:\n"
            "   - Put the unit in the 'label', e.g. 'Fat Content (%)', 'SNF (%)', 'Operating Voltage (V)', 'Length (m)'.\n"
            "   - 'options' for numeric attributes MUST be purely numerical values with NO symbols, NO %, and NO text. Example: ['6.0', '4.5', '3.0'] NOT ['6.0% (Full Cream)'].\n"
            "   - 'placeholder' for numeric attributes MUST be purely numerical, e.g. 'e.g. 6.0' or 'e.g. 220'.\n\n"
            "5. Deep Industry Examples:\n"
            "   - If Dairy / Milk:\n"
            "     * 'animal_source' -> input_type: 'select', options: ['Desi Cow A2', 'Buffalo Milk', 'Goat (Bakri) Milk'], must_match: true.\n"
            "     * 'fat_content' -> label: 'Fat Content (%)', input_type: 'numeric', placeholder: 'e.g. 6.0', options: ['6.0', '4.5', '3.0', '0.5'], must_match: true.\n"
            "     * 'snf_content' -> label: 'SNF (%)', input_type: 'numeric', placeholder: 'e.g. 8.5', options: ['8.5', '9.0', '9.5'], must_match: false.\n"
            "     * 'processing_type' -> input_type: 'select', options: ['Pasteurized Chilled (HTST)', 'UHT Homogenized Aseptic', 'Raw Bulk'].\n"
            "   - If Cables / Wires / Electrical:\n"
            "     * 'connector_type' -> input_type: 'select', options: ['Type-C to Type-C', 'Type-C to USB-A', 'Type-C to Lightning', 'Bare Stripped Wire'], must_match: true.\n"
            "     * 'jacket_material' -> input_type: 'select', options: ['Braided Nylon Fabric', 'Flame Retardant PVC', 'TPE Ultra-Flexible', 'Silicone'].\n"
            "     * 'voltage_rating' -> label: 'Voltage Rating (V)', input_type: 'numeric', placeholder: 'e.g. 20', options: ['5', '20', '220', '1100'].\n"
            "     * 'cable_length' -> label: 'Cable Length (m)', input_type: 'numeric', placeholder: 'e.g. 1.5', options: ['1.0', '1.5', '2.0', '3.0'].\n"
            "     * 'shielding_layers' -> input_type: 'select', options: ['Double Shielded (Al-Foil + Braid)', 'Triple Layer Shielded', 'UTP Unshielded'].\n"
            "   - If Beverages / Soft Drinks:\n"
            "     * 'flavor_variant' -> input_type: 'select', options: ['Sparkling Apple', 'Classic Cola', 'Orange Citrus', 'Lemon Lime'].\n"
            "     * 'carbonation_level' -> input_type: 'select', options: ['Carbonated / Sparkling Fizz', 'Non-Carbonated / Still'].\n"
            "     * 'sugar_content' -> label: 'Sugar Content (g/100ml)', input_type: 'numeric', placeholder: 'e.g. 10.5', options: ['0.0', '8.5', '10.5'].\n\n"
            "Output Rules:\n"
            "- Use clean snake_case keys (e.g. 'animal_source', 'fat_content', 'connector_type', 'voltage_rating').\n"
            "- Provide 3 to 5 realistic options for each attribute.\n"
            "- Provide a concise 1-sentence commercial rationale for why this specification determines price, grade, or compatibility.\n"
            "- Set 'must_match': true for critical grading, origin, or compatibility specifications.\n\n"
            "Output ONLY a valid JSON object matching this schema:\n"
            "{\n"
            '  "suggested_attributes": [\n'
            '    {\n'
            '      "key": "snake_case_key",\n'
            '      "label": "Human Readable Title (with Units in parentheses)",\n'
            '      "input_type": "numeric" | "select" | "text",\n'
            '      "placeholder": "e.g. 6.0",\n'
            '      "options": ["Option 1", "Option 2", "Option 3"],\n'
            '      "explanation": "1-sentence commercial trade rationale",\n'
            '      "must_match": true\n'
            '    }\n'
            '  ]\n'
            "}"
        )

        # Candidate models prioritizing high-capability models
        configured_model = getattr(settings, "ATTRIBUTE_AI_MODEL", "")
        candidates = [
            configured_model,
            "qwen3-coder:30b",
            "qwen3-coder-gpu:30b",
            "qwen3-coder-next:q4_K_M",
            getattr(settings, "OLLAMA_MODEL", ""),
            getattr(settings, "AI_CHAT_MODEL", ""),
        ]
        models_to_try = [m for m in dict.fromkeys(candidates) if m]

        async with httpx.AsyncClient(timeout=timeout) as client:
            for model_name in models_to_try:
                try:
                    resp = await client.post(
                        f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/generate",
                        json={
                            "model": model_name,
                            "prompt": prompt,
                            "stream": False,
                            "format": "json",
                            "options": {
                                "temperature": 0.15,
                                "num_ctx": 4096,
                            },
                            "keep_alive": "30m",
                        },
                    )
                    if resp.status_code != 200:
                        continue

                    resp_data = resp.json()
                    raw_text = resp_data.get("response", "") or ""

                    # Extract JSON object from raw response
                    json_str = raw_text.strip()
                    if "{" in json_str:
                        start_idx = json_str.find("{")
                        end_idx = json_str.rfind("}")
                        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                            json_str = json_str[start_idx : end_idx + 1]

                    raw_res = json.loads(json_str)
                    raw_attrs = raw_res.get("suggested_attributes", [])
                    if not isinstance(raw_attrs, list) or len(raw_attrs) == 0:
                        continue

                    cleaned_attrs: list[dict[str, Any]] = []
                    for item in raw_attrs:
                        if not isinstance(item, dict):
                            continue
                        raw_k = str(item.get("key", "")).strip().lower()
                        k = re.sub(r"[^a-z0-9_]+", "_", raw_k).strip("_")

                        label = str(item.get("label", "")).strip()
                        if not k or not label:
                            continue

                        # Determine input_type: "numeric", "select", or "text"
                        raw_type = str(item.get("input_type", "")).strip().lower()
                        if any(w in raw_type for w in ("num", "int", "float", "dec", "percent")):
                            input_type = "numeric"
                        elif any(w in raw_type for w in ("select", "choice", "dropdown", "option", "enum")):
                            input_type = "select"
                        elif "options" in item and isinstance(item["options"], list) and len(item["options"]) >= 2:
                            input_type = "select"
                        else:
                            input_type = "text"

                        placeholder = str(item.get("placeholder", "")).strip() or f"e.g. {label}"
                        raw_opts = [str(o).strip() for o in item.get("options", []) if str(o).strip()]

                        # If numeric, enforce strictly numerical values (strip %, units, or extra text)
                        opts: list[str] = []
                        if input_type == "numeric":
                            for opt in raw_opts:
                                match = re.search(r"[-+]?\d+(?:\.\d+)?", opt)
                                if match:
                                    opts.append(match.group(0))
                                elif opt.strip():
                                    opts.append(opt.strip())
                            # Clean placeholder if needed
                            if "%" in placeholder or any(c.isalpha() for c in placeholder.replace("e.g.", "")):
                                m_pl = re.search(r"\d+(?:\.\d+)?", placeholder)
                                if m_pl:
                                    placeholder = f"e.g. {m_pl.group(0)}"
                        else:
                            opts = raw_opts

                        explanation = str(item.get("explanation", "")).strip() or None
                        must_match = bool(item.get("must_match", False))

                        cleaned_attrs.append({
                            "key": k,
                            "label": label,
                            "input_type": input_type,
                            "placeholder": placeholder,
                            "options": opts[:6],
                            "explanation": explanation,
                            "must_match": must_match,
                        })

                    if cleaned_attrs:
                        return cleaned_attrs

                except Exception as model_err:
                    logger.debug("Failed attribute generation with model '%s': %s", model_name, model_err)

                    continue

        return None

    except Exception as exc:
        logger.warning("AI product attribute suggestion error: %s", exc)
        return None


async def recommend_product_attributes(
    product_name: str, category: Optional[str] = None, role: Optional[str] = None
) -> tuple[list[dict[str, Any]], str]:
    """Suggests deep, discriminating wholesale specifications for a given product name live in real time via LLM.

    Returns:
        (suggested_attributes, source) where source is 'llm', 'cache', or 'none'.
    """
    clean_name = product_name.strip()
    if not clean_name:
        return [], "none"

    cache_key = f"{clean_name.lower()}::{category or ''}::{role or ''}"
    if cache_key in _attributes_cache:
        _attributes_cache.move_to_end(cache_key)
        return _attributes_cache[cache_key], "cache"

    # 1. Live real-time Big LLM generation via Ollama
    llm_attrs = await _query_llm_for_attributes(clean_name, category=category, role=role)
    if llm_attrs:
        _attributes_cache[cache_key] = llm_attrs
        if len(_attributes_cache) > _CACHE_SIZE:
            _attributes_cache.popitem(last=False)
        return llm_attrs, "llm"

    return [], "none"
