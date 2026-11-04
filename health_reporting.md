# Scout health reporting

Author: Yazad Madan

`health.py` keeps reporting independent of the scout. `merge_shards`, `parse_scout_log`, `build_summary` and `render_markdown` are pure functions. The CLI reads config, queue, state, shard results, the scout log and the previous health report. It atomically overwrites root-level health.json and appends bounded Markdown to GITHUB_STEP_SUMMARY. It never changes data/ or the scout source.

The workflow captures the scout's output with tee and pipefail, so a scout failure retains its original failing status. An always-run health step and artifact upload retain diagnostics even when the scout fails. Successful workflow commits include health.json, preserving measured last-success timestamps for later runs. Failed runs retain their report as a seven-day artifact but do not update the committed history. This is measured history, not an assertion that no unrecorded successful fetch occurred.

Run locally:

```powershell
py health.py --shards path/to/shards --scout-log path/to/scout.log --output health.json
```

Shard inputs may be flat workflow downloads or nested artifact directories. Only shard_*.json files are consumed. Missing companies are detected against the configured company universe, including companies with no queue or state rows. Conflicting or malformed results are reported and never treated as successful boards.

Fetched OK means an error-free, well-formed response, including zero-result and incomplete boards. A last-success timestamp advances only for a complete, nonempty, error-free board, matching the scout's trust threshold. Empty boards are reported separately and are not automatically declared broken. On the first health run, state IDs and queue sightings do not fabricate timestamps; companies with unknown history are identified separately from companies with stale measured success. The default freshness window is 24 hours.

Description bodies are not stored in queue.json. Explicitly empty bulk descriptions and logged detail failures are counted as observations, with repeated company/title pairs deduplicated. Omitted bulk description fields are reported as unknown because many ATS fetchers enrich them later. New description_status flags expose unresolved queue failures; legacy rows remain unknown. Posted-date gaps are reported separately for bulk results and queue rows, since detail enrichment can fill dates after the shard was written.

Closed/reopened values come from the final scout log counters. Same-day closed_on or reposted_on fields cannot distinguish hourly runs, so no per-run transitions are invented when the final log is missing. Pending alerts include both eligible work and stale flags on closed, acted-on or exhausted rows; these categories are shown separately without modifying queue entries.

Tests: `python -m pytest -q test_health.py test_scout_review_guards.py`. Review reproductions use the explicit command in scout_review.md and are intentionally excluded from normal CI discovery.

Archived run 37128573750 smoke check: 857 expected companies, 856 fetched OK, one failed fetch, zero missing fetches, 169 incomplete boards, eight empty boards, zero closed and seven reopened. The initial report is approximately 35 KB as compact JSON and about 2 KB as Markdown. No historical description coverage or last-success dates were invented.
