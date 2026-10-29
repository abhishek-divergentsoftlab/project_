#!/usr/bin/env python
"""Inspect, re-validate or purge the value canonicalizer cache without hand-editing JSON.

    .venv/bin/python scripts/canon_cache.py validate                 # dry run: what would be purged / upgraded
    .venv/bin/python scripts/canon_cache.py validate --apply         # purge failing entries, upgrade trusted legacy ones
    .venv/bin/python scripts/canon_cache.py purge --term chili --apply        # drop every entry whose canonical is "chili"
    .venv/bin/python scripts/canon_cache.py purge --contains chini --apply    # drop entries whose key contains "chini"
    .venv/bin/python scripts/canon_cache.py list --contains sugar

``--path`` selects another cache file (default: data/value_canon.json); ``--scenarios`` selects the scenario harness cache.
A purged word is simply canonicalized again, with its context, the next time a listing carrying it is saved or matched.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import value_canon  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["validate", "purge", "list"])
    ap.add_argument("--path", type=Path)
    ap.add_argument("--scenarios", action="store_true", help="use data/value_canon_scenarios.json")
    ap.add_argument("--contains")
    ap.add_argument("--term")
    ap.add_argument("--apply", action="store_true", help="write the changes (default: report only)")
    args = ap.parse_args()
    path = args.path or (DATA / "value_canon_scenarios.json" if args.scenarios else DATA / "value_canon.json")
    if not path.exists():
        print(f"{path}: no such cache")
        return 0
    if args.command == "validate":
        report = value_canon.revalidate_cache(apply=args.apply, path=path)
        for item in report["purged"]:
            print(f"PURGE  {item['key']}  ->  {item['term']}  ({item['why']})")
        for key in report["upgraded"]:
            print(f"UPGRADE {key}")
        print(f"{len(report['purged'])} purged, {len(report['upgraded'])} upgraded, {report['kept']} kept"
              f"{'' if args.apply else '  (dry run; --apply to write)'}")
    elif args.command == "purge":
        if not (args.contains or args.term):
            ap.error("purge needs --contains and/or --term")
        gone = value_canon.purge_cache(contains=args.contains, term=args.term, apply=args.apply, path=path)
        print("\n".join(gone) or "nothing matched")
        print(f"{len(gone)} entr{'y' if len(gone) == 1 else 'ies'}{' removed' if args.apply else ' would be removed (--apply)'}")
    else:
        entries = json.loads(path.read_text(encoding="utf-8")).get("entries", {})
        for key, entry in sorted(entries.items()):
            if args.contains and args.contains.lower() not in key.lower():
                continue
            print(key, "=>", json.dumps(entry, ensure_ascii=False)[:160])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
