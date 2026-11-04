#!/usr/bin/env python3
"""One-off backfill: re-fetches the live description for every queue.json entry still
classified sponsorship_status="Unclear" and reclassifies it against the CURRENT
classify_sponsorship() regex in job_scout.py.

Why this exists: queue.json only ever accumulates - matches()/classify_sponsorship() run
once at intake and are never re-run against what's already queued, so when BLOCK_PATTERNS
gets widened (as it was on 2026-09-27, commit 34b55b7 - "not eligible for ... sponsorship"
phrasing), every entry scraped before that commit keeps its old, now-stale classification
forever unless something goes back and re-checks it. This script is that one-time catch-up
pass; it is not meant to run on a schedule - job_scout.py's own intake-time classification
is what covers everything scraped from here on.

Only touches sponsorship_status/sponsorship_evidence, plus `state` in the one narrow case
where a flip to Blocked should move a still-undecided entry (state="new") into
"auto_blocked" so it stops appearing in the open/actionable table - it otherwise never
touches state (shortlisted/applied/dismissed/auto_blocked are left exactly as they are),
so an already-applied or already-decided role never gets silently relabeled. Companies no
longer in config.json (a target site was retired) are skipped and counted, not treated as
errors.

Added 2026-10-10: classify_sponsorship() now splits no-sponsorship wording into "OPT Only" (plain
refusal to sponsor) and "Blocked" (citizenship, clearance, permanent authorization, OPT excluded).
`--blocked` re-checks entries already marked Blocked and moves those that are really "OPT Only" to
state "opt_only" (only from state auto_blocked or new; any other state is left as it is).

Usage: py -3 reclassify_sponsorship.py [--dry-run] [--blocked | --opt-only] [--limit N]
"""
import sys
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).resolve().parent))
import job_scout
import atomic_json

HOME = job_scout.HOME
DESCRIBE_WORKERS = 12


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="Report what would change, write nothing")
    ap.add_argument("--blocked", action="store_true", help="Re-check Blocked entries instead of Unclear ones")
    ap.add_argument("--opt-only", action="store_true", help="Re-check OPT Only entries; moves those that are really Blocked to auto_blocked")
    ap.add_argument("--limit", type=int, default=None, help="Only check the first N Unclear entries (testing)")
    args = ap.parse_args()

    cfg = job_scout.load_json(HOME / "config.json", None)
    if cfg is None:
        sys.exit(f"No config at {HOME / 'config.json'}")
    company_by_name = {c["name"]: c for c in cfg["companies"]}

    queue = job_scout.load_json(HOME / "queue.json", [])
    from_status = "OPT Only" if args.opt_only else "Blocked" if args.blocked else "Unclear"
    targets = [e for e in queue if e.get("sponsorship_status") == from_status]
    if args.limit:
        targets = targets[:args.limit]
    print(f"{len(targets)} entries currently {from_status}, out of {len(queue)} total")

    no_company, fetch_errors, flipped, unchanged = 0, 0, 0, 0

    def _reclassify(entry):
        name = entry["company"]
        c = company_by_name.get(name)
        if c is None:
            return entry, None, "no_company"
        prefix = f"{name}:"
        if not entry["id"].startswith(prefix):
            return entry, None, "no_company"  # eid format changed/unexpected - skip, don't guess
        job_id = entry["id"][len(prefix):]
        job = {"id": job_id, "url": entry.get("link", ""), "site": entry.get("site")}
        try:
            text, _posted, _src = job_scout.describe(c, job)
        except Exception as e:
            return entry, None, f"error: {e}"
        spons, evidence = job_scout.classify_sponsorship(text)
        return entry, (spons, evidence), None

    with ThreadPoolExecutor(max_workers=DESCRIBE_WORKERS) as pool:
        futures = [pool.submit(_reclassify, e) for e in targets]
        for i, fut in enumerate(as_completed(futures), 1):
            entry, result, err = fut.result()
            if err == "no_company":
                no_company += 1
                continue
            if err is not None:
                fetch_errors += 1
                print(f"! {entry['company']} - {entry['role']}: {err}", file=sys.stderr)
                continue
            spons, evidence = result
            if (args.blocked or args.opt_only) and spons == "Unclear":
                unchanged += 1  # a re-fetch that finds no wording proves nothing; keep the earlier Blocked verdict
                continue
            if spons != entry["sponsorship_status"]:
                new_state = {"Blocked": "auto_blocked", "OPT Only": "opt_only"}.get(spons)
                moved = new_state is not None and entry["state"] in ("new", "auto_blocked", "opt_only")                     and entry["state"] != new_state
                suffix = f" (state: {entry['state']} -> {new_state})" if moved else ""
                print(f"  {entry['company']} | {entry['role']}: {from_status} -> {spons}{suffix}")
                if not args.dry_run:
                    entry["sponsorship_status"] = spons
                    entry["sponsorship_evidence"] = evidence
                    if moved:
                        entry["state"] = new_state
                flipped += 1
            else:
                unchanged += 1
            if i % 200 == 0:
                print(f"...{i}/{len(targets)} checked")

    print(f"\n{flipped} reclassified (from {from_status}), {unchanged} unchanged, "
          f"{no_company} skipped (company not in config), {fetch_errors} fetch errors")

    if args.dry_run:
        print("(dry run - queue.json not written)")
    else:
        atomic_json.write(HOME / "queue.json", queue)
        print(f"wrote {HOME / 'queue.json'}")


if __name__ == "__main__":
    main()
