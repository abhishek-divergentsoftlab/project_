# B2B Marketplace

An AI-assisted buyer/seller marketplace. Users post RFQs (requests for quotation);
the platform matches each RFQ against the opposite side of the market.

**The central rule:** a buyer RFQ is only ever matched against seller RFQs, and a
seller RFQ only against buyer RFQs. Matching direction is decided per RFQ, never
by the account, because a trader is frequently both.

Two user journeys share one matching engine:

| Journey | Creates an RFQ? | Flow |
| --- | --- | --- |
| **Onboarding** | Yes | AI asks for what is missing → structured RFQ → saved → matched |
| **Direct search** | No (optional) | Natural-language query → requirements extracted → matched |

---

## Status

Phase 1 (foundation) is complete and verified end to end, and matching now
works on top of it. Later phases are planned but not built.

| Phase | Scope | State |
| --- | --- | --- |
| 1 | PostgreSQL schema, auth, RFQ CRUD, React shell, Docker | **Done** |
| 2 | Streaming AI gateway, SSE event protocol, conversation persistence | Not started |
| 3 | Onboarding graph, structured extraction | Not started |
| 4 | Qdrant, embedding pipeline, `search_marketplace` | **Done** |
| 5 | Direct search, clarification, search sessions | **Done** |
| 6 | Business-rule reranking, match explanations | **Done** |
| 7 | Email and notification actions | Not started |
| 8 | Observability, rate limits, background jobs, hardening | Not started |

Both user journeys work end to end today:

- **My RFQs** → publish an RFQ, press **Find sellers** (or **Find buyers**) and
  you get ranked counterparties with a per-dimension explanation of why each one
  ranked where it did.
- **Search** → describe what you want in a sentence, get a ranked table back,
  and refine it conversationally. This creates no RFQ.

Retrieval is real vector search: RFQs are embedded with `nomic-embed-text` and
retrieved from Qdrant by cosine similarity, with the marketplace's hard filters
applied inside the query. See [Matching](#matching) below.

The database already carries the tables later phases need (`conversations`,
`messages`, `message_events`, `match_searches`, `match_results`), so adding the
agent does not require a schema rewrite.

---

## Running it

Requires Docker, Python 3.13+ and Node 20+.

### 1. Backing services

```bash
docker compose up -d
```

Starts PostgreSQL, Qdrant and Redis. Qdrant and Redis are unused until phases 4
and 8 — they run now so the compose file does not need revisiting later.

> PostgreSQL is published on **5433**, not 5432, to avoid colliding with a
> system PostgreSQL. Change the mapping in `docker-compose.yml` and
> `DATABASE_URL` together if you want a different port.

### 2. Backend

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # then set SECRET_KEY
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app:app --reload --port 8011
# or: .venv/bin/python app.py
```

> Port **8011**, not the usual 8000: this machine already runs another service
> there. If yours does not, use 8000 and set `VITE_PROXY_TARGET` in
> `frontend/.env` to match.

> **Use the `.venv/bin/` prefix.** A bare `uvicorn`, `python` or `alembic` picks
> up whichever interpreter is first on `PATH` — on a machine with conda
> auto-activating its base environment, that is miniconda, which does not have
> this project's dependencies and fails with
> `ModuleNotFoundError: No module named 'argon2'`. Either keep the prefix, or
> run `source .venv/bin/activate` once per shell.

Generate a secret with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(64))"
```

API on <http://127.0.0.1:8011>, interactive docs at `/docs`, health at
`/api/v1/health` (which performs a real database round trip, so it reports
`degraded` when PostgreSQL is unreachable).

### 3. Demo data

```bash
.venv/bin/python scripts/seed_data.py --reset          # 10,000 listings
.venv/bin/python scripts/index_vectors.py --all        # ~3 min to embed
```

Creates **10,000 listings** from 280 trading companies across 62 cities in 24
countries, priced in each city's own currency. Products come from
`scripts/catalogue.py`, which carries real specifications — cell chemistry, PD
wattage and port layout for a power bank; burst strength, flute and GSM for a
corrugated box; grain length and ageing for rice — because the attribute
dimension can only score what the data actually states.

Dependent values are kept coherent: a 5,000 mAh power bank does not weigh 560 g
or advertise 100 W, and a bearing's outer diameter follows its bore.

**Twenty credentialed logins**, all with the password `trade2026demo`:

```
buyer1@marketplace.dev  …  buyer10@marketplace.dev
seller1@marketplace.dev …  seller10@marketplace.dev
```

Each owns 25 listings, so their dashboards stay readable; the market's depth
comes from the background companies. The original `demo@marketplace.dev`
(password `demo password 123`) still exists and is registered as `both`.

Then build the vector index:

```bash
.venv/bin/python scripts/index_vectors.py --all
```

The demo account is registered as `both`, and comes with one buyer RFQ and one
seller RFQ, so you can watch matching run in both directions.

`--reset` only removes accounts on `seed.marketplace.dev` plus the demo user, so
it will not touch accounts you created yourself.

**Re-run `scripts/index_vectors.py --all` after any reseed.** Seeding writes
straight to PostgreSQL and does not touch Qdrant, so without it the index still
points at the previous corpus: searches return ids that no longer exist and the
results look stale for no visible reason. Running `index_vectors.py` with no
arguments is the cheap version — it indexes whatever is pending and prunes
points with no RFQ behind them.

### 4. Frontend

```bash
cd frontend
npm install
npm run dev
```

On <http://localhost:5175>. Calls to `/api` are proxied to the backend by Vite
(target set by `VITE_PROXY_TARGET`),
so development is same-origin and CORS never enters the picture. If Vite reports
the port is in use it will move to the next available port (e.g. 5176), which can also be added to the backend's default
CORS list.

### 5. Tests

```bash
cd backend
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest          # 186 tests, ~50s
```

The suite builds its own `marketplace_test` database, runs the Alembic
migrations into it and drops it again, so it never reads or writes the seeded
development data. It also refuses to talk to Qdrant: creating an RFQ indexes it
inline, and without that guard every run would write throwaway rows into the
live `marketplace_rfq` collection. The vector path being stubbed out means the
suite exercises the PostgreSQL fallback end to end, which is the path that has
to work when the index is down.

Requires the Docker services from step 1 to be up. Nothing else — no Ollama, no
Qdrant, no network.

The frontend has no test runner; its checks are the compiler and the linter:

```bash
cd frontend
npm run typecheck && npm run lint && npm run build
```

### 6. Smoke test a deployment

`pytest` proves the code is right against a throwaway database with the vector
index stubbed out. That is the wrong question to ask of a deployment, so there
is a second check that asks the right one — it drives a **running** instance
over HTTP, with whatever Qdrant, Ollama and data that instance actually has:

```bash
cd backend
.venv/bin/python scripts/smoke_test.py
.venv/bin/python scripts/smoke_test.py --base-url https://api.example.com/api/v1
```

55 checks across health, auth, profiles, listings, matching, connections,
messaging and direct search. It signs up two throwaway accounts on
`@smoke.example.com`, closes the listings it made, and exits non-zero on the
first thing a user would notice. Safe to run against a populated environment.

It is also the fastest way to tell an empty index from a broken one: "matching
returns candidates" failing while everything else passes means
`scripts/index_vectors.py --all` has not been run.

---

## Layout

```
backend/
  app.py              FastAPI entry point
  core/               settings, password hashing, JWT
  db/                 declarative base, async session
  models/             SQLAlchemy tables
  schemas/            Pydantic request/response contracts
  services/           business logic (auth, RFQ, search-text generation)
  api/v1/             routers
  alembic/            migrations
frontend/
  src/api/            axios client with refresh interception
  src/context/        auth state
  src/pages/          login, signup, profile, RFQ list, RFQ create
```

---

## Design decisions worth knowing

### PostgreSQL is the source of truth

Qdrant is an index, never a database. Every RFQ carries an `embedding_status`
(`pending` → `indexed`, or `stale` / `failed`), so a failed index write is
retryable and the entire Qdrant collection can be rebuilt from PostgreSQL alone.
Any edit to an RFQ sets the status back to `stale`.

### Prices are compared across currencies

A corpus spanning 21 currencies would otherwise score almost no prices at all,
since a mismatch used to yield `null`. `services/currency.py` converts through a
**static, illustrative** rate table. It is explicitly not a rates feed: an
unknown currency still returns `null` rather than being assumed to be the base,
because silently treating VND as USD is wrong by four orders of magnitude.

### Specifications are extracted from the query too

Detailed listings are only useful if a search can name the same details, so the
parser reads measurements and codes: `20000 mah`, `65w`, `3mm`, `180 gsm`,
`4 ports`, `SS 316L`, `PD 3.0`, `FSC`. Each maps onto the attribute name the
catalogue uses, so asking for 316L at 3 mm ranks an exact match above a nearer
supplier of SS 304.

Numeric attributes are scored by distance — 20,000 mAh against 27,000 is a near
miss (0.74), against 5,000 is not (0.25). Categorical ones are matched exactly
or heavily discounted, because partial token overlap on a value like "SS 316L"
against "SS 304" only reflects the shared prefix.

### The index is allowed to be wrong

Qdrant can lag PostgreSQL — a deleted account cascades its RFQs away in the
database while their vectors remain, for instance. Retrieval therefore treats
Qdrant's answer as a *suggestion*: it supplies ids and ranking, and the rows are
then read from PostgreSQL with the hard filters re-applied, so a stale point
cannot leak into anyone's results. Running `scripts/index_vectors.py --all`
against a dropped collection restores exact parity (verified: 79 stale points
became 74, matching the 74 rows in the database).

### Three representations of every RFQ

Product attributes differ wildly by industry — a cable has amperage, a dining
table has dimensions, apples have a variety. So each RFQ is stored three ways:

1. **Typed columns** — quantity, price, location, deadline. Indexable, and what
   business-rule matching compares.
2. **`product_details` JSONB** — whatever this product needs, with no migration
   per product type. GIN-indexed.
3. **`search_text` + `search_tags`** — a canonical rendering generated from the
   two above, and the text the embedding will be built from.

Nothing embeds raw JSON. `services/rfq_indexing.py` renders the payload into a
stable block so that two RFQs describing the same thing produce similar text
regardless of which keys the extractor emitted. The rendering is deterministic:
identical input always produces byte-identical output, so re-indexing does not
churn embeddings.

Attribute values may be plain (`"5-ply"`) or normalised
(`{"value": 2.4, "unit": "A", "raw": "2.4amp"}`). The normalised form is what
numeric matching will read in phase 6; both render correctly today.

### Matching

Two stages, because vector similarity alone is not a match:

```
stage 1   hard filters + relevance retrieval   ->  candidate pool (200)
stage 2   business compatibility + reranking   ->  the top K shown
```

**Stage 1** is a Qdrant vector search. The filters that are not negotiable —
opposite side of the market, status active, not expired, not your own — travel
with the query as payload conditions, so the central marketplace rule is
enforced *inside* the search rather than after it. Ids and scores come back from
Qdrant; the rows themselves are always read from PostgreSQL, and the hard
filters are re-applied on the way out, because the index can lag the database.

If Qdrant or the embedding model is unreachable, stage 1 falls back to a
PostgreSQL query that orders by tag overlap. Both failure paths are exercised:
with Qdrant down the search still returns candidates, and with the embedding
model down as well it still returns candidates.

### Location is scored by distance, and the distance is shown

Proximity decays exponentially past a 50km metro band, with a floor for sharing
a country:

| Apart | Score |
| --- | --- |
| same city | 1.00 |
| 140km (Mumbai–Nashik) | 0.82 |
| 444km (Jaipur–Ludhiana) | 0.42 |
| 1,148km (Mumbai–Delhi) | 0.30 (domestic floor) |
| cross-border | 0.10 |

The decay used to be linear to 1,500km, which scored Jaipur to Ludhiana at 0.73
— two days by road reading as "nearby". The floor exists because a domestic
supplier 1,200km away is still domestic: no customs, one currency, one set of
transport rules.

`distance_km` is returned on every candidate and shown on the card, because a
bare "42%" cannot be checked and "444 km away" can.

Two things are deliberately kept apart here: a listing's location and its
owner's registered address. The score, the distance and the delivery all refer
to the **listing**. The card used to print the company's profile city beside the
score, so a trader registered in São Paulo with stock in Izmir read as "São
Paulo" next to a perfect location match against an Izmir buyer.

A city typed without coordinates is geocoded on write, for RFQs and profiles
alike, so hand-made listings can be ranked by real distance rather than by
string equality on the city name. A city that contradicts a stated country —
"Hamburg, India" — is left ungeocoded rather than being moved to Germany.

### Contact details are the thing a connection buys

A match card shows who and where, never how to reach them. Email, phone, contact
name and address appear only once a connection request between those two
accounts has been **accepted**, and the unlock is per viewer: somebody else's
accepted request against the same listing reveals nothing to you. Without that
rule the match page is a scrapeable lead list and the request step is theatre.

The card reports its own standing — no request, sent, accepted, declined — so
the button cannot be pressed twice and a declined request cannot simply be sent
again.

### Choosing the embedding model

Measured rather than assumed, against a cable listing:

| Model | Sibling cable | GaN charger | Corrugated box | Time |
| --- | --- | --- | --- | --- |
| `nomic-embed-text` (768d) | 0.986 | 0.577 | 0.418 | 1.5s / 5 texts |
| `qwen3-embedding:8b` (4096d) | 0.961 | **0.767** | 0.513 | 14.4s / 5 texts |

The larger model was ten times slower and put a charger at 0.767 against a
cable — too close to separate them. `nomic-embed-text` leaves a wide gap to cut
in.

**What gets embedded is the product-only projection**, not `search_text`. The
full listing repeats Role, Quantity, Target price and Location on every row, and
embedding that shared boilerplate pulls every product towards every other: with
it, a GaN charger scored 0.82 against a cable query and the same rows came back
for every search. Embedding `build_match_text` instead widened the gap between
the right product (0.74–0.84) and a different product in the same category
(0.55–0.67), which is what `RELEVANCE_FLOOR = 0.70` cuts between.

**Stage 2** (`services/match_scoring.py`) scores seven dimensions — relevance,
attributes, category, price, quantity, location, deadline — and blends them by
weight. Every function there is pure, so the rules can be tested without a
database.

**Attributes the requester actually named get their own weight.** Leaving colour
to the fuzzy text score meant that asking for black returned red and white
listings above it, because quantity and distance outvoted the one thing the user
had been explicit about. A candidate that declares the attribute and differs
scores zero for it; one that never mentions it scores a half, since silence is
not a contradiction but is not a confirmation either.

Three decisions worth knowing:

- **A dimension that cannot be evaluated is `null`, not zero.** Mismatched
  currencies, incomparable units (500 kg against 500 cartons), a missing price —
  all yield `null`, and the blend renormalises over what remains. An RFQ is
  never punished for a field its counterparty left blank.
- **Price and quantity are only scored between comparable products.** Otherwise
  a ₹30 corrugated box scores full marks against a ₹200 cable budget simply for
  being cheaper, and unrelated products flood the results.
- **Scores are returned per dimension**, so the UI explains a match rather than
  asserting it. Ranking is also directional: whoever is buying supplies the
  target price and the requirement, whoever is selling supplies the ask and the
  stock.

The `relevance` dimension is cosine similarity from the vector index, falling
back to term overlap when the index is unavailable. The two sit on different
scales, so each carries its own floor.

Text is compared by **token**, never by whole string, because categories and
product names are typed by hand:

| Written as | Also written as | Compared |
| --- | --- | --- |
| `type c cable` | `usb type-c cable` | 3 shared tokens, not two unequal strings |
| `electronic` | `Electronics` | same category |
| `cables` | `cable` | same token |

Tokenising lowercases, splits on punctuation (so hyphens separate), strips a
small stopword list, and singularises. Single letters survive — the `C` in
Type-C is the entire difference between two otherwise identical cables — while
single digits are dropped as quantity noise.

Every search is recorded in `match_searches` / `match_results` with its filters,
the ids already shown and the score breakdown, so "show me 20 more" continues
rather than repeating, and any result set can be audited after the fact.

### Direct search parses, it does not prompt a model

`/search` accepts a sentence like *"i want usb type c cable of white color in
indore within 7 days at price 2 dollar per unit"* and returns a ranked table.
Each message is merged into the requirements the conversation has already
established, so *"show me black instead, deadline 9 days"* swaps two fields and
keeps the product, the city and the price.

Extraction is a parser (`services/query_extractor.py`), not a model call.
Three local models were probed first on exactly these sentences:

| Model | Result |
| --- | --- |
| `llama3.1:8b` | missed the price; read *"i can supply"* as a buy intent |
| `gpt-oss:20b` | returned no parseable JSON at all |
| `qwen3:32b` | correct intent, still missed the price, emitted `"null"` as a string, 7–21s |

Numbers, currencies, units, relative dates and known city names are exactly
what a parser gets right every time and instantly. The parser also forgives
one-character typos of filler words, because people type quickly in a chat box
and *"withing"* should not become part of the product name.

**A model handles the parts a parser should not.** With `DIRECT_SEARCH_LLM=true`:

- `services/conversation_llm.py` writes each question in natural language
  instead of reciting a fixed string, and classifies what a reply meant —
  an answer, a refusal, "just show me", or a change of product — instead of
  matching a keyword list. *"doesn't really matter, just show me what's out
  there"* is a sentence no word list handles well; the model reads it correctly
  as `show_results`.
- `services/llm_extractor.py` fills the product and category when the parser
  cannot work them out, and never overrides a value the parser found.

Everything is time-boxed at `LLM_TIMEOUT_SECONDS` (8s) and falls back to the
deterministic path, which is itself contextual rather than fixed — it reads the
product back ("How many white USB type-c cables do you need?").

It is **off by default on this machine only**, for a measured reason:
generation took 40–60s per call with the model already resident in VRAM and the
GPU at 0% utilisation, which is unusable in a chat. Embeddings on the same box
take about 300ms, which is why vector search is on by default and generation is
not. On hardware where generation is quick, set `DIRECT_SEARCH_LLM=true` and
the questions become model-written with no other change.

The conversation **gathers before it searches**. While anything is still
missing it asks for one field at a time (quantity → price → location →
deadline), reading back what it already understood, and does not call the
search tool at all. Once every field is known or skipped it searches once, and
from then on each message refines live rather than dropping the user back into
a questionnaire. `"skip"` retires the current question; `"show results"`
retires all of them and searches immediately. State lives in
`conversations.state`, and the search itself reuses the ordinary matcher by
building a **transient RFQ that is never persisted** — so a direct search never
makes the user a counterparty in anybody else's results.

When a stated price cannot be compared — a USD target against INR listings, with
no FX layer — the reply says so instead of quietly leaving the dimension
unscored.

### Deadlines are absolute

"Within 3 days" is meaningless once the row is a week old, so a deadline is
resolved to an instant on write. The original phrasing is kept in
`deadline_raw` for audit. Callers may send either `date` or `in_days`.

A calendar `date` resolves to the **end** of that day. "I need it by the 30th"
includes the 30th; combining with midnight scored a counterparty who could
deliver that afternoon as having missed it.

`deadline_at` (a delivery requirement) is deliberately separate from
`expires_at` (when the listing stops being matchable).

### Tokens carry a type claim

Access and refresh tokens are both JWTs with a `type` claim, and verification
demands the expected type — a refresh token presented to a protected route is
rejected. Passwords use argon2, and hashes are transparently upgraded on login
when parameters change.

Known gaps, deferred to phase 8: refresh tokens are not revoked on rotation
(needs a `jti` denylist in Redis), and the frontend keeps tokens in
`localStorage` rather than httpOnly cookies.

### Unknown request fields are rejected

Every request schema sets `extra="forbid"`, so a typo in a field name fails
loudly at 422 instead of being silently dropped.

### Requesting someone else's RFQ returns 404, not 403

A 403 would confirm that the id exists, which is an enumeration oracle.

---

## Migrations

```bash
.venv/bin/alembic revision --autogenerate -m "what changed"
.venv/bin/alembic upgrade head
.venv/bin/alembic check          # fails if models and schema have drifted
```

The initial migration drops its PostgreSQL enum types explicitly on downgrade.
Alembic's `drop_table` does not, and without it a downgrade followed by an
upgrade fails with `type user_role already exists`.

---

## Scripts

```bash
.venv/bin/python scripts/seed_data.py --reset    # rebuild the demo marketplace
.venv/bin/python scripts/reindex.py --dry-run    # preview derived-field changes
.venv/bin/python scripts/reindex.py              # recompute search_text/search_tags
.venv/bin/python scripts/index_vectors.py        # embed what is stale into Qdrant
.venv/bin/python scripts/index_vectors.py --all  # rebuild the whole collection
.venv/bin/python scripts/smoke_test.py           # drive a running instance over HTTP
```

`index_vectors.py` is what makes "PostgreSQL is the source of truth" real: drop
the Qdrant collection and this rebuilds it — 10,000 rows in about three minutes
at a batch size of 256 (measured at ~72 texts/sec; a batch of 32 managed only
16). It also prunes points whose RFQ has been deleted, which happens whenever an
account is removed and its listings cascade away. New and edited RFQs are indexed
inline on write so they are searchable immediately; that belongs in a background
job once there is a queue, and until then a failure is recorded on the row
(`embedding_status`) for this script to retry rather than being lost.

`reindex.py` is what you run after changing how search text or tags are
generated — existing rows are stale until re-derived. It is also the operation
that rebuilds the vector index from PostgreSQL: it re-renders each row and marks
it for re-embedding, and only writes rows that actually changed.

Generation is deterministic, so a reindex over unchanged logic is a no-op. Note
that JSONB does not preserve key insertion order, which is why dimensions render
in a canonical order (`length`, `width`, `height`, …) rather than whatever order
the keys come back in.

## Conventions

- **Never commit `.env`.** `.gitignore` covers it; commit `.env.example` instead.
  Note that `backend/.env` was committed before this was fixed, so its old
  `SECRET_KEY` is still in git history and should be treated as compromised.
- Enums are lowercase strings, native PostgreSQL enum types. The one exception
  is `message_events.type`, kept a plain string so the streaming protocol can
  gain event types without a migration.
- `Decimal` columns are serialised as plain JSON numbers, not strings —
  `Numeric(18,4)` otherwise round-trips a quantity of 8000 as `"8000.0000"`.
