#!/usr/bin/env python3
"""
cli.py — end-to-end: screenshot(s) -> new row in the master workbook.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python cli.py --master ../template/fitness_master.xlsx img1.png img2.png

    # See what would be extracted without touching the file:
    python cli.py --dry-run --master ../template/fitness_master.xlsx img1.png img2.png

    # Feed a ready JSON instead of calling the model (for testing / re-runs):
    python cli.py --from-json row.json --master ../template/fitness_master.xlsx
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from schema import normalise, ExtractionError
from append_row import append_record, is_duplicate


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Append an Apple Fitness run to the master workbook.")
    ap.add_argument("images", nargs="*", help="screenshot image files for ONE workout")
    ap.add_argument("--master", required=True, help="path to the master .xlsx")
    ap.add_argument("--from-json", help="skip the model; read the raw JSON from this file")
    ap.add_argument("--year", type=int, default=None,
                    help="year to use when the screenshot shows none (default: current year)")
    ap.add_argument("--dry-run", action="store_true", help="print the row, do not save")
    ap.add_argument("--allow-duplicate", action="store_true",
                    help="append even if a row with the same date+time exists")
    args = ap.parse_args(argv)

    # 1. Get the raw JSON — either from a file or from the vision model.
    if args.from_json:
        raw = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
    else:
        if not args.images:
            ap.error("provide screenshot image files, or use --from-json")
        from extract import extract_from_images  # imported lazily (needs requests + key)
        raw = extract_from_images(args.images)

    print("Raw extraction:", json.dumps(raw, ensure_ascii=False))

    # 2. Normalise into a spreadsheet-ready row.
    try:
        record = normalise(raw, default_year=args.year)
    except ExtractionError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print("Normalised row:", record.as_row())

    # 3. Guard against re-adding the same workout twice.
    if not args.allow_duplicate and not args.dry_run and is_duplicate(args.master, record):
        print(f"SKIP: a row for {record.date} {record.time_range} already exists.")
        return 0

    # 4. Append.
    row = append_record(args.master, record, dry_run=args.dry_run)
    verb = "Would write" if args.dry_run else "Wrote"
    print(f"{verb} row {row} to {args.master}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
