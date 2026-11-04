#!/usr/bin/env python3
"""One-off: recover real dates for queue entries stamped with the pre-fix Workday "30+ Days Ago"
floor artifact - _workday_posted() used to compute today-minus-30 for that bucket and store it as
if it were exact, so any entry whose posted date sits exactly 29 or 30 days before its first_seen
is almost certainly that fake floor date, not a real one (a real 30th-day match happening on the
exact day it was first seen, every time, would be an absurd coincidence at this volume - see the
327/100-entry spikes on the two floor dates found in the queue).

Re-fetches each flagged entry's Workday detail endpoint (same path as backfill_posted.py's
Workday branch) to recover the real startDate. Whatever still can't be recovered (closed/gated
posting, 403/404) is set to "" (unknown) rather than left holding the fake floor date - unknown
is honest, a specific-looking wrong date is not.

Run once from the repo root: py -3 backfill_floor_dates.py
"""
import json
import sys
import time
from datetime import date

import atomic_json
from job_scout import HOME, describe

THROTTLE_S = 0.3


def main():
    cfg = json.loads((HOME / "config.json").read_text(encoding="utf-8"))
    queue = json.loads((HOME / "queue.json").read_text(encoding="utf-8"))
    by_company = {c["name"]: c for c in cfg["companies"]}

    flagged = []
    for e in queue:
        posted, fs = e.get("posted") or "", e.get("first_seen") or ""
        if not posted or not fs:
            continue
        try:
            offset = (date.fromisoformat(fs) - date.fromisoformat(posted)).days
        except ValueError:
            continue
        if offset in (29, 30):
            flagged.append(e)

    print(f"{len(flagged)} of {len(queue)} queue entries carry the old 30+ floor artifact.")

    recovered, nulled, skipped = 0, 0, 0
    for name in sorted({e["company"] for e in flagged}):
        entries = [e for e in flagged if e["company"] == name]
        c = by_company.get(name)
        # The 29/30-day-before-first_seen fingerprint is only reliable evidence of the fake floor
        # on Workday - Greenhouse/Oracle/etc. return exact dates natively, so the same offset there
        # is far more likely a real coincidence than the floor bug. Leave those alone rather than
        # nulling a genuine date just because it happened to land 29-30 days before we saw it.
        if not c:
            print(f"  {name}: no longer in config.json, can't confirm ATS - leaving {len(entries)} as-is")
            skipped += len(entries)
            continue
        if c.get("ats") != "workday":
            print(f"  {name}: {c['ats']}, not workday - leaving {len(entries)} as-is (its dates are exact, not floored)")
            skipped += len(entries)
            continue
        n_recovered, n_nulled = 0, 0
        for e in entries:
            job_id = e["id"].split(":", 1)[1]
            try:
                _text, posted, posted_source = describe(c, {"id": job_id, "site": e.get("site")})
            except Exception as exc:
                print(f"    {job_id}: fetch failed ({exc})", file=sys.stderr)
                posted, posted_source = "", ""
            if posted:
                e["posted"], e["posted_source"] = posted, posted_source
                n_recovered += 1
            else:
                e["posted"] = ""
                n_nulled += 1
            time.sleep(THROTTLE_S)
        recovered += n_recovered
        nulled += n_nulled
        print(f"  {name}: {n_recovered}/{len(entries)} recovered, {n_nulled} nulled to unknown")

    atomic_json.write(str(HOME / "queue.json"), queue)
    print(f"\nrecovered {recovered}, nulled to unknown {nulled}, left as-is (non-workday) {skipped}.")


if __name__ == "__main__":
    sys.exit(main())
