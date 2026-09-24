# Deep-Dive Architecture: B2B Search & Matching Logic

This document provides a comprehensive technical breakdown of how the **Search** and **Matchmaking** engines operate across the platform.

The system employs a **dual-engine architecture**:
1. **The B2B Multi-Dimensional RFQ Matchmaking Engine**: A two-stage hybrid retrieval & multi-attribute scoring pipeline combining high-dimensional dense vector embeddings with deterministic commercial compatibility rules.
2. **The Marketplace Sourcing Catalog Engine**: A high-performance faceted discovery and full-text search engine powering `/marketplace`.
3. **The Natural Language Query Extractor**: A high-speed deterministic parser converting free-form text queries into structured search parameters without LLM latency.

---

## Architecture Overview

```
                      +---------------------------------------+
                      |   Incoming Query / RFQ Requirement    |
                      +---------------------------------------+
                                          |
                   [ Natural Language Parser / Extraction ]
                                          |
                      +---------------------------------------+
                      |         Structured RFQ Payload        |
                      +---------------------------------------+
                                          |
        +---------------------------------+---------------------------------+
        |                                                                   |
        v                                                                   v
+-----------------------------+                           +-----------------------------------+
|  1. Marketplace Search      |                           |  2. Two-Stage Matchmaking Engine  |
|     (Catalog Exploration)   |                           |     (B2B Counterparty Matching)   |
+-----------------------------+                           +-----------------------------------+
| - Faceted filtering         |                           | STAGE 1: Retrieval & Hard Filters |
| - PostgreSQL ILIKE / Full   |                           | - Counterparty Role Inversion     |
|   text over search_text     |                           | - Qdrant Vector Cosine Retrieval  |
| - Proximity Haversine       |                           | - Hard Exclusions & Expiry Check  |
| - Trust & KYC filters       |                           +-----------------------------------+
| - Privacy Shield masking    |                                             |
+-----------------------------+                                             v
                                                          | STAGE 2: 7-D Scoring & Reranking  |
                                                          | - Relevance (28%)                 |
                                                          | - Attributes & Specs (22%)        |
                                                          | - Target vs Ask Price (16%)       |
                                                          | - Quantity & Unit Conversion (12%)|
                                                          | - Category Overlap (10%)          |
                                                          | - Distance & Logistics (8%)       |
                                                          | - Delivery Deadline (4%)          |
                                                          | - Guardrails & Reputation Delta   |
                                                          +-----------------------------------+
                                                                            |
                                                                            v
                                                          +-----------------------------------+
                                                          | Ranked Compatible Counterparties  |
                                                          +-----------------------------------+
```

---

## 1. The Two-Stage Matchmaking Engine

The core matching service (`services/match_service.py`) operates in two distinct stages: **Stage 1 (Broad Candidate Retrieval)** and **Stage 2 (Business Compatibility Scoring & Reranking)**.

### Stage 1: Retrieval & Hard Filtering
Stage 1 fetches a pool of up to `200` candidate listings (`CANDIDATE_POOL = 200`). Candidates must strictly pass non-negotiable hard constraints before any scoring occurs:

1. **Market Side Inversion (The Central Rule)**:
   - A **Buyer** is *only ever* matched against **Sellers**.
   - A **Seller** is *only ever* matched against **Buyers**.
   - Implemented via `RFQ.role == rfq.role.counterpart`.
2. **Self-Exclusion**:
   - A user cannot match against their own RFQs (`RFQ.user_id != requester_id`).
3. **Active Status & Expiry Enforcement**:
   - `RFQ.status == RFQStatus.ACTIVE`.
   - `or_(RFQ.expires_at.is_(None), RFQ.expires_at > now)`.

#### Primary Vector Retrieval (Qdrant + Dense Embeddings)
- **Text Canonicalization** (`services/rfq_indexing.py`):
  Before embedding, the RFQ is compiled into a canonical semantic string (`build_match_text`):
  ```
  [Category] [Product Name] [Title] [Attribute Key: Value...] [Description Notes]
  ```
  *Note:* Quantity, price, deadline, and city are deliberately excluded from `build_match_text` so that two different products in the same city or with the same price do not artificially score high vector similarity.
- **Embedding Generation** (`services/embeddings.py`):
  Generates 384-dimensional dense vectors using fast local or API embedding models (`bge-small-en-v1.5` / `all-MiniLM-L6-v2`).
- **Qdrant Vector Search** (`services/qdrant_index.py`):
  Executes an exact filtered vector query with payload constraints:
  - Role, status, and unexpired filters are run *inside* Qdrant payload filters.
  - **Relevance Gate**: Cut at `score_threshold = settings.RELEVANCE_FLOOR` (0.70).
  - **Fallback Tier**: If zero matches clear the 0.70 bar, the search automatically falls back to `settings.RELEVANCE_FALLBACK_FLOOR` (0.55) to prevent dead ends.
- **Database Hydration**:
  Qdrant supplies candidate UUIDs and cosine scores. The full relational model is hydrated from PostgreSQL via `selectinload(RFQ.user).selectinload(User.profile)`.

#### Fallback Lexical Retrieval (PostgreSQL)
If Qdrant or embedding services are temporarily offline, the system falls back to a PostgreSQL query ordering candidate rows by `search_tags` array overlap (`RFQ.search_tags.overlap(rfq.search_tags).desc()`) and recency.

---

## 2. Stage 2: 7-Dimensional Scoring & Reranking

In `services/match_scoring.py`, each candidate is evaluated across **7 independent dimensions**. Each dimension yields a normalized score between `0.0` and `1.0`.

### Dimension Weights Table

| Dimension | Weight | Code Evaluator | Description |
|---|:---:|---|---|
| **Relevance** | **28%** (`0.28`) | `similarity` or `relevance_score()` | Dense vector cosine similarity or token overlap on match text & tags. |
| **Attributes** | **22%** (`0.22`) | `attribute_score()` | Technical specifications matching with synonym graph and numerical tolerances. |
| **Price** | **16%** (`0.16`) | `price_score()` | Directional target vs. ask price comparison with multi-currency conversion. |
| **Quantity** | **12%** (`0.12`) | `quantity_score()` | Fulfillment capacity with unit conversions (kg, tonnes, packs, etc.). |
| **Category** | **10%** (`0.10`) | `category_score()` | Normalized token overlap across category taxonomy. |
| **Location** | **8%** (`0.08`) | `location_score()` | Great-circle Haversine distance with exponential decay and administrative fallback. |
| **Deadline** | **4%** (`0.04`) | `deadline_score()` | Lead time and fulfillment date overlap with 14-day shortfall decay. |

---

### Detailed Mathematical Formulas by Dimension

#### 1. Relevance Score (Weight: 0.28)
- When Qdrant vector retrieval runs:
  $$\text{Score}_{\text{relevance}} = \text{cosine\_similarity}(\vec{v}_{\text{query}}, \vec{v}_{\text{candidate}})$$
- When lexical fallback runs:
  $$\text{Score}_{\text{relevance}} = 0.6 \times \text{Jaccard}(\text{Tags}_{\text{req}}, \text{Tags}_{\text{cand}}) + 0.4 \times \text{Overlap}(\text{Tokens}_{\text{req}}, \text{Tokens}_{\text{cand}})$$

#### 2. Technical Attributes Score (Weight: 0.22)
Evaluates custom JSONB attributes (e.g. `gsm`, `voltage`, `color`, `material`, `thickness`):
- **Numeric Attributes** (e.g. `{"value": 65, "unit": "W"}` vs `{"value": 60, "unit": "W"}`):
  If units match:
  $$\text{Similarity}(a, b) = \max\left(0.0, 1.0 - \frac{|a - b|}{\max(|a|, |b|)}\right)$$
- **Synonym Graph Matching**:
  Text values are matched against a multi-lingual, multi-shade synonym dictionary (`_SYNONYM_GROUPS`):
  - Exact synonyms (e.g., `grey` $\leftrightarrow$ `gray`): Score = `1.0`
  - Close shades (e.g., `charcoal` $\leftrightarrow$ `dark grey`, `matte black` $\leftrightarrow$ `black`): Score = `0.85`–`0.90`
  - Distant tints (e.g., `silver` $\leftrightarrow$ `grey`): Score = `0.80`
- **Missing vs Stated Attributes**:
  - Attribute explicitly matches: Score = `1.0`
  - Attribute unmentioned by counterparty: Score = `0.5` *(silence is neither confirmation nor contradiction)*
  - Attribute explicitly contradicts: Score = `0.0`
- **`__must_match` Multiplier**:
  If the requester flags an attribute with `__must_match = True`:
  - Its weight increases by **$3\times$** (`_MUST_MATCH_WEIGHT = 3.0`).
  - Missing the attribute incurs a severe penalty: Score drops to **`0.15`** instead of `0.5`.

#### 3. Directional Price Score (Weight: 0.16)
Price comparison is strictly directional:
- **Buyer**: Supplies the Target Budget ($P_{\text{target}}$)
- **Seller**: Supplies the Asking Price ($P_{\text{ask}}$)
- **Cross-Currency Normalization**: If currencies differ (e.g. USD vs INR), $P_{\text{ask}}$ is dynamically converted to $P_{\text{target}}$ currency using `services/currency.py`.
- **Score Curve**:
  $$\text{Score}_{\text{price}} = \begin{cases} 1.0 & \text{if } P_{\text{ask}} \le P_{\text{target}} \\ \max\left(0.0, 1.0 - \frac{P_{\text{ask}} - P_{\text{target}}}{P_{\text{target}}}\right) & \text{if } P_{\text{ask}} > P_{\text{target}} \end{cases}$$
  *Behavior:* A seller charging $\le$ buyer target receives full marks ($1.0$). At $50\%$ above target, score is $0.50$. At $2\times$ target ($100\%$ overshoot), score drops to $0.0$.
- **Relevance Gate**: Only scored if $\text{Relevance} \ge 0.35$ (`COMPARABLE_FLOOR`). Comparing prices between unrelated items is meaningless.

#### 4. Quantity Fulfillment Score (Weight: 0.12)
Evaluates capacity ($Q_{\text{available}}$) vs requirement ($Q_{\text{needed}}$):
- **Multi-Unit Normalization** (`unit_converter.py`):
  - Converts mass units (`kg`, `quintal`, `tonne`, `mt`, `lbs`) to a common baseline (kilograms).
  - Handles packaging conversions: e.g., $1,000 \text{ boras}$ with `pack_weight_kg = 55` is automatically resolved to $55,000 \text{ kg} = 55 \text{ tonnes}$.
- **Score Curve**:
  $$\text{Score}_{\text{quantity}} = \begin{cases} 1.0 & \text{if } Q_{\text{available}} \ge Q_{\text{needed}} \\ \frac{Q_{\text{available}}}{Q_{\text{needed}}} & \text{if } Q_{\text{available}} < Q_{\text{needed}} \end{cases}$$
  *Behavior:* Partial supply is rewarded proportionally (e.g. providing $50\%$ of an industrial batch receives a score of $0.50$, acknowledging split sourcing).

#### 5. Category Score (Weight: 0.10)
Uses normalized token overlap across hierarchical categories:
$$\text{Score}_{\text{category}} = \text{Jaccard}(\text{Tokens}(\text{Cat}_{\text{req}}), \text{Tokens}(\text{Cat}_{\text{cand}}))$$
Prevents minor spelling or phrasing variances (e.g., *"Electronics & Hardware"* vs *"Electronic Components"*) from dropping to zero.

#### 6. Location & Proximity Score (Weight: 0.08)
Combines **geocoded geographic distance** with an **administrative hierarchy**:
- **Haversine Distance**:
  $$d = 2 R \arcsin\left(\sqrt{\sin^2\left(\frac{\Delta \text{lat}}{2}\right) + \cos(\text{lat}_1)\cos(\text{lat}_2)\sin^2\left(\frac{\Delta \text{lon}}{2}\right)}\right)$$
- **Exponential Geographic Decay**:
  $$\text{Score}_{\text{geo}} = \begin{cases} 1.0 & \text{if } d \le 50 \text{ km} \text{ (Same metro)} \\ \exp\left(-\frac{d - 50}{450}\right) & \text{if } d > 50 \text{ km} \end{cases}$$
- **Administrative Baseline**:
  - Same City: `1.0`
  - Same State: `0.6`
  - Same Country: `0.3`
  - Cross-Border: `0.1`
- **Blended Location Score**:
  $$\text{Score}_{\text{location}} = \max(\text{Score}_{\text{geo}}, \text{Score}_{\text{admin}})$$
  *Rationale:* Domestic freight 1,000 km away does not require customs or foreign exchange, so it should not score worse than an un-geocoded domestic counterpart.

#### 7. Deadline Fulfillment Score (Weight: 0.04)
Evaluates whether the seller can dispatch before the buyer's required delivery date:
$$\text{Score}_{\text{deadline}} = \begin{cases} 1.0 & \text{if } T_{\text{seller}} \le T_{\text{buyer}} \\ \max\left(0.0, 1.0 - \frac{T_{\text{seller}} - T_{\text{buyer}}}{14 \text{ days}}\right) & \text{if } T_{\text{seller}} > T_{\text{buyer}} \end{cases}$$

---

### Dynamic Renormalization & Weighted Blending

When an RFQ leaves a field blank (for example, no budget target specified, or no delivery deadline given), that dimension is dropped and the remaining weights are dynamically renormalized:

$$\text{Total Score} = \frac{\sum_{i \in \text{Available}} W_i \times \text{Score}_i}{\sum_{i \in \text{Available}} W_i}$$

*Key Principle:* An RFQ is never penalized for commercial terms that a counterparty left open for negotiation.

---

### Critical Guardrails & Quality Filters

To prevent high non-product scores (e.g. being in the same city or having a cheap price) from artificially pushing the wrong product to the top, two hard guardrails are enforced in `services/match_service.py`:

1. **Relevance Ceiling Rule**:
   ```python
   if relevance < settings.RELEVANCE_FLOOR:
       base_total = min(base_total, relevance)
   ```
   *Impact:* If product relevance is only `0.60`, the total score can **never** exceed `0.60`, regardless of how perfect the price, location, or quantity are.
2. **Distinct Commodity Guardrail**:
   If the requester specifies a product (e.g. `"tomatoes"`) and the candidate is a distinct commodity (e.g. `"apples"`):
   - Unless vector cosine similarity is $\ge 0.85$ (indicating high semantic synonymy), the candidate is completely eliminated (`keep = False`).
3. **Reputation Adjustment**:
   Candidate ranking is adjusted by verified user reviews:
   $$\text{Final Score} = \min\left(1.0, \max\left(0.0, \text{Total Score} + (\text{Avg Rating} - 3.5) \times 0.04\right)\right)$$

---

## 3. Marketplace Sourcing Catalog Engine

For open exploration at `/marketplace`, the platform uses **Catalog Service** (`services/catalog_service.py`):

- **Faceted Filters**:
  - `role`: Filter by Supply vs. Demand listings.
  - `category`: Substring and taxonomy filtering.
  - `min_price` / `max_price` & `currency`: Exact monetary windowing.
  - `city` / `country`: Geographic origin filters.
  - `verified_only`: Limits listings to KYC-verified sellers (`kyc_status = verified`) or high trust scores (`trust_score >= 60`).
- **Full-Text Multi-Field Search**:
  Case-insensitive search over `title`, `category`, `description`, `search_text`, and `location_city`.
- **Live Proximity Calculations**:
  Computes great-circle distance between the logged-in user's profile coordinates and each listing on the fly.
- **Privacy Shield (PII Protection)**:
  Direct telephone numbers, email addresses, and street addresses are stripped from `CatalogCounterpartyOut`. Only verified company names, trust scores, and city/country locations are exposed publicly until a connection is mutually accepted.

---

## 4. Natural Language Query Extractor

In conversational search and AI chat (`services/query_extractor.py`), user requests are converted into structured RFQ queries using a fast, deterministic tokenizer:

1. **Intent Cue Detection**:
   - `_SELL_CUES`: *"i can supply"*, *"we sell"*, *"have stock"*, *"for sale"* $\rightarrow$ `role = seller`
   - `_BUY_CUES`: *"looking for"*, *"need to buy"*, *"i want"*, *"requirement of"* $\rightarrow$ `role = buyer`
2. **Quantity & Measurement Parsing**:
   Detects numeric values adjacent to 30+ units (`pcs`, `units`, `kg`, `tonnes`, `quintals`, `meters`, `boras`, `cartons`, `pallets`).
3. **Price & Currency Extraction**:
   Detects currencies (`$`, `₹`, `€`, `£`, `rs`, `inr`, `usd`) and per-unit rate indicators (`per unit`, `each`, `/kg`).
4. **Geographic Entity Recognition**:
   Matches city and state tokens against the built-in database of thousands of Indian and global cities (`services/locations.py`) and resolves coordinates.
5. **Specification Extraction**:
   Separates descriptive qualities (`color`, `finish`, `grade`, `voltage`, `dimensions`) from the core product noun.

---

## Summary File Reference

| Component | Primary File Path | Responsibility |
|---|---|---|
| **Match Coordinator** | [`backend/services/match_service.py`](file:///home/divergent/Videos/venv/pp/project_/backend/services/match_service.py) | Two-stage orchestration, pool management, filtering, hydration. |
| **Scoring Engine** | [`backend/services/match_scoring.py`](file:///home/divergent/Videos/venv/pp/project_/backend/services/match_scoring.py) | 7-D mathematical formulas, synonym graph, unit & distance calculations. |
| **Vector Retrieval** | [`backend/services/qdrant_index.py`](file:///home/divergent/Videos/venv/pp/project_/backend/services/qdrant_index.py) | Qdrant client, collection schemas, payload filtering, vector search. |
| **Embeddings** | [`backend/services/embeddings.py`](file:///home/divergent/Videos/venv/pp/project_/backend/services/embeddings.py) | Text vectorization and embedding model interfaces. |
| **RFQ Text Canonicalizer** | [`backend/services/rfq_indexing.py`](file:///home/divergent/Videos/venv/pp/project_/backend/services/rfq_indexing.py) | Stable representation text (`search_text`, `search_tags`, `build_match_text`). |
| **Catalog Explorer** | [`backend/services/catalog_service.py`](file:///home/divergent/Videos/venv/pp/project_/backend/services/catalog_service.py) | Marketplace faceted search, PII masking, distance sorting. |
| **Query Parser** | [`backend/services/query_extractor.py`](file:///home/divergent/Videos/venv/pp/project_/backend/services/query_extractor.py) | Zero-latency regex & NLP parser for search intents. |
| **Unit Converter** | [`backend/services/unit_converter.py`](file:///home/divergent/Videos/venv/pp/project_/backend/services/unit_converter.py) | Mass and packaging volume normalization. |
| **Currency Converter** | [`backend/services/currency.py`](file:///home/divergent/Videos/venv/pp/project_/backend/services/currency.py) | Cross-border FX conversions for price matching. |
