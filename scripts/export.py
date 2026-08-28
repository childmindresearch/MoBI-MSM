#!/usr/bin/env python3
"""Export collected data to analysis-ready files.

    .venv/bin/python scripts/export.py                  # every session
    .venv/bin/python scripts/export.py --participant TEST001
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.store import Store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--participant", help="Limit the export to one Participant ID")
    ap.add_argument("--out", type=Path, default=config.DATA_DIR / "exports")
    args = ap.parse_args()

    store = Store()
    args.out.mkdir(parents=True, exist_ok=True)

    if args.participant:
        session_ids = [r["session_id"] for r in store.existing_sessions(args.participant)]
        if not session_ids:
            print(f"No sessions on record for {args.participant}", file=sys.stderr)
            return 1
        blocks = store.export_blocks_csv(args.out / f"{args.participant}_blocks.csv", session_ids)
        print(f"wrote {blocks}")
        for sid in session_ids:
            path = store.export_session_json(args.out / f"{args.participant}_{sid[:8]}_session.json", sid)
            print(f"wrote {path}")
    else:
        blocks = store.export_blocks_csv(args.out / "all_blocks.csv")
        print(f"wrote {blocks}")

    print(f"\nMarker logs (one JSONL per session) are in {config.MARKER_LOG_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
