#!/usr/bin/env python3
"""One-off: fill `posted` on queue.json entries for companies whose own site publishes no posting date
(Oppenheimer, D. E. Shaw), using LinkedIn's public guest job search. Per entry it searches "<title> <company>",
keeps only cards whose company and normalized title match exactly, and writes a date only when every matching
card agrees on one location-compatible date. The date is LinkedIn's, so posted_source is "approx" (shown with ~).
Run locally, slowly: LinkedIn 429s fast and blocks shared runner IPs, so this is never part of the hourly job.

Run once from the repo root: py -3 backfill_linkedin_posted.py [--dry-run]
"""
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import atomic_json
from job_scout import HOME

COMPANIES = {"Oppenheimer": "oppenheimer", "D. E. Shaw": "shaw"}  # queue company -> substring of the LinkedIn company name
PAUSE_S = 4
BACKOFF_S = 60
MAX_429_STREAK = 3
UA = {"User-Agent": "Mozilla/5.0"}


def norm(s):
    return re.sub(r"[^a-z0-9]", "", html.unescape(s or "").lower())


def search(keywords):
    url = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords="
           + urllib.parse.quote(keywords) + "&location=" + urllib.parse.quote("United States") + "&start=0")
    html_text = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30).read().decode("utf-8", "ignore")
    cards = []
    for c in html_text.split("<li>")[1:]:
        ti = re.search(r'base-search-card__title">\s*([^<]+?)\s*<', c)
        co = re.search(r'base-search-card__subtitle">.*?>\s*([^<]+?)\s*<', c, re.S)
        dt = re.search(r'datetime="([^"]+)"', c)
        lo = re.search(r'job-search-card__location">\s*([^<]+?)\s*<', c)
        if ti and co and dt:
            cards.append(dict(title=html.unescape(ti.group(1)), company=html.unescape(co.group(1)), date=dt.group(1),
                              location=html.unescape(lo.group(1)) if lo else ""))
    return cards


def location_ok(entry_loc, card_loc):
    a, b = norm(entry_loc), norm(card_loc)
    return not a or not b or a in b or b in a or norm(entry_loc.split(",")[0]) in b


def main():
    dry = "--dry-run" in sys.argv
    path = HOME / "queue.json"
    queue = json.loads(path.read_text(encoding="utf-8"))
    todo = [e for e in queue if e.get("company") in COMPANIES and not e.get("posted") and not e.get("closed_on")]
    print(f"{len(todo)} entries to try")
    fixed = skipped = streak = 0
    for e in todo:
        want = norm(e["role"])
        try:
            cards = search(f'{e["role"]} {e["company"]}')
            streak = 0
        except urllib.error.HTTPError as err:
            if err.code == 429:
                streak += 1
                print(f"  429 on {e['id']} (streak {streak})")
                if streak >= MAX_429_STREAK:
                    print("stopping: LinkedIn is throttling; rerun later")
                    break
                time.sleep(BACKOFF_S)
                continue
            raise
        hits = [c for c in cards if COMPANIES[e["company"]] in c["company"].lower() and norm(c["title"]) == want
                and location_ok(e.get("location", ""), c["location"])]
        dates = {c["date"] for c in hits}
        if len(dates) == 1:
            if not dry:
                e["posted"], e["posted_source"] = dates.pop(), "approx"
            else:
                print(f"  {e['role'][:50]} -> {next(iter(dates))}")
            fixed += 1
        else:
            skipped += 1
        time.sleep(PAUSE_S)
    if not dry and fixed:
        atomic_json.write(str(path), queue)
    print(f"filled {fixed}, left on first_seen {skipped}, untried {len(todo) - fixed - skipped}")


if __name__ == "__main__":
    main()
