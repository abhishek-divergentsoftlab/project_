"""Rewrite existing RFQs' ``product_details`` through the spec catalogue.

    .venv/bin/python scripts/normalize_rfq_specs.py             # dry run (the default): print old -> new per RFQ
    .venv/bin/python scripts/normalize_rfq_specs.py --apply     # write the changes and re-index the changed RFQs

Each RFQ's family is looked up in ``spec_catalog`` (by product name, then HSN heading); a product with no stored family
is normalized against the common attributes only. The catalogue is NEVER generated here (no model call): run the
RFQ form or the chat once for a product to create its family, then run this again.

Keys become the catalogue's keys ("Apple Variety" -> variety, "pack_weight_kg" -> pack_size) and values their canonical
form ("20000 g" -> "20 kg", certifications as a list); ``__must_match: false`` keys are dropped. Unknown keys are kept as free
specs. Idempotent: a second run changes nothing.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from db.session import SessionLocal, engine  # noqa: E402
from models.rfq import RFQ  # noqa: E402
from services import rfq_service, spec_catalog  # noqa: E402
from services.rfq_indexing import refresh_index_fields  # noqa: E402


def _show(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


async def normalize_all(db: AsyncSession, *, apply: bool, limit: Optional[int] = None, out=print) -> list[dict[str, Any]]:
    """Normalize every RFQ's product_details. Returns one record per RFQ that changed (or would change)."""
    query = select(RFQ).order_by(RFQ.created_at)
    if limit:
        query = query.limit(limit)
    changes: list[dict[str, Any]] = []
    for rfq in (await db.scalars(query)).all():
        old = rfq.product_details or {}
        if not old:
            continue
        name = old.get("name") or rfq.title
        family = await spec_catalog.find_family(db, name, rfq.category, old.get("hsn_code"))
        if family is None:
            family = spec_catalog.family_from_suggestions(name, rfq.category, hsn_code=old.get("hsn_code"))
        new = family.normalize_details(old)
        if new == old:
            continue
        changes.append({"id": str(rfq.id), "title": rfq.title, "family": family.family_key, "old": old, "new": new})
        out(f"{rfq.id}  {rfq.title!r}  [{family.family_key}{'' if family.persisted else ', common only'}]")
        out(f"    old: {_show(old)}")
        out(f"    new: {_show(new)}")
        if apply:
            rfq.product_details = new
            refresh_index_fields(rfq)
            await db.commit()
            await rfq_service.index_rfq(db, rfq)
    return changes


async def run(apply: bool, limit: Optional[int]) -> int:
    async with SessionLocal() as db:
        changes = await normalize_all(db, apply=apply, limit=limit)
    verb = "updated" if apply else "would update"
    print(f"\n{verb} {len(changes)} RFQ(s)" + ("" if apply else "  (dry run: nothing written; pass --apply)"))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument("--dry-run", action="store_true", help="print the changes only (the default)")
    parser.add_argument("--limit", type=int, default=None, help="only the first N RFQs (oldest first)")
    args = parser.parse_args()
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")

    async def go() -> int:
        try:
            return await run(args.apply, args.limit)
        finally:
            await engine.dispose()

    sys.exit(asyncio.run(go()))


if __name__ == "__main__":
    main()
