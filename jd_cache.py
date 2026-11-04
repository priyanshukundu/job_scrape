"""Static JD cache for the hosted page's Copy JD button.

Reads every open new/opt_only queue row, fetches its description through job_scout.describe() (one code
path for every ATS) and writes docs/jd/<key>.txt, so the page can copy a JD without a live ATS call.
Files for rows no longer shown are deleted. Time-boxed so a scheduled run never stalls.

    python jd_cache.py [--budget 420] [--workers 12] [--probe N]
"""
import argparse
import concurrent.futures as cf
import hashlib
import sys
import time
from pathlib import Path

import job_scout

OUT_DIR = Path(__file__).resolve().parent / "docs" / "jd"
MIN_CHARS = 120  # shorter than this is an empty or stub description, not a JD
SHOWN_STATES = ("new", "opt_only")


def jd_key(entry_id):
    return hashlib.sha1(entry_id.encode("utf-8")).hexdigest()[:20]


def _company_cfgs():
    cfg = job_scout.load_json(job_scout.HOME / "config.json", {})
    return {c["name"]: c for c in cfg.get("companies", [])}


def fetch_text(entry, c, board_cache):
    """Full description for one queue entry, or '' if the ATS gives none."""
    job_id = entry["id"].split(":", 1)[1]
    if c["ats"] in ("lever", "ashby"):  # description only comes with the board list call
        if c["name"] not in board_cache:
            board_cache[c["name"]] = (job_scout.lever if c["ats"] == "lever" else job_scout.ashby)(c)[0]
        return next((j.get("desc", "") for j in board_cache[c["name"]] if str(j["id"]) == job_id), "")
    last = ""
    for attempt in range(3):  # session-based sites fail intermittently
        try:
            text, _, _ = job_scout.describe(c, {"id": job_id, "site": entry.get("site"), "url": entry.get("link"),
                                                "location": entry.get("location", "")})
            if text:
                return text
        except Exception as e:  # noqa: BLE001
            last = str(e)
        time.sleep(1.5)
    return ""


def render(entry, text):
    head = [entry["role"], entry["company"], f"Location: {entry.get('location', '')}", f"Link: {entry.get('link', '')}"]
    return "\n".join(head) + "\n\n" + text


def shown_rows():
    q = job_scout.load_json(job_scout.HOME / "queue.json", [])
    return [e for e in q if e.get("state") in SHOWN_STATES and not e.get("closed_on")]


def probe(n):
    cfgs = _company_cfgs()
    by_ats = {}
    for e in shown_rows():
        c = cfgs.get(e["company"])
        if c and len(by_ats.setdefault(c["ats"], [])) < n:
            by_ats[c["ats"]].append((e, c))
    jobs = [(a, e, c) for a, lst in by_ats.items() for e, c in lst]
    bad = {}
    cache = {}

    def one(t):
        a, e, c = t
        try:
            return a, e, len(fetch_text(e, c, cache))
        except Exception as ex:  # noqa: BLE001
            return a, e, f"ERR {ex}"[:90]

    with cf.ThreadPoolExecutor(12) as ex:
        res = list(ex.map(one, jobs))
    for a in sorted(by_ats):
        r = [x for x in res if x[0] == a]
        ok = [x for x in r if isinstance(x[2], int) and x[2] >= MIN_CHARS]
        print(f"{a:16} {len(ok)}/{len(r)} ok", "" if len(ok) == len(r) else [x[2] for x in r if x not in ok])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=420, help="seconds of fetching per run")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--probe", type=int, default=0, help="test N rows per ATS and exit")
    args = ap.parse_args()
    if args.probe:
        probe(args.probe)
        return 0
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = shown_rows()
    keep = {jd_key(e["id"]) for e in rows}
    for f in OUT_DIR.glob("*.txt"):
        if f.stem not in keep:
            f.unlink()
    cfgs = _company_cfgs()
    todo = [e for e in rows if not (OUT_DIR / f"{jd_key(e['id'])}.txt").exists() and e["company"] in cfgs]
    print(f"{len(rows)} shown rows, {len(todo)} without a cached JD")
    deadline = time.time() + args.budget
    cache, done, failed = {}, 0, 0

    def one(e):
        if time.time() > deadline:
            return e, None
        return e, fetch_text(e, cfgs[e["company"]], cache)

    with cf.ThreadPoolExecutor(args.workers) as ex:
        for fut in cf.as_completed([ex.submit(one, e) for e in todo]):  # write as each finishes, not in queue order
            try:
                e, text = fut.result()
            except Exception:  # noqa: BLE001
                failed += 1
                continue
            if text is None:
                continue
            if len(text) >= MIN_CHARS:
                (OUT_DIR / f"{jd_key(e['id'])}.txt").write_text(render(e, text), encoding="utf-8")
                done += 1
            else:
                failed += 1
    print(f"cached {done}, empty {failed}, deferred {len(todo) - done - failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
