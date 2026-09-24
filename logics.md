# Platform Logics: Trust Score, KYC Verification & Rating Systems

This document provides a technical and operational breakdown of how **Trust Scores**, **Company KYC Verification**, **Anti-Fraud Protections**, and the **Mutual Review & Rating System** work across the B2B marketplace platform.

---

## 1. Architectural Overview & Reputation Lifecycle

Reputation on the platform is governed by three interlinked layers:
1. **Static Verification Layer (KYC & Documents)**: Tax identity validation, company credentials, certificates, and establishment history.
2. **Behavioral Layer (Trust Score)**: Dynamic score computed between `0` and `100` that adapts based on verified credentials, uploaded compliance documents, and counterparty satisfaction.
3. **Transaction Feedback Layer (Mutual Rating Protocol)**: Post-delivery double-blind review process that rates orders across 4 dimensions and applies real-time bonuses or penalties to the trust score.

```
       +-------------------------------------------------------------+
       |                   User Registration (Base = 20)             |
       +-------------------------------------------------------------+
                                      |
         +----------------------------+----------------------------+
         |                                                         |
         v                                                         v
+-------------------------------+                       +-------------------------------+
|     KYC Verification Engine   |                       |    Compliance & Certs         |
|  - GSTIN / VAT / EIN Check    |                       |  - ISO / CE / GMP / FDA       |
|  - Anti-Fraud Dedup Check     |                       |  - Magic-byte validation      |
|  - Award: +35 (Pending: +15)  |                       |  - Award: +10 per cert        |
+-------------------------------+                       +-------------------------------+
         |                                                         |
         +----------------------------+----------------------------+
                                      |
                                      v
                      +-------------------------------+
                      |   Dynamic Trust Score Engine  |
                      |          (Range: 0 - 100)     |
                      +-------------------------------+
                                      |
         +----------------------------+----------------------------+
         |                                                         |
         v                                                         v
+-------------------------------+                       +-------------------------------+
|    Marketplace Discovery      |                       |   Matching & Deal Execution   |
|  - Verified Supplier Badging  |                       |  - Deal Room Quotation Flow   |
|  - "Verified Only" Filter     |                       |  - Match Score Boost / Penalty|
|  - Trust Descending Sort      |                       |  - High-Value Escrow Gate     |
+-------------------------------+                       +-------------------------------+
                                      |
                                      v
                      +-------------------------------+
                      |   Post-Delivery Mutual Review |
                      |    (4-Dimensional Rating)     |
                      |  - 4-5 Stars: +5 Trust Points |
                      |  - 3 Stars:   +1 Trust Point  |
                      |  - 1-2 Stars: -5 Trust Penalty|
                      +-------------------------------+
```

---

## 2. Dynamic Trust Score Calculation

The Trust Score is a normalized integer from **`0` to `100`** stored on the `UserProfile` table (`profile.trust_score`). It is computed and updated in real-time by [`kyc_service.py`](file:///home/divergent/Videos/venv/pp/project_/backend/services/kyc_service.py), [`certificate_service.py`](file:///home/divergent/Videos/venv/pp/project_/backend/services/certificate_service.py), and [`review_service.py`](file:///home/divergent/Videos/venv/pp/project_/backend/services/review_service.py).

### A. Point Allocation Matrix

| Credential / Verification Milestone | Points Added | Logic & Verification Source |
| :--- | :---: | :--- |
| **Account Creation Baseline** | **`+20`** | Base score awarded on successful profile initialization. |
| **KYC Status: `VERIFIED`** | **`+35`** | Awarded when tax registration (GSTIN / VAT / EIN) passes format and anti-fraud validation. |
| **KYC Status: `PENDING`** | **`+15`** | Awarded when documents are submitted and awaiting admin or automated review. |
| **Validated Phone Number** | **`+10`** | User has a validated contact number on file. |
| **Physical Address & City** | **`+10`** | User provides registered operational facility and city. |
| **Company Registration or PAN** | **`+10`** | Verified Corporate Identity (CIN), Business Reg No., or PAN. |
| **Business Vintage (< 2024)** | **`+10`** | Company established prior to 2024 (rewards seasoned enterprises). |
| **Official Corporate Website** | **`+5`** | Verified corporate domain URL provided on profile. |
| **Industry Compliance Certificate** | **`+10`** | Awarded for each verified certificate (e.g., ISO 9001, CE, GMP, FDA) up to cap. |

*Mathematical Constraint*: The final score is strictly clamped to:
$$\text{Trust Score} = \max(0, \min(100, \text{Calculated Score}))$$

### B. Python Implementation Reference

From `backend/services/kyc_service.py`:
```python
def calculate_trust_score(profile: UserProfile) -> int:
    """Calculates dynamic trust score (0 - 100) based on verified credentials."""
    score = 20  # baseline account creation

    if profile.kyc_status == KYCStatus.VERIFIED:
        score += 35
    elif profile.kyc_status == KYCStatus.PENDING:
        score += 15

    if profile.phone:
        score += 10
    if profile.address and profile.city:
        score += 10
    if profile.website:
        score += 5
    if profile.registration_number or profile.pan_number:
        score += 10
    if profile.year_established and profile.year_established < 2024:
        score += 10

    return max(0, min(100, score))
```

---

## 3. KYC Verification & Anti-Fraud Security Engine

The KYC verification service ([`backend/services/kyc_service.py`](file:///home/divergent/Videos/venv/pp/project_/backend/services/kyc_service.py)) prevents fraud, synthetic identities, and duplicate registrations across enterprise accounts.

### A. Tax Identifier Parsing & Format Validation
The engine supports domestic Indian GSTIN as well as international tax formats:

1. **Indian GSTIN (15 Alphanumeric characters)**:
   - Regex Pattern: `^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$`
   - State Prefix Check: Validates that the first 2 digits match genuine Indian State/UT codes (`01` through `38`, `97`, or `99`).
2. **International VAT**:
   - Regex Pattern: `^[A-Z]{2}[0-9A-Z]{8,12}$` (2-letter ISO country code followed by 8–12 alphanumeric characters).
3. **US Employer Identification Number (EIN)**:
   - Regex Pattern: `^\d{2}-\d{7}$` (`XX-XXXXXXX`).

### B. Cross-Tenant Anti-Fraud Check
To prevent bad actors from registering multiple dummy accounts with stolen or recycled tax credentials, the engine executes a cross-tenant uniqueness check before committing:

```python
dup_query = select(UserProfile).where(
    UserProfile.gst_number == cleaned_gst,
    UserProfile.user_id != user_id,
)
dup = (await db.execute(dup_query)).scalar_one_or_none()
if dup is not None:
    raise KYCError(
        "Anti-Fraud Alert: This GST / Tax ID is already registered to another enterprise account. "
        "Please contact fraud-support if you believe this is in error."
    )
```

### C. Certificate Upload Magic-Byte Validation
In [`certificate_service.py`](file:///home/divergent/Videos/venv/pp/project_/backend/services/certificate_service.py), uploaded certificate files (PDF, PNG, JPG) are validated against binary **magic bytes** (`%PDF-`, `\xFF\xD8\xFF`, `\x89PNG`, etc.) to prevent MIME-sniffing, file extension spoofing, or executable injection.

---

## 4. Mutual Rating & Review Protocol

The platform implements an **enforced sequential post-delivery review protocol** in [`review_service.py`](file:///home/divergent/Videos/venv/pp/project_/backend/services/review_service.py). This prevents review extortion, fake reviews, and retaliation.

### A. The 5-Step Order & Feedback Lifecycle

```
[Quotation ACCEPTED]
         |
         v
[Step 1: Seller Marks DISPATCHED]
         |
         v
[Step 2: Seller Marks DELIVERED]
         |
         v
[Step 3: Buyer Confirms Delivery (RECEIVED)]
         |
         v
[Step 4: Buyer Submits Review (Required First!)]
         |
         v
[Step 5: Seller Submits Reciprocal Review]
         |
         v
[Quotation Moves to COMPLETED]
```

1. **Step 1 - Dispatch**: Seller dispatches goods via Deal Room (`QuotationStatus.DISPATCHED`).
2. **Step 2 - Delivery Confirmation**: Seller marks shipment delivered at destination (`QuotationStatus.DELIVERED`).
3. **Step 3 - Goods Inspection & Acceptance**: Buyer physically inspects and confirms acceptance (`QuotationStatus.RECEIVED`).
4. **Step 4 - Buyer Rates Order**: The rating gate unlocks **only** after Step 3. The buyer must submit their review first. If the seller attempts to rate before the buyer, the system raises:
   `"The buyer must rate the order delivery first before the seller can review the buyer."`
5. **Step 5 - Seller Reciprocal Rating & Order Completion**: The seller can now rate the buyer's communication and payment timeliness. When both parties have submitted feedback, the order status permanently transitions to `QuotationStatus.COMPLETED`.

### B. 4-Dimensional Feedback Criteria
Each review requires ratings across 4 distinct operational vectors on a 1 to 5 scale:

1. **Overall Rating** (`rating`): General transaction satisfaction.
2. **Communication** (`communication_rating`): Counterparty responsiveness, transparency, and clarity.
3. **Delivery Punctuality** (`delivery_rating`): Adherence to agreed lead time and shipping schedule.
4. **Product Quality** (`quality_rating`): Compliance with technical specs, tolerance limits, and packaging integrity.

### C. Review Impact on Trust Score & Rolling Average
When a review is submitted:
1. **Dynamic Trust Score Adjustment**:
   - **4 or 5 Stars**: **`+5` Trust Points** (Reward for exemplary service).
   - **3 Stars**: **`+1` Trust Point** (Neutral/acceptable fulfillment).
   - **1 or 2 Stars**: **`-5` Trust Points** (Penalty for substandard fulfillment or disputes).
2. **Continuous Rolling Average**:
   $$\text{Average Rating} = \text{round}\left(\frac{\sum_{i=1}^N \text{rating}_i}{N}, 2\right)$$
   Stored as `Numeric(3, 2)` on `UserProfile.average_rating`, along with `UserProfile.total_reviews`.

---

## 5. How Reputation Impacts Marketplace & Matchmaking

Reputation is not merely cosmetic; it directly controls access, ranking, and matchmaking across the platform:

### A. Marketplace Search & Filtering (`/marketplace`)
- **Verified Supplier Badge**: Users with `kyc_status == VERIFIED` or `trust_score >= 60` display a green verified badge with live trust score.
- **`verified_only` Filter**: Buyers can filter search results to exclude any supplier without active KYC or with a trust score under 60.
- **Sort by Trust (`trust_desc`)**: Orders results by highest `trust_score` first, ensuring top-tier suppliers capture primary buyer impressions.

### B. Matchmaking Ranking Engine (`match_service.py`)
In the AI semantic & hybrid match scoring pipeline, counterparty review reputation directly influences final match score:

$$\text{Reputation Adjustment} = (\text{Supplier Average Rating} - 3.5) \times 0.04$$

- A 5.0-star supplier receives a **`+6%`** rank boost.
- A 2.0-star supplier receives a **`-6%`** rank penalty.

### C. Deal Room AI Verification Agent
The conversational `verification` agent in the Deal Room uses this data to generate real-time risk indicators:
- 🟢 **Low Risk**: KYC Verified, 4.0+ rating, active certificates, >1 year established.
- 🟡 **Medium Risk**: Pending KYC, <3 reviews, or missing certifications.
- 🔴 **High Risk**: Unverified KYC, new account (<30 days), or multiple low ratings.

---

## 6. Summary of Key Database Fields

| Model | Field | Type | Description |
| :--- | :--- | :--- | :--- |
| `UserProfile` | `trust_score` | `Integer` | Dynamic 0–100 reputation score. |
| `UserProfile` | `kyc_status` | `Enum (KYCStatus)` | `UNVERIFIED`, `PENDING`, `VERIFIED`, `REJECTED`. |
| `UserProfile` | `gst_number` | `String` | Validated tax ID (unique per tenant). |
| `UserProfile` | `average_rating`| `Decimal(3, 2)` | Continuous rolling star average (1.00 to 5.00). |
| `UserProfile` | `total_reviews` | `Integer` | Count of verified completed reviews. |
| `Certificate` | `verification_status` | `Enum` | `PENDING`, `VERIFIED`, `EXPIRED`, `REJECTED`. |
| `Quotation`   | `status` | `Enum` | `DRAFT`, `SENT`, `ACCEPTED`, `DISPATCHED`, `DELIVERED`, `RECEIVED`, `COMPLETED`. |
| `Review`      | `rating`, `communication_rating`, `delivery_rating`, `quality_rating` | `Integer` | 1–5 score per dimension. |
