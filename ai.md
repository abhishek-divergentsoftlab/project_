# AI Architecture & Capabilities: Where & How AI is Used

This document provides a comprehensive technical overview of all Artificial Intelligence (AI) and Machine Learning (ML) subsystems implemented across the B2B marketplace platform, detailing their exact models, architectures, prompts, fallback mechanisms, and operational flows.

---

## 1. High-Level AI Architecture & Tech Stack

The platform is designed to be **privacy-preserving, low-latency, and local-first**, utilizing locally hosted open-weight models managed through **Ollama** and a dedicated **Qdrant Vector Database**.

```
+----------------------------------------------------------------------------------------------------+
|                                      FRONTEND INTERFACES                                           |
|       Marketplace Search Bar     |     Deal Room Live Chat     |     AI Copilot Assistant          |
+----------------------------------------------------------------------------------------------------+
              |                                     |                                   |
              v                                     v                                   v
+-----------------------------+       +-----------------------------+       +-----------------------------+
|    Dense Vector Search      |       |  Vision & Translation AI    |       |   Multi-Agent Business AI   |
|                             |       |                             |       |                             |
| - Model: nomic-embed-text   |       | - Vision: qwen3-vl:8b       |       | - Model: qwen3-coder-next   |
|   (or qwen3-embedding:8b)   |       |   (Content moderation)      |       | - 6 Specialized Personas    |
| - Vector DB: Qdrant         |       | - Translation: Ollama LLM   |       | - Autonomous Tool Calling   |
| - Hybrid Lexical Re-rank    |       |   (11 languages + LRU cache)|       | - 64,000 token context      |
+-----------------------------+       +-----------------------------+       +-----------------------------+
              |                                     |                                   |
              +-------------------------------------+-----------------------------------+
                                                    |
                                                    v
                                    +-------------------------------+
                                    |     Local Ollama Runtime      |
                                    |     (http://localhost:11434)  |
                                    +-------------------------------+
```

### AI Model Roster

| Functionality | Model / Engine | Context / Dimension | Host / Protocol |
| :--- | :--- | :--- | :--- |
| **Conversational Multi-Agent Copilot** | `qwen3-coder-next:q4_K_M` | 64,000 tokens | Ollama (`/api/chat`, `/api/generate`) |
| **Dense Vector Semantic Embeddings** | `nomic-embed-text` / `qwen3-embedding:8b` | 768 / 4096 dims | Ollama (`/api/embed`) + Qdrant |
| **Vision AI Content Moderation** | `qwen3-vl:8b` | 4,096 tokens | Ollama (`/api/generate` with base64 images) |
| **Multilingual Real-Time Translation**| `qwen3-coder-next` + Dictionary Engine | 4,096 tokens | In-memory LRU + Ollama API |
| **B2B Query Specification Extractor**| Rule Engine + `qwen3-coder-next` | 4,096 tokens | Deterministic Parser + LLM Fallback |

---

## 2. Conversational Multi-Agent Business AI System

Located in [`backend/services/agent_registry.py`](file:///home/divergent/Videos/venv/pp/project_/backend/services/agent_registry.py) and [`backend/services/ai_chat_service.py`](file:///home/divergent/Videos/venv/pp/project_/backend/services/ai_chat_service.py).

Instead of a generic chatbot, the system utilizes a **Specialized Multi-Agent Persona Architecture**. Each agent is assigned distinct system prompts, operational constraints, and executable tool permissions.

```
                             +------------------------+
                             |    User In Deal Room   |
                             +------------------------+
                                          |
                                          v
                             +------------------------+
                             |  Agent Router & Prompt |
                             +------------------------+
                                          |
        +----------------+----------------+---------------+----------------+
        |                |                |               |                |
        v                v                v               v                v
+---------------+ +---------------+ +---------------+ +---------------+ +---------------+
|Market Research| | RFQ Drafting  | |  Negotiation  | | Price Analyst | | Logistics     |
|Agent (🔍)     | | Agent (📋)    | | Coach (🤝)    | | Agent (📊)    | | Advisor (🚚)  |
+---------------+ +---------------+ +---------------+ +---------------+ +---------------+
        |                |                |               |                |
        |                +-------+--------+-------+-------+                |
        |                        |                |                        |
        v                        v                v                        v
+---------------+        +---------------+  +---------------+      +---------------+
|Marketplace    |        | Tool:         |  | Tool:         |      | Verification  |
|Catalog DB     |        | update_rfq_   |  | send_counter- |      | Agent (✅)    |
|Search API     |        | draft         |  | party_message |      | Trust Analysis|
+---------------+        +---------------+  +---------------+      +---------------+
```

### The 6 Specialized Personas

#### 1. Market Research Agent (`market_research` - 🔍)
- **Role**: Analyzes catalog supply/demand, price distributions, competitor density, and identifies market gaps across 10,000+ live listings.
- **Rules**: Zero conversational filler. Delivers tabular comparisons (`Company | Price | Location | Rating | Verified`) and highlights market opportunities (e.g., *"Only 3 sellers offer X in Y region — low competition"*).

#### 2. RFQ Drafting Agent (`rfq_drafting` - 📋)
- **Role**: Guides buyers and sellers through conversational RFQ authoring with automated slot extraction (product, role, quantity, units, budget, location, deadlines, and specs).
- **Tool Calling**:
  - `update_rfq_draft`: Dynamically updates the active RFQ state in the database while chatting.
  - `create_rfq`: Formally submits and publishes the finalized RFQ to the live marketplace.

#### 3. Negotiation Coach (`negotiation` - 🤝)
- **Role**: Formulates tactical B2B counter-offers, payment terms (advance vs. LC vs. milestone-based), and volume discount requests.
- **Principles**: Anchoring strategies, BATNA identification, and win-win trade terms.
- **Tool Calling**:
  - `draft_counterparty_message`: Prepares structured negotiation message drafts for user review before the user explicitly confirms and sends to counterparties.

#### 4. Price Analyst Agent (`price_analyst` - 📊)
- **Role**: Normalizes multi-currency quotes (USD, EUR, INR, GBP), computes distance-based shipping estimates, and calculates **Total Landed Cost of Ownership** (product price + freight + customs tariffs).
- **Rules**: Outputs structured tables with best-value deals highlighted with ⭐.

#### 5. Logistics Advisor (`logistics` - 🚚)
- **Role**: Provides expert guidance on Incoterms 2020 (`EXW`, `FOB`, `CIF`, `CFR`, `DDP`, `CIP`), shipping modes (air, ocean, road), transit time projections, customs paperwork, and industrial packaging requirements.

#### 6. Supplier Verification Agent (`verification` - ✅)
- **Role**: Evaluates counterparty trust and fraud risk using live platform metrics:
  - 🟢 **Low Risk**: Verified KYC, 4.0+ star rating, valid ISO/CE certificates, >1 year established.
  - 🟡 **Medium Risk**: Pending KYC, <3 reviews, or missing compliance documents.
  - 🔴 **High Risk**: Unverified KYC, new account (<30 days), or negative feedback history.

### Strict B2B Guardrail Policy
All agents enforce strict scope bounding:
```
If the query is not about business, trade, or procurement:
Respond with ONLY: "I only assist with business, procurement, and B2B trade inquiries."
```

---

## 3. Dense Vector Embeddings & Semantic Search

Located in [`backend/services/embeddings.py`](file:///home/divergent/Videos/venv/pp/project_/backend/services/embeddings.py), [`backend/services/rfq_indexing.py`](file:///home/divergent/Videos/venv/pp/project_/backend/services/rfq_indexing.py), and [`backend/services/qdrant_index.py`](file:///home/divergent/Videos/venv/pp/project_/backend/services/qdrant_index.py).

### How It Works Under the Hood
1. **Canonical Text Projection**:
   Before generating embeddings, the product description is cleaned using `build_match_text()`. It isolates product names, technical specifications, and categories, while intentionally stripping volatile numbers (quantities, prices, dates) so the vector space reflects pure product identity.
2. **Dense Vector Generation**:
   The normalized text is transformed into a high-dimensional vector using `nomic-embed-text` (768-dim) or `qwen3-embedding:8b` (4096-dim) via Ollama's `/api/embed` endpoint.
3. **Qdrant Vector Indexing**:
   Vectors are stored in the Qdrant collection `marketplace_rfq` with **Cosine Distance**. Search queries use Approximate Nearest Neighbor (ANN) search to match semantically equivalent terms (e.g. *"corrugated carton"* matches *"5-ply packaging shipping box"*).
4. **Resilient Best-Effort Degradation**:
   If Ollama or Qdrant is unreachable or times out, the search engine automatically degrades to SQL `ILIKE` lexical matching. The search interface never crashes due to an AI sidecar outage.

---

## 4. Vision AI Content Moderation

Located in [`backend/services/image_moderator.py`](file:///home/divergent/Videos/venv/pp/project_/backend/services/image_moderator.py).

To prevent harassment, fraud, and illicit materials during real-time video webcam verifications and compliance certificate uploads, the platform incorporates a multimodal Vision AI moderator.

```
[Webcam Capture / Certificate Upload]
                 |
                 v
     [Base64 Image Encoding]
                 |
                 v
   [Ollama API: qwen3-vl:8b Vision]
   - Temperature: 0.0
   - Structured JSON Output Enforcement
                 |
                 v
  +-----------------------------+
  |    AI Safety Inspection     |
  |  1. Vulgarity / Profanity   |
  |  2. Weapons / Violence      |
  |  3. Sexual Content          |
  +-----------------------------+
                 |
        +--------+--------+
        |                 |
     [SAFE]          [FLAGGED]
        |                 |
        v                 v
[Proceed with Upload]  [Block Upload & Display Reason]
```

### Key Security Design Patterns
- **Fail-Closed Architecture**: If `IMAGE_MODERATION_FAIL_CLOSED=True` and the AI service experiences a timeout or network disruption, the upload is blocked by default rather than allowing potentially harmful media through.
- **Industrial Context Awareness**: The prompt explicitly trains the model that industrial products, warehouse pallets, raw materials, electronic components, and machinery are normal safe workplace items.
- **Structured JSON Schema**:
  ```json
  {
    "flagged": true,
    "category": "vulgarity" | "violence" | "sexual" | null,
    "reason": "short explanation if flagged, or empty string if safe"
  }
  ```

---

## 5. Multilingual Real-Time Translation Engine

Located in [`backend/services/translation_service.py`](file:///home/divergent/Videos/venv/pp/project_/backend/services/translation_service.py).

Cross-border B2B trade requires seamless communication across global buyers and sellers. The platform features an AI translation engine with **multi-tiered latency optimization**.

### Supported Languages (11)
- 🇺🇸 English (`en`)
- 🇪🇸 Spanish (`es`)
- 🇨🇳 Chinese Simplified (`zh`)
- 🇮🇳 Hindi (`hi`)
- 🇩🇪 German (`de`)
- 🇫🇷 French (`fr`)
- 🇦🇪 Arabic (`ar`)
- 🇯🇵 Japanese (`ja`)
- 🇷🇺 Russian (`ru`)
- 🇧🇷 Portuguese (`pt`)
- 🇮🇹 Italian (`it`)

### 4-Layer Low Latency Execution Flow

```
                      [Incoming Deal Room Message]
                                   |
                                   v
             +-------------------------------------------+
             | 1. In-Memory LRU Cache Check (2048 slots) |
             +-------------------------------------------+
                        |                          |
                     (Miss)                      (Hit) ---> Instant Return (~0.1ms)
                        v
             +-------------------------------------------+
             | 2. Pre-compiled B2B Phrase Dictionary     |
             |    (MOQs, Incoterms, payment terms)       |
             +-------------------------------------------+
                        |                          |
                     (Miss)                      (Hit) ---> Instant Return (~0.2ms)
                        v
             +-------------------------------------------+
             | 3. Ollama LLM Inference Translation       |
             |    (Zero-shot translation prompt)         |
             +-------------------------------------------+
                        |                          |
                    (Success)                   (Error)
                        v                          v
              [Save to LRU Cache]        +-----------------------------------+
              [Return Translation]       | 4. Clean Offline Tagged Fallback  |
                                         |    e.g. "[ES] Message Content"    |
                                         +-----------------------------------+
```

1. **Layer 1: LRU In-Memory Cache**: Stores up to 2,048 recent translations. Identical phrases return instantly with zero LLM overhead.
2. **Layer 2: B2B Trade Phrase Dictionary**: High-frequency trade terms (e.g., *"What is your MOQ?"*, *"Can you provide CIF price?"*, *"We accept your quotation"*) are matched directly against native-language dictionaries.
3. **Layer 3: Local Ollama Translation**: Complex messages are sent to Ollama with a strict zero-preamble translation prompt.
4. **Layer 4: Offline Resilient Fallback**: If Ollama is unavailable, the system safely prefixes the message with the target language badge (e.g. `[ES] original text`) rather than throwing an exception.

---

## 6. Hybrid Query Specification Extractor

Located in [`backend/services/query_extractor.py`](file:///home/divergent/Videos/venv/pp/project_/backend/services/query_extractor.py) and [`backend/services/llm_extractor.py`](file:///home/divergent/Videos/venv/pp/project_/backend/services/llm_extractor.py).

Search queries in B2B marketplaces often contain mixed requirements in a single sentence (e.g., *"Need 500 units of 5-ply corrugated carton boxes in Mumbai under ₹45/box"*).

The platform uses a **hybrid extraction architecture**:
1. **Deterministic Regex Engine (Primary)**:
   - Executes in **< 1ms**.
   - Reliably extracts numerical quantities, units (`kg`, `pcs`, `tons`, `meters`), target price amounts, currencies (`INR`, `USD`, `EUR`), and city names.
2. **LLM Gap Filler (`llm_extractor.py` - Secondary/Opt-in)**:
   - Activated via `DIRECT_SEARCH_LLM=true`.
   - Used **only to fill missing semantic gaps** (e.g. identifying complex product names and attributes like material grade `SS 316L`).
   - **Non-Overriding Guarantee**: The LLM is never allowed to override numerical prices or quantities detected by the deterministic parser, ensuring precision and eliminating hallucination risks.

---

## 7. AI Configuration Parameters

All AI components are controlled via environment settings in [`backend/core/config.py`](file:///home/divergent/Videos/venv/pp/project_/backend/core/config.py):

| Environment Variable | Default Value | Description |
| :--- | :--- | :--- |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Endpoint for the Ollama inference server. |
| `OLLAMA_MODEL` | `qwen3-coder-next:q4_K_M` | Primary LLM model for agents, chat, and drafting. |
| `OLLAMA_NUM_CTX` | `64000` | Context window size for complex multi-turn chats. |
| `EMBEDDING_MODEL` | `nomic-embed-text` | Dense vector embedding model for catalog matching. |
| `QDRANT_URL` | `http://localhost:6333` | Vector database instance endpoint. |
| `IMAGE_MODERATION_ENABLED`| `true` | Toggles vision-based content moderation. |
| `IMAGE_MODERATION_MODEL` | `qwen3-vl:8b` | Multimodal vision model for image safety. |
| `IMAGE_MODERATION_FAIL_CLOSED`| `true` | Rejects image uploads if the AI moderator is offline. |
| `DIRECT_SEARCH_LLM` | `false` | Enables LLM semantic assist for marketplace search parsing. |
