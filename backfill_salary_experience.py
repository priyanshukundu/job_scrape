#!/usr/bin/env python3
"""One-off, resumable backfill: re-derives salary/salary_evidence and experience/experience_evidence for
existing queue.json rows using the CURRENT classify_salary() / classify_experience() in job_scout.py.

Why: queue rows only ever get these fields at intake (or in _retry_enrichment), so every row scraped before
the classifiers were improved keeps its stale "Unclear" salary / missing experience. This is the catch-up pass.

Rows considered: salary missing/"Unclear" OR a lower-bound-only value ("from $X", or a single figure whose
evidence says "starting at / minimum / from"), OR experience missing (None). Closed rows (closed_on set) and
rows in state applied/dismissed are skipped unless --all-states.

Overwrite rules (a field is only ever improved, never downgraded):
  - salary: old Unclear/missing -> any real new value; old lower-bound-only ("from $X") or upper-bound-only
    ("up to $X") -> a new full range that contains the old figure. A real value is never replaced by Unclear.
  - experience: old missing -> new value if the classifier found one. A real value is never replaced.
No other field is touched (sponsorship, posted, state, ...).

Rows whose ATS has no describe path (describe() returns empty text) are counted as 'no describe path' and
skipped; describe() exceptions are logged as errors and skipped, never fatal.

Resumable: each fetched row's classifier output is appended to <out>/cache.jsonl keyed by row id; reruns skip
rows already cached (delete the cache if the classifiers change). Fetches are rate limited to 3 req/s overall
with 3 worker threads.

Default is a DRY RUN: writes <out>/changes.jsonl, <out>/report.json and a patched COPY of the queue at
<out>/queue.patched.json, and never touches --queue. --apply additionally rewrites --queue with atomic_json.write.

Usage: python backfill_salary_experience.py [--queue PATH] [--config PATH] [--out DIR] [--limit N]
                                            [--all-states] [--threads 3] [--apply]
"""
import argparse
import copy
import json
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atomic_json
import job_scout

HOME = job_scout.HOME
DEFAULT_OUT = "/tmp/claude-0/backfill"
MAX_REQ_PER_S = 3.0
SKIP_STATES = ("applied", "dismissed")
_NUM_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([kK])?")


class RateLimiter:
    """Global spacing between request starts so N threads together stay under the per-second cap."""
    def __init__(self, per_s):
        self.gap, self.next, self.lock = 1.0 / per_s, 0.0, threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next)
            self.next = t + self.gap
        if t > now:
            time.sleep(t - now)


def is_real_salary(s):
    return bool(s) and s != "Unclear"


def is_partial_salary(row):
    """True when the stored salary only states one bound (lower or upper)."""
    s = row.get("salary")
    if not is_real_salary(s):
        return False
    if s.startswith(("from ", "up to ")):
        return True
    if " - " in s:
        return False
    ev = row.get("salary_evidence") or ""
    m = re.search(r"\$?\s*[\d,]+(?:\.\d+)?k?", ev)
    lead = ev[:m.start()] if m else ""
    return bool(re.search(r"(?:starting (?:at|from)|\bfrom|minimum|\bmin|at least|base of)\W*$", lead, re.I))


def annual_bounds(s):
    """(lo, hi) annual figures from a rendered salary string, or None."""
    if "(~" in s:
        s = s.split("(~", 1)[1]
    vals = []
    for num, k in _NUM_RE.findall(s):
        v = float(num.replace(",", ""))
        vals.append(v * 1000 if k else v)
    if not vals:
        return None
    return min(vals), max(vals)


def salary_improvement(old_row, new):
    """Return a reason string when `new` strictly improves on the row's stored salary, else None."""
    old = old_row.get("salary")
    if not is_real_salary(new):
        return None
    if not is_real_salary(old):
        return "salary N/A->value"
    if is_partial_salary(old_row) and " - " in new and not new.startswith(("from ", "up to ")):
        ob, nb = annual_bounds(old), annual_bounds(new)
        if ob and nb and nb[0] * 0.999 <= ob[0] <= nb[1] * 1.001:
            return "salary lower-bound->range"
    return None


def wants_salary(row):
    return not is_real_salary(row.get("salary")) or is_partial_salary(row)


def wants_experience(row):
    return row.get("experience") is None


def load_cache(path):
    cache = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                cache[rec["id"]] = rec
            except (ValueError, KeyError):
                continue  # a torn last line from a killed run: just refetch that row
    return cache


def select_rows(queue, all_states=False):
    out = []
    for e in queue:
        if e.get("closed_on"):
            continue
        if not all_states and e.get("state") in SKIP_STATES:
            continue
        if wants_salary(e) or wants_experience(e):
            out.append(e)
    return out


def fetch_one(row, c, limiter):
    """Returns a cache record for the row. status: ok | no_path | error (errors are not cached)."""
    rec = {"id": row["id"], "company": row["company"], "ats": c.get("ats") if c else None}
    if c is None:
        return dict(rec, status="no_path", note="company not in config")
    if not row["id"].startswith(row["company"] + ":"):
        return dict(rec, status="no_path", note="unexpected id format")
    job = {"id": row["id"].split(":", 1)[1], "site": row.get("site"), "url": row.get("link")}
    limiter.wait()
    try:
        text, _p, _s = job_scout.describe(c, job)
    except Exception as exc:  # noqa: BLE001
        return dict(rec, status="error", note=str(exc)[:200])
    if not text:
        return dict(rec, status="no_path", note="describe returned no text")
    salary, sev = job_scout.classify_salary(text)
    exp, eev = job_scout.classify_experience(row.get("role", ""), text)
    return dict(rec, status="ok", salary=salary, salary_evidence=sev, experience=exp, experience_evidence=eev)


def apply_cached(row, rec, stated_only=False):
    """Changes proposed for one row from its cache record."""
    changes = []
    if rec.get("status") != "ok":
        return changes
    if wants_salary(row):
        reason = salary_improvement(row, rec["salary"])
        if reason:
            changes.append(dict(kind=reason, field="salary", old=row.get("salary"), new=rec["salary"],
                                evidence=rec.get("salary_evidence", ""),
                                extra={"salary_evidence": rec.get("salary_evidence", "")}))
    guessed = str(rec.get("experience_evidence") or "").startswith("inferred from title")  # a title guess, not stated years
    if wants_experience(row) and rec.get("experience") is not None and not (stated_only and guessed):
        changes.append(dict(kind="experience N/A->value", field="experience", old=row.get("experience"),
                            new=rec["experience"], evidence=rec.get("experience_evidence", ""),
                            extra={"experience_evidence": rec.get("experience_evidence", "")}))
    return changes


def run(queue_path, config_path, out_dir, limit=None, all_states=False, threads=3, apply=False, rate=MAX_REQ_PER_S, stated_only=False):
    queue_path, out_dir = Path(queue_path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    by_company = {c["name"]: c for c in cfg["companies"]}
    queue = json.loads(queue_path.read_text(encoding="utf-8"))

    targets = select_rows(queue, all_states)
    if limit:
        targets = targets[:limit]
    cache_path = out_dir / "cache.jsonl"
    cache = load_cache(cache_path)
    todo = [e for e in targets if e["id"] not in cache]
    print(f"{len(targets)} rows considered of {len(queue)}; {len(targets) - len(todo)} cached, {len(todo)} to fetch",
          flush=True)

    errors = []
    limiter = RateLimiter(rate)
    lock = threading.Lock()
    done = [0]
    started = time.monotonic()
    with open(cache_path, "a", encoding="utf-8") as cf, ThreadPoolExecutor(max_workers=threads) as pool:
        def work(row):
            try:
                rec = fetch_one(row, by_company.get(row["company"]), limiter)
            except Exception as exc:  # noqa: BLE001
                rec = {"id": row["id"], "company": row["company"], "status": "error", "note": str(exc)[:200]}
            with lock:
                if rec["status"] == "error":
                    errors.append(rec)
                    print(f"! {row['company']} {row['id']}: {rec.get('note')}", file=sys.stderr)
                else:
                    cache[row["id"]] = rec
                    cf.write(json.dumps(rec) + "\n")
                    cf.flush()
                done[0] += 1
                if done[0] % 100 == 0:
                    print(f"...{done[0]}/{len(todo)} fetched ({time.monotonic() - started:.0f}s)", flush=True)
        list(pool.map(work, todo))

    patched = copy.deepcopy(queue)
    pby = {e["id"]: e for e in patched}
    changes, counts, by_ats = [], Counter(), defaultdict(Counter)
    for row in targets:
        rec = cache.get(row["id"])
        ats = (by_company.get(row["company"]) or {}).get("ats", "?")
        if rec is None:
            continue
        counts["fetched" if rec["status"] == "ok" else rec["status"]] += 1
        by_ats[ats][rec["status"]] += 1
        for ch in apply_cached(row, rec, stated_only):
            counts[ch["kind"]] += 1
            by_ats[ats][ch["kind"]] += 1
            changes.append(dict(id=row["id"], company=row["company"], field=ch["field"], old=ch["old"],
                                new=ch["new"], evidence=ch["evidence"], kind=ch["kind"], ats=ats))
            pr = pby[row["id"]]
            pr[ch["field"]] = ch["new"]
            for k, v in ch["extra"].items():
                pr[k] = v

    patched_path = out_dir / "queue.patched.json"
    if patched_path.resolve() == queue_path.resolve():
        sys.exit("refusing: patched copy path equals the queue path")
    with open(out_dir / "changes.jsonl", "w", encoding="utf-8") as f:
        for ch in changes:
            f.write(json.dumps(ch) + "\n")
    atomic_json.write(str(patched_path), patched)
    report = dict(rows_in_queue=len(queue), rows_considered=len(targets), fetched_this_run=len(todo) - len(errors),
                  fetched_ok_total=counts["fetched"], no_describe_path=counts["no_path"], errors_this_run=len(errors),
                  salary_na_to_value=counts["salary N/A->value"],
                  salary_lower_bound_to_range=counts["salary lower-bound->range"],
                  experience_na_to_value=counts["experience N/A->value"],
                  rows_changed=len({c["id"] for c in changes}),
                  by_ats={k: dict(v) for k, v in sorted(by_ats.items())})
    (out_dir / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps(report, indent=1))

    if apply:
        atomic_json.write(str(queue_path), patched)
        print(f"APPLIED: wrote {queue_path} ({report['rows_changed']} rows changed)")
    else:
        print(f"(dry run: {queue_path} not written; patched copy at {patched_path}, changes at {out_dir / 'changes.jsonl'})")
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--queue", default=str(HOME / "queue.json"))
    ap.add_argument("--config", default=str(HOME / "config.json"))
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--limit", type=int, default=None, help="Only consider the first N candidate rows")
    ap.add_argument("--all-states", action="store_true", help="Also process applied/dismissed rows")
    ap.add_argument("--threads", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true", help="Default behaviour; accepted for symmetry")
    ap.add_argument("--stated-only", action="store_true",
                    help="Skip experience values inferred from the title; write only years the description states")
    ap.add_argument("--apply", action="store_true", help="Rewrite --queue with the patched rows (default: dry run)")
    a = ap.parse_args()
    if a.apply and a.dry_run:
        ap.error("--apply and --dry-run are mutually exclusive")
    run(a.queue, a.config, a.out, a.limit, a.all_states, a.threads, a.apply, stated_only=a.stated_only)


if __name__ == "__main__":
    main()
