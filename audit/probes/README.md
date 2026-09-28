# Audit probe tests (2026-09-28)

These are negative tests written during the project audit (see `../../AUDIT_REPORT.md`).
Most of them FAIL on purpose: each failing test demonstrates one defect. When you fix a
defect, its probe should start passing; then move that test into `backend/tests/`.

They live outside `backend/tests/` so they do not break the normal test suite.

Run one area against its own throwaway database:

```bash
cp audit/probes/test_zz_audit_matching.py backend/tests/
cd backend
TEST_DB_NAME=marketplace_audit_matching .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_zz_audit_matching.py
rm tests/test_zz_audit_matching.py
```

Files: `matching`, `rfq_marketplace`, `security`, `deals`, `ai`, `perf` (perf prints timings; run it with `-s`).
