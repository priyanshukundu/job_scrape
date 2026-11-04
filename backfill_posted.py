#!/usr/bin/env python3
"""One-off: fill in `posted` on queue.json entries that predate the field being added to
_process_company() (or, for Workday, that predate describe() capturing the detail endpoint's
exact startDate - see job_scout.describe()/_workday_detail_posted()).

Two recovery paths:
  - Workday entries go through describe(), which hits the per-job DETAIL endpoint. That's what
    carries the exact startDate; the bulk listing that FETCH[] uses omits postedOn entirely for
    some tenants (confirmed: Ares) and only gives a floored "30+ Days Ago" bucket for others.
    Throttled (one request at a time, brief pause) since Workday 429s on concurrency bursts -
    see the comment on describe_pool in job_scout.run().
  - Every other ATS already returns an exact date from its bulk list call (Greenhouse
    first_published, Lever createdAt, Ashby publishedAt, SmartRecruiters releasedDate, Oracle
    PostedDate), so the original re-fetch-the-listing-and-match-by-id approach still applies.

A posting no longer live (filled/pulled - detail endpoint 404s, or the id isn't in the current
bulk listing) has no way to recover its date and is left as "unknown" - a real limitation, not
a bug.

Run once from the repo root: py -3 backfill_posted.py
"""
import json
import sys
import time
from pathlib import Path

import atomic_json
from job_scout import FETCH, HOME, describe

REPO_DIR = Path(__file__).resolve().parent
WORKDAY_THROTTLE_S = 0.3


def _backfill_workday(c, entries):
    fixed = 0
    for e in entries:
        job_id = e["id"].split(":", 1)[1]
        try:
            _text, posted, posted_source = describe(c, {"id": job_id, "site": e.get("site")})
        except Exception as exc:
            print(f"    {job_id}: fetch failed ({exc})", file=sys.stderr)
            posted, posted_source = "", ""
        if posted:
            e["posted"], e["posted_source"] = posted, posted_source
            fixed += 1
        time.sleep(WORKDAY_THROTTLE_S)
    return fixed


def _backfill_from_listing(c, entries):
    live, _complete = FETCH[c["ats"]](c)
    live_by_id = {j["id"]: j.get("posted") for j in live}
    fixed = 0
    for e in entries:
        job_id = e["id"].split(":", 1)[1]
        posted = live_by_id.get(job_id)
        if posted:
            e["posted"] = posted
            fixed += 1
    return fixed


def main():
    cfg = json.loads((HOME / "config.json").read_text(encoding="utf-8"))
    queue = json.loads((HOME / "queue.json").read_text(encoding="utf-8"))
    by_company = {c["name"]: c for c in cfg["companies"]}

    missing = [e for e in queue if not e.get("posted")]
    print(f"{len(missing)} of {len(queue)} queue entries have no posted date.")

    fixed, errors = 0, 0
    for name in sorted({e["company"] for e in missing}):
        entries = [e for e in missing if e["company"] == name]
        c = by_company.get(name)
        if not c:
            print(f"  {name}: not in config.json anymore, skipping {len(entries)}")
            continue
        try:
            n = _backfill_workday(c, entries) if c["ats"] == "workday" else _backfill_from_listing(c, entries)
        except Exception as exc:
            print(f"  {name}: fetch failed ({exc}), skipping {len(entries)}")
            errors += len(entries)
            continue
        fixed += n
        print(f"  {name}: {n}/{len(entries)} recovered")

    atomic_json.write(str(HOME / "queue.json"), queue)
    still_unknown = len(missing) - fixed
    print(f"\nfixed {fixed}, still unknown (delisted or fetch error) {still_unknown} (of which {errors} were fetch errors).")


if __name__ == "__main__":
    sys.exit(main())
