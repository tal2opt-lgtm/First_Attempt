#!/usr/bin/env python3
"""
csv_to_xlsx.py — merge the CSV log that the iPhone Shortcut appends to into the
branded master workbook (restoring the "ריצות" formatting and keeping the
"תזונה" sheet with its formulas).

The Shortcut writes lines like:
    4.8.26,05:17-06:17,1:00:04,8.65,160,6:56,X,151,161,25
to a file in iCloud Drive (no header, one line per run). This tool reads that
file and appends any run that is not already in the workbook.

Usage:
    python csv_to_xlsx.py --csv Runs.csv --master ../template/fitness_master.xlsx
    python csv_to_xlsx.py --csv Runs.csv --master fitness_master.xlsx --out updated.xlsx
"""

from __future__ import annotations

import argparse
import csv
import shutil
from pathlib import Path

from schema import CSV_FIELDS, normalise, ExtractionError
from append_row import append_record, is_duplicate


def load_rows(csv_path: Path) -> list[dict]:
    rows: list[dict] = []
    text = csv_path.read_text(encoding="utf-8-sig").strip()
    if not text:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = next(csv.reader([line]))
        if len(parts) < len(CSV_FIELDS):
            print(f"  ! skipping malformed line ({len(parts)} fields): {line}")
            continue
        # First line may be a header the user pasted in — skip it.
        if parts[0].strip().lower() in ("date", "date "):
            continue
        rows.append(dict(zip(CSV_FIELDS, parts)))
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Merge the Shortcut CSV log into the branded workbook.")
    ap.add_argument("--csv", required=True, help="the CSV log file the Shortcut appends to")
    ap.add_argument("--master", required=True, help="branded master .xlsx (template)")
    ap.add_argument("--out", help="write here instead of editing --master in place")
    ap.add_argument("--year", type=int, default=None, help="fallback year for undated rows")
    args = ap.parse_args(argv)

    master = Path(args.master)
    target = Path(args.out) if args.out else master
    if args.out:
        shutil.copy(master, target)

    rows = load_rows(Path(args.csv))
    print(f"Read {len(rows)} run line(s) from {args.csv}")

    added = skipped = 0
    for raw in rows:
        try:
            rec = normalise(raw, default_year=args.year)
        except ExtractionError as e:
            print(f"  ! bad row {raw}: {e}")
            continue
        if is_duplicate(target, rec):
            skipped += 1
            continue
        r = append_record(target, rec)
        added += 1
        print(f"  + row {r}: {rec.as_row()}")

    print(f"Done. Added {added}, skipped {skipped} (already present). -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
