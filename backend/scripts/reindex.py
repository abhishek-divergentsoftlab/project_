"""Recompute the derived search fields for every RFQ.

    .venv/bin/python scripts/reindex.py [--dry-run]

``search_text`` and ``search_tags`` are generated from the typed columns and the
JSONB payload, so whenever that generation logic changes, existing rows are
stale until they are re-derived. This is the command that does it, and it is the
same operation that rebuilds the vector index from PostgreSQL: it re-renders the
text and marks each row for re-embedding.

Rows are only written when something actually changed, so re-running it is cheap
and does not needlessly queue work for the embedding pipeline.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from db.session import SessionLocal, engine  # noqa: E402
from models.rfq import RFQ  # noqa: E402
from services.rfq_indexing import build_search_tags, build_search_text  # noqa: E402


async def reindex(dry_run: bool) -> tuple[int, int]:
    changed = 0
    async with SessionLocal() as db:
        rfqs = (await db.scalars(select(RFQ))).all()

        for rfq in rfqs:
            text, tags = build_search_text(rfq), build_search_tags(rfq)
            if text == rfq.search_text and tags == list(rfq.search_tags or []):
                continue

            changed += 1
            print(f"  {rfq.id}  {rfq.title[:52]}")
            removed = set(rfq.search_tags or []) - set(tags)
            added = set(tags) - set(rfq.search_tags or [])
            if removed:
                print(f"      - {', '.join(sorted(removed))}")
            if added:
                print(f"      + {', '.join(sorted(added))}")

            if not dry_run:
                rfq.search_text = text
                rfq.search_tags = tags
                # Whatever is in Qdrant for this row no longer reflects it.
                rfq.mark_for_reindex()

        if not dry_run and changed:
            await db.commit()

    return changed, len(rfqs)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Recompute RFQ search fields.")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args()

    changed, total = await reindex(args.dry_run)
    verb = "would update" if args.dry_run else "updated"
    print(f"{verb} {changed} of {total} RFQs")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
