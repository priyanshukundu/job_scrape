#!/usr/bin/env python3
"""dedup_applied.py - local-only view of this repo's data/queue.json with anything you've
already applied to (per your own application tracker) filtered out. Never touches job-scrap's
own repo data - reads queue.json read-only and writes its own HTML output, which you should
keep out of version control if your tracker contains anything private (add it to your own
.gitignore).

Your tracker can be a .csv or .xlsx with one row per application. Point CONFIG below at it and
tell it which columns hold what - the column names don't need to match these defaults, just
edit the constants. If your tracker has no per-row Link column, leave LINK_COL = None and
matching falls back to Company + Role Title + Location for every row.

Run: python dedup_applied.py
"""
import csv
import sys
from datetime import datetime, date
from pathlib import Path

from identity_lib import IdentityIndex
import job_scout

# --------------------------------------------------------------------------- CONFIG
# Point these at your own tracker. None of this is specific to job-scrap's own data -
# it's entirely about the shape of the applications file you already keep.

APPLICATIONS_FILE = "Applications.xlsx"   # your tracker: .csv or .xlsx, relative to this script
COMPANY_COL = "Company"
ROLE_COL = "Role Title"
LOCATION_COL = "Location"
LINK_COL = "Link"                         # set to None if your tracker has no link column
STATUS_COL = "Status"                     # set to None to treat every row as "already applied"
APPLIED_STATUSES = {"Applied", "Screening", "Interview", "Offer", "Rejected"}
                                           # rows with any other status are NOT treated as
                                           # applied (they'll still show up in the deduped
                                           # queue) - ignored entirely if STATUS_COL is None

OUTPUT_HTML = "job_scrap_dedup.html"
# ---------------------------------------------------------------------------------


def _read_csv_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _read_xlsx_rows(path):
    try:
        import openpyxl
    except ImportError:
        sys.exit("Reading .xlsx requires openpyxl: pip install openpyxl (or export your "
                 "tracker as .csv instead, which needs no extra dependency).")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    headers = list(next(rows_iter))
    rows = [dict(zip(headers, row)) for row in rows_iter]
    wb.close()
    return rows


def load_applied_index():
    path = Path(APPLICATIONS_FILE)
    if not path.exists():
        sys.exit(f"Applications file not found: {path.resolve()}\n"
                  f"Edit APPLICATIONS_FILE at the top of dedup_applied.py to point at your own tracker.")
    rows = _read_csv_rows(path) if path.suffix.lower() == ".csv" else _read_xlsx_rows(path)

    idx = IdentityIndex()
    n = 0
    for i, row in enumerate(rows):
        if STATUS_COL and row.get(STATUS_COL) not in APPLIED_STATUSES:
            continue
        idx.add(row.get(COMPANY_COL), row.get(ROLE_COL), row.get(LOCATION_COL),
                 row.get(LINK_COL) if LINK_COL else None, key=i)
        n += 1
    return idx, n


def sort_key(e):
    posted = e.get("posted") or ""
    try:
        ordinal = date.fromisoformat(posted).toordinal()
    except ValueError:
        try:
            ordinal = date.fromisoformat(e.get("first_seen") or "").toordinal()
        except ValueError:
            ordinal = 0
    return (-ordinal, -len(e["kw_hits"]), e["company"])


def main():
    applied_idx, applied_count = load_applied_index()
    print(f"{applied_count} applied rows loaded from {APPLICATIONS_FILE}")

    queue = job_scout.load_json(job_scout.HOME / "queue.json", [])
    kept, dropped = [], 0
    for e in queue:
        match = applied_idx.lookup(e["company"], e["role"], e["location"], e["link"])
        if match is not None:
            dropped += 1
            continue
        kept.append(e)
    print(f"{len(queue)} queue entries, {dropped} already applied to, {len(kept)} kept")

    kept.sort(key=sort_key)
    job_scout.write_queue_html(kept, OUTPUT_HTML, datetime.now())
    print(f"wrote {Path(OUTPUT_HTML).resolve()}")


if __name__ == "__main__":
    main()
