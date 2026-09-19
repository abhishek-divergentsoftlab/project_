"""Embed RFQs and push them into Qdrant.

    .venv/bin/python scripts/index_vectors.py            # only what is stale
    .venv/bin/python scripts/index_vectors.py --all      # rebuild everything

This is the operation that makes the "PostgreSQL is the source of truth" claim
real: drop the Qdrant collection and this rebuilds it. Rows are embedded from their
product-only projection in batches, and ``embedding_status`` is advanced only for rows
that actually landed, so a partial failure is retried on the next run rather
than being silently lost.
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from db.session import SessionLocal, engine  # noqa: E402
from models.enums import EmbeddingStatus  # noqa: E402
from models.rfq import RFQ  # noqa: E402
from services import qdrant_index  # noqa: E402
from services.embeddings import embed_many  # noqa: E402
from services.rfq_indexing import build_match_text  # noqa: E402

# 256 measured at ~72 texts/sec against nomic-embed-text; 32 managed only 16.
BATCH = 256


async def prune() -> int:
    """Drop points whose RFQ no longer exists.

    Deleting a user cascades their RFQs away in PostgreSQL while their vectors
    stay behind. Retrieval already re-checks every hit against the database so
    a stale point cannot reach a user, but the collection should not grow
    junk either.
    """
    indexed = await qdrant_index.all_point_ids()
    if indexed is None:
        return 0

    async with SessionLocal() as db:
        live = set((await db.scalars(select(RFQ.id))).all())

    orphans = sorted(indexed - live)
    if orphans:
        await qdrant_index.delete(orphans)
    print(f"  pruned {len(orphans)} point(s) with no RFQ behind them")
    return len(orphans)


async def run(rebuild_all: bool) -> int:
    if not await qdrant_index.ensure_collection():
        print("! Qdrant is unreachable -- nothing indexed")
        return 1

    indexed = failed = 0
    async with SessionLocal() as db:
        query = select(RFQ).order_by(RFQ.created_at)
        if not rebuild_all:
            query = query.where(RFQ.embedding_status != EmbeddingStatus.INDEXED)
        rfqs = (await db.scalars(query)).all()

        if not rfqs:
            print("  nothing to index")
            return 0

        print(f"  embedding {len(rfqs)} RFQs in batches of {BATCH}")
        for start in range(0, len(rfqs), BATCH):
            chunk = rfqs[start : start + BATCH]
            # build_match_text, not search_text: the full listing repeats
            # Role/Quantity/Price/Location on every row, and embedding that
            # shared boilerplate pulls every product towards every other.
            texts = [build_match_text(rfq) for rfq in chunk]

            vectors = await embed_many(texts)
            if vectors is None:
                for rfq in chunk:
                    rfq.embedding_status = EmbeddingStatus.FAILED
                    rfq.embedding_error = "embedding model unavailable"
                failed += len(chunk)
                continue

            points = [
                (rfq.id, vector, qdrant_index.build_payload(rfq))
                for rfq, vector in zip(chunk, vectors)
            ]
            if await qdrant_index.upsert(points):
                now = datetime.now(UTC)
                for rfq in chunk:
                    rfq.embedding_status = EmbeddingStatus.INDEXED
                    rfq.embedded_at = now
                    rfq.embedding_error = None
                indexed += len(chunk)
            else:
                for rfq in chunk:
                    rfq.embedding_status = EmbeddingStatus.FAILED
                    rfq.embedding_error = "qdrant upsert failed"
                failed += len(chunk)

            print(f"    {min(start + BATCH, len(rfqs))}/{len(rfqs)}")

        await db.commit()

    total = await qdrant_index.count()
    print(f"  indexed {indexed}, failed {failed}; collection now holds {total} points")
    await engine.dispose()
    return 0 if failed == 0 else 1


async def main() -> None:
    parser = argparse.ArgumentParser(description="Embed RFQs into Qdrant.")
    parser.add_argument("--all", action="store_true", help="re-embed every RFQ")
    parser.add_argument("--prune", action="store_true",
                        help="only remove points whose RFQ is gone")
    args = parser.parse_args()

    if args.prune:
        await prune()
        await engine.dispose()
        raise SystemExit(0)

    code = await run(args.all)
    await prune()
    raise SystemExit(code)


if __name__ == "__main__":
    asyncio.run(main())
