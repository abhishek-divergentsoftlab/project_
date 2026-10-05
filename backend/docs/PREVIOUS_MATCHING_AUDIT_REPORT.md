# Preserved Audit Report: Universal B2B Counterparty Matching Engine

> This file preserves the previous architectural audit, test suite breakdown, and weak-area analysis generated on September 29, 2026.

See the complete artifact at:
[file:///home/divergent/.gemini/antigravity-ide/brain/d3712c74-be51-4493-ab4c-e3eeaff8d90a/B2B_MATCHING_EVALUATION_REPORT.md](file:///home/divergent/.gemini/antigravity-ide/brain/d3712c74-be51-4493-ab4c-e3eeaff8d90a/B2B_MATCHING_EVALUATION_REPORT.md)

### Key Discovered Weaknesses Preserved:
1. **Homonym Shadowing in Synonym Lookup**: Flat lookup dictionary allowed "flat" (shape) to overwrite "flat" (finish), scoring 0.0 between "matte" and "flat".
2. **Agricultural Commodity vs Mass Unit Stopword**: "Gram" was stripped by `_STOPWORDS`, breaking search and gating for pulses (Bengal Gram, Green Gram).
3. **Timezone Awareness Mismatch Crash**: Comparing naive and UTC-aware datetimes in `deadline_score` caused unhandled `TypeError` exceptions.
4. **Ghost Listing Renormalization Exploit**: Omitting all commercial attributes awarded listings an effective 1.0 commercial utility.
5. **MOQ & Wholesale Oversupply Blindspot**: Huge wholesale inventories matching retail orders with a 1.0 score.
6. **Negation Blindness**: Compounds like "non-leather", "gluten-free" tokenized into "leather", "gluten".
7. **Cross-Border Administrative Floor Clamping**: International distance decay floored at 0.10 past 1,085 km.
8. **Null Island (0,0) Coordinate Trap**: Geocoding failures defaulting to (0,0) received a 1.0 "Same Metro" score.
9. **Packaging Unit Incomparability**: Box vs piece without pack count silently dropped the price dimension.
10. **Stage 1 Candidate Pool Starvation**: Top 200 cutoff based solely on text rank excluded high-discount local suppliers.
