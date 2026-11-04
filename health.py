#!/usr/bin/env python3
"""Build compact scout health reports without changing scout inputs.

Pure functions accept decoded data and an explicit time. The CLI alone reads
files and overwrites health.json. Legacy state records contain IDs, not fetch
timestamps; health.json preserves measured fetch history from this point on.
"""
import argparse
from datetime import datetime, timedelta, timezone
import html
import json
import os
from pathlib import Path
import re

import atomic_json


ROOT = Path(__file__).resolve().parent


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def merge_shards(shards):
    """Merge (filename, decoded object) pairs; never trust conflicting copies."""
    merged, errors = {}, []
    for filename, data in sorted(shards):
        if not isinstance(data, dict):
            errors.append(f"{filename}: shard must be an object")
            continue
        for name, row in data.items():
            if name in merged and merged[name] != row:
                errors.append(f"{name}: conflicting shard results")
                merged[name] = {"jobs": None, "complete": None, "err": "conflicting shard results"}
            else:
                merged[name] = row
    return merged, errors


def parse_scout_log(text):
    """Use observed detail errors and final counters, not same-day queue dates."""
    failures = sorted(set(re.findall(r"! (.+?) description for (.+?): [^\n]*", text or "")))
    totals = re.findall(r"done: [^\n]*? (\d+) closed, (\d+) reopened,", text or "")
    return {
        "finished": bool(totals),
        "closed": int(totals[-1][0]) if totals else None,
        "reopened": int(totals[-1][1]) if totals else None,
        "description_failures": [{"company": c, "role": r} for c, r in failures],
    }


def build_summary(queue, state, fetched, *, now, companies=None, previous=None,
                  scout_log=None, stale_hours=24, input_errors=(), run_id=None,
                  alert_max_attempts=3):
    """Summarize one run without mutating inputs or guessing unavailable facts.

    fetched_ok means a well-formed, error-free response, including empty and
    incomplete listings. History advances only for nonempty complete responses.
    Missing listing descriptions are counted only when desc is explicitly empty;
    an omitted desc usually means that the ATS needs a separate detail request.
    The queue does not store description bodies, so historical coverage is unknown.
    """
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    if stale_hours <= 0:
        raise ValueError("stale_hours must be positive")
    now = now.astimezone(timezone.utc)
    generated = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    names = sorted(set(companies if companies is not None else
                       set(state) | set(fetched) | {e["company"] for e in queue}))
    previous = previous or {}
    history = previous.get("last_success", {})
    if not isinstance(history, dict):
        history = {}
    last_success = {}
    for name in names:
        timestamp = _timestamp(history.get(name))
        last_success[name] = timestamp.isoformat(timespec="seconds").replace("+00:00", "Z") if timestamp and timestamp <= now else None
    missing, failed, incomplete, zero = [], [], [], []
    ok, complete, verified, jobs_count = 0, 0, 0, 0
    missing_posted, listing_desc_unknown = 0, 0
    empty_descriptions = set()
    errors = list(input_errors)
    for name in names:
        if name not in fetched:
            missing.append(name)
            continue
        row = fetched[name]
        if not isinstance(row, dict):
            failed.append(name)
            errors.append(f"{name}: malformed fetch result")
            continue
        if row.get("err") is not None:
            failed.append(name)
            continue
        jobs = row.get("jobs")
        if not isinstance(jobs, list) or not isinstance(row.get("complete"), bool) or any(not isinstance(j, dict) or not _nonempty(j.get("id")) for j in jobs):
            failed.append(name)
            errors.append(f"{name}: malformed fetch result")
            continue
        ok += 1
        if row["complete"]:
            complete += 1
        else:
            incomplete.append(name)
        if not jobs:
            zero.append(name)
        elif row["complete"]:
            verified += 1
            last_success[name] = generated
        # A fetcher may repeat IDs across terms or sites. Count each company ID once.
        for job in {j["id"]: j for j in jobs}.values():
            jobs_count += 1
            if not _nonempty(job.get("posted")):
                missing_posted += 1
            if "desc" not in job:
                listing_desc_unknown += 1
            elif not _nonempty(job["desc"]):
                empty_descriptions.add((name, job.get("title", job["id"])))
    observed = parse_scout_log(scout_log)
    detail_failures = {(r["company"], r["role"]) for r in observed["description_failures"]}
    description_missing = empty_descriptions | detail_failures
    stale = [name for name in names if last_success[name] is not None
             and _timestamp(last_success[name]) < now - timedelta(hours=stale_hours)]
    unknown = [name for name in names if last_success[name] is None]
    pending = [e for e in queue if e.get("alert_pending")]
    eligible = [e for e in pending if not e.get("closed_on") and e.get("state") == "new"
                and e.get("alert_attempts", 1) < alert_max_attempts]
    counts = {
        "companies_expected": len(names), "companies_fetched_ok": ok,
        "complete_boards": complete, "verified_nonempty_boards": verified,
        "missing_fetches": len(missing), "failed_fetches": len(failed),
        "incomplete_boards": len(incomplete), "zero_result_companies": len(zero),
        "jobs_returned": jobs_count, "missing_posted_dates": missing_posted,
        "missing_descriptions_observed": len(description_missing),
        "detail_description_failures": len(detail_failures) if scout_log is not None else None,
        "listing_description_unknown": listing_desc_unknown,
        "queue_failed_descriptions": sum(e.get("description_status") == "failed" for e in queue),
        "queue_description_status_unknown": sum(e.get("description_status") not in ("ok", "failed") for e in queue),
        "queue_missing_posted_dates": sum(not _nonempty(e.get("posted")) for e in queue),
        "pending_alerts": len(pending), "eligible_pending_alerts": len(eligible),
        "inactive_pending_alerts": len(pending) - len(eligible),
        "closed_this_run": observed["closed"], "reopened_this_run": observed["reopened"],
        "no_success_lately": len(stale) + len(unknown), "success_history_unknown": len(unknown),
        "companies_without_seeded_state": sum(name not in state for name in names),
    }
    return {
        "version": 1, "run_id": run_id, "generated_at": generated,
        "scout_finished": observed["finished"], "stale_hours": stale_hours,
        "counts": counts,
        "companies": {"missing": missing, "failed": failed, "incomplete": incomplete,
                      "zero_results": zero, "stale_success": stale, "unknown_success": unknown},
        "description_failures": observed["description_failures"],
        "input_errors": sorted(set(errors)), "last_success": last_success,
    }


def render_markdown(summary, limit=20):
    """Render bounded human-readable diagnostics for GITHUB_STEP_SUMMARY."""
    counts = summary["counts"]
    metrics = [
        ("Companies fetched OK", "companies_fetched_ok"),
        ("Expected companies", "companies_expected"),
        ("Missing fetches", "missing_fetches"), ("Failed fetches", "failed_fetches"),
        ("Incomplete boards", "incomplete_boards"), ("Zero-result companies", "zero_result_companies"),
        ("Jobs returned (unique company IDs)", "jobs_returned"),
        ("Missing posted dates in listings", "missing_posted_dates"),
        ("Observed missing descriptions", "missing_descriptions_observed"),
        ("Description detail failures", "detail_description_failures"),
        ("Listing descriptions not supplied", "listing_description_unknown"),
        ("Queue description fetches still failed", "queue_failed_descriptions"),
        ("Queue description status unknown", "queue_description_status_unknown"),
        ("Queue rows without posted dates", "queue_missing_posted_dates"),
        ("Pending alerts", "pending_alerts"), ("Eligible pending alerts", "eligible_pending_alerts"),
        ("Inactive or exhausted pending alerts", "inactive_pending_alerts"),
        ("Closed this run", "closed_this_run"), ("Reopened this run", "reopened_this_run"),
        ("No measured successful fetch lately", "no_success_lately"),
        ("Success history unknown", "success_history_unknown"),
    ]
    lines = ["## Job scout health", "", f"Generated: {summary['generated_at']}.", "",
             f"Scout completed: {'yes' if summary['scout_finished'] else 'not confirmed'}.", "",
             "| Metric | Count |", "| --- | ---: |"]
    for label, key in metrics:
        value = counts[key]
        lines.append(f"| {label} | {value if value is not None else 'unknown'} |")
    lines.extend(["", "Fetched OK includes empty and incomplete responses. Fetch history advances only on error-free, complete, nonempty listings.", "",
                  "Descriptions omitted from a bulk listing are unknown, not failed. Observed missing descriptions count distinct company/title pairs from explicit empty descriptions and logged detail failures. Queue description_status flags report unresolved failures when present; legacy rows have unknown status. Description bodies are not stored.", "",
                  f"Freshness window: {summary['stale_hours']} hours. Legacy state IDs and queue sighting dates do not prove a successful complete fetch. Unknown history is shown separately. Closed/reopened counters come from the final scout log, not daily date fields."])
    groups = [("Missing fetches", summary["companies"]["missing"]),
              ("Failed fetches", summary["companies"]["failed"]),
              ("Incomplete boards", summary["companies"]["incomplete"]),
              ("Zero-result companies", summary["companies"]["zero_results"]),
              ("Stale successful fetches", summary["companies"]["stale_success"]),
              ("Success history unknown", summary["companies"]["unknown_success"]),
              ("Input problems", summary["input_errors"])]
    for label, values in groups:
        if values:
            # Prevent board labels from injecting Markdown/HTML into the summary.
            safe = [re.sub(r"([\\`*{}\[\]()#+.!_|>])", r"\\\1", html.escape(str(v)).replace("\n", " ").replace("\r", " ")) for v in values[:limit]]
            lines.extend(["", f"**{label}:** " + "; ".join(safe) + (f"; and {len(values) - limit} more" if len(values) > limit else "")])
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, default=ROOT / "data/queue.json")
    parser.add_argument("--state", type=Path, default=ROOT / "data/state.json")
    parser.add_argument("--config", type=Path, default=ROOT / "data/config.json")
    parser.add_argument("--shards", type=Path, required=True)
    parser.add_argument("--scout-log", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "health.json")
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--summary", type=Path, help="Append Markdown here, usually GITHUB_STEP_SUMMARY")
    parser.add_argument("--stale-hours", type=float, default=24)
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID"))
    args = parser.parse_args(argv)
    if args.stale_hours <= 0:
        parser.error("--stale-hours must be positive")
    errors = []

    def read(path, default, optional=False):
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            if not optional or path.exists():
                errors.append(f"{path.name}: {type(exc).__name__}")
            return default

    queue, state, config = read(args.queue, []), read(args.state, {}), read(args.config, {})
    if not isinstance(queue, list) or any(not isinstance(e, dict) or not _nonempty(e.get("company")) for e in queue):
        errors.append("queue.json: malformed queue")
        queue = []
    if not isinstance(state, dict):
        errors.append("state.json: malformed state")
        state = {}
    companies = None
    if isinstance(config, dict) and isinstance(config.get("companies"), list) and all(isinstance(c, dict) and _nonempty(c.get("name")) for c in config["companies"]):
        companies = [c["name"] for c in config["companies"]]
    else:
        errors.append("config.json: configured company universe unavailable")
    shard_files = sorted(args.shards.rglob("shard_*.json"))
    if not shard_files:
        errors.append("No shard result files found")
    fetched, merge_errors = merge_shards([(p.name, read(p, {})) for p in shard_files])
    errors.extend(merge_errors)
    previous = read(args.previous or args.output, {}, optional=True)
    if not isinstance(previous, dict):
        errors.append("previous health report: malformed object")
        previous = {}
    log = None
    if args.scout_log:
        try:
            log = args.scout_log.read_text(encoding="utf-8-sig")
        except OSError:
            errors.append("Scout log unavailable; per-run counters are unknown")
    summary = build_summary(queue, state, fetched, now=datetime.now(timezone.utc),
                            companies=companies, previous=previous, scout_log=log,
                            stale_hours=args.stale_hours, input_errors=errors, run_id=args.run_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    atomic_json.write(str(args.output), summary)
    markdown = render_markdown(summary)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as stream:
            stream.write(markdown)
    else:
        print(markdown, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
